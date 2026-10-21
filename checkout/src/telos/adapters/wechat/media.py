"""WeChat media handling."""

from __future__ import annotations

import asyncio
import base64
import copy
import logging
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib import request as urllib_request
from urllib.parse import quote, urlparse

from telos.adapters.base import AttachmentRef
from telos.adapters.wechat.config import WeChatConfig
from telos.connectors.voice import MAX_AUDIO_BYTES, VoiceInputError
from telos.core.image_media import (
    image_ext_from_bytes,
    looks_like_image,
    mime_for_ext,
    sha256_bytes,
)

logger = logging.getLogger(__name__)

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    _CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - optional runtime dependency
    default_backend = None  # type: ignore[assignment]
    Cipher = None  # type: ignore[assignment]
    algorithms = None  # type: ignore[assignment]
    modes = None  # type: ignore[assignment]
    _CRYPTO_AVAILABLE = False


ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

FetchBytesFn = Callable[[str, float], Awaitable[bytes] | bytes]
MediaResolverFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]
VoiceTranscriberFn = Callable[[bytes, dict], Awaitable[str]]

_WECHAT_CDN_ALLOWLIST: frozenset[str] = frozenset(
    {
        "novac2c.cdn.weixin.qq.com",
        "ilinkai.weixin.qq.com",
        "wx.qlogo.cn",
        "thirdwx.qlogo.cn",
        "res.wx.qq.com",
        "mmbiz.qpic.cn",
        "mmbiz.qlogo.cn",
    }
)


def extract_attachments(items: list[dict]) -> list[AttachmentRef]:
    attachments: list[AttachmentRef] = []
    for item in items:
        if not isinstance(item, dict):continue
        ref = (item.get("ref_msg") or {}).get("message_item")
        if isinstance(ref, dict) and item_type_value(ref) in (ITEM_IMAGE, ITEM_FILE):
            kind = "image" if item_type_value(ref) == ITEM_IMAGE else "file"
            attachments.append(_attachment(kind, ref.get(kind + "_item") or ref))
        item_type = item_type_value(item)
        if item_type == ITEM_IMAGE:
            attachments.append(_attachment("image", item.get("image_item") or item))
        elif item_type == ITEM_VOICE:
            if not _voice_has_transcript(item) and not (item.get("voice_item") or {}).get("transcription_error"):
                attachments.append(_attachment("voice", item.get("voice_item") or item))
        elif item_type == ITEM_FILE:
            attachments.append(_attachment("file", item.get("file_item") or item))
        elif item_type == ITEM_VIDEO:
            attachments.append(_attachment("video", item.get("video_item") or item))
    return attachments


def _attachment(kind: str, raw: object) -> AttachmentRef:
    data = raw if isinstance(raw, dict) else {}
    uri = str(data.get("url") or data.get("uri") or data.get("file_id") or "")
    name = str(data.get("name") or data.get("file_name") or "")
    mime_type = str(data.get("mime_type") or "")
    return AttachmentRef(kind=kind, uri=uri, name=name, mime_type=mime_type)


def _voice_has_transcript(item: dict) -> bool:
    voice_item = item.get("voice_item") if isinstance(item.get("voice_item"), dict) else {}
    return bool(str(voice_item.get("text") or "").strip())


def item_type_value(item: dict) -> int:
    raw_type = item.get("type")
    if isinstance(raw_type, int):
        return raw_type
    text = str(raw_type or "").strip().lower()
    if text:
        try:
            return int(text)
        except ValueError:
            return 0
    return 0


