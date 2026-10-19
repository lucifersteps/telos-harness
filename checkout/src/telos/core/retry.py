"""Retry policy configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from telos.core.errors import ConfigurationError


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0
    backoff_multiplier: float = 2.0

    def delay_for_attempt(self, attempt: int) -> float:
        if self.initial_delay_seconds <= 0:
            return 0.0
        multiplier = max(self.backoff_multiplier, 1.0) ** max(attempt - 1, 0)
        return min(self.initial_delay_seconds * multiplier, self.max_delay_seconds)


def parse_retry_policy(
    raw: Any,
    *,
    field_prefix: str,
    default_max_attempts: int = 3,
) -> RetryPolicy:
    if raw is None:
        return RetryPolicy(max_attempts=int(default_max_attempts))
    if isinstance(raw, bool):
        return RetryPolicy(max_attempts=int(default_max_attempts) if raw else 1)
    data = _as_dict(raw, field=field_prefix)
    return RetryPolicy(
        max_attempts=_positive_int(
            data.get("max_attempts"),
            field=f"{field_prefix}.max_attempts",
            default=int(default_max_attempts),
        ),
        initial_delay_seconds=_non_negative_float(
            data.get("initial_delay_seconds"),
            field=f"{field_prefix}.initial_delay_seconds",
            default=0.25,
        ),
        max_delay_seconds=_non_negative_float(
            data.get("max_delay_seconds"),
            field=f"{field_prefix}.max_delay_seconds",
            default=2.0,
        ),
        backoff_multiplier=_positive_float(
            data.get("backoff_multiplier"),
            field=f"{field_prefix}.backoff_multiplier",
            default=2.0,
        ),
    )


def _as_dict(value: Any, *, field: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    raise ConfigurationError(f"{field} must be an object or boolean")


def _positive_int(value: Any, *, field: str, default: int) -> int:
    if value is None or str(value).strip() == "":
        return int(default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{field} must be a positive integer") from error
    if parsed <= 0:
        raise ConfigurationError(f"{field} must be a positive integer")
    return parsed


def _non_negative_float(value: Any, *, field: str, default: float) -> float:
    if value is None or str(value).strip() == "":
        return float(default)
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{field} must be a non-negative number") from error
    if parsed < 0:
        raise ConfigurationError(f"{field} must be a non-negative number")
    return parsed


def _positive_float(value: Any, *, field: str, default: float) -> float:
    if value is None or str(value).strip() == "":
        return float(default)
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{field} must be a positive number") from error
    if parsed <= 0:
        raise ConfigurationError(f"{field} must be a positive number")
    return parsed
