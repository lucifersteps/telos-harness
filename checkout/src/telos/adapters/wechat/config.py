"""WeChat configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from telos.core.coerce import coerce_bool


@dataclass(frozen=True)
class WeChatConfig:
    account_id: str = ""
    token: str = ""
    base_url: str = "https://ilinkai.weixin.qq.com"
    cdn_base_url: str = "https://novac2c.cdn.weixin.qq.com/c2c"
    dm_policy: str = "open"
    allowed_users: list[str] = field(default_factory=list)
    group_policy: str = "disabled"
    group_allowed_users: list[str] = field(default_factory=list)
    split_multiline_messages: bool = True
    image_followup_wait_seconds: int = 30
    max_text_chars: int = 1900
    max_bubbles: int = 8

    @classmethod
    def from_env(cls) -> "WeChatConfig":
        return cls(
            account_id=os.getenv("WECHAT_ACCOUNT_ID", "").strip(),
            token=os.getenv("WECHAT_TOKEN", "").strip(),
            base_url=os.getenv("WECHAT_BASE_URL", "").strip() or "https://ilinkai.weixin.qq.com",
            cdn_base_url=os.getenv("WECHAT_CDN_BASE_URL", "").strip()
            or "https://novac2c.cdn.weixin.qq.com/c2c",
            dm_policy=os.getenv("WECHAT_DM_POLICY", "open").strip().lower(),
            allowed_users=_coerce_list(os.getenv("WECHAT_ALLOWED_USERS", "")),
            group_policy=os.getenv("WECHAT_GROUP_POLICY", "disabled").strip().lower(),
            group_allowed_users=_coerce_list(os.getenv("WECHAT_GROUP_ALLOWED_USERS", "")),
            split_multiline_messages=coerce_bool(
                os.getenv("WECHAT_SPLIT_MULTILINE_MESSAGES"),
                default=True,
            ),
            image_followup_wait_seconds=_coerce_int(
                os.getenv("WECHAT_IMAGE_FOLLOWUP_WAIT_SECONDS"),
                default=30,
                minimum=0,
            ),
        )


def _coerce_int(value: object, *, default: int, minimum: int | None = None) -> int:
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        return max(minimum, result)
    return result


def _coerce_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list | tuple | set):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []
