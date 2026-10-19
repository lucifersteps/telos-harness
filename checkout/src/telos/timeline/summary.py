"""Timeline summarization runner."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telos.agent.context.template import render_prompt_template
from telos.agent.loop.request import build_chat_request
from telos.core.clock import parse_user_time, user_now_iso
from telos.core.ids import generate_id
from telos.core.retry import RetryPolicy
from telos.memory_space.paths import is_memory_space_uri
from telos.memory_space.runs import (
    RUN_TRACE_VERSION,
    run_context_message_item,
    sanitize_run_error,
    sanitize_run_retry_events,
    start_run_context_file,
)
from telos.memory_space.timeline import (
    FAILED_SUMMARY,
    NormalizedTimelineResponse,
    TimelineSummaryRecord,
    TimelineSummaryRepository,
    decode_timeline_response_object,
    link_summary_screenshot,
    normalize_timeline_response,
    timeline_run_context_uri,
    timeline_run_json_uri,
)
from telos.providers.base import (
    PROMPT_CACHE_KEY_METADATA,
    ChatProvider,
    ChatRequest,
    Message,
    message_text,
)
from telos.providers.registry import create_provider
from telos.storage.filesystem import write_json_atomic
from telos.storage.jsonl import append_jsonl, read_jsonl
from telos.storage.paths import default_source_root
from telos.timeline.repository import (
    TimelineCapture,
    TimelineRepository,
    TimelineSession,
)

logger = logging.getLogger(__name__)
PROCESSING_SESSION_STALE_SECONDS = 30 * 60

DEFAULT_SUMMARY_PROMPT_PATH = default_source_root() / "profiles" / "timeline_summary_prompt.md"

_AX_OBJECT_RE = re.compile(r"\s*<AXUIElement\s+0x[0-9a-fA-F]+>\s*\{pid=\d+\}\s*")
_DROP_CONTENT_PREFIXES = (
    "App:",
    "Window:",
    "AXWindow:",
    "AXGroup:",
    "AXToolbar:",
    "AXTabGroup:",
    "AXScrollBar:",
)
_DROP_CONTENT_LINES = {
    "AXButton: 此按钮也可以执行缩放窗口的操作",
    "AXButton: this button also has an action to zoom the window",
}
_TIMELINE_MERGE_INSTRUCTION = (
    "上一批与本批属于同一个时间线活动组，且上一批时间早于本批。"
    "以上是上次总结的 summary 和 details 内容，请将它们与当前待总结内容合并，"
    "输出一个完整的 summary 和 details。"
    "如果上次总结的最后一个 detail 与待总结内容相关，可以合并为一条。"
)


@dataclass(frozen=True)
class TimelineSummaryResult:
    timeline_id: str
    detail_count: int
    capture_count: int
    failed: bool = False


@dataclass(frozen=True)
class _ValidatedTimelineSummary:
    response: str
    normalized: NormalizedTimelineResponse
    attempt_count: int
    retry_events: list[dict[str, Any]]
    usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class _TimelineMergeContext:
    record: TimelineSummaryRecord
    app_name: str
    previous_output: str
    screenshot_ids: list[str]
    capture_ids_by_screenshot_id: dict[str, str]


class _TimelineSummaryRetryExhausted(Exception):
    def __init__(
        self,
        *,
        cause: Exception,
        attempt_count: int,
        retry_events: list[dict[str, Any]],
        response: str = "",
        usage: dict[str, Any] | None = None,
    ):
        super().__init__(str(cause))
        self.cause = cause
        self.attempt_count = int(attempt_count or 0)
        self.retry_events = list(retry_events)
        self.response = str(response or "")
        self.usage = dict(usage) if isinstance(usage, dict) else None


async def run_due_timeline_summary(
    settings,
    *,
    provider: ChatProvider | None = None,
) -> int:
    """消费全部待送审的活动组，返回本次产出的 timeline run 数。

    每个 worker 持有独立的 sqlite 连接并循环领取活动组，直到没有可领取的组为止。
    活动组级别的锁在 `claim_next_ready_sessions_for_summary` 中，见其 docstring。
    """
    timeline = settings.daemon.timeline
    if not timeline.enabled or not timeline.summary.enabled:
        return 0
    shared_provider = provider or create_provider(
        settings.providers,
        timeline.summary.provider or None,
    )
    worker_count = max(1, int(timeline.summary.max_concurrency))
    counts = await asyncio.gather(
        *(
            _drain_timeline_summaries(settings, provider=shared_provider)
            for _ in range(worker_count)
        )
    )
    return sum(counts)


async def _drain_timeline_summaries(settings, *, provider: ChatProvider) -> int:
    timeline = settings.daemon.timeline
    produced = 0
    with TimelineRepository.for_agent_dir(
        settings.paths.agent_dir,
    ) as runtime_repository, TimelineSummaryRepository.for_memory_space(
        settings.paths.memory_space_dir
    ) as summary_repository:
        while True:
            result = await summarize_next_timeline_summary(
                runtime_repository,
                summary_repository=summary_repository,
                settings=settings,
                provider=provider,
                retry_policy=timeline.summary.retry,
            )
            if result is None:
                return produced
            produced += 1
            runtime_repository.set_state(
                "summary",
                {
                    "last_run_at": user_now_iso(),
                    "last_timeline_id": result.timeline_id,
                    "last_detail_count": result.detail_count,
                    "last_failed": result.failed,
                },
            )


async def summarize_next_timeline_summary(
    runtime_repository: TimelineRepository,
    *,
    summary_repository: TimelineSummaryRepository,
    settings,
    provider: ChatProvider,
    retry_policy: RetryPolicy,
) -> TimelineSummaryResult | None:
    timeline = settings.daemon.timeline
    released_count = runtime_repository.release_stale_processing_sessions(
        stale_seconds=PROCESSING_SESSION_STALE_SECONDS,
    )
    if released_count:
        logger.warning(
            "释放卡住的 timeline summary 会话 | count=%d",
            released_count,
        )
    sessions = runtime_repository.claim_next_ready_sessions_for_summary()
    if not sessions:
        return None
    target_captures = _select_pending_captures(
        runtime_repository,
        sessions=sessions,
        limit=timeline.summary.max_captures_per_batch,
    )
    if not target_captures:
        for session in sessions:
            runtime_repository.complete_session(session_id=session.id, summary_id="")
        return None

    # A batch may span multiple ready sessions; _select_pending_captures concatenates
    # them in session order, not global time order. If any sessions overlap in time,
    # target_captures[-1] would not be the latest capture, understating source_end while
    # later captures are still rendered into the prompt — the model then emits detail
    # start_times past source_end and every validation retry fails. Sort chronologically so
    # the bounds are the true min/max and the prompt stays monotonic.
    target_captures = sorted(target_captures, key=lambda capture: (capture.timestamp, capture.seq))

    source_start = target_captures[0].timestamp
    source_end = target_captures[-1].timestamp
    app_name = _common_app_name(target_captures) or sessions[0].app_name
    screenshot_ids_by_capture_id = _screenshot_ids_for_captures(target_captures)
    timeline_id = generate_id("timeline")
    started_at = user_now_iso()
    provider_name = str(getattr(provider, "name", "") or "")
    provider_model = str(getattr(provider, "model", "") or "")
    request = build_chat_request(
        messages=[],
        tools=[],
        tool_schemas=[],
        metadata=_timeline_request_metadata(settings),
    )
    merge_context: _TimelineMergeContext | None = None
    record_start = source_start

    try:
        _link_consumed_screenshots(
            settings=settings,
            captures=target_captures,
        )
        previous_summary_id = _previous_group_summary_id(
            runtime_repository.session_group([session.id for session in sessions]),
            claimed_session_ids=[session.id for session in sessions],
        )
        merge_context = _previous_timeline_merge_context(
            summary_repository,
            runtime_repository=runtime_repository,
            timeline_id=previous_summary_id,
            source_start_time=source_start,
            settings=settings,
        )
        record_start = (
            merge_context.record.start_time if merge_context is not None else source_start
        )
        system_prompt = render_timeline_summary_prompt(
            prompt_template=_read_prompt_template(timeline.summary.prompt_template),
            target_captures=target_captures,
            app_name=app_name,
            screenshot_ids_by_capture_id=screenshot_ids_by_capture_id,
            previous_screenshot_ids=merge_context.screenshot_ids
            if merge_context is not None
            else [],
        )
        messages = _timeline_request_messages(
            target_captures,
            screenshot_ids_by_capture_id=screenshot_ids_by_capture_id,
            settings=settings,
            merge_context=merge_context,
        )
        request = build_chat_request(
            messages=messages,
            tools=[],
            tool_schemas=[],
            metadata={
                **_timeline_request_metadata(settings),
                "run_id": timeline_id,
            },
            system_prompt=system_prompt,
        )
        summary = await _validated_timeline_summary_with_retry(
            provider,
            request=request,
            capture_ids_by_screenshot_id=_capture_ids_by_screenshot_id(
                screenshot_ids_by_capture_id,
                merge_context=merge_context,
            ),
            source_end_time=_timeline_prompt_time(source_end),
            retry_policy=retry_policy,
            timeout_seconds=timeline.summary.timeout_seconds,
        )
        record = summary_repository.append(
            timeline_id=timeline_id,
            app_name=app_name or summary.normalized.app,
            start_time=record_start,
            end_time=source_end,
            summary=summary.normalized.summary,
            details=summary.normalized.details,
        )
        _write_timeline_run_files(
            run_dir=settings.paths.memory_space_dir / "timeline" / "runs" / timeline_id,
            timeline_id=timeline_id,
            start_time=record_start,
            end_time=source_end,
            provider_name=provider_name,
            provider_model=provider_model,
            request=request,
            status="completed",
            started_at=started_at,
            attempt_count=summary.attempt_count,
            retry_events=summary.retry_events,
            captures=target_captures,
            settings=settings,
            assistant_message=Message(role="assistant", content=summary.response),
            usage=summary.usage,
        )
        failed = False
        detail_count = len(summary.normalized.details)
    except _TimelineSummaryRetryExhausted as error:
        logger.exception("timeline summary failed after retries")
        record = summary_repository.append_failure(
            timeline_id=timeline_id,
            app_name=app_name,
            start_time=source_start,
            end_time=source_end,
        )
        assistant_message = (
            Message(role="assistant", content=error.response) if error.response else None
        )
        _write_timeline_run_files(
            run_dir=settings.paths.memory_space_dir / "timeline" / "runs" / timeline_id,
            timeline_id=timeline_id,
            start_time=source_start,
            end_time=source_end,
            provider_name=provider_name,
            provider_model=provider_model,
            request=request,
            status="failed",
            started_at=started_at,
            attempt_count=error.attempt_count,
            retry_events=error.retry_events,
            captures=target_captures,
            settings=settings,
            assistant_message=assistant_message,
            usage=error.usage,
            error=str(error.cause or error),
        )
        failed = True
        detail_count = 0
    except Exception as error:
        logger.exception("timeline summary failed")
        record = summary_repository.append_failure(
            timeline_id=timeline_id,
            app_name=app_name,
            start_time=source_start,
            end_time=source_end,
        )
        _write_timeline_run_files(
            run_dir=settings.paths.memory_space_dir / "timeline" / "runs" / timeline_id,
            timeline_id=timeline_id,
            start_time=source_start,
            end_time=source_end,
            provider_name=provider_name,
            provider_model=provider_model,
            request=request,
            status="failed",
            started_at=started_at,
            attempt_count=1 if request.messages else 0,
            retry_events=[],
            captures=target_captures,
            settings=settings,
            error=str(error),
        )
        failed = True
        detail_count = 0

    runtime_repository.mark_captures_summarized(
        capture_ids=[capture.id for capture in target_captures],
        summary_id=record.timeline_id,
    )
    if failed:
        consumed_session_ids = {
            capture.session_id for capture in target_captures if capture.session_id
        }
        for session in sessions:
            if session.id in consumed_session_ids:
                runtime_repository.fail_session(
                    session_id=session.id,
                    summary_id=record.timeline_id,
                )
            else:
                runtime_repository.release_session(
                    session_id=session.id,
                    summary_id=session.summary_id,
                )
    else:
        if merge_context is not None:
            runtime_repository.replace_summary_id(
                old_summary_id=merge_context.record.timeline_id,
                new_summary_id=record.timeline_id,
            )
        _finish_claimed_sessions(
            runtime_repository,
            sessions=sessions,
            summary_id=record.timeline_id,
        )
        if merge_context is not None:
            summary_repository.delete(merge_context.record.timeline_id)
    return TimelineSummaryResult(
        timeline_id=record.timeline_id,
        detail_count=detail_count,
        capture_count=len(target_captures),
        failed=failed,
    )


async def _validated_timeline_summary_with_retry(
    provider: ChatProvider,
    *,
    request: ChatRequest,
    capture_ids_by_screenshot_id: dict[str, str],
    source_end_time: str,
    retry_policy: RetryPolicy | None = None,
    timeout_seconds: float = 120.0,
) -> _ValidatedTimelineSummary:
    policy = retry_policy or RetryPolicy()
    max_attempts = max(1, int(policy.max_attempts))
    attempt = 1
    retry_events: list[dict[str, Any]] = []
    while True:
        stage = "provider"
        response = ""
        usage: dict[str, Any] | None = None
        try:
            response, usage = await _provider_text_with_timeout(
                provider,
                request=request,
                timeout_seconds=timeout_seconds,
            )
            stage = "validation"
            normalized = normalize_timeline_response(
                response,
                capture_ids_by_screenshot_id=capture_ids_by_screenshot_id,
                source_end_time=source_end_time,
            )
            return _ValidatedTimelineSummary(
                response=response,
                normalized=normalized,
                attempt_count=attempt,
                retry_events=retry_events,
                usage=usage,
            )
        except Exception as error:
            if attempt >= max_attempts:
                raise _TimelineSummaryRetryExhausted(
                    cause=error,
                    attempt_count=attempt,
                    retry_events=retry_events,
                    response=response if stage == "validation" else "",
                    usage=usage,
                ) from error
            delay = policy.delay_for_attempt(attempt)
            retry_event = _timeline_summary_retry_event(
                attempt=attempt,
                next_attempt=attempt + 1,
                delay_seconds=delay,
                stage=stage,
                error=error,
            )
            retry_events.append(retry_event)
            _log_timeline_summary_retry(
                retry_event=retry_event,
            )
            if delay > 0:
                await asyncio.sleep(delay)
            attempt += 1


def _select_pending_captures(
    repository: TimelineRepository,
    *,
    sessions: list[TimelineSession],
    limit: int,
) -> list[TimelineCapture]:
    selected: list[TimelineCapture] = []
    for session in sessions:
        captures = repository.list_session_captures(
            session.id,
            include_duplicates=False,
            pending_only=True,
        )
        if not captures:
            continue
        if limit > 0:
            remaining = limit - len(selected)
            if remaining <= 0:
                break
            captures = captures[:remaining]
        selected.extend(captures)
        if limit > 0 and len(selected) >= limit:
            break
    return selected


def _finish_claimed_sessions(
    repository: TimelineRepository,
    *,
    sessions: list[TimelineSession],
    summary_id: str,
) -> None:
    for session in sessions:
        remaining = repository.list_session_captures(
            session.id,
            include_duplicates=False,
            pending_only=True,
        )
        if remaining:
            repository.release_session(session_id=session.id, summary_id=summary_id)
        else:
            repository.complete_session(session_id=session.id, summary_id=summary_id)


def render_timeline_summary_prompt(
    *,
    prompt_template: str,
    target_captures: list[TimelineCapture],
    app_name: str = "",
    screenshot_ids_by_capture_id: dict[str, str] | None = None,
    previous_screenshot_ids: list[str] | None = None,
) -> str:
    return render_prompt_template(
        prompt_template,
        {
            "app_name": str(app_name or "").strip(),
            "capture_count": len(target_captures),
            "screenshot_ids": _render_prompt_screenshot_ids(
                target_captures,
                screenshot_ids_by_capture_id=screenshot_ids_by_capture_id,
                previous_screenshot_ids=previous_screenshot_ids,
            ),
        },
        section="timeline.summary",
    ).strip()


def _render_prompt_screenshot_ids(
    captures: list[TimelineCapture],
    *,
    screenshot_ids_by_capture_id: dict[str, str] | None,
    previous_screenshot_ids: list[str] | None = None,
) -> str:
    fallback_ids = _screenshot_ids_for_captures(captures)
    configured_ids = dict(screenshot_ids_by_capture_id or {})
    lines: list[str] = []
    seen: set[str] = set()
    for screenshot_id in list(previous_screenshot_ids or []):
        clean_id = str(screenshot_id or "").strip()
        if clean_id and clean_id not in seen:
            lines.append(f"- {clean_id}")
            seen.add(clean_id)
    for capture in captures:
        screenshot_id = str(
            configured_ids.get(capture.id) or fallback_ids.get(capture.id) or ""
        ).strip()
        if screenshot_id and screenshot_id not in seen:
            lines.append(f"- {screenshot_id}")
            seen.add(screenshot_id)
    return "\n".join(lines)


def _timeline_request_metadata(settings) -> dict[str, str]:
    agent_id = str(getattr(settings.paths, "agent_id", "") or "").strip() or "default"
    return {
        "run_type": "timeline_summary",
        "memory_space_root": str(settings.paths.memory_space_dir),
        "runtime_timeline_screenshots_root": str(
            settings.paths.agent_dir / "runtime" / "timeline" / "screenshots"
        ),
        PROMPT_CACHE_KEY_METADATA: f"timeline_summary:{agent_id}",
    }


async def _provider_text(
    provider: ChatProvider,
    *,
    request: ChatRequest,
) -> tuple[str, dict[str, Any] | None]:
    assistant, usage = await _provider_message(
        provider,
        request=request,
    )
    return message_text(assistant.content).strip(), usage


async def _provider_text_with_timeout(
    provider: ChatProvider,
    *,
    request: ChatRequest,
    timeout_seconds: float,
) -> tuple[str, dict[str, Any] | None]:
    timeout = max(0.001, float(timeout_seconds))
    try:
        return await asyncio.wait_for(
            _provider_text(provider, request=request),
            timeout=timeout,
        )
    except TimeoutError as error:
        raise TimeoutError(
            f"timeline summary provider timed out after {timeout:g}s"
        ) from error


def _timeline_request_messages(
    captures: list[TimelineCapture],
    *,
    screenshot_ids_by_capture_id: dict[str, str],
    settings,
    merge_context: _TimelineMergeContext | None = None,
) -> list[Message]:
    return [
        Message(
            role="user",
            content=_prompt_content_parts(
                captures,
                screenshot_ids_by_capture_id=screenshot_ids_by_capture_id,
                settings=settings,
                merge_context=merge_context,
            ),
        )
    ]


def _write_timeline_run_files(
    *,
    run_dir: Path,
    timeline_id: str,
    start_time: str,
    end_time: str,
    provider_name: str,
    provider_model: str,
    request: ChatRequest,
    status: str,
    started_at: str,
    attempt_count: int,
    retry_events: list[dict[str, Any]],
    captures: list[TimelineCapture],
    settings,
    assistant_message: Message | None = None,
    usage: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    safe_error = sanitize_run_error(error)
    safe_retry_events = sanitize_run_retry_events(retry_events)
    context_path = start_run_context_file(
        run_dir,
        system_prompt=request.system_prompt,
        system_prompt_source="timeline_system_prompt",
    )
    current_prompt_index = max(0, len(request.messages) - 1)
    for index, message in enumerate(request.messages):
        append_jsonl(
            context_path,
            run_context_message_item(
                message,
                source="timeline_prompt"
                if index == current_prompt_index
                else "timeline_context",
                content=_memory_safe_timeline_content(message.content, settings=settings),
            ),
        )
    if assistant_message is not None:
        append_jsonl(
            context_path,
            run_context_message_item(assistant_message, source="assistant"),
        )
    if safe_error:
        append_jsonl(context_path, {"type": "error", "error": safe_error})

    write_json_atomic(
        run_dir / "run.json",
        {
            "version": RUN_TRACE_VERSION,
            "run_id": timeline_id,
            "run_type": "timeline_summary",
            "status": status,
            "source_start_time": str(start_time),
            "source_end_time": str(end_time),
            "context_uri": timeline_run_context_uri(timeline_id),
            "run_uri": timeline_run_json_uri(timeline_id),
            "started_at": started_at,
            "finished_at": user_now_iso(),
            "provider": {
                "name": provider_name,
                "model": provider_model,
            },
            "attempt_count": int(attempt_count or 0),
            "retry_events": safe_retry_events,
            "usage": dict(usage) if isinstance(usage, dict) else None,
            "capture_count": len(captures),
            "error": safe_error,
        },
    )


def _memory_safe_timeline_content(content: object, *, settings) -> object:
    if not isinstance(content, list):
        return content
    safe_parts: list[object] = []
    for item in content:
        if not isinstance(item, dict):
            safe_parts.append(item)
            continue
        clean = dict(item)
        if clean.get("type") == "image_file":
            uri = _timeline_screenshot_uri_for_provider_path(
                str(clean.get("path") or ""),
                settings=settings,
            )
            clean["path"] = uri
            if not uri:
                clean["unresolved_path"] = True
        safe_parts.append(clean)
    return safe_parts


def _timeline_screenshot_uri_for_provider_path(path: str, *, settings) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    if is_memory_space_uri(text):
        return text
    source = Path(text).expanduser().resolve()
    runtime_root = (
        settings.paths.agent_dir / "runtime" / "timeline" / "screenshots"
    ).resolve()
    try:
        relative = source.relative_to(runtime_root)
    except ValueError:
        return ""
    return f"memory_space://timeline/screenshots/{relative.as_posix()}"


def _capture_ids_by_screenshot_id(
    screenshot_ids_by_capture_id: dict[str, str],
    *,
    merge_context: _TimelineMergeContext | None,
) -> dict[str, str]:
    """把提示词里出现的 screenshot_id 映射回 capture_id，含被合并的上一批。"""
    mapping = dict(merge_context.capture_ids_by_screenshot_id) if merge_context else {}
    for capture_id, screenshot_id in screenshot_ids_by_capture_id.items():
        clean = str(screenshot_id or "").strip()
        if clean:
            mapping[clean] = str(capture_id)
    return mapping


def _previous_timeline_merge_context(
    summary_repository: TimelineSummaryRepository,
    *,
    runtime_repository: TimelineRepository,
    timeline_id: str,
    source_start_time: str,
    settings,
) -> _TimelineMergeContext | None:
    clean_id = str(timeline_id or "").strip()
    if not clean_id:
        return None
    record = summary_repository.get(clean_id)
    if record is None or str(record.summary or "").strip() == FAILED_SUMMARY:
        raise ValueError("时间线活动组的上一批摘要不可用")
    try:
        previous_end = parse_user_time(record.end_time)
        current_start = parse_user_time(source_start_time)
    except ValueError as error:
        raise ValueError("时间线活动组的摘要时间无效") from error
    if previous_end > current_start:
        raise ValueError("时间线活动组的上一批结束时间晚于当前批开始时间")
    context = _timeline_merge_context_from_record(
        record,
        runtime_repository=runtime_repository,
        settings=settings,
    )
    if context is None:
        raise ValueError("时间线活动组的上一批摘要上下文不可用")
    return context


def _previous_group_summary_id(
    group: list[TimelineSession],
    *,
    claimed_session_ids: list[str],
) -> str:
    claimed = {
        str(item or "").strip()
        for item in claimed_session_ids
        if str(item or "").strip()
    }
    if not claimed:
        return ""
    positions = [index for index, session in enumerate(group) if session.id in claimed]
    if not positions:
        raise ValueError("已领取的时间线会话不在对应活动组内")
    current_ids = list(
        dict.fromkeys(
            str(session.summary_id or "").strip()
            for session in group
            if session.id in claimed and str(session.summary_id or "").strip()
        )
    )
    if len(current_ids) > 1:
        raise ValueError("时间线活动组存在多个部分摘要")
    if current_ids:
        return current_ids[0]
    for session in reversed(group[: min(positions)]):
        summary_id = str(session.summary_id or "").strip()
        if session.status == "completed" and summary_id:
            return summary_id
    return ""


def _timeline_merge_context_from_record(
    record: TimelineSummaryRecord,
    *,
    runtime_repository: TimelineRepository,
    settings,
) -> _TimelineMergeContext | None:
    if str(record.summary or "").strip() == FAILED_SUMMARY:
        return None
    context_path = (
        settings.paths.memory_space_dir
        / "timeline"
        / "runs"
        / record.timeline_id
        / "context.jsonl"
    )
    try:
        items = list(read_jsonl(context_path))
    except (OSError, ValueError):
        return None
    assistant = _last_timeline_run_message(items, source="assistant", role="assistant")
    if assistant is None:
        return None
    data = _timeline_response_data(assistant.content)
    if data is None:
        return None
    app_name = str(record.app_name or data.get("app") or "").strip()
    if not app_name:
        return None
    previous_captures = runtime_repository.list_summary_captures(record.timeline_id)
    return _TimelineMergeContext(
        record=record,
        app_name=app_name,
        previous_output=json.dumps(data, ensure_ascii=False, indent=2),
        screenshot_ids=_timeline_response_screenshot_ids(data),
        capture_ids_by_screenshot_id={
            screenshot_id: capture_id
            for capture_id, screenshot_id in _screenshot_ids_for_captures(
                previous_captures
            ).items()
        },
    )


def _last_timeline_run_message(
    items: list[dict[str, Any]],
    *,
    source: str,
    role: str,
) -> Message | None:
    for item in reversed(items):
        if item.get("type") != "message":
            continue
        if str(item.get("source") or "") != source:
            continue
        if str(item.get("role") or "") != role:
            continue
        return _timeline_run_message(item)
    return None


def _timeline_run_message(item: dict[str, Any]) -> Message:
    cache_control = item.get("cache_control")
    return Message(
        role=str(item.get("role") or ""),
        content=item.get("content", ""),
        tool_calls=[
            dict(call)
            for call in list(item.get("tool_calls") or [])
            if isinstance(call, dict)
        ],
        tool_call_id=str(item.get("tool_call_id") or ""),
        name=str(item.get("tool_name") or ""),
        cache_control=dict(cache_control) if isinstance(cache_control, dict) else None,
    )


def _timeline_response_data(content: object) -> dict[str, Any] | None:
    text = message_text(content).strip()
    if not text:
        return None
    try:
        data = decode_timeline_response_object(text)
    except ValueError:
        return None
    return data


def _timeline_response_screenshot_ids(data: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    details = data.get("details")
    if not isinstance(details, list):
        return ids
    for detail in details:
        if not isinstance(detail, dict):
            continue
        screenshot_ids = detail.get("screenshot_ids")
        if not isinstance(screenshot_ids, list):
            continue
        for value in screenshot_ids:
            clean = str(value or "").strip()
            if clean and clean not in seen:
                ids.append(clean)
                seen.add(clean)
    return ids


def _merge_context_prompt_block(context: _TimelineMergeContext) -> str:
    return "\n".join(
        [
            "上次输出:",
            context.previous_output,
            "",
            _TIMELINE_MERGE_INSTRUCTION,
            "",
            "当前待总结内容:",
        ]
    )


def _observation_prompt_block(
    capture: TimelineCapture,
    *,
    screenshot_id: str,
    seen_content: dict[str, str],
) -> str:
    shot_id = str(screenshot_id or "").strip() or _screenshot_ids_for_captures([capture]).get(
        capture.id,
        "",
    )
    lines = [
        f"screenshot_id: {shot_id}",
        f"time: {_timeline_prompt_time(capture.timestamp)}",
        f"trigger: {_normalize_trigger(capture.trigger)}",
        f"app: {capture.app_name or 'null'}",
        f"window: {capture.window_title or 'null'}",
    ]
    content = _useful_content(capture)
    if not content:
        lines.append("content: null")
    elif content in seen_content:
        lines.append(f"content: save as screenshot_id: {seen_content[content]}")
    else:
        seen_content[content] = shot_id
        lines.append("content: |")
        lines.extend(f"  {line}" for line in content.splitlines())
    return "\n".join(lines)


def _prompt_content_parts(
    captures: list[TimelineCapture],
    *,
    screenshot_ids_by_capture_id: dict[str, str],
    settings,
    merge_context: _TimelineMergeContext | None = None,
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if merge_context is not None:
        parts.append(
            {
                "type": "text",
                "text": _merge_context_prompt_block(merge_context),
            }
        )
    seen_content: dict[str, str] = {}
    for capture in captures:
        screenshot_id = screenshot_ids_by_capture_id.get(capture.id, "")
        parts.append(
            {
                "type": "text",
                "text": _observation_prompt_block(
                    capture,
                    screenshot_id=screenshot_id,
                    seen_content=seen_content,
                ),
            }
        )
        path = Path(capture.screenshot_path).expanduser()
        if not capture.screenshot_path or not path.is_file():
            continue
        screenshot_uri = _timeline_screenshot_uri_for_provider_path(
            str(path),
            settings=settings,
        )
        if not screenshot_uri:
            continue
        parts.append(
            {
                "type": "image_file",
                "path": screenshot_uri,
                "mime_type": _screenshot_mime_type(path),
            }
        )
    return parts


def _link_consumed_screenshots(*, settings, captures: list[TimelineCapture]) -> None:
    runtime_root = settings.paths.agent_dir / "runtime" / "timeline" / "screenshots"
    memory_root = settings.paths.memory_space_dir / "timeline" / "screenshots"
    for capture in captures:
        if not capture.screenshot_path:
            continue
        source = Path(capture.screenshot_path).expanduser()
        if not source.is_file():
            continue
        try:
            source.resolve().relative_to(runtime_root.resolve())
        except ValueError:
            continue
        link_summary_screenshot(
            runtime_screenshot_path=source,
            memory_screenshots_dir=memory_root,
            runtime_screenshots_dir=runtime_root,
        )


def _screenshot_ids_for_captures(captures: list[TimelineCapture]) -> dict[str, str]:
    seen: dict[str, int] = {}
    ids: dict[str, str] = {}
    for capture in captures:
        base = _screenshot_id(capture)
        count = seen.get(base, 0) + 1
        seen[base] = count
        ids[capture.id] = base if count == 1 else f"{base}_{count}"
    return ids


def _screenshot_id(capture: TimelineCapture) -> str:
    timestamp = str(capture.timestamp or "").strip()
    try:
        parsed = parse_user_time(timestamp)
        return f"shot_{parsed:%Y%m%d_%H%M%S}"
    except ValueError:
        clean = re.sub(r"[^0-9A-Za-z]+", "_", timestamp).strip("_")
        return f"shot_{clean or capture.seq or capture.id}"


def _timeline_prompt_time(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = parse_user_time(text)
    except ValueError:
        return text.replace("T", " ")
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _useful_content(capture: TimelineCapture) -> str:
    seen_values: set[str] = set()
    lines: list[str] = []
    for raw_line in str(capture.ax_text or "").splitlines():
        line = _clean_content_line(raw_line)
        if not line:
            continue
        if line in _DROP_CONTENT_LINES or _has_drop_content_prefix(line):
            continue
        value = _content_value(line)
        if not value or value in seen_values:
            continue
        seen_values.add(value)
        lines.append(line)
    return "\n".join(lines).strip()


def _clean_content_line(line: str) -> str:
    cleaned = _AX_OBJECT_RE.sub("", str(line or "")).strip()
    return re.sub(r":(?=\S)", ": ", cleaned)


def _has_drop_content_prefix(line: str) -> bool:
    return any(line.startswith(prefix) for prefix in _DROP_CONTENT_PREFIXES)


def _content_value(line: str) -> str:
    if ":" in line:
        return line.split(":", 1)[1].strip()
    return line.strip()


def _normalize_trigger(value: str) -> str:
    mapping = {
        "app_focus": "change",
        "quiet_gap": "change",
        "enter_key": "enter",
    }
    text = str(value or "").strip()
    return mapping.get(text, text)


def _common_app_name(captures: list[TimelineCapture]) -> str:
    names = [capture.app_name for capture in captures if capture.app_name]
    if not names:
        return ""
    first = names[0]
    return first if all(name == first for name in names) else ""


async def _provider_message(
    provider: ChatProvider,
    *,
    request: ChatRequest,
) -> tuple[Message, dict[str, Any] | None]:
    chunks: list[str] = []
    final_message: Message | None = None
    usage: dict[str, Any] | None = None
    async for event in provider.chat(request):
        if event.type == "text_delta" and event.payload is not None:
            chunks.append(str(event.payload))
        elif event.type == "message" and isinstance(event.payload, Message):
            final_message = event.payload
        elif event.type == "usage" and isinstance(event.payload, dict):
            usage = dict(event.payload)
    if final_message is None:
        return Message(role="assistant", content="".join(chunks)), usage
    if chunks and not final_message.content:
        return (
            Message(
                role=final_message.role,
                content="".join(chunks),
                tool_calls=final_message.tool_calls,
                tool_call_id=final_message.tool_call_id,
                name=final_message.name,
                cache_control=final_message.cache_control,
            ),
            usage,
        )
    return final_message, usage


def _timeline_summary_retry_event(
    *,
    attempt: int,
    next_attempt: int,
    delay_seconds: float,
    stage: str,
    error: Exception,
) -> dict[str, Any]:
    return {
        "attempt": int(attempt),
        "next_attempt": int(next_attempt),
        "delay_seconds": float(delay_seconds),
        "stage": str(stage),
        "error_type": type(error).__name__,
        "error": str(error),
    }


def _log_timeline_summary_retry(
    *,
    retry_event: dict[str, Any],
) -> None:
    logger.warning(
        "timeline summary retry | attempt=%d next_attempt=%d delay=%.2f stage=%s error_type=%s error=%s",
        retry_event.get("attempt"),
        retry_event.get("next_attempt"),
        retry_event.get("delay_seconds"),
        retry_event.get("stage"),
        retry_event.get("error_type"),
        retry_event.get("error"),
    )


def _screenshot_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".gif":
        return "image/gif"
    return "image/png" if suffix == ".png" else "image/jpeg"


def _read_prompt_template(path: str) -> str:
    text = str(path or "").strip()
    prompt_path = Path(text).expanduser() if text else DEFAULT_SUMMARY_PROMPT_PATH
    return prompt_path.read_text(encoding="utf-8").strip()
