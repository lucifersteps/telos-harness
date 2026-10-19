"""Identifier helpers for sessions, agents, turns, and tool calls."""

from __future__ import annotations

import hashlib
import uuid


def generate_id(prefix: str) -> str:
    """Return a random opaque id with a stable prefix."""

    clean_prefix = prefix.strip().rstrip("_") or "id"
    return f"{clean_prefix}_{uuid.uuid4().hex[:24]}"


def stable_id(prefix: str, key: str, length: int = 20) -> str:
    """Return a stable id for a durable lookup key."""

    clean_prefix = prefix.strip().rstrip("_") or "id"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:length]
    return f"{clean_prefix}_{digest}"


def new_agent_id() -> str:
    return generate_id("agt")


def new_turn_id() -> str:
    return generate_id("turn")


def new_tool_call_id() -> str:
    return generate_id("tool")
