"""Native Apple connector protocol for Things import/export.

This module is the Python boundary for the EventKit/ReminderKit helper.
The helper owns macOS-specific access; Telos talks to it using a small JSON
request/response protocol so tests can use a fake runner.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


DEFAULT_TIMEOUT_SECONDS = 60
BUNDLED_HELPER_PATH = Path(__file__).resolve().parent / "native_helper" / "telos-apple-helper"


class AppleNativeError(RuntimeError):
    """Expected native Apple helper failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        retriable: bool = False,
        remediation: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.retriable = retriable
        self.remediation = remediation


@dataclass(frozen=True)
class NativeAppleCapabilities:
    """Capability report for Apple import/export."""

    eventkit: dict[str, bool]
    private_reminders: dict[str, bool]
    sqlite_reads: dict[str, bool]

    @classmethod
    def default(cls) -> "NativeAppleCapabilities":
        return cls(
            eventkit={
                "reminder_lists": True,
                "reminder_crud": True,
                "calendar_crud": True,
                "alarms": True,
                "recurrence": True,
                "url": True,
                "priority": True,
                "structured_location": True,
                "attachments": False,
                "travel_time": False,
                "audio_alarms": True,
            },
            private_reminders={
                "sections": False,
                "tags": False,
                "subtasks": False,
                "flagged": False,
                "attachments": False,
                "urgent": False,
            },
            sqlite_reads={
                "high_fidelity_reminders": False,
                "sections": False,
                "tags": False,
                "subtasks": False,
            },
        )

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "NativeAppleCapabilities":
        default = cls.default()
        return cls(
            eventkit={
                **default.eventkit,
                **_bool_mapping(payload.get("eventkit")),
            },
            private_reminders={
                **default.private_reminders,
                **_bool_mapping(payload.get("private_reminders")),
            },
            sqlite_reads={
                **default.sqlite_reads,
                **_bool_mapping(payload.get("sqlite_reads")),
            },
        )

    def to_dict(self) -> dict[str, dict[str, bool]]:
        return {
            "eventkit": dict(self.eventkit),
            "private_reminders": dict(self.private_reminders),
            "sqlite_reads": dict(self.sqlite_reads),
        }


class NativeAppleRunner(Protocol):
    def run(
        self,
        argv: list[str],
        *,
        input: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessNativeAppleRunner:
    def run(
        self,
        argv: list[str],
        *,
        input: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            input=input,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )


class AppleNativeJsonHelperClient:
    """Call a native Swift/Objective-C helper through JSON stdin/stdout."""

    def __init__(
        self,
        *,
        runner: NativeAppleRunner | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.runner = runner or SubprocessNativeAppleRunner()
        self.timeout = timeout

    def capabilities(self) -> NativeAppleCapabilities:
        payload = self.request("capabilities")
        return NativeAppleCapabilities.from_mapping(payload)

    def list_items(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("list", {"filters": dict(filters or {})})

    def detail(self, ids: list[str]) -> dict[str, Any]:
        return self.request("detail", {"ids": _clean_strings(ids)})

    def create(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        return self.request("create", {"items": _dict_items(items)})

    def update(self, updates: list[dict[str, Any]]) -> dict[str, Any]:
        return self.request("update", {"updates": _dict_items(updates)})

    def delete(self, ids: list[str], *, force: bool = False) -> dict[str, Any]:
        return self.request("delete", {"ids": _clean_strings(ids), "force": bool(force)})

    def request(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        clean_action = str(action or "").strip()
        if not clean_action:
            raise AppleNativeError("native Apple helper action is required", code="invalid_action")
        helper = self._resolve_helper()
        request_payload = {"action": clean_action, **dict(payload or {})}
        try:
            result = self.runner.run(
                [helper, clean_action],
                input=json.dumps(request_payload, ensure_ascii=False, sort_keys=True),
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise AppleNativeError(
                f"native Apple helper timed out after {self.timeout} seconds",
                code="native_apple_timeout",
                retriable=True,
            ) from error
        if result.returncode != 0:
            raise AppleNativeError(
                _stderr_or_stdout(result) or f"native Apple helper failed: {result.returncode}",
                code="native_apple_helper_failed",
            )
        return _parse_helper_output(result.stdout)

    def _resolve_helper(self) -> str:
        if BUNDLED_HELPER_PATH.exists():
            return str(BUNDLED_HELPER_PATH)
        raise AppleNativeError(
            f"native Apple helper not found: {BUNDLED_HELPER_PATH}",
            code="native_apple_helper_not_found",
            remediation=(
                "Build src/telos/connectors/apple/native_helper/telos-apple-helper "
                "before using Apple import/export."
            ),
        )


def _parse_helper_output(output: str) -> dict[str, Any]:
    text = str(output or "").strip()
    if not text:
        raise AppleNativeError("native Apple helper returned empty output", code="empty_output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise AppleNativeError(
            f"native Apple helper returned invalid JSON: {error.msg}",
            code="invalid_json",
        ) from error
    if not isinstance(payload, dict):
        raise AppleNativeError("native Apple helper output must be an object", code="invalid_output")
    if payload.get("ok") is False:
        raw_error = payload.get("error")
        if isinstance(raw_error, dict):
            raise AppleNativeError(
                str(raw_error.get("message") or "native Apple helper failed"),
                code=str(raw_error.get("code") or "native_apple_error"),
                retriable=bool(raw_error.get("retriable")),
                remediation=str(raw_error.get("remediation") or ""),
            )
        raise AppleNativeError(str(raw_error or "native Apple helper failed"))
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise AppleNativeError("native Apple helper data must be an object", code="invalid_data")
    return data


def _stderr_or_stdout(result: subprocess.CompletedProcess[str]) -> str:
    return str(result.stderr or result.stdout or "").strip()


def _clean_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in list(values or []):
        text = str(value or "").strip()
        if text:
            result.append(text)
    return result


def _dict_items(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for item in list(values or []) if isinstance(item, dict)]


def _bool_mapping(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {str(key): bool(raw) for key, raw in value.items()}
