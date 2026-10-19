"""Business clock helpers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


USER_TIMEZONE_NAME = "Asia/Shanghai"
USER_TIMEZONE = ZoneInfo(USER_TIMEZONE_NAME)


def user_now() -> datetime:
    return datetime.now(USER_TIMEZONE).replace(tzinfo=None)


def user_now_iso(*, timespec: str = "seconds") -> str:
    return user_now().isoformat(timespec=timespec)


def to_user_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=None)
    return value.astimezone(USER_TIMEZONE).replace(tzinfo=None)


def ensure_user_time(value: datetime | None = None) -> datetime:
    if value is None:
        return user_now()
    return to_user_time(value)


def user_time_iso(value: datetime | None = None, *, timespec: str = "seconds") -> str:
    return ensure_user_time(value).isoformat(timespec=timespec)


def parse_user_time(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("business time is empty")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        raise ValueError("business time must not include timezone")
    return parsed
