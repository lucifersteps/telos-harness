"""Accessibility text compaction for timeline captures."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any


DEFAULT_COMPACT_TEXT_CHARS = 12000
DEFAULT_CHROME_TEXT_BUDGET_RATIO = 0.15
CHROME_TEXT_CAP = 1200
TEXT_KEYS = ("title", "description", "value", "selected_text", "placeholder", "help")

MENU_ROLES = {
    "AXMenuItem",
    "AXMenuBarItem",
    "AXMenuBar",
    "AXMenu",
}
CHROME_CONTAINER_ROLES = {
    "AXToolbar",
    "AXTabGroup",
    "AXTab",
    "AXScrollBar",
}
CONTROL_ROLES = {
    "AXButton",
    "AXPopUpButton",
    "AXCheckBox",
    "AXRadioButton",
    "AXRadioGroup",
    "AXDisclosureTriangle",
    "AXSlider",
    "AXIncrementor",
    "AXComboBox",
    "AXSwitch",
}
CONTENT_ROLES = {
    "AXStaticText",
    "AXTextArea",
    "AXTextField",
    "AXSearchField",
    "AXHeading",
    "AXLink",
    "AXWebArea",
    "AXParagraph",
}
STRUCTURED_ROLES = {
    "AXCell",
    "AXRow",
    "AXColumn",
    "AXTable",
    "AXOutline",
    "AXOutlineRow",
    "AXList",
}
VISUAL_ROLES = {
    "AXCanvas",
    "AXImage",
    "AXMatte",
    "AXUnknown",
}
CONTAINER_ROLES = {
    "AXApplication",
    "AXWindow",
    "AXSheet",
    "AXDialog",
    "AXSystemDialog",
    "AXGroup",
    "AXScrollArea",
    "AXSplitGroup",
    "AXLayoutArea",
    "AXLayoutItem",
}


@dataclass(frozen=True)
class CompactAccessibility:
    text: str
    digest: str
    needs_vision: bool
    text_source: str
    meta: dict[str, Any]


def compact_accessibility_payload(
    payload: dict[str, Any],
    *,
    max_chars: int = DEFAULT_COMPACT_TEXT_CHARS,
) -> CompactAccessibility:
    header_lines: list[str] = []
    content_lines: list[str] = []
    control_lines: list[str] = []
    visual_lines: list[str] = []
    chrome_lines: list[str] = []
    total_chars = 0
    content_chars = 0
    control_chars = 0
    visual_chars = 0
    chrome_chars = 0
    role_counts: Counter[str] = Counter()
    seen: set[str] = set()
    limit = _compact_limit(max_chars)

    app = payload.get("app") if isinstance(payload.get("app"), dict) else {}
    window = payload.get("window") if isinstance(payload.get("window"), dict) else {}
    focused = payload.get("focused") if isinstance(payload.get("focused"), dict) else {}
    _append_line(header_lines, seen, f"App: {app.get('name') or ''}")
    _append_line(header_lines, seen, f"Window: {window.get('title') or ''}")
    for label, value in (
        ("URL", _first_text(payload, "url", window, focused)),
        ("Document", _first_text(payload, "document", window, focused)),
        ("Focused", _node_text(focused)),
    ):
        if value:
            _append_line(header_lines, seen, f"{label}: {value}")

    tree = payload.get("tree")
    if isinstance(tree, dict):
        for node, ancestors in _walk_nodes(tree):
            role = str(node.get("role") or "")
            role_counts[role] += 1
            node_text = _node_text(node)
            if node_text:
                total_chars += len(node_text)
            bucket = _classify_node(node, ancestors, app=app, window=window)
            if bucket == "content":
                content_chars += len(node_text)
            elif bucket == "control":
                control_chars += len(node_text)
            elif bucket == "visual":
                visual_chars += len(node_text)
            else:
                chrome_chars += len(node_text)

            if bucket == "chrome" and role in CONTAINER_ROLES and _is_redundant_container_text(
                node_text,
                app=app,
                window=window,
                role=role,
            ):
                continue
            line = _node_line(node)
            if not line:
                continue
            if bucket == "content":
                _append_line(content_lines, seen, line)
            elif bucket == "control":
                _append_line(control_lines, seen, line)
            elif bucket == "visual":
                _append_line(visual_lines, seen, line)
            else:
                _append_line(chrome_lines, seen, line)

    lines = list(header_lines)
    content_start = len(lines)
    _extend_with_budget(lines, content_lines, limit=limit)
    emitted_content_lines = len(lines) - content_start

    control_start = len(lines)
    _extend_with_budget(lines, control_lines, limit=limit)
    emitted_control_lines = len(lines) - control_start

    visual_start = len(lines)
    _extend_with_budget(lines, visual_lines, limit=limit, added_limit=round(limit * 0.1))
    emitted_visual_lines = len(lines) - visual_start

    has_primary_lines = bool(content_lines or control_lines)
    chrome_budget = min(CHROME_TEXT_CAP, round(limit * DEFAULT_CHROME_TEXT_BUDGET_RATIO))
    if not has_primary_lines:
        chrome_budget = min(chrome_budget, 700)
    chrome_start = len(lines)
    _extend_with_budget(lines, chrome_lines, limit=limit, added_limit=chrome_budget)
    emitted_chrome_lines = len(lines) - chrome_start

    text = _trim_text("\n".join(lines), max_chars=max_chars)
    digest = stable_digest({"text": text})
    has_error = isinstance(payload.get("error"), dict)
    has_skipped = isinstance(payload.get("skipped"), dict)
    source = "none" if has_error or has_skipped or not text.strip() else "accessibility"
    informative_chars = content_chars + control_chars
    ratio = (informative_chars / total_chars) if total_chars > 0 else 0.0
    needs_vision_reason = _needs_vision_reason(
        source=source,
        total_chars=total_chars,
        informative_chars=informative_chars,
        content_line_count=len(content_lines),
        control_line_count=len(control_lines),
        role_counts=role_counts,
        visual_chars=visual_chars,
    )
    candidate_line_count = (
        len(content_lines) + len(control_lines) + len(visual_lines) + len(chrome_lines)
    )
    emitted_candidate_line_count = len(lines) - len(header_lines)
    meta = {
        "line_count": len(lines),
        "total_chars": total_chars,
        "content_chars": content_chars,
        "control_chars": control_chars,
        "visual_chars": visual_chars,
        "chrome_chars": chrome_chars,
        "informative_chars": informative_chars,
        "content_ratio": round(ratio, 3),
        "needs_vision_reason": needs_vision_reason,
        "truncated": (
            _line_text_length(
                header_lines + content_lines + control_lines + visual_lines + chrome_lines
            )
            > limit
            or candidate_line_count > emitted_candidate_line_count
        ),
        "content_candidate_line_count": len(content_lines),
        "control_candidate_line_count": len(control_lines),
        "visual_candidate_line_count": len(visual_lines),
        "chrome_candidate_line_count": len(chrome_lines),
        "emitted_content_line_count": emitted_content_lines,
        "emitted_control_line_count": emitted_control_lines,
        "emitted_visual_line_count": emitted_visual_lines,
        "emitted_chrome_line_count": emitted_chrome_lines,
        "top_role_counts": role_counts.most_common(10),
        "tree_meta": payload.get("tree_meta") if isinstance(payload.get("tree_meta"), dict) else {},
    }
    return CompactAccessibility(
        text=text,
        digest=digest,
        needs_vision=bool(needs_vision_reason),
        text_source=source,
        meta=meta,
    )


def stable_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _walk_nodes(node: dict[str, Any], ancestors: tuple[str, ...] = ()):
    yield node, ancestors
    children = node.get("children")
    if not isinstance(children, list):
        return
    role = str(node.get("role") or "")
    for child in children:
        if isinstance(child, dict):
            yield from _walk_nodes(child, ancestors + (role,))


def _node_text(node: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in TEXT_KEYS:
        value = node.get(key)
        if isinstance(value, str) and _is_useful_text(value):
            parts.append(value)
    return _normalize_text(" ".join(parts))


def _first_text(payload: dict[str, Any], key: str, *nodes: dict[str, Any]) -> str:
    value = payload.get(key)
    if isinstance(value, str) and _is_useful_text(value):
        return _normalize_text(value)
    for node in nodes:
        value = node.get(key)
        if isinstance(value, str) and _is_useful_text(value):
            return _normalize_text(value)
    return ""


def _node_line(node: dict[str, Any]) -> str:
    role = str(node.get("role") or "")
    text = _node_text(node)
    state = _state_text(node, role=role)
    if not text and not state:
        return ""
    body = " ".join(part for part in (text, state) if part)
    return f"{_role_prefix(role)}{body}"


def _classify_node(
    node: dict[str, Any],
    ancestors: tuple[str, ...],
    *,
    app: dict[str, Any],
    window: dict[str, Any],
) -> str:
    role = str(node.get("role") or "")
    ancestor_roles = set(ancestors)
    node_text = _node_text(node)
    if role in MENU_ROLES or ancestor_roles.intersection(MENU_ROLES):
        return "chrome"
    if role in VISUAL_ROLES:
        return "visual"
    if role in CHROME_CONTAINER_ROLES or ancestor_roles.intersection(CHROME_CONTAINER_ROLES):
        return "chrome"
    if role in CONTENT_ROLES or role in STRUCTURED_ROLES:
        return "content"
    if role in CONTROL_ROLES:
        return "control"
    if role in CONTAINER_ROLES:
        if _is_redundant_container_text(node_text, app=app, window=window, role=role):
            return "chrome"
        if len(node_text) >= 24:
            return "content"
        return "chrome"
    if node_text:
        return "content"
    return "chrome"


def _state_text(node: dict[str, Any], *, role: str) -> str:
    parts: list[str] = []
    if role in CONTROL_ROLES or role in STRUCTURED_ROLES:
        selected = node.get("selected")
        if isinstance(selected, bool):
            parts.append(f"selected={str(selected).lower()}")
        enabled = node.get("enabled")
        if enabled is False:
            parts.append("enabled=false")
        focused = node.get("focused")
        if focused is True:
            parts.append("focused=true")
    return f"[{', '.join(parts)}]" if parts else ""


def _is_redundant_container_text(
    text: str,
    *,
    app: dict[str, Any],
    window: dict[str, Any],
    role: str,
) -> bool:
    if not text:
        return True
    values = {
        _normalize_text(str(app.get("name") or "")),
        _normalize_text(str(window.get("title") or "")),
    }
    normalized = _normalize_text(text)
    if normalized in values:
        return True
    return role in {"AXApplication", "AXWindow"} and len(normalized) < 24


def _needs_vision_reason(
    *,
    source: str,
    total_chars: int,
    informative_chars: int,
    content_line_count: int,
    control_line_count: int,
    role_counts: Counter[str],
    visual_chars: int,
) -> str:
    if source != "accessibility":
        return "source_unavailable"
    informative_lines = content_line_count + control_line_count
    role_total = sum(role_counts.values())
    menu_role_count = sum(role_counts.get(role, 0) for role in MENU_ROLES)
    menu_role_ratio = (menu_role_count / role_total) if role_total else 0.0
    visual_role_count = sum(role_counts.get(role, 0) for role in VISUAL_ROLES)
    if total_chars < 100:
        return "visual_only" if visual_role_count else "insufficient_accessibility_text"
    if menu_role_ratio >= 0.6 and informative_chars < 600 and informative_lines < 12:
        return "menu_only"
    if visual_role_count and informative_chars < 600 and visual_chars <= informative_chars:
        return "visual_dominant"
    ratio = informative_chars / total_chars if total_chars else 0.0
    if ratio < 0.2 and informative_chars < 700 and informative_lines < 12:
        return "low_content_ratio"
    return ""


def _append_line(lines: list[str], seen: set[str], line: str) -> None:
    normalized = _normalize_text(line)
    if not normalized:
        return
    key = normalized.casefold()
    if key in seen:
        return
    seen.add(key)
    lines.append(normalized)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _is_useful_text(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    if normalized.startswith("<AXUIElement ") or "<AXUIElement " in normalized:
        return False
    lowered = normalized.lower()
    if lowered.startswith("data:image/") or lowered.startswith("blob:"):
        return False
    if len(normalized) > 500 and re.match(r"^[a-z]+://\S+$", lowered):
        return False
    return True


def _trim_text(text: str, *, max_chars: int) -> str:
    limit = _compact_limit(max_chars)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"...[truncated {len(text) - limit} chars]"


def _compact_limit(max_chars: int) -> int:
    return max(200, int(max_chars or DEFAULT_COMPACT_TEXT_CHARS))


def _line_text_length(lines: list[str]) -> int:
    return len("\n".join(lines))


def _extend_with_budget(
    lines: list[str],
    candidates: list[str],
    *,
    limit: int,
    added_limit: int | None = None,
) -> None:
    added = 0
    current = _line_text_length(lines)
    for line in candidates:
        separator = 1 if lines else 0
        line_cost = separator + len(line)
        if current + line_cost > limit:
            break
        if added_limit is not None and added + line_cost > added_limit:
            break
        lines.append(line)
        current += line_cost
        added += line_cost


def _role_prefix(role: str) -> str:
    return f"{role}: " if role else ""
