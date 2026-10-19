"""对话历史查询层：优先从 conversation_events 投影当前会话时间线。

返回分型条目（user / assistant / tool / recap / notice），让前端清楚展示每条消息的来源、
边界、工具调用和滚动摘要。恢复类 notice 标记为 toast，不进入对话消息流。
HTTP 层只跟这个 view 说话，不碰 sqlite。
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from telos.conversation.labels import (
    command_availability_label,
    command_context_policy_label,
)
from telos.conversation.repository import ConversationRepository
from telos.conversation.types import (
    BACKGROUND_NOTIFICATION_EVENT_TYPE,
    ConversationApprovalStatus,
    ConversationEventRecord,
    ConversationInputKind,
    ConversationMessageStatus,
    ConversationToolCallRecord,
    ConversationToolCallStatus,
)
from telos.core.coerce import clean_ids
from telos.memory_space.context import ContextMessageRepository
from telos.memory_space.recap import RecapRepository, recaps_covering_turn_ids

_MESSAGE_WRAPPER_RE = re.compile(r"<message\b[^>]*>\s*(.*?)\s*</message>", re.DOTALL)
_INJECTION_MARKERS = ("<system_reminder>", "<things_reminder>", "<schedulefire")
_QUEUE_EVENT_TYPES = {
    "conversation.message.queued",
    "conversation.message.edited",
    "conversation.message.append_mode_changed",
    "conversation.message.promoted",
    "conversation.message.replaced",
    "conversation.message.resume_required",
    "conversation.message.resume_requested",
    "conversation.queue.reordered",
}
_HISTORY_FILTERS = {"all", "failed", "tools", "approvals"}
_FAILED_DETAIL_MARKERS = ("failed", "error", "失败", "出错")
_FAILED_TITLE_MARKERS = ("失败", "出错")
_APPROVAL_TITLES = {"等待审批", "审批通过", "审批拒绝", "审批已取消", "审批超时", "审批记录"}
_ASSISTANT_MESSAGE_EVENT_TYPES = {
    "assistant.message.completed",
    "assistant.message.interrupted",
}


@dataclass(frozen=True)
class ChatItem:
    kind: str  # "user" | "assistant" | "thinking" | "tool" | "model_status" | "recap" | "event" | "notice"
    presentation: str = ""  # "toast" 表示不进入消息流，只在事件发生时短暂提示
    text: str = ""
    session_id: str = ""
    message_id: str = ""
    # 展示归属的轮次。重试产生的内容归并到被重试的那一轮，此处是被重试轮的 turn_id。
    turn_id: str = ""
    # 内容真实运行所在的轮次。没有重试时与 turn_id 相同；重试内容归并后保留原值，
    # 供前端按真实轮次对齐流式增量和工具卡。
    source_turn_id: str = ""
    # 该内容所属轮次在重试链上的位置：被重试的原轮为 0，第一次重试为 1，依次递增。
    retry_index: int = 0
    seq: int = 0
    # 同一轮内的次序。取自该内容对应运行事件的 run_events.seq；工具取触发它的
    # tool.call.started 事件的 seq。同一轮内所有 item 的 seq 相同，
    # (retry_index, runtime_seq) 才是轮内排序依据，也是前端判断流式增量是否已被
    # 投影覆盖的依据。
    runtime_seq: int = 0
    created_at: str = ""
    origin: str = ""  # 消息来源平台，如 "wechat"；空表示本端（web）
    # tool:
    tool_record_id: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    args: str = ""
    output: str = ""
    is_error: bool = False
    # 审批以工具卡上的状态 tag 呈现（pending/approved/denied/expired/cancelled），
    # 不再作为独立事件卡插进消息流。
    approval_status: str = ""
    # recap:
    recap_id: str = ""
    title: str = ""
    source_seq_start: int = 0
    source_seq_end: int = 0
    source_started_at: str = ""
    source_ended_at: str = ""
    agent_name: str = ""
    # 编辑重发：superseded=该轮已被后续 edit_and_redo 从模型上下文剔除（前端不再展示），
    # edited=该 user 消息本身是编辑重发产物（前端标"已编辑"）。
    superseded: bool = False
    edited: bool = False
    # user item 对应消息的当前状态（accepted/queued/running/completed/failed/...）。
    # 尚未开始执行的消息由队列面板呈现，不进消息流；前端据此字段判断，不再比对队列快照。
    # 主智能体注入的轮次输入没有消息记录，该字段为空。
    message_status: str = ""
    # 提交这条消息的客户端动作编号。发起方据此判断自己那条本地未确认的输入已经进入
    # 投影，可以撤下；它由发起方自己生成，比服务端回执里的 message_id 更可靠。
    client_action_id: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)
    child_items: list["ChatItem"] = field(default_factory=list)
    status_items: list[dict[str, Any]] = field(default_factory=list)
    tone: str = ""
    detail: str = ""
    status_key: str = ""


@dataclass(frozen=True)
class ChatHistoryPage:
    items: list[ChatItem]
    next_before_seq: int = 0
    has_more: bool = False


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and str(item.get("type") or "") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content)


def _message_status_value(message: Any) -> str:
    status = getattr(message, "status", "")
    return str(getattr(status, "value", status) or "")


def _message_client_action_id(message: Any) -> str:
    metadata = dict(getattr(message, "metadata", {}) or {})
    return str(metadata.get("client_action_id") or "")


def _visible_user_text(text: str) -> str:
    matches = _MESSAGE_WRAPPER_RE.findall(text)
    if matches:
        return html.unescape(matches[-1].strip())
    lowered = text.lstrip().lower()
    if any(marker in lowered for marker in _INJECTION_MARKERS) or lowered.startswith("<"):
        return ""
    return text


# 后台触发轮次的“用户消息”文本是内部标记；渲染成一张系统提示卡而不是裸标记或整条隐藏。
_TRIGGER_NOTICE_LABELS = {
    "thingsremindertrigger": "🔔 收到 Things 提醒",
    "schedulefiretrigger": "📅 定时任务触发",
    "backgroundrecoverytrigger": "⏰ 离线期间错过的提醒",
}
_INTERNAL_RESPONSE_INPUT_KINDS = {
    ConversationInputKind.APPROVAL_RESPONSE.value,
    ConversationInputKind.QUESTION_RESPONSE.value,
    ConversationInputKind.CONFIRMATION_RESPONSE.value,
}
_TRIGGER_MARKER_RE = re.compile(r"^<\s*([A-Za-z]+)\s*/?>$")


def _reminder_trigger_notice(text: str) -> str | None:
    match = _TRIGGER_MARKER_RE.match(str(text or "").strip())
    return _TRIGGER_NOTICE_LABELS.get(match.group(1).lower()) if match else None


def _normalize_history_filter(value: str) -> str:
    clean = str(value or "all").strip().lower()
    return clean if clean in _HISTORY_FILTERS else "all"


def _filter_history_items(items: list[ChatItem], history_filter: str) -> list[ChatItem]:
    clean_filter = _normalize_history_filter(history_filter)
    if clean_filter == "all":
        return list(items)
    return [item for item in items if _history_item_matches(item, clean_filter)]


def _history_item_matches(item: ChatItem, history_filter: str) -> bool:
    if any(_history_item_matches(child, history_filter) for child in item.child_items):
        return True
    if any(_status_item_matches(status, history_filter) for status in item.status_items):
        return True
    if history_filter == "tools":
        return item.kind == "tool"
    if history_filter == "approvals":
        return item.kind == "event" and (
            item.title in _APPROVAL_TITLES or _has_detail_kind(item, "approval")
        )
    if history_filter == "failed":
        if item.kind == "user":
            return False
        if item.is_error:
            return True
        if any(marker in item.title for marker in _FAILED_TITLE_MARKERS):
            return True
        return any(
            detail.get("kind") in {"attempt", "tool"}
            and any(
                marker in str(detail.get("title") or detail.get("text") or "").lower()
                for marker in _FAILED_DETAIL_MARKERS
            )
            for detail in item.details
        )
    return True


def _status_item_matches(status: dict[str, Any], history_filter: str) -> bool:
    if history_filter == "failed":
        if str(status.get("tone") or "") == "danger":
            return True
        title = str(status.get("title") or "").lower()
        text = str(status.get("text") or "").lower()
        return any(marker in title or marker in text for marker in _FAILED_DETAIL_MARKERS)
    return False


def _has_detail_kind(item: ChatItem, kind: str) -> bool:
    return any(str(detail.get("kind") or "") == kind for detail in item.details)


def _tool_name(tc: dict[str, Any]) -> str:
    name = tc.get("name") or (tc.get("function") or {}).get("name")
    return str(name or "tool")


def _pretty_args(tc: dict[str, Any]) -> str:
    raw = tc.get("arguments")
    if raw is None:
        raw = (tc.get("function") or {}).get("arguments")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return raw
    if raw is None:
        return ""
    return json.dumps(raw, ensure_ascii=False, indent=2)


# turn_id 列表 -> {turn_id: source_platform}。由 web 层用 RunEventRepository 构造注入，
# 使本 view 不必知道 runs.sqlite / agent_dir 的位置。
TurnOriginResolver = Callable[[list[str]], dict[str, str]]
TurnEventsResolver = Callable[[list[str]], dict[str, list[Any]]]
SessionRunsResolver = Callable[[str], list[Any]]
DescendantRunsResolver = Callable[[list[str]], list[Any]]


@dataclass(frozen=True)
class ChatHistoryView:
    memory_space_dir: Path
    turn_origin_resolver: TurnOriginResolver | None = None
    conversation_repository: ConversationRepository | None = None
    turn_events_resolver: TurnEventsResolver | None = None
    session_runs_resolver: SessionRunsResolver | None = None
    descendant_runs_resolver: DescendantRunsResolver | None = None

    def _origins_for(self, records: list) -> dict[str, str]:
        if self.turn_origin_resolver is None:
            return {}
        turn_ids = clean_ids([r.turn_id for r in records])
        if not turn_ids:
            return {}
        try:
            return self.turn_origin_resolver(turn_ids) or {}
        except Exception:  # noqa: BLE001 - 来源标注是可选装饰，失败不该拖垮历史加载
            return {}

    def items(self, session_id: str) -> list[ChatItem]:
        return self.items_for_sessions([session_id])

    def items_for_sessions(self, session_ids: list[str]) -> list[ChatItem]:
        ids = clean_ids(session_ids)
        if not ids:
            return []
        if len(ids) == 1:
            projected = self._conversation_page(
                session_id=ids[0],
                before_seq=0,
                limit=1000,
            )
            if projected is not None:
                return projected.items
        repo = ContextMessageRepository.for_memory_space(self.memory_space_dir)
        try:
            records = repo.list_messages(session_ids=ids)
            recap_repo = RecapRepository.for_memory_space(self.memory_space_dir)
            try:
                recaps = recap_repo.list(session_ids=ids)
            finally:
                recap_repo.close()
            return self._items_from_records(records, recaps, repo=repo)
        finally:
            repo.close()

    def page(
        self,
        *,
        session_ids: list[str] | None,
        before_seq: int = 0,
        limit: int = 80,
        history_filter: str = "all",
    ) -> ChatHistoryPage:
        page_size = min(200, max(1, int(limit or 80)))
        clean_filter = _normalize_history_filter(history_filter)
        cleaned_session_ids = (
            clean_ids(session_ids) if session_ids is not None else None
        )
        if cleaned_session_ids is not None and len(cleaned_session_ids) == 1:
            projected = self._conversation_page(
                session_id=cleaned_session_ids[0],
                before_seq=before_seq,
                limit=page_size,
                history_filter=clean_filter,
            )
            if projected is not None:
                return projected
        repo = ContextMessageRepository.for_memory_space(self.memory_space_dir)
        try:
            records = repo.list_messages_page(
                session_ids=cleaned_session_ids,
                before_seq=max(0, int(before_seq or 0)),
                limit=page_size + 1,
            )
            has_more = len(records) > page_size
            if has_more:
                records = records[-page_size:]
            if not records:
                return ChatHistoryPage(items=[])

            seq_start = min(record.seq for record in records)
            seq_end = max(record.seq for record in records)
            scoped_session_ids = (
                cleaned_session_ids
                if cleaned_session_ids is not None
                else sorted({record.session_id for record in records})
            )
            extra_tool_records = repo.list_tool_messages(
                _tool_call_ids(records),
                session_ids=scoped_session_ids if cleaned_session_ids is not None else None,
            )

            recap_repo = RecapRepository.for_memory_space(self.memory_space_dir)
            try:
                recaps = recap_repo.list(
                    session_ids=scoped_session_ids,
                    seq_start=seq_start,
                    seq_end=seq_end,
                )
            finally:
                recap_repo.close()

            return ChatHistoryPage(
                items=_filter_history_items(
                    self._items_from_records(
                        records,
                        recaps,
                        extra_records=extra_tool_records,
                        repo=repo,
                    ),
                    clean_filter,
                ),
                next_before_seq=seq_start,
                has_more=has_more,
            )
        finally:
            repo.close()

    def _conversation_page(
        self,
        *,
        session_id: str,
        before_seq: int,
        limit: int,
        history_filter: str = "all",
    ) -> ChatHistoryPage | None:
        repository = self.conversation_repository
        if repository is None or repository.latest_event_seq(session_id) <= 0:
            return None
        page_size = min(200, max(1, int(limit or 80)))
        clean_filter = _normalize_history_filter(history_filter)
        clean_before_seq = max(0, int(before_seq or 0))
        all_events: list[ConversationEventRecord] | None = None
        if clean_filter == "all":
            events = repository.list_events_page(
                session_id,
                before_seq=clean_before_seq,
                limit=page_size + 1,
            )
            has_more = len(events) > page_size
            if has_more:
                events = events[-page_size:]
        else:
            all_events = repository.list_events_page(session_id, limit=10000)
            events = [
                event
                for event in all_events
                if clean_before_seq <= 0 or event.seq < clean_before_seq
            ]
            has_more = False
        if not events:
            return ChatHistoryPage(items=[], has_more=False)

        if all_events is None:
            all_events = repository.list_events_page(session_id, limit=10000)
        messages = {
            message.message_id: message
            for message in repository.list_messages(
                session_id,
                limit=5000,
            )
        }

        # 用户可以重排尚未执行的消息。accepted 事件只代表发送顺序，不能作为
        # 已执行轮次在聊天流里的顺序；否则会出现「测 6 的气泡 + 测 7 的回复」
        # 这种错位。以消息真正进入 running 的事件为主，旧数据缺少该事件时再
        # 回退到该消息的首个运行事件。
        turn_id_by_message_id: dict[str, str] = {}
        running_anchor_by_message_id: dict[str, tuple[int, str]] = {}
        runtime_anchor_by_message_id: dict[str, tuple[int, str]] = {}
        for candidate in sorted(all_events, key=lambda item: int(item.seq or 0)):
            message_id = str(candidate.message_id or "").strip()
            if not message_id:
                continue
            runtime_record = (
                dict((candidate.payload or {}).get("event") or {})
                if candidate.type == "conversation.runtime.event"
                else {}
            )
            candidate_turn_id = str(
                candidate.turn_id or runtime_record.get("turn_id") or ""
            ).strip()
            if candidate_turn_id and message_id not in turn_id_by_message_id:
                turn_id_by_message_id[message_id] = candidate_turn_id
            anchor = (int(candidate.seq or 0), str(candidate.created_at or ""))
            if candidate.type == "conversation.message.running":
                running_anchor_by_message_id.setdefault(message_id, anchor)
            elif str(runtime_record.get("type") or "") in {
                "turn.started",
                "inbound.message.received",
            }:
                runtime_anchor_by_message_id.setdefault(message_id, anchor)

        def resolved_message_turn_id(
            event: ConversationEventRecord,
            message: Any | None,
        ) -> str:
            return str(
                event.turn_id
                or (message.turn_id if message is not None else "")
                or turn_id_by_message_id.get(str(event.message_id or ""), "")
                or ""
            )

        def message_execution_anchor(
            event: ConversationEventRecord,
        ) -> tuple[int, str]:
            message_id = str(event.message_id or "")
            return (
                running_anchor_by_message_id.get(message_id)
                or runtime_anchor_by_message_id.get(message_id)
                or (int(event.seq or 0), str(event.created_at or ""))
            )

        if clean_filter == "all":
            # 原始事件很多（尤其是流式文字和工具参数增量），按事件数量分页很容易
            # 从一轮对话中间切开。投影前把页首涉及的轮次向前补完整，避免最新页只
            # 有 assistant/tool、用户消息却落在上一页。
            def event_turn_id(event: ConversationEventRecord) -> str:
                message = messages.get(event.message_id)
                return resolved_message_turn_id(event, message)

            page_turn_ids = {
                turn_id for event in events if (turn_id := event_turn_id(event))
            }
            if page_turn_ids:
                page_event_ids = {event.event_id for event in events}
                events = [
                    event
                    for event in all_events
                    if (
                        event.event_id in page_event_ids
                        or event_turn_id(event) in page_turn_ids
                    )
                    and (clean_before_seq <= 0 or event.seq < clean_before_seq)
                ]
                events.sort(key=lambda event: event.seq)
                first_seq = min((event.seq for event in events), default=0)
                has_more = any(event.seq < first_seq for event in all_events)
        # 编辑重发：被 redo 剔除出模型上下文的旧轮 + 作为编辑产物的新消息。
        redo_excluded_turn_ids: set[str] = set()
        edited_message_ids: set[str] = set()
        for message in messages.values():
            metadata = dict(message.metadata or {})
            for excluded in list(metadata.get("redo_exclude_turn_ids") or []):
                clean_excluded = str(excluded or "").strip()
                if clean_excluded:
                    redo_excluded_turn_ids.add(clean_excluded)
            if str(metadata.get("retry_source") or "").strip() == "edit":
                edited_message_ids.add(message.message_id)
        invalid_recap_ids = self._recap_ids_covering_turns(
            session_id,
            redo_excluded_turn_ids,
        )
        turn_ids: list[str] = []
        for event in events:
            message = messages.get(event.message_id)
            turn_id = resolved_message_turn_id(event, message)
            if turn_id:
                turn_ids.append(turn_id)
        turn_ids = clean_ids(turn_ids)
        session_runs = self._session_runs_for(session_id)
        # Cross-session delegated descendants (e.g. an assistant) run in their
        # own session but are foreground children of a turn here. Fold their RUNS
        # into session_runs so the child-nesting logic recognises them, then pull
        # THEIR tools/approvals from THEIR own session keyed by turn. Read-time
        # join — nothing is copied into this session's log.
        descendant_runs = self._descendant_runs_for(turn_ids)
        if descendant_runs:
            session_runs = [*session_runs, *descendant_runs]
        foreground_child_turn_ids = _foreground_child_turn_ids(session_runs)
        tools_by_turn = _tools_by_turn(
            repository.list_tool_calls(
                session_id,
                limit=1000,
            )
        )
        descendant_sessions: set[str] = set()
        descendant_turn_ids: set[str] = set()
        for run in descendant_runs:
            d_session = str(getattr(run, "session_id", "") or "")
            d_turn = str(getattr(run, "turn_id", "") or "")
            if not d_session or not d_turn or d_session == session_id:
                continue
            descendant_sessions.add(d_session)
            descendant_turn_ids.add(d_turn)
            d_tools = repository.list_tool_calls(d_session, turn_id=d_turn, limit=1000)
            if d_tools:
                tools_by_turn.setdefault(d_turn, []).extend(d_tools)
        foreground_child_turn_ids.update(_foreground_child_turn_ids_from_tools(tools_by_turn))
        nested_child_turn_ids = _nested_foreground_child_turn_ids(
            session_runs=session_runs,
            tools_by_turn=tools_by_turn,
            foreground_child_turn_ids=foreground_child_turn_ids,
        )
        surface_root_child_turn_ids = foreground_child_turn_ids - nested_child_turn_ids
        child_turns_by_parent_tool = _child_turns_by_parent_tool(
            session_runs=session_runs,
            tools_by_turn=tools_by_turn,
            foreground_child_turn_ids=nested_child_turn_ids,
        )
        all_turn_ids = clean_ids(turn_ids + list(foreground_child_turn_ids))
        origins = self._origins_for_turn_ids(all_turn_ids)
        run_events_by_turn = self._turn_events_for(all_turn_ids)
        approvals_by_turn = _approvals_by_turn(
            repository.list_approvals(
                session_id,
                limit=1000,
            )
        )
        for d_session in descendant_sessions:
            for approval in repository.list_approvals(d_session, limit=1000):
                a_turn = str(getattr(approval, "turn_id", "") or "")
                if a_turn in descendant_turn_ids:
                    approvals_by_turn.setdefault(a_turn, []).append(approval)
        attempts = repository.list_turn_attempts(
            session_id=session_id,
            limit=1000,
        )
        attempts_by_message = _attempts_by_message(attempts)
        attempts_by_turn = _attempts_by_turn(attempts)
        attempts_by_id = {str(attempt.attempt_id): attempt for attempt in attempts}
        renderable_turn_ids = {
            str(message.turn_id)
            for message in messages.values()
            if str(message.turn_id or "").strip()
        }
        renderable_turn_ids.update(
            turn for turn, turn_events in run_events_by_turn.items() if turn_events
        )
        renderable_turn_ids.update(tools_by_turn.keys())

        # 排序键 (seq, rank, runtime_seq, order)：seq 定轮次先后，rank 让用户消息排在
        # 本轮内容之前，runtime_seq 定轮内内容次序，order 兜住三者全等的情况。
        entries: list[tuple[int, int, int, int, ChatItem]] = []
        order = 0
        content_added_turns: set[str] = set()
        # 轮次 -> 该轮用户消息的展示锚点。进行中的轮次靠它把已完成的内容投影出来。
        anchor_by_turn: dict[str, tuple[int, str]] = {}
        surface_root_user_turn_ids: set[str] = set()
        main_message_ids: set[str] = set()
        status_by_message_id: dict[str, list[dict[str, Any]]] = {}
        status_by_turn_id: dict[str, list[dict[str, Any]]] = {}
        visible_output_turn_ids: set[str] = set()

        def add(seq: int, rank: int, item: ChatItem) -> None:
            nonlocal order
            if item.turn_id and not item.source_turn_id:
                item = replace(item, source_turn_id=item.turn_id)
            entries.append(
                (int(seq or 0), rank, int(item.runtime_seq or 0), order, item)
            )
            order += 1

        def add_message_status(message_id: str, status: dict[str, Any]) -> None:
            clean = str(message_id or "").strip()
            if clean:
                status_by_message_id.setdefault(clean, []).append(status)

        def add_turn_status(turn_id: str, status: dict[str, Any]) -> None:
            clean = str(turn_id or "").strip()
            if clean:
                status_by_turn_id.setdefault(clean, []).append(status)

        for recap_seq, recap_rank, recap_item in self._recap_entries_for_events(
            session_id,
            events,
            has_more=has_more,
            before_seq=clean_before_seq,
        ):
            add(recap_seq, recap_rank, recap_item)

        def child_items_for_tool(
            tool: ConversationToolCallRecord,
            *,
            seen_turn_ids: set[str] | None = None,
        ) -> list[ChatItem]:
            seen = set(seen_turn_ids or set())
            child_turn_ids = list(
                child_turns_by_parent_tool.get((tool.turn_id, tool.tool_call_id), [])
            )
            if tool.child_turn_id:
                child_turn_ids.append(tool.child_turn_id)
            items: list[ChatItem] = []
            for child_turn_id in clean_ids(child_turn_ids):
                if child_turn_id not in foreground_child_turn_ids:
                    continue
                if child_turn_id in seen:
                    continue
                seen.add(child_turn_id)
                items.extend(child_items_for_turn(child_turn_id, seen_turn_ids=seen))
            return items

        def child_items_for_turn(
            child_turn_id: str,
            *,
            seen_turn_ids: set[str] | None = None,
        ) -> list[ChatItem]:
            seen = set(seen_turn_ids or set())
            items: list[ChatItem] = []
            child_tools = tools_by_turn.get(child_turn_id, [])
            child_approvals = approvals_by_turn.get(child_turn_id, [])
            child_details = (
                _tool_details(child_tools)
                + _approval_details(child_approvals)
                + _turn_run_details(session_runs, child_turn_id)
            )
            for kind, value, runtime_seq in _ordered_turn_content(
                run_events_by_turn.get(child_turn_id, []), child_tools
            ):
                if kind == "thinking":
                    thinking_item = _thinking_item_from_run_event(
                        value,
                        session_id=session_id,
                        turn_id=child_turn_id,
                        seq=0,
                        runtime_seq=runtime_seq,
                        created_at="",
                        origin=origins.get(child_turn_id, ""),
                        details=child_details,
                    )
                    if thinking_item is not None:
                        items.append(thinking_item)
                    continue
                if kind == "assistant":
                    assistant_item = _assistant_item_from_run_event(
                        value,
                        session_id=session_id,
                        turn_id=child_turn_id,
                        seq=0,
                        runtime_seq=runtime_seq,
                        created_at="",
                        origin=origins.get(child_turn_id, ""),
                        details=child_details,
                    )
                    if assistant_item is not None:
                        items.append(assistant_item)
                    continue
                if kind == "model_status":
                    status_item = _model_status_item_from_run_event(
                        value,
                        session_id=session_id,
                        turn_id=child_turn_id,
                        seq=0,
                        runtime_seq=runtime_seq,
                        created_at="",
                        origin=origins.get(child_turn_id, ""),
                    )
                    if status_item is not None:
                        items.append(status_item)
                    continue
                child_tool = value
                child_tool_approvals = [
                    approval
                    for approval in child_approvals
                    if getattr(approval, "tool_call_id", "")
                    == child_tool.tool_call_id
                ]
                items.append(
                    _tool_item_from_record(
                        child_tool,
                        seq=0,
                        runtime_seq=runtime_seq,
                        created_at="",
                        origin=origins.get(child_turn_id, ""),
                        details=_tool_details([child_tool])
                        + _approval_details(child_tool_approvals),
                        child_items=child_items_for_tool(
                            child_tool,
                            seen_turn_ids=seen,
                        ),
                        approvals=child_tool_approvals,
                    )
                )
            return items

        def add_turn_content(
            turn_id: str,
            *,
            seq: int,
            created_at: str,
            details: list[dict[str, Any]],
        ) -> None:
            if not turn_id or turn_id in content_added_turns:
                return
            content_added_turns.add(turn_id)
            for kind, value, runtime_seq in _ordered_turn_content(
                run_events_by_turn.get(turn_id, []),
                tools_by_turn.get(turn_id, []),
            ):
                if kind == "thinking":
                    thinking_item = _thinking_item_from_run_event(
                        value,
                        session_id=session_id,
                        turn_id=turn_id,
                        seq=seq,
                        runtime_seq=runtime_seq,
                        created_at=created_at,
                        origin=origins.get(turn_id, ""),
                        details=details,
                    )
                    if thinking_item is not None:
                        add(seq, 1, thinking_item)
                    continue
                if kind == "assistant":
                    assistant_item = _assistant_item_from_run_event(
                        value,
                        session_id=session_id,
                        turn_id=turn_id,
                        seq=seq,
                        runtime_seq=runtime_seq,
                        created_at=created_at,
                        origin=origins.get(turn_id, ""),
                        details=details,
                    )
                    if assistant_item is not None:
                        add(seq, 1, assistant_item)
                    continue
                if kind == "model_status":
                    status_item = _model_status_item_from_run_event(
                        value,
                        session_id=session_id,
                        turn_id=turn_id,
                        seq=seq,
                        runtime_seq=runtime_seq,
                        created_at=created_at,
                        origin=origins.get(turn_id, ""),
                    )
                    if status_item is not None:
                        add(seq, 1, status_item)
                    continue
                tool = value
                tool_approvals = [
                    approval
                    for approval in approvals_by_turn.get(turn_id, [])
                    if getattr(approval, "tool_call_id", "") == tool.tool_call_id
                ]
                add(
                    seq,
                    1,
                    _tool_item_from_record(
                        tool,
                        seq=seq,
                        runtime_seq=runtime_seq,
                        created_at=created_at,
                        origin=origins.get(turn_id, ""),
                        details=_tool_details([tool])
                        + _approval_details(tool_approvals),
                        child_items=child_items_for_tool(tool),
                        approvals=tool_approvals,
                    ),
                )

        for event in events:
            message = messages.get(event.message_id)
            turn_id = resolved_message_turn_id(event, message)
            if turn_id and event.type.startswith("conversation.tool_call."):
                visible_output_turn_ids.add(turn_id)
            if turn_id and turn_id in nested_child_turn_ids:
                continue
            if (
                turn_id in surface_root_child_turn_ids
                and event.type == "conversation.runtime.event"
            ):
                runtime_record = dict((event.payload or {}).get("event") or {})
                runtime_type = str(
                    runtime_record.get("type")
                    or (event.payload or {}).get("runtime_type")
                    or ""
                )
                if (
                    runtime_type == "inbound.message.received"
                    and turn_id not in surface_root_user_turn_ids
                    and str(runtime_record.get("input_kind") or "")
                    not in _INTERNAL_RESPONSE_INPUT_KINDS
                ):
                    text = _visible_user_text(
                        str(runtime_record.get("text_preview") or "")
                    ).strip()
                    if text:
                        message_from = str(runtime_record.get("message_from") or "")
                        origin = (
                            "telos"
                            if message_from == "main_agent"
                            else str(runtime_record.get("origin") or origins.get(turn_id, ""))
                        )
                        add(
                            event.seq,
                            0,
                            ChatItem(
                                "user",
                                text=text,
                                session_id=session_id,
                                turn_id=turn_id,
                                seq=event.seq,
                                created_at=event.created_at,
                                origin=origin,
                                details=_event_details(event)
                                + _turn_run_details(session_runs, turn_id),
                            ),
                        )
                        surface_root_user_turn_ids.add(turn_id)
                if runtime_type == "provider.retry":
                    add_turn_status(
                        turn_id,
                        _provider_retry_status(
                            event,
                            runtime_record,
                            turn_id=turn_id,
                            placement=(
                                "assistant"
                                if turn_id in visible_output_turn_ids
                                else "user"
                            ),
                        ),
                    )
                if runtime_type in _ASSISTANT_MESSAGE_EVENT_TYPES:
                    visible_output_turn_ids.add(turn_id)
                if runtime_type in {"turn.completed", "turn.cancelled", "turn.failed"}:
                    add_turn_content(
                        turn_id,
                        seq=event.seq,
                        created_at=event.created_at,
                        details=_event_details(event)
                        + _tool_details(tools_by_turn.get(turn_id, []))
                        + _approval_details(approvals_by_turn.get(turn_id, []))
                        + _turn_run_details(session_runs, turn_id),
                    )
                continue
            if event.type == "conversation.runtime.event":
                runtime_record = dict((event.payload or {}).get("event") or {})
                runtime_type = str(
                    runtime_record.get("type")
                    or (event.payload or {}).get("runtime_type")
                    or ""
                )
                if runtime_type == "provider.retry" and turn_id:
                    add_turn_status(
                        turn_id,
                        _provider_retry_status(
                            event,
                            runtime_record,
                            turn_id=turn_id,
                            placement=(
                                "assistant"
                                if turn_id in visible_output_turn_ids
                                else "user"
                            ),
                        ),
                    )
                    continue
                if runtime_type in _ASSISTANT_MESSAGE_EVENT_TYPES and turn_id:
                    visible_output_turn_ids.add(turn_id)
            if (
                event.type == "conversation.message.appended_to_current_turn"
                and message is not None
            ):
                text = _visible_user_text(message.text).strip()
                if text:
                    status = _inline_status(
                        title="你补充",
                        text=text,
                        kind="appended_input",
                        tone="info",
                        event=event,
                        details=_message_details(message)
                        + _event_details(event)
                        + _attempt_details(
                            attempts_by_message.get(message.message_id, [])
                        ),
                        message_id=message.message_id,
                    )
                    target_turn_id = _append_target_turn_id(
                        event,
                        message=message,
                        messages=messages,
                        attempts_by_id=attempts_by_id,
                        attempts_by_message=attempts_by_message,
                    )
                    if target_turn_id:
                        add_turn_status(target_turn_id, status)
                    else:
                        add_message_status(message.message_id, status)
                continue
            if event.type == "conversation.message.accepted" and message is not None:
                display_seq, display_created_at = message_execution_anchor(event)
                if turn_id:
                    anchor_by_turn.setdefault(turn_id, (display_seq, display_created_at))
                if (
                    message.status == ConversationMessageStatus.CANCELLED
                    and not str(message.turn_id or "").strip()
                ):
                    continue
                notice = _reminder_trigger_notice(message.text)
                if notice:
                    add(
                        display_seq,
                        0,
                        ChatItem(
                            "notice",
                            text=notice,
                            session_id=session_id,
                            message_id=message.message_id,
                            turn_id=turn_id,
                            seq=display_seq,
                            created_at=display_created_at,
                            origin=message.platform,
                        ),
                    )
                    main_message_ids.add(message.message_id)
                    continue
                skill_notice = _skill_activation_notice(message)
                if skill_notice:
                    add(
                        display_seq,
                        0,
                        ChatItem(
                            "notice",
                            text=skill_notice,
                            session_id=session_id,
                            message_id=message.message_id,
                            turn_id=turn_id,
                            seq=display_seq,
                            created_at=display_created_at,
                            origin=message.platform,
                            details=_message_details(message),
                        ),
                    )
                    main_message_ids.add(message.message_id)
                    continue
                text = _visible_user_text(message.text).strip()
                if text:
                    add(
                        display_seq,
                        0,
                        ChatItem(
                            "user",
                            text=text,
                            session_id=session_id,
                            message_id=message.message_id,
                            turn_id=turn_id,
                            seq=display_seq,
                            created_at=display_created_at,
                            origin=message.platform,
                            message_status=_message_status_value(message),
                            client_action_id=_message_client_action_id(message),
                            details=_message_details(message)
                            + _attempt_details(
                                attempts_by_message.get(message.message_id, [])
                            ),
                        ),
                    )
                    main_message_ids.add(message.message_id)
                continue
            if event.type in {
                "conversation.message.completed",
                "conversation.message.failed",
                "conversation.message.cancelled",
            }:
                if (
                    event.type == "conversation.message.cancelled"
                    and message is not None
                    and message.status == ConversationMessageStatus.CANCELLED
                    and not str(message.turn_id or event.turn_id or "").strip()
                ):
                    continue
                if (
                    event.type != "conversation.message.completed"
                    and message is not None
                    and message.message_id not in main_message_ids
                ):
                    text = _visible_user_text(message.text).strip()
                    if text:
                        add(
                            event.seq,
                            0,
                            ChatItem(
                                "user",
                                text=text,
                                session_id=session_id,
                                message_id=message.message_id,
                                turn_id=message.turn_id or event.turn_id,
                                seq=event.seq,
                                created_at=event.created_at,
                                origin=message.platform,
                                message_status=_message_status_value(message),
                                client_action_id=_message_client_action_id(message),
                                details=_message_details(message)
                                + _attempt_details(
                                    attempts_by_message.get(message.message_id, [])
                                ),
                            ),
                        )
                        main_message_ids.add(message.message_id)
                if turn_id:
                    turn_details = (
                        _event_details(event)
                        + _attempt_details(attempts_by_turn.get(turn_id, []))
                        + _tool_details(tools_by_turn.get(turn_id, []))
                        + _approval_details(approvals_by_turn.get(turn_id, []))
                    )
                    add_turn_content(
                        turn_id,
                        seq=event.seq,
                        created_at=event.created_at,
                        details=turn_details,
                    )
                if event.type != "conversation.message.completed":
                    status = _status_item_from_message_event(
                        event,
                        turn_id=turn_id,
                        message=message,
                        attempts=attempts_by_turn.get(turn_id, []),
                        tools=tools_by_turn.get(turn_id, []),
                        approvals=approvals_by_turn.get(turn_id, []),
                    )
                    if turn_id:
                        add_turn_status(turn_id, status)
                    elif message is not None:
                        add_message_status(message.message_id, status)
                continue
            if event.type.startswith("conversation.command.") or event.type.startswith(
                "conversation.stop."
            ):
                item = _command_or_stop_item(event, session_id=session_id)
                if event.type.startswith("conversation.command."):
                    command_item = _command_result_history_item(
                        event,
                        item=item,
                        session_id=session_id,
                    )
                    if command_item is not None:
                        add(event.seq, 3, command_item)
                    continue
                if str((event.payload or {}).get("cancel_target") or "") in {
                    "message",
                    "input_request",
                }:
                    continue
                status = _inline_status_from_item(
                    item,
                    kind="stop",
                    tone="danger" if event.type == "conversation.stop.noop" else "muted",
                )
                if event.type.startswith("conversation.stop."):
                    target_turn_id = _stop_target_turn_id(event)
                    if target_turn_id and target_turn_id in renderable_turn_ids:
                        add_turn_status(target_turn_id, status)
                    else:
                        add(
                            event.seq,
                            3,
                            ChatItem(
                                "notice",
                                text=item.text,
                                session_id=session_id,
                                turn_id=event.turn_id,
                                seq=event.seq,
                                created_at=event.created_at,
                                details=item.details,
                            ),
                        )
                continue
            if event.type == BACKGROUND_NOTIFICATION_EVENT_TYPE:
                text = str((event.payload or {}).get("text") or "").strip()
                if text:
                    add(
                        event.seq,
                        1,
                        ChatItem(
                            "assistant",
                            text=text,
                            session_id=session_id,
                            turn_id=event.turn_id,
                            seq=event.seq,
                            created_at=event.created_at,
                            origin="telos",
                            details=_event_details(event),
                        ),
                    )
                continue
            if event.type == "conversation.input.classified":
                item = _input_classification_item(event, session_id=session_id)
                if item is not None:
                    if item.title not in {
                        "未知斜杠按消息发送",
                        "命令已分类",
                        "技能激活",
                        "智能体控制",
                    }:
                        add(event.seq, 3, item)
                continue
            if event.type in _QUEUE_EVENT_TYPES and _queue_event_visible(event):
                item = _queue_event_item(
                    event,
                    session_id=session_id,
                    message=message,
                )
                if event.type in {
                    "conversation.message.resume_required",
                    "conversation.message.resume_requested",
                }:
                    add(
                        event.seq,
                        3,
                        ChatItem(
                            "notice",
                            presentation="toast",
                            text=item.text,
                            session_id=session_id,
                            message_id=event.message_id,
                            turn_id=event.turn_id,
                            seq=event.seq,
                            created_at=event.created_at,
                            details=item.details,
                        ),
                    )
                else:
                    add_message_status(
                        event.message_id,
                        _inline_status_from_item(item, kind="queue", tone="muted"),
                    )
                continue
            if event.type.startswith("conversation.approval."):
                add(
                    event.seq,
                    3,
                    _approval_item(
                        event,
                        session_id=session_id,
                        approvals=approvals_by_turn.get(turn_id, []),
                    ),
                )
                continue
            if event.type.startswith("conversation.input_request."):
                item = _input_request_item(event, session_id=session_id)
                if item is not None:
                    add(event.seq, 3, item)
                continue
            if event.type == "conversation.run.recovered":
                item = _run_recovery_item(event, session_id=session_id)
                add(
                    event.seq,
                    3,
                    ChatItem(
                        "notice",
                        presentation="toast",
                        text=item.text,
                        session_id=session_id,
                        turn_id=item.turn_id,
                        seq=item.seq,
                        created_at=item.created_at,
                        details=item.details,
                    ),
                )
                continue
            if event.type == "conversation.recovery.completed":
                add(
                    event.seq,
                    3,
                    ChatItem(
                        "notice",
                        presentation="toast",
                        text=_recovery_text(event.payload),
                        session_id=session_id,
                        seq=event.seq,
                        created_at=event.created_at,
                        details=_event_details(event),
                    ),
                )

        # 还没终止的轮次也要投影它已经完成的内容（已结束的工具调用、已固化的文本）。
        # 只在轮次终止时投影会让进行中的工具卡直到这一轮结束才出现，界面上就是回复
        # 先到、工具卡后到。
        for turn_id in clean_ids(list(renderable_turn_ids)):
            if turn_id in content_added_turns or turn_id in nested_child_turn_ids:
                continue
            anchor = anchor_by_turn.get(turn_id)
            if anchor is None:
                continue
            add_turn_content(
                turn_id,
                seq=anchor[0],
                created_at=anchor[1],
                details=_tool_details(tools_by_turn.get(turn_id, []))
                + _approval_details(approvals_by_turn.get(turn_id, []))
                + _turn_run_details(session_runs, turn_id),
            )

        items = [item for _seq, _rank, _runtime_seq, _order, item in sorted(entries)]
        if status_by_message_id or status_by_turn_id:
            items = _attach_status_items(
                items,
                status_by_message_id=status_by_message_id,
                status_by_turn_id=status_by_turn_id,
            )
        items = _merge_retry_turns(items, _retry_chain(messages))
        if redo_excluded_turn_ids or edited_message_ids:
            items = [
                _mark_redo_state(item, redo_excluded_turn_ids, edited_message_ids)
                for item in items
            ]
        if invalid_recap_ids:
            items = [
                item
                for item in items
                if item.kind != "recap" or item.recap_id not in invalid_recap_ids
            ]
        if clean_filter == "all":
            # 审批状态是运行遥测：实时有输入框上方的审批条，事后有「审批」筛选；
            # 默认流里插卡会把一轮回复拆成两张卡，故剔除。
            items = [item for item in items if not _is_approval_status_item(item)]
        if clean_filter != "all":
            items = _filter_history_items(items, clean_filter)
            has_more = len(items) > page_size
            if has_more:
                items = items[-page_size:]
        next_before_seq = (
            min((item.seq for item in items), default=0)
            if clean_filter != "all"
            else min(event.seq for event in events)
        )
        return ChatHistoryPage(
            items=items,
            next_before_seq=next_before_seq,
            has_more=has_more,
        )

    def _items_from_records(
        self,
        records: list,
        recaps: list,
        *,
        extra_records: list | None = None,
        repo: ContextMessageRepository | None = None,
    ) -> list[ChatItem]:
        """Legacy compatibility projection from ``context.sqlite`` records.

        The primary history path is :meth:`_conversation_page` (projected from
        ``conversation_events``). This record-based projection is the intentional
        fallback for sessions that have **no** conversation events — pre-
        conversation-system (legacy) sessions and context-only sessions — plus
        multi-session queries. Callers reach it only when
        ``_conversation_page`` returns ``None``.
        """
        output_records = list(records) + list(extra_records or [])
        outputs: dict[str, str] = {}
        for record in output_records:
            if record.role == "tool" and record.tool_call_id:
                outputs[record.tool_call_id] = _flatten_content(record.content)
        origins = self._origins_for(records)
        record_seq_start = min((int(record.seq or 0) for record in records), default=0)
        record_seq_end = max((int(record.seq or 0) for record in records), default=0)

        # 与 _conversation_page 同一套排序键。context 消息仓库没有运行事件，
        # runtime_seq 保持 0，轮内顺序由 order 决定。
        entries: list[tuple[int, int, int, int, ChatItem]] = []
        order = 0

        def add(seq: int, rank: int, item: ChatItem) -> None:
            nonlocal order
            entries.append(
                (int(seq or 0), rank, int(item.runtime_seq or 0), order, item)
            )
            order += 1

        for record in records:
            if record.role == "user":
                raw = _flatten_content(record.content)
                notice = _reminder_trigger_notice(raw)
                if notice:
                    add(
                        record.seq,
                        0,
                        ChatItem(
                            "notice",
                            text=notice,
                            session_id=record.session_id,
                            turn_id=record.turn_id,
                            seq=record.seq,
                            created_at=record.created_at,
                            origin=origins.get(record.turn_id, ""),
                        ),
                    )
                    continue
                text = _visible_user_text(raw).strip()
                if text:
                    add(
                        record.seq,
                        0,
                        ChatItem(
                            "user",
                            text=text,
                            session_id=record.session_id,
                            turn_id=record.turn_id,
                            seq=record.seq,
                            created_at=record.created_at,
                            origin=origins.get(record.turn_id, ""),
                        ),
                    )
            elif record.role == "assistant":
                text = _flatten_content(record.content).strip()
                if text:
                    add(
                        record.seq,
                        0,
                        ChatItem(
                            "assistant",
                            text=text,
                            session_id=record.session_id,
                            turn_id=record.turn_id,
                            seq=record.seq,
                            created_at=record.created_at,
                            origin=origins.get(record.turn_id, ""),
                        ),
                    )
                for tc in record.tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    tcid = str(tc.get("id") or "")
                    add(
                        record.seq,
                        0,
                        ChatItem(
                            "tool",
                            session_id=record.session_id,
                            turn_id=record.turn_id,
                            seq=record.seq,
                            created_at=record.created_at,
                            tool_name=_tool_name(tc),
                            args=_pretty_args(tc),
                            output=outputs.get(tcid, ""),
                            origin=origins.get(record.turn_id, ""),
                        )
                    )
        for recap in recaps:
            source_started_at, source_ended_at = _source_message_time_range(
                records,
                source_seq_start=recap.source_seq_start,
                source_seq_end=recap.source_seq_end,
            )
            range_is_partial = (
                not record_seq_start
                or record_seq_start > int(recap.source_seq_start or 0)
                or record_seq_end < int(recap.source_seq_end or 0)
            )
            if repo is not None and (
                range_is_partial or not source_started_at or not source_ended_at
            ):
                source_started_at, source_ended_at = repo.message_time_range(
                    session_id=recap.session_id,
                    seq_start=recap.source_seq_start,
                    seq_end=recap.source_seq_end,
                )
            add(
                recap.source_seq_end,
                1,
                ChatItem(
                    "recap",
                    text=recap.content,
                    session_id=recap.session_id,
                    seq=recap.source_seq_end,
                    created_at=recap.created_at,
                    recap_id=recap.recap_id,
                    title=recap.title,
                    source_seq_start=recap.source_seq_start,
                    source_seq_end=recap.source_seq_end,
                    source_started_at=source_started_at,
                    source_ended_at=source_ended_at,
                    agent_name=recap.agent_name,
                ),
            )
        return [
            item for _seq, _rank, _runtime_seq, _order, item in sorted(entries)
        ]

    def _recap_entries_for_events(
        self,
        session_id: str,
        events: list[ConversationEventRecord],
        *,
        has_more: bool,
        before_seq: int,
    ) -> list[tuple[int, int, ChatItem]]:
        """把 recap 注入事件投影页（事件投影此前不含 recap，聊天页 recap 点位因此消失）。

        recap 的 source_seq_* 属于 context.sqlite 序号空间，与 conversation_events
        的 seq 不同域，无法直接对位；按创建时间对齐：落在「创建时间之前最近一条事件」
        之后（rank 4）。首页（无更早分页）把更早的 recap 排在页首（rank -1）。
        """
        if not events:
            return []
        try:
            recap_repo = RecapRepository.for_memory_space(self.memory_space_dir)
        except Exception:  # noqa: BLE001 - recap 缺失不影响历史主线
            return []
        try:
            recaps = recap_repo.list(session_ids=[session_id])
        except Exception:  # noqa: BLE001
            return []
        finally:
            recap_repo.close()
        if not recaps:
            return []

        def normalize_ts(value: str) -> str:
            return str(value or "").replace(" ", "T")

        ordered_events = sorted(events, key=lambda event: int(event.seq or 0))
        oldest = normalize_ts(ordered_events[0].created_at)
        newest = normalize_ts(ordered_events[-1].created_at)
        entries: list[tuple[int, int, ChatItem]] = []
        context_repo = None
        try:
            for recap in recaps:
                created = normalize_ts(getattr(recap, "created_at", ""))
                if has_more and created < oldest:
                    continue  # 属于更早的分页
                if before_seq > 0 and created > newest:
                    continue  # 属于更新的分页
                seq = int(ordered_events[0].seq or 0)
                rank = -1
                for event in ordered_events:
                    if normalize_ts(event.created_at) <= created:
                        seq = int(event.seq or 0)
                        rank = 4
                    else:
                        break
                if context_repo is None:
                    context_repo = ContextMessageRepository.for_memory_space(
                        self.memory_space_dir
                    )
                source_started_at, source_ended_at = context_repo.message_time_range(
                    session_id=recap.session_id,
                    seq_start=recap.source_seq_start,
                    seq_end=recap.source_seq_end,
                )
                entries.append(
                    (
                        seq,
                        rank,
                        ChatItem(
                            "recap",
                            text=recap.content,
                            session_id=recap.session_id,
                            seq=seq,
                            created_at=recap.created_at,
                            recap_id=recap.recap_id,
                            title=recap.title,
                            source_seq_start=recap.source_seq_start,
                            source_seq_end=recap.source_seq_end,
                            source_started_at=source_started_at,
                            source_ended_at=source_ended_at,
                            agent_name=recap.agent_name,
                        ),
                    )
                )
        finally:
            if context_repo is not None:
                context_repo.close()
        return entries

    def _recap_ids_covering_turns(
        self,
        session_id: str,
        turn_ids: set[str],
    ) -> set[str]:
        if not turn_ids:
            return set()
        try:
            context_repo = ContextMessageRepository.for_memory_space(
                self.memory_space_dir
            )
            recap_repo = RecapRepository.for_memory_space(self.memory_space_dir)
            try:
                messages = context_repo.list_messages(session_id=session_id)
                recaps = recap_repo.list(session_id=session_id)
            finally:
                context_repo.close()
                recap_repo.close()
        except Exception:  # noqa: BLE001 - 失效判断失败不影响对话主线
            return set()
        return {
            recap.recap_id
            for recap in recaps_covering_turn_ids(recaps, messages, turn_ids)
        }

    def _origins_for_turn_ids(self, turn_ids: list[str]) -> dict[str, str]:
        if self.turn_origin_resolver is None:
            return {}
        try:
            return self.turn_origin_resolver(clean_ids(turn_ids)) or {}
        except Exception:  # noqa: BLE001 - 来源标注失败不影响历史
            return {}

    def _turn_events_for(self, turn_ids: list[str]) -> dict[str, list[Any]]:
        if self.turn_events_resolver is None:
            return {}
        try:
            return self.turn_events_resolver(clean_ids(turn_ids)) or {}
        except Exception:  # noqa: BLE001 - 运行事件只用于补全助手文本
            return {}

    def _session_runs_for(self, session_id: str) -> list[Any]:
        if self.session_runs_resolver is None:
            return []
        try:
            return list(self.session_runs_resolver(str(session_id)) or [])
        except Exception:  # noqa: BLE001 - 子智能体展示是增强信息，失败不影响历史主线
            return []

    def _descendant_runs_for(self, root_turn_ids: list[str]) -> list[Any]:
        """Cross-session foreground descendant runs of the surface's turns (e.g. a
        delegated assistant turn living in its own session), so their tools/text can be
        nested under the parent tool card. Read-time join — nothing is copied."""
        if self.descendant_runs_resolver is None:
            return []
        ids = clean_ids(root_turn_ids)
        if not ids:
            return []
        try:
            return list(self.descendant_runs_resolver(ids) or [])
        except Exception:  # noqa: BLE001 - 增强信息，失败不影响历史主线
            return []

def _source_message_time_range(
    records: list,
    *,
    source_seq_start: int,
    source_seq_end: int,
) -> tuple[str, str]:
    in_range = [
        record
        for record in records
        if int(record.seq or 0) >= int(source_seq_start or 0)
        and int(record.seq or 0) <= int(source_seq_end or 0)
        and str(record.created_at or "").strip()
    ]
    if not in_range:
        return "", ""
    return str(in_range[0].created_at), str(in_range[-1].created_at)


def _tool_call_ids(records: list) -> list[str]:
    ids: list[str] = []
    for record in records:
        for tool_call in list(record.tool_calls or []):
            if isinstance(tool_call, dict):
                tool_call_id = str(tool_call.get("id") or "").strip()
                if tool_call_id:
                    ids.append(tool_call_id)
    return clean_ids(ids)


def _tools_by_turn(
    tools: list[ConversationToolCallRecord],
) -> dict[str, list[ConversationToolCallRecord]]:
    grouped: dict[str, list[ConversationToolCallRecord]] = {}
    for tool in tools:
        grouped.setdefault(tool.turn_id, []).append(tool)
    return grouped


def _attempts_by_message(attempts: list) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for attempt in attempts:
        grouped.setdefault(str(attempt.message_id), []).append(attempt)
    return grouped


def _attempts_by_turn(attempts: list) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for attempt in attempts:
        turn_id = str(attempt.turn_id or "")
        if turn_id:
            grouped.setdefault(turn_id, []).append(attempt)
    return grouped


def _approvals_by_turn(
    approvals: list,
) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for approval in approvals:
        grouped.setdefault(str(approval.turn_id), []).append(approval)
    return grouped


def _attach_status_items(
    items: list[ChatItem],
    *,
    status_by_message_id: dict[str, list[dict[str, Any]]],
    status_by_turn_id: dict[str, list[dict[str, Any]]],
) -> list[ChatItem]:
    if not items:
        return []
    assistant_turn_last_index: dict[str, int] = {}
    ordered_model_status_turn_ids = {
        item.turn_id for item in items if item.kind == "model_status" and item.turn_id
    }
    for index, item in enumerate(items):
        if item.kind in {"assistant", "thinking"} and item.turn_id:
            assistant_turn_last_index[item.turn_id] = index

    output: list[ChatItem] = []
    for index, item in enumerate(items):
        statuses = list(item.status_items or [])
        if item.kind == "user" and item.message_id:
            statuses.extend(status_by_message_id.get(item.message_id, []))
        turn_statuses = list(status_by_turn_id.get(item.turn_id, [])) if item.turn_id else []
        if item.kind == "user" and item.turn_id:
            statuses.extend(
                status
                for status in turn_statuses
                if str(status.get("placement") or "") == "user"
            )
        if item.kind in {"assistant", "thinking"} and item.turn_id:
            if assistant_turn_last_index.get(item.turn_id) == index:
                statuses.extend(
                    status
                    for status in turn_statuses
                    if str(status.get("placement") or "") != "user"
                    and not (
                        item.turn_id in ordered_model_status_turn_ids
                        and str(status.get("kind") or "")
                        in {"provider_retry", "message"}
                    )
                )
        elif (
            item.kind == "user"
            and item.turn_id
            and item.turn_id not in assistant_turn_last_index
        ):
            statuses.extend(
                status
                for status in turn_statuses
                if str(status.get("placement") or "") not in {"user", "assistant"}
            )
        if statuses:
            item = replace(item, status_items=_dedupe_status_items(statuses))
        output.append(item)
    return output


def _dedupe_status_items(statuses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    output: list[dict[str, Any]] = []
    for status in statuses:
        key = (
            str(status.get("kind") or ""),
            str(status.get("title") or ""),
            str(status.get("text") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(status)
    return output


def _inline_status(
    *,
    title: str,
    text: str,
    kind: str,
    tone: str = "muted",
    event: ConversationEventRecord | None = None,
    details: list[dict[str, Any]] | None = None,
    message_id: str = "",
    turn_id: str = "",
    detail: str = "",
) -> dict[str, Any]:
    status: dict[str, Any] = {
        "kind": str(kind or "status"),
        "title": str(title or "状态"),
        "text": str(text or ""),
        "tone": str(tone or "muted"),
        "message_id": str(message_id or ""),
        "turn_id": str(turn_id or ""),
    }
    if event is not None:
        status.update(
            {
                "seq": int(event.seq or 0),
                "created_at": event.created_at,
                "event_type": event.type,
                "message_id": str(message_id or event.message_id or ""),
                "turn_id": str(turn_id or event.turn_id or ""),
            }
        )
    if details:
        status["details"] = list(details)
    if detail:
        status["detail"] = str(detail)
    return status


def _provider_retry_status(
    event: ConversationEventRecord,
    runtime_record: dict[str, Any],
    *,
    turn_id: str,
    placement: str,
) -> dict[str, Any]:
    attempt = int(runtime_record.get("attempt") or 0)
    next_attempt = int(runtime_record.get("next_attempt") or max(2, attempt + 1))
    max_attempts = int(runtime_record.get("max_attempts") or 0)
    progress = f"{next_attempt}/{max_attempts}" if max_attempts > 0 else str(next_attempt)
    status = _inline_status(
        title="模型请求重试",
        text=f"第 {attempt or max(1, next_attempt - 1)} 次请求失败，正在进行第 {progress} 次尝试",
        kind="provider_retry",
        tone="muted",
        event=event,
        turn_id=turn_id,
        detail=str(runtime_record.get("error") or ""),
        details=_event_details(event),
    )
    status["placement"] = str(placement or "user")
    return status


def _inline_status_from_item(
    item: ChatItem,
    *,
    kind: str,
    tone: str = "muted",
) -> dict[str, Any]:
    return {
        "kind": str(kind or "status"),
        "title": item.title or "状态",
        "text": item.text or "",
        "tone": tone,
        "message_id": item.message_id,
        "turn_id": item.turn_id,
        "seq": item.seq,
        "created_at": item.created_at,
        "details": list(item.details or []),
    }


def _append_target_turn_id(
    event: ConversationEventRecord,
    *,
    message: Any | None,
    messages: dict[str, Any],
    attempts_by_id: dict[str, Any],
    attempts_by_message: dict[str, list],
) -> str:
    payload = dict(event.payload or {})
    target_attempt_id = str(payload.get("target_attempt_id") or "").strip()
    attempt = attempts_by_id.get(target_attempt_id)
    if attempt is not None:
        turn_id = str(getattr(attempt, "turn_id", "") or "").strip()
        if turn_id:
            return turn_id
    target_message_id = str(payload.get("target_message_id") or "").strip()
    target_message = messages.get(target_message_id)
    if target_message is not None:
        turn_id = str(getattr(target_message, "turn_id", "") or "").strip()
        if turn_id:
            return turn_id
        for target_attempt in attempts_by_message.get(target_message_id, []):
            turn_id = str(getattr(target_attempt, "turn_id", "") or "").strip()
            if turn_id:
                return turn_id
    if message is not None:
        return str(getattr(message, "turn_id", "") or "").strip()
    return str(event.turn_id or "").strip()


def _stop_target_turn_id(event: ConversationEventRecord) -> str:
    payload = dict(event.payload or {})
    requested = str(payload.get("requested_turn_id") or "").strip()
    if requested and not requested.startswith("session:"):
        return requested
    event_turn = str(event.turn_id or "").strip()
    if event_turn and not event_turn.startswith("session:"):
        return event_turn
    return ""


def _command_text_from_payload(payload: dict[str, Any] | None) -> str:
    data = dict(payload or {})
    command = str(data.get("command_token") or data.get("command_name") or "").strip()
    args = str(data.get("command_args") or "").strip()
    return " ".join(part for part in [command, args] if part).strip() or "命令"


def _command_result_history_item(
    event: ConversationEventRecord,
    *,
    item: ChatItem,
    session_id: str,
) -> ChatItem | None:
    payload = dict(event.payload or {})
    name = str(payload.get("command_name") or "").strip().lower()
    token = _command_text_from_payload(payload)
    if event.type == "conversation.command.completed":
        if _command_result_display_mode(name) == "panel":
            return None
        if not item.text:
            return None
        return ChatItem(
            "notice",
            text=_command_notice_text(token, item.text),
            session_id=session_id,
            turn_id=event.turn_id,
            seq=event.seq,
            created_at=event.created_at,
            details=item.details,
        )
    if event.type in {
        "conversation.command.failed",
        "conversation.command.cancelled",
    }:
        return ChatItem(
            "notice",
            text=_command_notice_text(token, item.text or item.title),
            session_id=session_id,
            turn_id=event.turn_id,
            seq=event.seq,
            created_at=event.created_at,
            details=item.details,
        )
    return None


def _command_result_display_mode(command_name: str) -> str:
    if str(command_name or "").strip().lower() in {
        "help",
        "info",
        "list",
        "web_search_sources",
    }:
        return "panel"
    return "notice"


def _command_notice_text(command: str, response: str) -> str:
    clean_command = str(command or "").strip()
    text = str(response or "").strip()
    if clean_command and text:
        return f"{clean_command}：{text}"
    return text or clean_command


def _skill_activation_notice(message: Any) -> str:
    kind = str(getattr(message, "kind", "") or "").strip()
    if kind != ConversationInputKind.SKILL_ACTIVATION.value:
        return ""
    metadata = dict(getattr(message, "metadata", {}) or {})
    skill = str(metadata.get("skill_name") or "").strip()
    text = str(getattr(message, "text", "") or "").strip()
    return f"技能 {skill or text or 'skill'} 已激活"


def _foreground_child_turn_ids(session_runs: list[Any]) -> set[str]:
    ids: set[str] = set()
    for run in session_runs:
        turn_id = str(getattr(run, "turn_id", "") or "")
        parent_turn_id = str(getattr(run, "parent_turn_id", "") or "")
        parent_tool_call_id = str(getattr(run, "parent_tool_call_id", "") or "")
        if turn_id and parent_turn_id and parent_tool_call_id and not _run_is_background(run):
            ids.add(turn_id)
    return ids


def _foreground_child_turn_ids_from_tools(
    tools_by_turn: dict[str, list[ConversationToolCallRecord]],
) -> set[str]:
    ids: set[str] = set()
    for tools in tools_by_turn.values():
        for tool in tools:
            child_turn_id = str(tool.child_turn_id or "")
            child_scope = str((tool.metadata or {}).get("child_scope") or "").lower()
            if child_turn_id and child_scope not in {"background", "background_subagent"}:
                ids.add(child_turn_id)
    return ids


def _nested_foreground_child_turn_ids(
    *,
    session_runs: list[Any],
    tools_by_turn: dict[str, list[ConversationToolCallRecord]],
    foreground_child_turn_ids: set[str],
) -> set[str]:
    """找出父轮次也属于当前会话的子轮次。

    Things 助手由主对话委派时，父轮次属于主会话。这个子轮次在 Things 助手页面
    应当作为一轮顶层对话展示，不能按主会话里的工具子卡片过滤掉。
    """

    session_turn_ids = {
        str(getattr(run, "turn_id", "") or "")
        for run in session_runs
        if str(getattr(run, "turn_id", "") or "")
    }
    nested = {
        str(getattr(run, "turn_id", "") or "")
        for run in session_runs
        if str(getattr(run, "turn_id", "") or "") in foreground_child_turn_ids
        and str(getattr(run, "parent_turn_id", "") or "") in session_turn_ids
    }
    for tools in tools_by_turn.values():
        for tool in tools:
            child_turn_id = str(tool.child_turn_id or "")
            if child_turn_id in foreground_child_turn_ids:
                nested.add(child_turn_id)
    return nested


def _child_turns_by_parent_tool(
    *,
    session_runs: list[Any],
    tools_by_turn: dict[str, list[ConversationToolCallRecord]],
    foreground_child_turn_ids: set[str],
) -> dict[tuple[str, str], list[str]]:
    grouped: dict[tuple[str, str], list[str]] = {}
    for run in session_runs:
        child_turn_id = str(getattr(run, "turn_id", "") or "")
        parent_turn_id = str(getattr(run, "parent_turn_id", "") or "")
        parent_tool_call_id = str(getattr(run, "parent_tool_call_id", "") or "")
        if child_turn_id not in foreground_child_turn_ids:
            continue
        if parent_turn_id and parent_tool_call_id:
            grouped.setdefault((parent_turn_id, parent_tool_call_id), []).append(
                child_turn_id
            )
    for tools in tools_by_turn.values():
        for tool in tools:
            child_turn_id = str(tool.child_turn_id or "")
            if child_turn_id not in foreground_child_turn_ids:
                continue
            grouped.setdefault((tool.turn_id, tool.tool_call_id), []).append(
                child_turn_id
            )
    return {
        key: clean_ids(value)
        for key, value in grouped.items()
    }


def _run_is_background(run: Any) -> bool:
    metadata = dict(getattr(run, "metadata", {}) or {})
    child_scope = str(metadata.get("child_scope") or metadata.get("scope") or "").lower()
    return bool(metadata.get("background")) or child_scope in {
        "background",
        "background_subagent",
    }


def _turn_run_details(session_runs: list[Any], turn_id: str) -> list[dict[str, Any]]:
    for run in session_runs:
        if str(getattr(run, "turn_id", "") or "") != str(turn_id or ""):
            continue
        fields = {
            "turn_id": getattr(run, "turn_id", ""),
            "agent_id": getattr(run, "agent_id", ""),
            "parent_turn_id": getattr(run, "parent_turn_id", ""),
            "parent_tool_call_id": getattr(run, "parent_tool_call_id", ""),
            "status": getattr(run, "status", ""),
            "started_at": getattr(run, "started_at", ""),
            "finished_at": getattr(run, "finished_at", ""),
            "stop_reason": getattr(run, "stop_reason", ""),
            "error": getattr(run, "error", ""),
            "metadata": getattr(run, "metadata", {}) or {},
        }
        text = json.dumps(fields, ensure_ascii=False, indent=2, sort_keys=True)
        return [{"kind": "turn_run", "title": "子智能体轮次", "text": text}]
    return []


def _is_approval_status_item(item: ChatItem) -> bool:
    return item.kind == "event" and item.title in _APPROVAL_TITLES


_TURN_CONTENT_KINDS = {"assistant", "thinking", "tool", "model_status"}
_RETRY_PENDING_MESSAGE_STATUSES = {"accepted", "queued", "running", "waiting_approval"}
_RETRY_STATUS_KINDS = {"message", "provider_retry"}


@dataclass(frozen=True)
class _RetryChain:
    """重试链：把每条重试消息映射到它续答的那一轮。

    display_turn_by_turn 只收录真实存在的重试轮次；pending_titles 覆盖重试已受理
    但还没产生任何内容的阶段。
    """

    retry_message_ids: set[str]
    # 重试消息 -> 它续答的展示轮次，用来把重试消息的诊断详情挂回原轮的用户气泡。
    display_turn_by_retry_message: dict[str, str]
    display_turn_by_turn: dict[str, str]
    retry_index_by_turn: dict[str, int]
    # 展示轮次 -> (重试链位置, 状态标题)，重试进行中时给出「正在重试」「正在继续」
    pending_status_by_display_turn: dict[str, tuple[int, str]]
    retried_display_turn_ids: set[str]


def _retry_chain(messages: dict[str, Any]) -> _RetryChain:
    parents: dict[str, str] = {}
    sources: dict[str, str] = {}
    for message in messages.values():
        original_message_id = str(getattr(message, "retry_of_message_id", "") or "").strip()
        metadata = dict(getattr(message, "metadata", {}) or {})
        retry_source = str(metadata.get("retry_source") or "").strip()
        # 编辑重发是替换原消息，由 superseded/edited 表达，不走重试归并。
        if not original_message_id or retry_source == "edit":
            continue
        parents[str(message.message_id)] = original_message_id
        sources[str(message.message_id)] = retry_source

    def root_and_depth(message_id: str) -> tuple[str, int]:
        current = message_id
        depth = 0
        seen: set[str] = set()
        while current in parents and current not in seen:
            seen.add(current)
            current = parents[current]
            depth += 1
        return current, depth

    retry_message_ids: set[str] = set()
    display_turn_by_retry_message: dict[str, str] = {}
    display_turn_by_turn: dict[str, str] = {}
    retry_index_by_turn: dict[str, int] = {}
    pending_status_by_display_turn: dict[str, tuple[int, str]] = {}
    retried_display_turn_ids: set[str] = set()
    for retry_message_id in parents:
        message = messages.get(retry_message_id)
        if message is None:
            continue
        root_message_id, depth = root_and_depth(retry_message_id)
        root_message = messages.get(root_message_id)
        metadata = dict(getattr(message, "metadata", {}) or {})
        display_turn = str(metadata.get("retry_after_turn_id") or "").strip() or str(
            getattr(root_message, "turn_id", "") or ""
        ).strip()
        # retry_after_turn_id 可能指向上一次重试的轮次，归一到链首那一轮。
        display_turn = display_turn_by_turn.get(display_turn, display_turn)
        # 被重试的消息从未产生轮次（例如排队阶段就失败），没有可归并的目标，
        # 该重试仍作为独立用户消息呈现，否则内容会整条消失。
        if not display_turn:
            continue
        retry_message_ids.add(retry_message_id)
        display_turn_by_retry_message[retry_message_id] = display_turn
        retried_display_turn_ids.add(display_turn)
        retry_turn = str(getattr(message, "turn_id", "") or "").strip()
        if retry_turn and retry_turn != display_turn:
            display_turn_by_turn[retry_turn] = display_turn
            retry_index_by_turn[retry_turn] = depth
        status = _message_status_value(message)
        if status in _RETRY_PENDING_MESSAGE_STATUSES:
            title = (
                "正在继续"
                if sources.get(retry_message_id) == "recovery_continuation"
                else "正在重试"
            )
            current = pending_status_by_display_turn.get(display_turn)
            if current is None or current[0] < depth:
                pending_status_by_display_turn[display_turn] = (depth, title)
    return _RetryChain(
        retry_message_ids=retry_message_ids,
        display_turn_by_retry_message=display_turn_by_retry_message,
        display_turn_by_turn=display_turn_by_turn,
        retry_index_by_turn=retry_index_by_turn,
        pending_status_by_display_turn=pending_status_by_display_turn,
        retried_display_turn_ids=retried_display_turn_ids,
    )


def _merge_retry_turns(items: list[ChatItem], chain: _RetryChain) -> list[ChatItem]:
    """把重试轮次的内容归并进被重试的那一轮。

    重试消息本身不再作为用户气泡出现；它的内容排在原轮内容之后，靠
    (retry_index, runtime_seq) 定序。同一展示轮里只保留最后一次尝试的模型请求状态，
    早先的失败提示不再叠加。
    """

    if not chain.retry_message_ids:
        return items

    # 重试消息不再单独成气泡，它的诊断详情（重试来源、失败工具、原轮次）挂到原轮的
    # 用户气泡，原轮没有用户气泡时挂到该轮第一条内容上，仍可在详情里查到每一次尝试。
    retry_details: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item.kind != "user" or item.message_id not in chain.retry_message_ids:
            continue
        display_turn = chain.display_turn_by_retry_message.get(item.message_id, "")
        if display_turn and item.details:
            retry_details.setdefault(display_turn, []).extend(item.details)

    detail_target: dict[str, int] = {}
    user_target: dict[str, int] = {}
    for index, item in enumerate(items):
        if item.kind == "user" and item.message_id in chain.retry_message_ids:
            continue
        turn_id = str(item.turn_id or "")
        if turn_id not in retry_details:
            continue
        detail_target.setdefault(turn_id, index)
        if item.kind == "user":
            user_target.setdefault(turn_id, index)
    detail_target.update(user_target)

    rewritten: list[ChatItem] = []
    for index, item in enumerate(items):
        if item.kind == "user" and item.message_id in chain.retry_message_ids:
            continue
        turn_id = str(item.turn_id or "")
        if detail_target.get(turn_id) == index:
            item = replace(item, details=[*item.details, *retry_details[turn_id]])
        display_turn = chain.display_turn_by_turn.get(turn_id, "")
        if display_turn and item.kind in _TURN_CONTENT_KINDS:
            rewritten.append(
                replace(
                    item,
                    turn_id=display_turn,
                    source_turn_id=turn_id,
                    retry_index=chain.retry_index_by_turn.get(turn_id, 0),
                )
            )
            continue
        if turn_id in chain.retried_display_turn_ids and item.status_items:
            # 原轮的失败提示由最后一次尝试的状态取代。
            kept = [
                status
                for status in item.status_items
                if str(status.get("kind") or "") not in _RETRY_STATUS_KINDS
            ]
            item = replace(item, status_items=kept)
        rewritten.append(item)

    for display_turn, (retry_index, title) in chain.pending_status_by_display_turn.items():
        anchor = next(
            (item for item in rewritten if item.turn_id == display_turn),
            None,
        )
        if anchor is None:
            continue
        rewritten.append(
            ChatItem(
                "model_status",
                session_id=anchor.session_id,
                turn_id=display_turn,
                source_turn_id=display_turn,
                retry_index=retry_index,
                seq=anchor.seq,
                created_at=anchor.created_at,
                origin=anchor.origin,
                title=title,
                tone="muted",
                status_key="model-request",
            )
        )

    latest_retry_index: dict[str, int] = {}
    for item in rewritten:
        if item.kind != "model_status" or not item.turn_id:
            continue
        latest_retry_index[item.turn_id] = max(
            latest_retry_index.get(item.turn_id, 0), item.retry_index
        )
    deduped = [
        item
        for item in rewritten
        if item.kind != "model_status"
        or not item.turn_id
        or item.retry_index == latest_retry_index.get(item.turn_id, 0)
    ]

    anchor_index: dict[str, int] = {}
    for index, item in enumerate(deduped):
        if item.turn_id:
            anchor_index.setdefault(item.turn_id, index)

    def sort_key(entry: tuple[int, ChatItem]) -> tuple[int, int, int, int]:
        index, item = entry
        if not item.turn_id:
            return (index, 0, 0, index)
        return (anchor_index[item.turn_id], item.retry_index, item.runtime_seq, index)

    return [item for _key, item in sorted(enumerate(deduped), key=sort_key)]


def _mark_redo_state(
    item: ChatItem,
    redo_excluded_turn_ids: set[str],
    edited_message_ids: set[str],
) -> ChatItem:
    superseded = (
        item.kind in {"user", "assistant", "thinking", "tool"}
        and bool(item.turn_id)
        and item.turn_id in redo_excluded_turn_ids
    )
    edited = item.kind == "user" and item.message_id in edited_message_ids
    if not superseded and not edited:
        return item
    return replace(item, superseded=superseded, edited=edited)


def _ordered_turn_content(
    events: list[Any],
    tools: list[ConversationToolCallRecord],
) -> list[tuple[str, Any, int]]:
    """按运行事件里的真实顺序交错返回助手消息和工具调用。

    第三个元素是该内容的 runtime_seq：运行事件用自己的 seq，工具用触发它的
    tool.call.started 事件的 seq。没有对应事件的工具排在末尾，按已见到的最大
    runtime_seq 递增编号，保持相对顺序。
    """

    tools_by_call_id = {
        str(tool.tool_call_id or ""): tool for tool in tools if tool.tool_call_id
    }
    tools_by_record_id = {
        str(tool.tool_record_id or ""): tool for tool in tools if tool.tool_record_id
    }
    ordered: list[tuple[str, Any, int]] = []
    # 按对象身份去重，而非 tool_record_id：两条 tool_record_id 为空的工具是不同调用，
    # 用空串做 key 会让第二条被静默丢弃。
    seen_tools: set[int] = set()
    has_visible_output = False
    max_runtime_seq = 0
    for event in sorted(
        events, key=lambda item: int(getattr(item, "seq", 0) or 0)
    ):
        event_type = str(getattr(event, "type", "") or "")
        payload = dict(getattr(event, "payload", {}) or {})
        runtime_seq = int(getattr(event, "seq", 0) or 0)
        max_runtime_seq = max(max_runtime_seq, runtime_seq)
        if event_type in _ASSISTANT_MESSAGE_EVENT_TYPES:
            if _flatten_content(payload.get("content")).strip():
                ordered.append(("assistant", event, runtime_seq))
                has_visible_output = True
            continue
        if event_type == "assistant.thinking.completed":
            if _flatten_content(payload.get("content")).strip():
                ordered.append(("thinking", event, runtime_seq))
                has_visible_output = True
            continue
        if event_type == "provider.retry":
            if has_visible_output:
                ordered.append(("model_status", event, runtime_seq))
            continue
        if event_type == "turn.failed":
            if has_visible_output and str(payload.get("error") or "").strip():
                ordered.append(("model_status", event, runtime_seq))
            continue
        if event_type != "tool.call.started":
            continue
        tool = tools_by_record_id.get(str(payload.get("tool_record_id") or ""))
        if tool is None:
            tool = tools_by_call_id.get(str(payload.get("tool_call_id") or ""))
        if tool is None or id(tool) in seen_tools:
            continue
        seen_tools.add(id(tool))
        ordered.append(("tool", tool, runtime_seq))
        has_visible_output = True

    for tool in tools:
        if id(tool) in seen_tools:
            continue
        seen_tools.add(id(tool))
        max_runtime_seq += 1
        ordered.append(("tool", tool, max_runtime_seq))
    return ordered


def _model_status_item_from_run_event(
    event: Any,
    *,
    session_id: str,
    turn_id: str,
    seq: int,
    runtime_seq: int,
    created_at: str,
    origin: str,
) -> ChatItem | None:
    event_type = str(getattr(event, "type", "") or "")
    payload = dict(getattr(event, "payload", {}) or {})
    detail = str(payload.get("error") or "").strip()
    if event_type == "provider.retry":
        attempt = int(payload.get("attempt") or 0)
        next_attempt = int(payload.get("next_attempt") or max(2, attempt + 1))
        max_attempts = int(payload.get("max_attempts") or 0)
        progress = f"{next_attempt}/{max_attempts}" if max_attempts > 0 else str(next_attempt)
        title = (
            f"第 {attempt or max(1, next_attempt - 1)} 次请求失败，"
            f"正在进行第 {progress} 次尝试"
        )
    elif event_type == "turn.failed" and detail:
        title = "请求失败"
    else:
        return None
    return ChatItem(
        "model_status",
        session_id=session_id,
        turn_id=turn_id,
        seq=seq,
        runtime_seq=runtime_seq,
        created_at=str(getattr(event, "created_at", "") or created_at),
        origin=origin,
        title=title,
        tone="muted",
        detail=detail,
        status_key="model-request",
    )


def _assistant_item_from_run_event(
    event: Any,
    *,
    session_id: str,
    turn_id: str,
    seq: int,
    runtime_seq: int,
    created_at: str,
    origin: str,
    details: list[dict[str, Any]] | None = None,
) -> ChatItem | None:
    if str(getattr(event, "type", "") or "") not in _ASSISTANT_MESSAGE_EVENT_TYPES:
        return None
    payload = dict(getattr(event, "payload", {}) or {})
    text = _flatten_content(payload.get("content")).strip()
    if not text:
        return None
    return ChatItem(
        "assistant",
        text=text,
        session_id=session_id,
        turn_id=turn_id,
        seq=seq,
        runtime_seq=runtime_seq,
        created_at=str(getattr(event, "created_at", "") or created_at),
        origin=origin,
        details=list(details or []),
    )


def _thinking_item_from_run_event(
    event: Any,
    *,
    session_id: str,
    turn_id: str,
    seq: int,
    runtime_seq: int,
    created_at: str,
    origin: str,
    details: list[dict[str, Any]] | None = None,
) -> ChatItem | None:
    if str(getattr(event, "type", "") or "") != "assistant.thinking.completed":
        return None
    payload = dict(getattr(event, "payload", {}) or {})
    text = _flatten_content(payload.get("content")).strip()
    if not text:
        return None
    return ChatItem(
        "thinking",
        text=text,
        session_id=session_id,
        turn_id=turn_id,
        seq=seq,
        runtime_seq=runtime_seq,
        created_at=str(getattr(event, "created_at", "") or created_at),
        origin=origin,
        details=list(details or []),
    )


def _tool_item_from_record(
    tool: ConversationToolCallRecord,
    *,
    seq: int,
    runtime_seq: int,
    created_at: str,
    origin: str,
    details: list[dict[str, Any]] | None = None,
    child_items: list[ChatItem] | None = None,
    approvals: list | None = None,
) -> ChatItem:
    output = _format_value(tool.result)
    if not output and tool.error:
        output = tool.error
    approval_status = ""
    for approval in approvals or []:
        status = getattr(approval, "status", "")
        approval_status = str(getattr(status, "value", status) or "")
        # 调度器等系统通道的自动批准单独标注，与用户手动批准区分。
        if (
            approval_status == "approved"
            and str(getattr(approval, "scope", "") or "") == "scheduler"
        ):
            approval_status = "auto_approved"
    return ChatItem(
        "tool",
        session_id=tool.session_id,
        turn_id=tool.turn_id,
        seq=seq,
        runtime_seq=runtime_seq,
        created_at=tool.completed_at or tool.started_at or created_at,
        origin=origin,
        tool_record_id=tool.tool_record_id,
        tool_call_id=tool.tool_call_id,
        tool_name=tool.tool_name or "tool",
        args=_format_value(tool.args),
        output=output,
        is_error=tool.is_error
        or tool.status
        in {
            ConversationToolCallStatus.FAILED,
            ConversationToolCallStatus.DENIED,
            ConversationToolCallStatus.CANCELLED,
        },
        approval_status=approval_status,
        details=list(details or []),
        child_items=list(child_items or []),
    )


def _status_item_from_message_event(
    event: ConversationEventRecord,
    *,
    turn_id: str,
    message: Any | None = None,
    attempts: list | None = None,
    tools: list[ConversationToolCallRecord] | None = None,
    approvals: list | None = None,
) -> dict[str, Any]:
    reason = str(
        event.payload.get("error")
        or event.payload.get("reason")
        or event.payload.get("stop_reason")
        or ""
    )
    if event.type == "conversation.message.failed" and reason == "restart_interrupted":
        title = "执行中断"
        fallback = "程序退出时，这一轮还没有完成。"
        reason = ""
    elif event.type == "conversation.message.failed":
        title = "消息失败"
        fallback = "这一轮没有完成。"
    elif event.type == "conversation.message.cancelled":
        title = "消息已停止"
        fallback = "这一轮已被取消。"
    else:
        title = "消息状态"
        fallback = event.type
    text = reason or fallback
    return _inline_status(
        title=title,
        text=text,
        kind="message",
        tone="danger",
        event=event,
        turn_id=turn_id,
        details=_event_details(event)
        + _message_details(message)
        + _attempt_details(list(attempts or []))
        + _tool_details(list(tools or []))
        + _approval_details(list(approvals or [])),
    )


def _command_or_stop_item(
    event: ConversationEventRecord,
    *,
    session_id: str,
) -> ChatItem:
    payload = dict(event.payload or {})
    if event.type == "conversation.stop.requested":
        title = "停止请求"
        text = _stop_event_text(payload, state="requested")
    elif event.type == "conversation.stop.completed":
        title = "已停止"
        text = _stop_event_text(payload, state="completed")
    elif event.type == "conversation.stop.noop":
        title = "停止请求"
        text = _stop_event_text(payload, state="noop")
    elif event.type == "conversation.command.completed":
        title = "命令结果"
        response = str(payload.get("response") or "").strip()
        if response:
            text = response
        else:
            command = str(payload.get("command_token") or payload.get("command_name") or "")
            args = str(payload.get("command_args") or "").strip()
            text = " ".join(part for part in [command, args] if part).strip() or "命令已完成。"
    elif event.type == "conversation.command.failed":
        title = "命令失败"
        text = str(payload.get("response") or payload.get("error") or "").strip() or "命令没有完成。"
    elif event.type == "conversation.command.cancelled":
        title = "命令已停止"
        text = str(payload.get("response") or "").strip() or "命令已被取消。"
    else:
        title = "命令"
        command = str(payload.get("command_token") or payload.get("command_name") or "")
        args = str(payload.get("command_args") or "").strip()
        text = " ".join(part for part in [command, args] if part).strip() or event.type
    return ChatItem(
        "event",
        title=title,
        text=text,
        session_id=session_id,
        turn_id=event.turn_id,
        seq=event.seq,
        created_at=event.created_at,
        details=_stop_event_details(payload) + _event_details(event),
    )


def _queue_event_item(
    event: ConversationEventRecord,
    *,
    session_id: str,
    message: Any | None = None,
) -> ChatItem:
    payload = dict(event.payload or {})
    title = "队列更新"
    text = "队列状态已更新。"
    if event.type == "conversation.message.queued":
        title = "消息重新排队"
        text = _queue_requeued_text(payload)
    elif event.type == "conversation.message.edited":
        title = "消息已编辑"
        text = _queue_edited_text(payload)
    elif event.type == "conversation.message.append_mode_changed":
        title = "发送方式已更新"
        text = _queue_append_mode_changed_text(payload)
    elif event.type == "conversation.message.promoted":
        title = "消息已提前"
        position = int(payload.get("queue_position") or 0)
        text = f"排队消息已提前到第 {position} 位。" if position else "排队消息已提前。"
    elif event.type == "conversation.message.replaced":
        title = "消息已替换"
        reason = str(payload.get("reason") or "").strip()
        text = "排队消息已替换。" if not reason else f"排队消息已替换：{reason}。"
    elif event.type == "conversation.message.resume_required":
        title = "消息待继续"
        reason = str(payload.get("reason") or "").strip()
        text = "重启后排队消息需要手动继续。" if reason else "排队消息需要手动继续。"
    elif event.type == "conversation.message.resume_requested":
        title = "继续排队消息"
        reason = str(payload.get("recovery_reason") or "").strip()
        text = "已继续重启恢复的排队消息。" if reason else "已继续排队消息。"
    elif event.type == "conversation.queue.reordered":
        title = "队列已重排"
        count = len(list(payload.get("order") or []))
        text = f"队列顺序已更新，共 {count} 条排队消息。" if count else "队列顺序已更新。"
    return ChatItem(
        "event",
        title=title,
        text=text,
        session_id=session_id,
        message_id=event.message_id,
        turn_id=event.turn_id,
        seq=event.seq,
        created_at=event.created_at,
        details=_event_details(event) + _message_details(message),
    )


def _queue_event_visible(event: ConversationEventRecord) -> bool:
    if event.type != "conversation.message.queued":
        return True
    payload = dict(event.payload or {})
    return bool(
        str(payload.get("old_status") or "").strip()
        or str(payload.get("reason") or "").strip()
    )


def _input_classification_item(
    event: ConversationEventRecord,
    *,
    session_id: str,
) -> ChatItem | None:
    payload = dict(event.payload or {})
    if not _input_classification_visible(payload):
        return None
    return ChatItem(
        "event",
        title=_input_classification_title(payload),
        text=_input_classification_text(payload),
        session_id=session_id,
        turn_id=event.turn_id,
        seq=event.seq,
        created_at=event.created_at,
        details=_input_classification_details(payload) + _event_details(event),
    )


def _input_classification_visible(payload: dict[str, Any]) -> bool:
    kind = str(payload.get("kind") or "").strip()
    # system_trigger 在 transcript 里由 notice 系统提示条呈现；background_notification
    # 由助手消息呈现。两者都不再重复渲染输入分类卡。
    return kind not in {
        "",
        ConversationInputKind.NORMAL_MESSAGE.value,
        ConversationInputKind.SYSTEM_TRIGGER.value,
        ConversationInputKind.BACKGROUND_NOTIFICATION.value,
    }


def _input_classification_title(payload: dict[str, Any]) -> str:
    kind = str(payload.get("kind") or "").strip()
    if kind == ConversationInputKind.UNKNOWN_SLASH_MESSAGE.value:
        return "未知斜杠按消息发送"
    if kind == ConversationInputKind.REGISTERED_COMMAND.value:
        return "命令已分类"
    if kind == ConversationInputKind.SKILL_ACTIVATION.value:
        return "技能激活"
    if kind == ConversationInputKind.AGENT_CONTROL.value:
        return "智能体控制"
    return _input_kind_label(kind)


def _input_classification_text(payload: dict[str, Any]) -> str:
    kind = str(payload.get("kind") or "").strip()
    text = str(payload.get("text") or "").strip()
    command = str(
        payload.get("command_token") or payload.get("command_name") or ""
    ).strip()
    policy = command_context_policy_label(
        str(payload.get("command_context_policy") or "")
    )
    availability = command_availability_label(
        str(payload.get("command_availability") or "")
    )
    target = _stop_target_label(str(payload.get("cancel_target") or ""))
    if kind == ConversationInputKind.UNKNOWN_SLASH_MESSAGE.value:
        value = text or "未知斜杠文本"
        return f"{value} 不是已注册命令，已作为普通消息进入模型。"
    if kind == ConversationInputKind.REGISTERED_COMMAND.value:
        parts = [part for part in [policy, availability] if part]
        parts.append(
            "创建命令触发轮次"
            if bool(payload.get("creates_turn"))
            else "不创建模型轮次"
        )
        if target:
            parts.append(f"取消目标：{target}")
        return f"命令 {command or text or 'command'}：" + "，".join(parts) + "。"
    if kind == ConversationInputKind.SKILL_ACTIVATION.value:
        skill = str(payload.get("skill_name") or "").strip()
        return f"技能 {skill or text or 'skill'} 已作为技能激活输入进入模型。"
    if kind == ConversationInputKind.AGENT_CONTROL.value:
        suffix = f"目标：{target}。" if target else ""
        return f"智能体控制已记录，不作为普通用户消息进入模型。{suffix}"
    if kind in {
        ConversationInputKind.APPROVAL_RESPONSE.value,
        ConversationInputKind.QUESTION_RESPONSE.value,
        ConversationInputKind.CONFIRMATION_RESPONSE.value,
    }:
        return f"{_input_kind_label(kind)}已记录，不作为普通用户消息进入模型。"
    if kind == ConversationInputKind.SYSTEM_TRIGGER.value:
        return "系统触发已按专门来源进入模型。"
    if kind == ConversationInputKind.BACKGROUND_NOTIFICATION.value:
        return "后台任务通知已记录。"
    enters_model = "进入模型" if bool(payload.get("enters_model")) else "不进入模型"
    creates_turn = "创建轮次" if bool(payload.get("creates_turn")) else "不创建轮次"
    return f"{_input_kind_label(kind)}：{enters_model}，{creates_turn}。"


def _input_classification_details(payload: dict[str, Any]) -> list[dict[str, Any]]:
    fields = [
        ("分类", _input_kind_label(str(payload.get("kind") or ""))),
        ("进入模型", _payload_bool_label(payload.get("enters_model"))),
        ("创建轮次", _payload_bool_label(payload.get("creates_turn"))),
    ]
    command = str(
        payload.get("command_token") or payload.get("command_name") or ""
    ).strip()
    if command:
        fields.append(("命令", command))
    args = str(payload.get("command_args") or "").strip()
    if args:
        fields.append(("命令参数", args))
    policy = command_context_policy_label(
        str(payload.get("command_context_policy") or "")
    )
    if policy:
        fields.append(("上下文策略", policy))
    availability = command_availability_label(
        str(payload.get("command_availability") or "")
    )
    if availability:
        fields.append(("运行中可用性", availability))
    target = _stop_target_label(str(payload.get("cancel_target") or ""))
    if target:
        fields.append(("取消目标", target))
    skill = str(payload.get("skill_name") or "").strip()
    if skill:
        fields.append(("技能", skill))
    source = str(payload.get("source") or "").strip()
    if source:
        fields.append(("来源", source))
    text = "\n".join(f"{label}：{value}" for label, value in fields)
    return [{"kind": "input_classification", "title": "输入分类", "text": text}]


def _input_kind_label(kind: str) -> str:
    labels = {
        "normal_message": "普通消息",
        "registered_command": "已注册命令",
        "unknown_slash_message": "未知斜杠",
        "skill_activation": "技能激活",
        "approval_response": "审批回复",
        "question_response": "追问回复",
        "confirmation_response": "确认回复",
        "system_trigger": "系统触发",
        "agent_control": "智能体控制",
        "background_notification": "后台通知",
    }
    return labels.get(str(kind or "").strip(), str(kind or "").strip() or "输入")


def _run_recovery_item(
    event: ConversationEventRecord,
    *,
    session_id: str,
) -> ChatItem:
    payload = dict(event.payload or {})
    background = bool(payload.get("background")) or str(
        payload.get("child_scope") or ""
    ).strip().lower() == "background"
    scope = "后台" if background else "前台"
    agent_id = str(payload.get("agent_id") or event.agent_id or "").strip()
    turn_id = str(payload.get("turn_id") or event.turn_id or "").strip()
    old_status = str(payload.get("old_status") or "").strip()
    target = agent_id or "智能体"
    text = f"{scope} {target} 的运行已在启动恢复时中断。"
    if turn_id:
        text += f"轮次：{turn_id}。"
    if old_status:
        text += f"原状态：{_run_status_label(old_status)}。"
    return ChatItem(
        "event",
        title="运行重启中断",
        text=text,
        session_id=session_id,
        turn_id=turn_id,
        seq=event.seq,
        created_at=event.created_at,
        details=_event_details(event),
    )


def _run_status_label(status: str) -> str:
    labels = {
        "queued": "排队中",
        "running": "运行中",
        "cancel_requested": "停止中",
        "failed": "失败",
        "cancelled": "已取消",
        "completed": "完成",
    }
    clean = str(status or "").strip()
    return labels.get(clean, clean or "未知")


def _queue_requeued_text(payload: dict[str, Any]) -> str:
    old_status = str(payload.get("old_status") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if old_status == "failed":
        return "失败消息已重新排队。"
    if old_status == "accepted":
        return "未开始消息已重新排队。"
    if reason:
        return f"消息已重新排队：{reason}。"
    return "消息已重新排队。"


def _queue_edited_text(payload: dict[str, Any]) -> str:
    old_text = str(payload.get("old_text") or "").strip()
    text = str(payload.get("text") or "").strip()
    old_append_mode = str(payload.get("old_append_mode") or "").strip()
    append_mode = str(payload.get("append_mode") or "").strip()
    parts: list[str] = []
    if old_text or text:
        parts.append("文本已更新")
    if append_mode and append_mode != old_append_mode:
        parts.append(f"发送方式改为 {_append_mode_label(append_mode)}")
    return "，".join(parts) + "。" if parts else "排队消息已编辑。"


def _queue_append_mode_changed_text(payload: dict[str, Any]) -> str:
    append_mode = str(payload.get("append_mode") or "").strip()
    if append_mode:
        return f"排队消息发送方式已改为 {_append_mode_label(append_mode)}。"
    return "排队消息发送方式已更新。"


def _stop_event_text(payload: dict[str, Any], *, state: str) -> str:
    target = str(payload.get("cancel_target") or "").strip()
    target_id = str(
        payload.get("target_id") or payload.get("requested_turn_id") or ""
    ).strip()
    label = _stop_target_label(target)
    suffix = f"：{target_id}" if target_id else ""
    if state == "requested":
        return f"正在取消{label}{suffix}。" if label else "正在停止当前前台任务。"
    if state == "completed":
        if str(payload.get("cancelled")).lower() == "false":
            return f"{label}没有被取消{suffix}。" if label else "没有任务被停止。"
        return f"{label}已取消{suffix}。" if label else "当前前台任务已停止。"
    if state == "noop":
        return f"没有可取消的{label}{suffix}。" if label else "没有正在运行的任务。"
    return "停止状态已更新。"


def _stop_event_details(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not str(payload.get("cancel_target") or "").strip():
        return []
    parts = [
        f"目标：{_stop_target_label(str(payload.get('cancel_target') or '')) or str(payload.get('cancel_target') or '')}",
    ]
    fields = [
        ("控制动作", "control_action"),
        ("客户端动作", "client_action_id"),
        ("目标编号", "target_id"),
        ("请求轮次", "requested_turn_id"),
        ("智能体", "agent_id"),
        ("工具调用", "tool_call_id"),
        ("工具记录", "tool_record_id"),
        ("来源", "source"),
        ("命令", "command_token"),
        ("命令名", "command_name"),
        ("命令参数", "command_args"),
    ]
    for label, key in fields:
        value = str(payload.get(key) or "").strip()
        if value:
            parts.append(f"{label}：{value}")
    if "cancelled" in payload:
        parts.append(f"已取消：{_payload_bool_label(payload.get('cancelled'))}")
    details = [
        {
            "kind": "stop",
            "title": "停止目标",
            "text": "\n".join(parts),
        }
    ]
    tree_detail = _stop_cancel_tree_detail(payload)
    if tree_detail:
        details.append(tree_detail)
    return details


def _stop_cancel_tree_detail(payload: dict[str, Any]) -> dict[str, Any] | None:
    tree = payload.get("cancel_tree")
    if not isinstance(tree, dict):
        return None
    runs = [item for item in list(tree.get("runs") or []) if isinstance(item, dict)]
    if not runs:
        return None
    lines: list[str] = []
    for run in runs:
        action = str(run.get("action") or "")
        turn_id = str(run.get("turn_id") or "")
        agent_id = str(run.get("agent_id") or "")
        status = _run_status_label(str(run.get("status") or ""))
        prefix = {
            "cancel": "取消",
            "keep_background": "保留后台",
            "already_terminal": "已完成/终止",
        }.get(action, action or "记录")
        started = "已开始" if bool(run.get("started")) else "未开始"
        background = "后台" if bool(run.get("background")) else "前台"
        target = " / ".join(part for part in [turn_id, agent_id] if part)
        lines.append(f"{prefix}：{target}（{background}，{started}，{status}）")
    return {
        "kind": "stop_tree",
        "title": "智能体树",
        "text": "\n".join(lines),
    }


def _payload_bool_label(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    text = str(value or "").strip().lower()
    if text in {"false", "0", "no", "否"}:
        return "否"
    if text in {"true", "1", "yes", "是"}:
        return "是"
    return "是" if bool(value) else "否"


def _stop_target_label(target: str) -> str:
    labels = {
        "message": "消息",
        "turn": "轮次",
        "agent": "智能体",
        "tool_call": "工具调用",
        "approval": "审批",
        "input_request": "待输入请求",
        "background_task": "后台任务",
        "current_foreground_tree": "当前前台任务",
        "entire_agent_tree": "整个智能体树",
    }
    return labels.get(str(target or "").strip(), "")


def _approval_item(
    event: ConversationEventRecord,
    *,
    session_id: str,
    approvals: list,
) -> ChatItem:
    payload = dict(event.payload or {})
    status = str(payload.get("status") or "").strip()
    approval_id = str(payload.get("approval_id") or "").strip()
    approval = next(
        (
            item
            for item in approvals
            if str(getattr(item, "approval_id", "") or "") == approval_id
        ),
        None,
    )
    tool_name = str(
        payload.get("tool_name")
        or getattr(approval, "tool_name", "")
        or payload.get("tool_call_id")
        or "工具"
    )
    if event.type == "conversation.approval.requested":
        title = "等待审批"
        action = str(payload.get("action") or getattr(approval, "action", "") or "")
        text = f"{tool_name}: {action}".strip(": ")
    else:
        title = _approval_title(status)
        feedback = str(
            payload.get("feedback")
            or payload.get("decision")
            or getattr(approval, "feedback", "")
            or getattr(approval, "decision", "")
            or status
        )
        text = f"{tool_name}: {feedback}".strip(": ")
    return ChatItem(
        "event",
        title=title,
        text=text,
        session_id=session_id,
        turn_id=event.turn_id,
        seq=event.seq,
        created_at=event.created_at,
        details=_event_details(event)
        + (_approval_details([approval]) if approval is not None else []),
    )


def _approval_title(status: str) -> str:
    try:
        value = ConversationApprovalStatus(status)
    except ValueError:
        return "审批记录"
    if value == ConversationApprovalStatus.APPROVED:
        return "审批通过"
    if value == ConversationApprovalStatus.DENIED:
        return "审批拒绝"
    if value == ConversationApprovalStatus.CANCELLED:
        return "审批已取消"
    if value == ConversationApprovalStatus.EXPIRED:
        return "审批超时"
    return "等待审批"


def _input_request_item(
    event: ConversationEventRecord,
    *,
    session_id: str,
) -> ChatItem | None:
    payload = dict(event.payload or {})
    kind = str(payload.get("kind") or "").strip()
    if kind == "approval":
        return None
    title = _input_request_title(event.type, kind, payload)
    text = _input_request_text(event.type, payload)
    return ChatItem(
        "event",
        title=title,
        text=text,
        session_id=session_id,
        turn_id=event.turn_id,
        seq=event.seq,
        created_at=event.created_at,
        details=_input_request_details(payload) + _event_details(event),
    )


def _input_request_title(
    event_type: str,
    kind: str,
    payload: dict[str, Any],
) -> str:
    if event_type == "conversation.input_request.requested":
        if kind == "confirmation":
            return "等待确认"
        if kind == "question":
            return "等待回答"
        return "等待输入"
    if event_type == "conversation.input_request.cancelled":
        if _input_request_recovery_interrupted(payload):
            if kind == "confirmation":
                return "确认已中断"
            if kind == "question":
                return "追问已中断"
            return "输入已中断"
        if kind == "confirmation":
            return "确认已取消"
        if kind == "question":
            return "追问已取消"
        return "输入已取消"
    if event_type == "conversation.input_request.expired":
        if kind == "question":
            return "追问已超时"
        return "输入已超时"
    if event_type == "conversation.input_request.resolved":
        if kind == "confirmation":
            return "确认已完成"
        if kind == "question":
            return "回答已收到"
        return "输入已完成"
    return "待输入记录"


def _input_request_text(event_type: str, payload: dict[str, Any]) -> str:
    kind = str(payload.get("kind") or "").strip()
    detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
    if event_type == "conversation.input_request.requested":
        if kind == "confirmation":
            command_text = str(detail.get("command_text") or payload.get("source_id") or "").strip()
            reason = str(detail.get("reason") or payload.get("prompt") or "").strip()
            prefix = f"确认后执行 {command_text}" if command_text else "确认后执行命令"
            return prefix + (f"。原因：{reason}" if reason else "。")
        prompt = str(payload.get("prompt") or "").strip()
        title = str(payload.get("title") or "").strip()
        return prompt or title or str(payload.get("request_id") or "等待用户输入。")
    if _input_request_recovery_interrupted(payload):
        return "启动恢复时已中断。"
    response = payload.get("response")
    answer = ""
    feedback = ""
    if isinstance(response, dict):
        answer = str(response.get("answer") or "").strip()
        feedback = str(response.get("feedback") or "").strip()
        if kind == "confirmation":
            command_text = str(
                response.get("command_text") or detail.get("command_text") or ""
            ).strip()
            decision = str(payload.get("decision") or response.get("decision") or "").strip()
            if decision in {"confirmed", "confirm"}:
                return f"已确认执行 {command_text}。" if command_text else "已确认执行命令。"
            if decision in {"cancelled", "cancel"}:
                return f"已取消 {command_text}。" if command_text else "已取消命令。"
    decision = str(payload.get("decision") or "").strip()
    return answer or feedback or decision or str(payload.get("status") or "")


def _input_request_details(payload: dict[str, Any]) -> list[dict[str, Any]]:
    kind = str(payload.get("kind") or "").strip()
    detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
    if kind != "confirmation" or not detail:
        return []
    parts: list[str] = []
    fields = [
        ("命令", "command_text"),
        ("命令名", "command_name"),
        ("命令参数", "command_args"),
        ("原因", "reason"),
        ("运行中可用性", "command_availability"),
        ("上下文策略", "command_context_policy"),
        ("来源", "source"),
        ("页面", "surface"),
    ]
    for label, key in fields:
        value = str(detail.get(key) or "").strip()
        if not value:
            continue
        if key == "command_availability":
            value = command_availability_label(value) or value
        elif key == "command_context_policy":
            value = command_context_policy_label(value) or value
        parts.append(f"{label}：{value}")
    if "enters_model" in detail:
        parts.append(f"进入模型：{_payload_bool_label(detail.get('enters_model'))}")
    if "creates_turn" in detail:
        parts.append(f"创建轮次：{_payload_bool_label(detail.get('creates_turn'))}")
    if not parts:
        return []
    return [
        {
            "kind": "command_confirmation",
            "title": "命令确认",
            "text": "\n".join(parts),
        }
    ]


def _input_request_recovery_interrupted(payload: dict[str, Any]) -> bool:
    decision = str(payload.get("decision") or "").strip()
    response = payload.get("response")
    if isinstance(response, dict) and bool(response.get("recovery")):
        return True
    return decision == "restart_interrupted"


def _recovery_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    labels = {
        "messages_requeued": "重新入队消息",
        "messages_resume_required": "待继续消息",
        "messages_failed": "中断消息",
        "message_leases_released": "释放消息占用",
        "tool_calls_recovered": "恢复工具",
        "tool_calls_cancelled": "取消工具",
        "tool_calls_failed": "失败工具",
        "approvals_recovered": "恢复审批",
        "approvals_cancelled": "取消审批",
        "input_requests_recovered": "恢复等待输入",
        "input_requests_cancelled": "取消等待输入",
        "attempts_failed": "中断尝试",
        "runs_interrupted": "中断运行",
        "queued_runs_interrupted": "中断排队运行",
        "running_runs_interrupted": "中断运行中",
        "cancel_requested_runs_interrupted": "中断停止中",
        "foreground_runs_interrupted": "中断前台运行",
        "background_runs_interrupted": "中断后台运行",
    }
    for key, label in labels.items():
        value = int(payload.get(key) or 0)
        if value:
            parts.append(f"{label} {value}")
    return "，".join(parts) if parts else "启动时检查了未完成状态。"


def _event_details(event: ConversationEventRecord) -> list[dict[str, Any]]:
    text_parts = [
        f"事件：{event.type}",
        f"序号：{event.seq}",
    ]
    if event.message_id:
        text_parts.append(f"消息：{event.message_id}")
    if event.turn_id:
        text_parts.append(f"轮次：{event.turn_id}")
    if event.agent_id:
        text_parts.append(f"智能体：{event.agent_id}")
    payload = _format_value(event.payload)
    if payload:
        text_parts.append("数据：\n" + payload)
    return [
        {
            "kind": "event",
            "title": "事件记录",
            "text": "\n".join(text_parts),
            "message_id": event.message_id,
            "turn_id": event.turn_id,
            "agent_id": event.agent_id,
            "event_seq": event.seq,
            "event_type": event.type,
        }
    ]


def _message_details(message: Any | None) -> list[dict[str, Any]]:
    if message is None:
        return []
    metadata_obj = getattr(message, "metadata", {}) or {}
    parts = [
        f"消息：{message.message_id}",
        f"状态：{_enum_value(message.status)}",
        f"类型：{_enum_value(message.kind)}",
        f"追加模式：{_enum_value(message.append_mode)}",
    ]
    if getattr(message, "turn_id", ""):
        parts.append(f"轮次：{message.turn_id}")
    if getattr(message, "retry_of_message_id", ""):
        parts.append(f"重试自消息：{message.retry_of_message_id}")
    parts.extend(_message_retry_detail_lines(metadata_obj))
    if getattr(message, "queue_position", 0):
        parts.append(f"排队位置：{message.queue_position}")
    if getattr(message, "source", ""):
        parts.append(f"来源：{message.source}")
    if getattr(message, "platform", ""):
        parts.append(f"平台：{message.platform}")
    metadata = _format_value(metadata_obj)
    if metadata and metadata != "{}":
        parts.append("元数据：\n" + metadata)
    return [
        {
            "kind": "message",
            "title": "消息记录",
            "text": "\n".join(parts),
            "message_id": message.message_id,
            "turn_id": getattr(message, "turn_id", ""),
        }
    ]


def _message_retry_detail_lines(metadata: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    retry_source = str(metadata.get("retry_source") or "").strip()
    if retry_source:
        parts.append(f"重试来源：{_retry_source_label(retry_source)}")
    retry_of_attempt_id = str(metadata.get("retry_of_attempt_id") or "").strip()
    if retry_of_attempt_id:
        parts.append(f"重试自尝试：{retry_of_attempt_id}")
    original_append_mode = str(metadata.get("retry_original_append_mode") or "").strip()
    if original_append_mode:
        parts.append(f"原发送方式：{_append_mode_label(original_append_mode)}")
    retry_tool_name = str(metadata.get("retry_tool_name") or "").strip()
    if retry_tool_name:
        parts.append(f"失败工具：{retry_tool_name}")
    retry_tool_record_id = str(metadata.get("retry_of_tool_record_id") or "").strip()
    if retry_tool_record_id:
        parts.append(f"工具记录：{retry_tool_record_id}")
    retry_tool_call_id = str(metadata.get("retry_of_tool_call_id") or "").strip()
    if retry_tool_call_id:
        parts.append(f"工具调用：{retry_tool_call_id}")
    retry_turn_id = str(metadata.get("retry_of_turn_id") or "").strip()
    if retry_turn_id:
        parts.append(f"原轮次：{retry_turn_id}")
    retry_tool_status = str(metadata.get("retry_tool_status") or "").strip()
    if retry_tool_status:
        parts.append(f"工具状态：{retry_tool_status}")
    retry_tool_error = str(metadata.get("retry_tool_error") or "").strip()
    if retry_tool_error:
        parts.append(f"工具错误：{retry_tool_error}")
    retry_tool_args = metadata.get("retry_tool_args")
    formatted_args = _format_value(retry_tool_args)
    if formatted_args and formatted_args != "{}":
        parts.append("工具参数：\n" + formatted_args)
    retry_after_tool_name = str(metadata.get("retry_after_tool_name") or "").strip()
    if retry_after_tool_name:
        parts.append(f"续答起点工具：{retry_after_tool_name}")
    retry_after_tool_record_id = str(
        metadata.get("retry_after_tool_record_id") or ""
    ).strip()
    if retry_after_tool_record_id:
        parts.append(f"续答起点工具记录：{retry_after_tool_record_id}")
    retry_after_tool_call_id = str(
        metadata.get("retry_after_tool_call_id") or ""
    ).strip()
    if retry_after_tool_call_id:
        parts.append(f"续答起点工具调用：{retry_after_tool_call_id}")
    retry_after_turn_id = str(metadata.get("retry_after_turn_id") or "").strip()
    if retry_after_turn_id:
        parts.append(f"续答原轮次：{retry_after_turn_id}")
    return parts


def _retry_source_label(source: str) -> str:
    labels = {
        "initial": "首次执行",
        "message_retry": "消息重试",
        "tool_failure": "工具失败点重试",
        "tool_result": "工具结果后续答重试",
    }
    return labels.get(str(source or "").strip(), str(source or "").strip())


def _append_mode_label(mode: str) -> str:
    labels = {
        "next_turn": "下一轮",
        "append_current_turn": "追加当前轮",
        "draft": "草稿",
    }
    return labels.get(str(mode or "").strip(), str(mode or "").strip())


def _attempt_details(attempts: list) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for attempt in attempts:
        parts = [
            f"尝试：{attempt.attempt_id}",
            f"序号：{attempt.attempt_index}",
            f"状态：{_enum_value(attempt.status)}",
        ]
        if getattr(attempt, "retry_source", ""):
            parts.append(f"重试来源：{attempt.retry_source}")
        if getattr(attempt, "retry_of_attempt_id", ""):
            parts.append(f"重试自尝试：{attempt.retry_of_attempt_id}")
        if getattr(attempt, "turn_id", ""):
            parts.append(f"轮次：{attempt.turn_id}")
        if getattr(attempt, "started_at", ""):
            parts.append(f"开始：{attempt.started_at}")
        if getattr(attempt, "completed_at", ""):
            parts.append(f"结束：{attempt.completed_at}")
        metadata = _format_value(getattr(attempt, "metadata", {}) or {})
        if metadata and metadata != "{}":
            parts.append("元数据：\n" + metadata)
        details.append(
            {
                "kind": "attempt",
                "title": f"尝试 {attempt.attempt_index} · {_enum_value(attempt.status)}",
                "text": "\n".join(parts),
                "attempt_id": attempt.attempt_id,
                "message_id": attempt.message_id,
                "turn_id": getattr(attempt, "turn_id", ""),
            }
        )
    return details


def _tool_details(tools: list[ConversationToolCallRecord]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for tool in tools:
        parts = [
            f"工具：{tool.tool_name or 'tool'}",
            f"状态：{_enum_value(tool.status)}",
            f"工具调用：{tool.tool_call_id}",
            f"记录：{tool.tool_record_id}",
        ]
        if tool.step:
            parts.append(f"步骤：{tool.step}")
        if tool.approval_request_id:
            parts.append(f"审批：{tool.approval_request_id}")
        if tool.child_turn_id:
            parts.append(f"子轮次：{tool.child_turn_id}")
        if tool.retryable:
            parts.append("可重试：是")
        if tool.error:
            parts.append(f"错误：{tool.error}")
        tool_metadata = dict(getattr(tool, "metadata", {}) or {})
        recovery_runtime_missing = bool(tool_metadata.get("recovery_runtime_missing"))
        recovery_reason = str(tool_metadata.get("recovery_reason") or "").strip()
        recovered_at = str(tool_metadata.get("recovered_at") or "").strip()
        if recovery_runtime_missing:
            parts.append("恢复状态：运行已不在")
        if recovery_reason:
            parts.append(f"恢复原因：{recovery_reason}")
        if recovered_at:
            parts.append(f"恢复时间：{recovered_at}")
        args = _format_value(tool.args)
        if args and args != "{}":
            parts.append("参数：\n" + args)
        output = _format_value(tool.result)
        if output:
            parts.append("结果：\n" + output)
        metadata = _format_value(tool.metadata)
        if metadata and metadata != "{}":
            parts.append("元数据：\n" + metadata)
        details.append(
            {
                "kind": "tool",
                "title": f"工具 {tool.tool_name or 'tool'} · {_enum_value(tool.status)}",
                "text": "\n".join(parts),
                "tool_record_id": tool.tool_record_id,
                "tool_call_id": tool.tool_call_id,
                "turn_id": tool.turn_id,
                "recovery_runtime_missing": recovery_runtime_missing,
                "recovery_reason": recovery_reason,
                "recovered_at": recovered_at,
            }
        )
    return details


def _approval_details(approvals: list) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for approval in approvals:
        parts = [
            f"审批：{approval.approval_id}",
            f"工具：{approval.tool_name or approval.tool_call_id}",
            f"状态：{_enum_value(approval.status)}",
        ]
        if approval.tool_record_id:
            parts.append(f"工具记录：{approval.tool_record_id}")
        if approval.tool_call_id:
            parts.append(f"工具调用：{approval.tool_call_id}")
        if approval.action:
            parts.append(f"动作：{approval.action}")
        if approval.decision:
            parts.append(f"决定：{approval.decision}")
        if approval.scope:
            parts.append(f"范围：{approval.scope}")
        if approval.feedback:
            parts.append(f"反馈：{approval.feedback}")
        detail = _format_value(approval.detail)
        if detail:
            parts.append("详情：\n" + detail)
        modified_args = _format_value(approval.modified_args)
        if modified_args and modified_args != "{}":
            parts.append("改后参数：\n" + modified_args)
        details.append(
            {
                "kind": "approval",
                "title": f"审批 {approval.tool_name or approval.tool_call_id} · {_enum_value(approval.status)}",
                "text": "\n".join(parts),
                "approval_id": approval.approval_id,
                "tool_record_id": approval.tool_record_id,
                "tool_call_id": approval.tool_call_id,
                "turn_id": approval.turn_id,
            }
        )
    return details


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)