def create_wechat_media_resolver(
    config: WeChatConfig,
    *,
    media_dir: str | Path,
    fetch_bytes_fn: FetchBytesFn | None = None,
    voice_transcriber: VoiceTranscriberFn | None = None,
) -> MediaResolverFn:
    fetch_bytes = fetch_bytes_fn or _default_fetch_bytes
    target_dir = Path(media_dir)

    async def resolve(raw: dict[str, Any]) -> dict[str, Any]:
        message = copy.deepcopy(raw)
        item_list = list(message.get("item_list") or message.get("items") or [])
        for item in item_list:
            if isinstance(item, dict) and item_type_value(item) == ITEM_VOICE and voice_transcriber:
                await _resolve_voice_item(item, config=config, fetch_bytes=fetch_bytes_fn or _default_fetch_voice_bytes, transcribe=voice_transcriber)
            if isinstance(item, dict) and item_type_value(item) == ITEM_IMAGE:
                await _resolve_image_item(
                    item,
                    config=config,
                    media_dir=target_dir,
                    fetch_bytes=fetch_bytes,
                )
            ref_message = item.get("ref_msg") if isinstance(item, dict) else None
            ref_item = ref_message.get("message_item") if isinstance(ref_message, dict) else None
            if isinstance(ref_item, dict) and item_type_value(ref_item) == ITEM_IMAGE:
                await _resolve_image_item(
                    ref_item,
                    config=config,
                    media_dir=target_dir,
                    fetch_bytes=fetch_bytes,
                )
            for candidate in (item, ref_item):
                if isinstance(candidate, dict) and item_type_value(candidate) == ITEM_FILE:
                    await _resolve_file_item(candidate, config=config, media_dir=target_dir, fetch_bytes=fetch_bytes)
        message["item_list"] = item_list
        return message

    return resolve


async def _resolve_file_item(item, *, config, media_dir, fetch_bytes):
    file = item.get("file_item")
    if not isinstance(file, dict):
        file = item["file_item"] = {}
    file["name"] = str(file.get("file_name") or file.get("name") or "attachment")[:240]
    try:
        media = file.get("media") or {}
        query = str(media.get("encrypt_query_param") or "")
        url = str(media.get("full_url") or file.get("url") or "")
        if query:
            url = _cdn_download_url(config.cdn_base_url, query)
        else:
            _assert_wechat_media_url(url)
        data = fetch_bytes(url, 30.0)
        if hasattr(data, "__await__"):
            data = await data
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("File exceeds 16 MiB")
        key = _image_aes_key(file, media)
        if query and not key:
            raise ValueError("Encrypted file has no key")
        if key:
            data = _aes128_ecb_decrypt(data, key)
        # Never use a remote filename/path for local storage.
        media_dir.mkdir(parents=True, exist_ok=True)
        path = media_dir / ("file_" + sha256_bytes(data))
        path.write_bytes(data)
        file["url"] = str(path)
    except Exception:
        file.pop("url", None)
        file.pop("uri", None)
        file.pop("file_id", None)
        file["media_error"] = True
        logger.warning("微信文件下载或解密失败 | msg_id=%s", item.get("msg_id") or "-")


async def _resolve_voice_item(item, *, config, fetch_bytes, transcribe):
    voice = item.setdefault("voice_item", {})
    media = voice.get("media") or {}
    query = str(media.get("encrypt_query_param") or voice.get("encrypt_query_param") or "")
    url = str(media.get("full_url") or voice.get("full_url") or voice.get("url") or "")
    # 平台仅提供文字、没有可下载音频时，保留平台已有转写。
    if not query and not url and _voice_has_transcript(item):
        return
    try:
        if query:
            url = _cdn_download_url(config.cdn_base_url, query)
        if not url:
            raise VoiceInputError("这条语音没有可下载的音频，请重新发送语音或改用文字。")
        if not query:_assert_wechat_media_url(url)
        data = fetch_bytes(url, 30.0)
        if hasattr(data, "__await__"):
            data = await data
        if len(data) > MAX_AUDIO_BYTES:
            raise VoiceInputError("语音超过 16 MiB，请缩短后重新发送。")
        key = _image_aes_key(voice, media)
        if key:
            data = _aes128_ecb_decrypt(data, key)
        voice["text"] = await transcribe(data, voice)
        if not voice["text"].strip():
            raise VoiceInputError("没有识别到清晰语音，请重新发送或改用文字。")
        voice.pop("transcription_error", None)
    except Exception as error:
        # CancelledError 不属于 Exception：取消必须传回个人调度器，不能变成错误回复。
        logger.warning("微信语音处理失败 | error_type=%s", type(error).__name__)
        voice["text"] = ""
        voice["transcription_error"] = str(error) if isinstance(error, VoiceInputError) else "这条语音下载或转写失败，请重新发送语音或改用文字。"


async def _default_fetch_voice_bytes(url: str, timeout_seconds: float) -> bytes:
    def fetch():
        with urllib_request.urlopen(url, timeout=timeout_seconds) as response:
            data = response.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise VoiceInputError("语音超过 16 MiB，请缩短后重新发送。")
        return data
    return await asyncio.to_thread(fetch)


