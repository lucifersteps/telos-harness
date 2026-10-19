"""Web-search source reveal support."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from telos.adapters.base import InboundTurn
from telos.conversation.command_router import (
    ConversationCommandContext,
    ConversationCommandResult,
)
from telos.session.session import Session
from telos.storage.filesystem import read_json, write_json_atomic


WEB_SOURCE_PROMPT = "如需查看信源，请回复“信源”。"


@dataclass(frozen=True)
class WebSearchSource:
    title: str
    url: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url}


class WebSearchSourceCache:
    """Persist latest web-search sources per session/conversation."""

    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, list[WebSearchSource]] = {}
        self._load()

    def get(self, key: str) -> list[WebSearchSource]:
        return list(self._data.get(str(key or "").strip(), []))

    def set(self, key: str, sources: list[WebSearchSource]) -> None:
        clean_key = str(key or "").strip()
        if not clean_key:
            return
        unique = _dedupe_sources(sources)
        if not unique:
            self.clear(key)
            return
        self._data[clean_key] = unique[:3]
        self._save()

    def clear(self, key: str) -> None:
        clean_key = str(key or "").strip()
        if clean_key and clean_key in self._data:
            self._data.pop(clean_key, None)
            self._save()

    def get_for_turn(self, session: Session, turn: InboundTurn) -> list[WebSearchSource]:
        for key in _cache_keys(session, turn):
            sources = self.get(key)
            if sources:
                return sources
        return []

    def set_for_turn(
        self,
        session: Session,
        turn: InboundTurn,
        sources: list[WebSearchSource],
    ) -> None:
        for key in _cache_keys(session, turn):
            self.set(key, sources)

    def clear_for_turn(self, session: Session, turn: InboundTurn) -> None:
        for key in _cache_keys(session, turn):
            self.clear(key)

    def _load(self) -> None:
        if not self.path.exists():
            self._data = {}
            return
        try:
            raw = read_json(self.path, {})
        except ValueError:
            self._data = {}
            return
        if not isinstance(raw, dict):
            self._data = {}
            return
        loaded: dict[str, list[WebSearchSource]] = {}
        for key, items in raw.items():
            if not isinstance(items, list):
                continue
            sources = _sources_from_list(items)
            if sources:
                loaded[str(key)] = sources[:3]
        self._data = loaded

    def _save(self) -> None:
        write_json_atomic(
            self.path,
            {
                key: [source.to_dict() for source in sources[:3]]
                for key, sources in self._data.items()
            },
        )


class WebSearchSourceCommandHandler:
    """Return the latest web-search sources without entering the agent context."""

    def __init__(self, cache: WebSearchSourceCache):
        self.cache = cache

    def handle(
        self,
        turn: InboundTurn,
        session: Session,
        context: ConversationCommandContext | None = None,
    ) -> ConversationCommandResult | None:
        del context
        if turn.attachments or not is_web_source_request(turn.text):
            return None
        sources = self.cache.get_for_turn(session, turn)
        return ConversationCommandResult(
            command="web_search:sources",
            response=format_web_search_sources(sources),
        )


def is_web_source_request(text: str) -> bool:
    return str(text or "").strip() == "信源"


def append_web_source_prompt(response: str) -> str:
    text = str(response or "").strip()
    if not text or WEB_SOURCE_PROMPT in text:
        return text
    return f"{text}\n\n{WEB_SOURCE_PROMPT}"


def format_web_search_sources(sources: list[WebSearchSource]) -> str:
    if not sources:
        return "我这里没有上一轮联网搜索的来源记录。你让我重新搜一下，我再给你整理。"
    lines = ["上一轮联网搜索的参考来源："]
    for source in sources[:3]:
        lines.append(f"• {source.title}\n  {source.url}")
    return "\n".join(lines)


def collect_web_search_sources_from_events(events: list[object]) -> tuple[bool, list[WebSearchSource]]:
    used_web_search = False
    collected: list[WebSearchSource] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") != "tool.result" or event.get("tool_name") != "web_search":
            continue
        used_web_search = True
        if event.get("is_error"):
            continue
        collected.extend(_sources_from_output(event.get("output")))
    return used_web_search, _dedupe_sources(collected)[:3]


def _sources_from_output(output: object) -> list[WebSearchSource]:
    if isinstance(output, dict):
        results = output.get("results")
        if isinstance(results, list):
            return _sources_from_list(results)
    if isinstance(output, list):
        return _sources_from_list(output)
    return []


def _sources_from_list(items: list[object]) -> list[WebSearchSource]:
    sources: list[WebSearchSource] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if title and url:
            sources.append(WebSearchSource(title=title, url=url))
    return _dedupe_sources(sources)


def _dedupe_sources(sources: list[WebSearchSource]) -> list[WebSearchSource]:
    seen: set[str] = set()
    unique: list[WebSearchSource] = []
    for source in sources:
        url = str(source.url or "").strip()
        title = str(source.title or "").strip()
        if not title or not url or url in seen:
            continue
        seen.add(url)
        unique.append(WebSearchSource(title=title, url=url))
    return unique


def _cache_keys(session: Session, turn: InboundTurn) -> list[str]:
    keys = [f"session:{session.id}"]
    source_id = str(turn.ref.source_id or "").strip()
    if source_id:
        keys.append(f"source:{turn.ref.platform}:{source_id}")
    chat_id = str(turn.ref.chat_id or "").strip()
    if chat_id:
        keys.append(f"chat:{turn.ref.platform}:{chat_id}")
    return keys
