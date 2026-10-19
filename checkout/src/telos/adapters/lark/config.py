"""Lark configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LarkConfig:
    app_id: str = ""
    app_secret: str = ""
    base_url: str = "https://open.feishu.cn"
    verification_token: str = ""
    encrypt_key: str = ""
    max_text_chars: int = 4000
    max_bubbles: int = 8
    primary_users: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "LarkConfig":
        return cls(
            app_id=os.getenv("LARK_APP_ID", "").strip(),
            app_secret=os.getenv("LARK_APP_SECRET", "").strip(),
            base_url=os.getenv("LARK_BASE_URL", "").strip() or "https://open.feishu.cn",
            verification_token=os.getenv("LARK_VERIFICATION_TOKEN", "").strip(),
            encrypt_key=os.getenv("LARK_ENCRYPT_KEY", "").strip(),
            primary_users=_coerce_list(os.getenv("LARK_PRIMARY_USERS", "")),
        )


def _coerce_list(value: str) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for item in str(value or "").split(","):
        clean = item.strip()
        if not clean or clean in seen:
            continue
        items.append(clean)
        seen.add(clean)
    return items