async def _resolve_image_item(
    item: dict[str, Any],
    *,
    config: WeChatConfig,
    media_dir: Path,
    fetch_bytes: FetchBytesFn,
) -> None:
    try:
        data = await _download_image_bytes(item, config=config, fetch_bytes=fetch_bytes)
        path = _cache_image(data, media_dir)
    except Exception as error:
        logger.warning(
            "微信图片下载失败 | msg_id=%s error_type=%s error=%s",
            item.get("msg_id") or "-",
            type(error).__name__,
            "下载或解密失败",
        )
        item.setdefault("image_item", {})["media_error"] = True
        return
    image_item = item.setdefault("image_item", {})
    if isinstance(image_item, dict):
        image_item["url"] = str(path)
        image_item.setdefault("name", path.name)
        image_item.setdefault("mime_type", mime_for_ext(path.suffix))


async def _download_image_bytes(
    item: dict[str, Any],
    *,
    config: WeChatConfig,
    fetch_bytes: FetchBytesFn,
) -> bytes:
    image_item = item.get("image_item") if isinstance(item.get("image_item"), dict) else {}
    media = image_item.get("media") if isinstance(image_item.get("media"), dict) else {}
    encrypted_query = str(media.get("encrypt_query_param") or image_item.get("encrypt_query_param") or "")
    full_url = str(media.get("full_url") or image_item.get("full_url") or image_item.get("url") or "")
    if encrypted_query:
        url = _cdn_download_url(config.cdn_base_url, encrypted_query)
    elif full_url:
        _assert_wechat_media_url(full_url)
        url = full_url
    else:
        raise ValueError("image item had neither encrypted query nor URL")
    data = fetch_bytes(url, 30.0)
    if hasattr(data, "__await__"):
        data = await data  # type: ignore[assignment]
    key = _image_aes_key(image_item, media)
    if key:
        data = _aes128_ecb_decrypt(data, key)
    if not looks_like_image(data):
        raise ValueError("downloaded media does not look like an image")
    return data


def _cdn_download_url(cdn_base_url: str, encrypted_query_param: str) -> str:
    return f"{cdn_base_url.rstrip('/')}/download?encrypted_query_param={quote(encrypted_query_param, safe='')}"


def _assert_wechat_media_url(url: str) -> None:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""
    if scheme not in {"http", "https"}:
        raise ValueError(f"Media URL has disallowed scheme: {scheme!r}")
    if host not in _WECHAT_CDN_ALLOWLIST:
        raise ValueError(f"Media URL host {host!r} is not in the WeChat CDN allowlist")


def _image_aes_key(image_item: dict[str, Any], media: dict[str, Any]) -> bytes:
    raw_hex = str(image_item.get("aeskey") or "").strip()
    if raw_hex:
        return bytes.fromhex(raw_hex)
    raw_b64 = str(media.get("aes_key") or "").strip()
    if not raw_b64:
        return b""
    return _parse_aes_key(raw_b64)


def _parse_aes_key(aes_key_b64: str) -> bytes:
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        text = decoded.decode("ascii", errors="ignore")
        if text and all(ch in "0123456789abcdefABCDEF" for ch in text):
            return bytes.fromhex(text)
    raise ValueError(f"unexpected aes_key format ({len(decoded)} decoded bytes)")


def _aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    if not _CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography is required to decrypt WeChat media")
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    return _pkcs7_unpad(decryptor.update(ciphertext) + decryptor.finalize())


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad_len = data[-1]
    if 1 <= pad_len <= 16 and data.endswith(bytes([pad_len]) * pad_len):
        return data[:-pad_len]
    return data


def _cache_image(data: bytes, media_dir: Path) -> Path:
    media_dir.mkdir(parents=True, exist_ok=True)
    path = media_dir / f"img_{sha256_bytes(data)}{image_ext_from_bytes(data)}"
    if not path.exists():
        path.write_bytes(data)
    return path


async def _default_fetch_bytes(url: str, timeout_seconds: float) -> bytes:
    return await asyncio.to_thread(_fetch_bytes_blocking, url, timeout_seconds)


def _fetch_bytes_blocking(url: str, timeout_seconds: float) -> bytes:
    with urllib_request.urlopen(url, timeout=timeout_seconds) as response:
        data = response.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:raise ValueError("Image exceeds 16 MiB")
        return data
