"""CLI rendering helpers."""

from __future__ import annotations

import sys


def render_text(text: str) -> str:
    return str(text or "")


def print_text(text: str) -> None:
    sys.stdout.write(render_text(text))
    sys.stdout.write("\n")
    sys.stdout.flush()
