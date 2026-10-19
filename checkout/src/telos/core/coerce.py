"""Small value-coercion helpers shared across config, adapters and views."""

from __future__ import annotations


_TRUE_TEXTS = {"1", "true", "yes", "on", "enabled"}
_FALSE_TEXTS = {"0", "false", "no", "off", "disabled"}


def coerce_bool(value: object, *, default: bool) -> bool:
    """Parse a config/user-supplied truthy value, falling back to ``default``."""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE_TEXTS:
        return True
    if text in _FALSE_TEXTS:
        return False
    return default


def safe_int(value: object, *, default: int = 0) -> int:
    """Best-effort int conversion that never raises."""

    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def clean_ids(values: list[str] | tuple[str, ...] | None) -> list[str]:
    """Strip, drop empties and de-duplicate an id list while keeping order."""

    seen: set[str] = set()
    cleaned: list[str] = []
    for value in list(values or []):
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            cleaned.append(item)
    return cleaned
