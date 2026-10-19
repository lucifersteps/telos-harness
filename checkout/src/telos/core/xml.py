"""XML escaping helpers for the prompt/system-reminder blocks the agent renders."""

from __future__ import annotations


def xml_attr(value: object) -> str:
    """Escape a value for use inside a double-quoted XML attribute."""

    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .strip()
    )


def xml_text(value: object) -> str:
    """Escape a value for use as XML character data."""

    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
