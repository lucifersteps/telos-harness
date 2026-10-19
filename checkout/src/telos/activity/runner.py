"""两条环路共用的模型调用、重试和提示词读取。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from telos.core.retry import RetryPolicy
from telos.providers.base import (
    PROMPT_CACHE_KEY_METADATA,
    ChatProvider,
    ChatRequest,
    Message,
    message_text,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ActivityRetryExhausted(Exception):
    """达到 max_retry 后抛出。调用方据此把该批标记为失败并继续往后推进。"""

    def __init__(self, *, cause: Exception, attempt_count: int):
        super().__init__(str(cause))
        self.cause = cause
        self.attempt_count = int(attempt_count or 0)


async def request_validated(
    provider: ChatProvider,
    *,
    request: ChatRequest,
    parse: Callable[[str], T],
    retry_policy: RetryPolicy | None,
    timeout_seconds: float,
    label: str,
) -> T:
    """调用模型并校验输出。provider 报错或校验失败都按退避重试。"""
    policy = retry_policy or RetryPolicy()
    max_attempts = max(1, int(policy.max_attempts))
    attempt = 1
    while True:
        try:
            response = await asyncio.wait_for(
                _provider_text(provider, request=request),
                timeout=max(0.001, float(timeout_seconds)),
            )
            return parse(response)
        except Exception as error:
            if attempt >= max_attempts:
                raise ActivityRetryExhausted(cause=error, attempt_count=attempt) from error
            logger.warning(
                "%s retry | attempt=%d error_type=%s error=%s",
                label,
                attempt,
                type(error).__name__,
                error,
            )
            delay = policy.delay_for_attempt(attempt)
            if delay > 0:
                await asyncio.sleep(delay)
            attempt += 1


async def _provider_text(provider: ChatProvider, *, request: ChatRequest) -> str:
    chunks: list[str] = []
    final: Message | None = None
    async for event in provider.chat(request):
        if event.type == "text_delta" and event.payload is not None:
            chunks.append(str(event.payload))
        elif event.type == "message" and isinstance(event.payload, Message):
            final = event.payload
    if final is not None and final.content:
        return message_text(final.content).strip()
    return "".join(chunks).strip()


def read_prompt_template(path: str, *, default: Path) -> str:
    text = str(path or "").strip()
    prompt_path = Path(text).expanduser() if text else default
    return prompt_path.read_text(encoding="utf-8").strip()


def request_metadata(settings: Any, *, run_type: str) -> dict[str, str]:
    agent_id = str(getattr(settings.paths, "agent_id", "") or "").strip() or "default"
    return {
        "run_type": run_type,
        PROMPT_CACHE_KEY_METADATA: f"{run_type}:{agent_id}",
    }
