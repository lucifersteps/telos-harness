"""MCP auth helpers and OAuth browser flow."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import queue
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Protocol

from telos.agent.mcp.config_loader import McpServerConfig
from telos.storage.filesystem import read_json, write_json_atomic


class McpAuthError(RuntimeError):
    """MCP auth configuration or token retrieval failed."""


class McpAuthProvider(Protocol):
    def headers(self) -> dict[str, str]: ...

    def invalidate(self) -> None: ...


@dataclass
class McpBearerTokenProvider:
    static_headers: dict[str, str] = field(default_factory=dict)
    bearer_token: str = ""
    bearer_token_env_var: str = ""
    env_lookup: Callable[[str], str | None] = os.environ.get
    _cached_token: str | None = field(default=None, init=False)

    @classmethod
    def from_config(
        cls,
        config: McpServerConfig,
        *,
        env_lookup: Callable[[str], str | None] = os.environ.get,
    ) -> "McpBearerTokenProvider":
        return cls(
            static_headers=dict(config.headers),
            bearer_token=config.bearer_token,
            bearer_token_env_var=config.bearer_token_env_var,
            env_lookup=env_lookup,
        )

    def headers(self) -> dict[str, str]:
        headers = dict(self.static_headers)
        token = self._token()
        if token:
            _remove_authorization_headers(headers)
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def invalidate(self) -> None:
        self._cached_token = None

    def _token(self) -> str:
        explicit = self.bearer_token.strip()
        env_var = self.bearer_token_env_var.strip()
        if explicit and env_var:
            raise McpAuthError("MCP bearerToken and bearerTokenEnvVar are mutually exclusive")
        if explicit:
            return explicit
        if not env_var:
            return ""
        if self._cached_token is None:
            token = (self.env_lookup(env_var) or "").strip()
            if not token:
                raise McpAuthError(
                    f"MCP bearer token env var {env_var!r} is not set or is empty"
                )
            self._cached_token = token
        return self._cached_token


@dataclass(frozen=True)
class McpOAuthConfig:
    authorization_url: str
    token_url: str
    client_id: str
    client_secret: str = ""
    scope: str = ""
    audience: str = ""
    use_pkce: bool = True

    @classmethod
    def resolve(
        cls,
        config: McpServerConfig,
        *,
        store: "McpOAuthTokenStore | None" = None,
        redirect_uri: str = "",
        client_label: str = "Telos",
    ) -> "McpOAuthConfig":
        raw = dict(config.oauth or {})
        authorization_url = str(
            raw.get("authorizationUrl") or raw.get("authorization_url") or ""
        ).strip()
        token_url = str(raw.get("tokenUrl") or raw.get("token_url") or "").strip()
        registration_url = str(
            raw.get("registrationUrl") or raw.get("registration_url") or ""
        ).strip()
        if not authorization_url or not token_url or (not registration_url and _needs_client_registration(raw)):
            metadata = _discover_oauth_metadata(config)
            authorization_url = authorization_url or str(metadata.get("authorization_endpoint") or "")
            token_url = token_url or str(metadata.get("token_endpoint") or "")
            registration_url = registration_url or str(metadata.get("registration_endpoint") or "")
        client_id = str(raw.get("clientId") or raw.get("client_id") or "").strip()
        client_secret = str(raw.get("clientSecret") or raw.get("client_secret") or "")
        if not client_id and store is not None:
            client = store.get_client(oauth_store_key(config.name, config.url))
            if client is not None:
                client_id = str(client.get("client_id") or "")
                client_secret = str(client.get("client_secret") or client_secret or "")
        if not client_id and redirect_uri and registration_url and store is not None:
            client = _register_oauth_client(
                registration_url,
                redirect_uri=redirect_uri,
                client_name=f"{client_label} ({config.name})",
            )
            store.set_client(oauth_store_key(config.name, config.url), client)
            client_id = str(client.get("client_id") or "")
            client_secret = str(client.get("client_secret") or client_secret or "")
        if not authorization_url or not token_url or not client_id:
            raise McpAuthError(
                (
                    "MCP OAuth config requires authorizationUrl, tokenUrl, and clientId, "
                    "or discoverable OAuth metadata plus dynamic client registration"
                )
            )
        return cls(
            authorization_url=authorization_url,
            token_url=token_url,
            client_id=client_id,
            client_secret=client_secret,
            scope=str(raw.get("scope") or ""),
            audience=str(raw.get("audience") or ""),
            use_pkce=bool(raw.get("usePkce", raw.get("use_pkce", True))),
        )


@dataclass(frozen=True)
class McpOAuthTokens:
    access_token: str
    refresh_token: str = ""
    token_type: str = "Bearer"
    expires_at: float = 0.0
    scope: str = ""

    @classmethod
    def from_token_response(cls, payload: dict[str, Any], *, previous_refresh_token: str = "") -> "McpOAuthTokens":
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise McpAuthError("OAuth token response missing access_token")
        expires_in = payload.get("expires_in")
        expires_at = 0.0
        if expires_in not in (None, ""):
            expires_at = time.time() + float(expires_in)
        return cls(
            access_token=access_token,
            refresh_token=str(payload.get("refresh_token") or previous_refresh_token or ""),
            token_type=str(payload.get("token_type") or "Bearer"),
            expires_at=expires_at,
            scope=str(payload.get("scope") or ""),
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "McpOAuthTokens":
        return cls(
            access_token=str(raw.get("access_token") or ""),
            refresh_token=str(raw.get("refresh_token") or ""),
            token_type=str(raw.get("token_type") or "Bearer"),
            expires_at=float(raw.get("expires_at") or 0.0),
            scope=str(raw.get("scope") or ""),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "scope": self.scope,
        }

    def usable_without_refresh(self) -> bool:
        if not self.access_token:
            return False
        return self.expires_at <= 0 or self.expires_at > time.time() + 60


class McpOAuthTokenStore:
    def __init__(self, path: Path):
        self.path = path

    def get(self, key: str) -> McpOAuthTokens | None:
        data = read_json(self.path, {})
        raw = data.get(key) if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return None
        if isinstance(raw.get("tokens"), dict):
            raw = raw["tokens"]
        tokens = McpOAuthTokens.from_dict(raw)
        return tokens if tokens.access_token else None

    def set(self, key: str, tokens: McpOAuthTokens) -> None:
        data = read_json(self.path, {})
        if not isinstance(data, dict):
            data = {}
        entry = _store_entry(data.get(key))
        entry["tokens"] = tokens.to_dict()
        data[key] = entry
        write_json_atomic(self.path, data)

    def get_client(self, key: str) -> dict[str, Any] | None:
        data = read_json(self.path, {})
        raw = data.get(key) if isinstance(data, dict) else None
        if not isinstance(raw, dict) or not isinstance(raw.get("client"), dict):
            return None
        return dict(raw["client"])

    def set_client(self, key: str, client: dict[str, Any]) -> None:
        data = read_json(self.path, {})
        if not isinstance(data, dict):
            data = {}
        entry = _store_entry(data.get(key))
        entry["client"] = dict(client)
        data[key] = entry
        write_json_atomic(self.path, data)

    def clear(self, key: str) -> None:
        data = read_json(self.path, {})
        if not isinstance(data, dict) or key not in data:
            return
        data.pop(key, None)
        write_json_atomic(self.path, data)


class McpOAuthProvider:
    def __init__(
        self,
        *,
        config: McpServerConfig,
        oauth_config: McpOAuthConfig,
        store: McpOAuthTokenStore,
    ):
        self.config = config
        self.oauth_config = oauth_config
        self.store = store
        self.store_key = oauth_store_key(config.name, config.url)
        self._cached_tokens: McpOAuthTokens | None = None

    def headers(self) -> dict[str, str]:
        headers = dict(self.config.headers)
        tokens = self._tokens()
        if not tokens:
            return headers
        if not tokens.usable_without_refresh():
            tokens = self._refresh(tokens)
        _remove_authorization_headers(headers)
        headers["Authorization"] = f"{tokens.token_type or 'Bearer'} {tokens.access_token}"
        return headers

    def invalidate(self) -> None:
        self._cached_tokens = None

    def _tokens(self) -> McpOAuthTokens | None:
        if self._cached_tokens is None:
            self._cached_tokens = self.store.get(self.store_key)
        return self._cached_tokens

    def _refresh(self, tokens: McpOAuthTokens) -> McpOAuthTokens:
        if not tokens.refresh_token:
            raise McpAuthError("MCP OAuth access token expired and no refresh_token is available")
        payload = _post_form_json(
            self.oauth_config.token_url,
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
                "client_id": self.oauth_config.client_id,
                **({"client_secret": self.oauth_config.client_secret} if self.oauth_config.client_secret else {}),
            },
        )
        refreshed = McpOAuthTokens.from_token_response(
            payload,
            previous_refresh_token=tokens.refresh_token,
        )
        self.store.set(self.store_key, refreshed)
        self._cached_tokens = refreshed
        return refreshed


class McpOAuthService:
    def __init__(self, *, token_store_path: Path):
        self.store = McpOAuthTokenStore(token_store_path)

    def has_tokens(self, config: McpServerConfig) -> bool:
        return self.store.get(oauth_store_key(config.name, config.url)) is not None

    def provider_for_config(self, config: McpServerConfig) -> McpOAuthProvider:
        return McpOAuthProvider(
            config=config,
            oauth_config=McpOAuthConfig.resolve(config, store=self.store),
            store=self.store,
        )

    async def begin_authorization(self, config: McpServerConfig) -> "McpOAuthAuthorizationFlow":
        callback = _OAuthCallbackServer()
        callback.start()
        oauth_config = McpOAuthConfig.resolve(
            config,
            store=self.store,
            redirect_uri=callback.redirect_uri,
        )
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48) if oauth_config.use_pkce else ""
        params = {
            "response_type": "code",
            "client_id": oauth_config.client_id,
            "redirect_uri": callback.redirect_uri,
            "state": state,
        }
        if oauth_config.scope:
            params["scope"] = oauth_config.scope
        if oauth_config.audience:
            params["audience"] = oauth_config.audience
        if verifier:
            params["code_challenge"] = _pkce_challenge(verifier)
            params["code_challenge_method"] = "S256"
        authorization_url = _url_with_query(oauth_config.authorization_url, params)
        return McpOAuthAuthorizationFlow(
            authorization_url=authorization_url,
            redirect_uri=callback.redirect_uri,
            state=state,
            code_verifier=verifier,
            server_config=config,
            oauth_config=oauth_config,
            callback=callback,
            store=self.store,
        )


@dataclass
class McpOAuthAuthorizationFlow:
    authorization_url: str
    redirect_uri: str
    state: str
    code_verifier: str
    server_config: McpServerConfig
    oauth_config: McpOAuthConfig
    callback: "_OAuthCallbackServer"
    store: McpOAuthTokenStore

    async def complete(self, *, timeout: float = 900.0) -> McpOAuthTokens:
        callback_payload = await asyncio.to_thread(self.callback.wait, timeout)
        if callback_payload.get("error"):
            raise McpAuthError(f"OAuth callback error: {callback_payload['error']}")
        if callback_payload.get("state") != self.state:
            raise McpAuthError("OAuth callback state mismatch")
        code = str(callback_payload.get("code") or "").strip()
        if not code:
            raise McpAuthError("OAuth callback missing authorization code")
        token_payload = await asyncio.to_thread(self._exchange_code, code)
        tokens = McpOAuthTokens.from_token_response(token_payload)
        self.store.set(oauth_store_key(self.server_config.name, self.server_config.url), tokens)
        self.callback.close()
        return tokens

    async def cancel(self) -> None:
        self.callback.close()

    def _exchange_code(self, code: str) -> dict[str, Any]:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.oauth_config.client_id,
        }
        if self.oauth_config.client_secret:
            payload["client_secret"] = self.oauth_config.client_secret
        if self.code_verifier:
            payload["code_verifier"] = self.code_verifier
        return _post_form_json(self.oauth_config.token_url, payload)


def oauth_store_key(server_name: str, server_url: str) -> str:
    parsed = urllib.parse.urlparse(server_url)
    resource = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))
    digest = hashlib.sha256(resource.encode("utf-8")).hexdigest()[:16]
    safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in server_name)
    return f"{safe_name}_{digest}"


def _remove_authorization_headers(headers: dict[str, str]) -> None:
    for key in list(headers):
        if key.lower() == "authorization":
            headers.pop(key, None)


def _store_entry(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    if "access_token" in raw:
        return {"tokens": dict(raw)}
    return dict(raw)


def _needs_client_registration(raw: dict[str, Any]) -> bool:
    return not str(raw.get("clientId") or raw.get("client_id") or "").strip()


def _discover_oauth_metadata(config: McpServerConfig) -> dict[str, Any]:
    raw = dict(config.oauth or {})
    metadata_url = str(
        raw.get("authorizationServerMetadataUrl")
        or raw.get("authorization_server_metadata_url")
        or raw.get("metadataUrl")
        or raw.get("metadata_url")
        or ""
    ).strip()
    if metadata_url:
        return _get_json_object(metadata_url, "OAuth authorization server metadata")

    issuer = str(raw.get("issuerUrl") or raw.get("issuer") or raw.get("issuer_url") or "").strip()
    if issuer:
        return _get_json_object(
            _well_known_authorization_server_url(issuer),
            "OAuth authorization server metadata",
        )

    resource_metadata_url = str(
        raw.get("protectedResourceMetadataUrl")
        or raw.get("protected_resource_metadata_url")
        or raw.get("resourceMetadataUrl")
        or raw.get("resource_metadata_url")
        or ""
    ).strip()
    if not resource_metadata_url and config.url:
        parsed = urllib.parse.urlparse(config.url)
        resource_metadata_url = urllib.parse.urlunparse(
            (parsed.scheme, parsed.netloc, "/.well-known/oauth-protected-resource", "", "", "")
        )
    resource_metadata = _get_json_object(
        resource_metadata_url,
        "OAuth protected resource metadata",
    )
    auth_servers = resource_metadata.get("authorization_servers") or []
    if not isinstance(auth_servers, list) or not auth_servers:
        raise McpAuthError("OAuth protected resource metadata missing authorization_servers")
    return _get_json_object(
        _well_known_authorization_server_url(str(auth_servers[0])),
        "OAuth authorization server metadata",
    )


def _well_known_authorization_server_url(issuer: str) -> str:
    parsed = urllib.parse.urlparse(issuer)
    base_path = parsed.path.rstrip("/")
    path = f"{base_path}/.well-known/oauth-authorization-server"
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _get_json_object(url: str, label: str) -> dict[str, Any]:
    if not url:
        raise McpAuthError(f"{label} URL is required")
    request = urllib.request.Request(url, headers={"accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        raise McpAuthError(f"{label} request failed: {error.code} {raw}") from error
    except Exception as error:  # noqa: BLE001 - normalize OAuth boundary errors.
        raise McpAuthError(f"{label} request failed: {error}") from error
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise McpAuthError(f"{label} response is not JSON: {raw!r}") from error
    if not isinstance(parsed, dict):
        raise McpAuthError(f"{label} response must be a JSON object")
    return parsed


def _register_oauth_client(
    registration_url: str,
    *,
    redirect_uri: str,
    client_name: str,
) -> dict[str, Any]:
    payload = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    result = _post_json_object(registration_url, payload, "OAuth dynamic client registration")
    if not str(result.get("client_id") or "").strip():
        raise McpAuthError("OAuth dynamic client registration response missing client_id")
    return result


def _post_json_object(url: str, payload: dict[str, Any], label: str) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        raise McpAuthError(f"{label} failed: {error.code} {raw}") from error
    except Exception as error:  # noqa: BLE001 - normalize OAuth boundary errors.
        raise McpAuthError(f"{label} failed: {error}") from error
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise McpAuthError(f"{label} response is not JSON: {raw!r}") from error
    if not isinstance(parsed, dict):
        raise McpAuthError(f"{label} response must be a JSON object")
    return parsed


def _url_with_query(url: str, params: dict[str, str]) -> str:
    parsed = urllib.parse.urlparse(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _post_form_json(url: str, payload: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        raise McpAuthError(f"OAuth token request failed: {error.code} {raw}") from error
    except Exception as error:  # noqa: BLE001 - normalize OAuth boundary errors.
        raise McpAuthError(f"OAuth token request failed: {error}") from error
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise McpAuthError(f"OAuth token response is not JSON: {raw!r}") from error
    if not isinstance(parsed, dict):
        raise McpAuthError("OAuth token response must be a JSON object")
    return parsed


class _OAuthCallbackServer:
    def __init__(self):
        self._queue: queue.Queue[dict[str, str]] = queue.Queue(maxsize=1)
        handler = self._make_handler(self._queue)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def redirect_uri(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/callback"

    def start(self) -> None:
        self._thread.start()

    def wait(self, timeout: float) -> dict[str, str]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty as error:
            raise McpAuthError("OAuth callback timed out") from error

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @staticmethod
    def _make_handler(result_queue: queue.Queue[dict[str, str]]):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib handler API.
                parsed = urllib.parse.urlparse(self.path)
                query = {
                    key: values[-1]
                    for key, values in urllib.parse.parse_qs(parsed.query).items()
                    if values
                }
                try:
                    result_queue.put_nowait(query)
                except queue.Full:
                    pass
                body = b"OAuth callback received. You can close this window."
                self.send_response(200)
                self.send_header("content-type", "text/plain; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):  # noqa: D401 - silence callback server.
                return

        return Handler
