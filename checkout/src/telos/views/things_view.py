"""Things 查询层：把 ThingsRepository 的树状 list() 结果拍平成工作台事项，供 web
常用视图、领域树和日/周/月日历渲染。HTTP 层只跟这个 view 说话，不碰 sqlite。

设计取自 Things/飞书日历：一条 item 落在它的 `when`（scheduled_at / start_at）那天，
或在没有 when 但有 deadline 时落在 deadline 那天；重复项由仓库按窗口展开成逐日
occurrence。容器（domain/list）单独给出，前端据此稳定配色。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from telos.core.clock import user_now
from telos.tools.things.repository import ThingsRepository


@dataclass(frozen=True)
class ThingsItem:
    id: str
    type: str  # task | event
    title: str
    status: str
    position: int
    when: str  # scheduled_at / start_at（"YYYY-MM-DD" 或含时刻），无则 ""
    duration_minutes: int
    deadline: str
    occurrence_date: str  # 重复项展开出的那天；非重复项为 ""
    flags: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    domain_id: str = ""
    domain_title: str = ""
    list_id: str = ""
    list_title: str = ""
    section_id: str = ""
    section_title: str = ""
    location: str = ""
    end_at: str = ""


@dataclass(frozen=True)
class ThingsContainer:
    id: str
    type: str  # domain | list | section
    title: str
    count: int
    position: int = 0
    domain_id: str = ""
    domain_title: str = ""
    list_id: str = ""
    list_title: str = ""
    status: str = "open"


@dataclass(frozen=True)
class ThingsAgenda:
    start: str
    end: str
    status: str
    items: list[ThingsItem] = field(default_factory=list)


@dataclass(frozen=True)
class ThingsBrowse:
    status: str
    items: list[ThingsItem] = field(default_factory=list)
    containers: list[ThingsContainer] = field(default_factory=list)


@dataclass(frozen=True)
class ThingsOverview:
    open: int = 0
    scheduled_today: int = 0
    overdue: int = 0
    deadline_passed: int = 0
    completed_today: int = 0


class ThingsView:
    """事项工作台查询与用户直接写入；每次操作独立打开并关闭仓库。"""

    def __init__(self, agent_dir: Path):
        self.agent_dir = Path(agent_dir)

    def agenda(self, *, start: str, end: str, status: str = "open") -> ThingsAgenda:
        """[start, end] 窗口内、按 when 或 deadline 命中的 task/event（含重复展开）。"""
        filters: dict[str, Any] = {
            "type": ["task", "event"],
            "scheduled_in": {"from": start, "to": end},
            "due_in": {"from": start, "to": end},
            "include_overdue": False,
            "include_anytime": False,
            "include_deferred": False,
            "limit": 2000,
        }
        items = self._items_for_status(filters, status)
        return ThingsAgenda(start=start, end=end, status=_clean_status(status), items=items)

    def unscheduled(self, *, status: str = "open") -> list[ThingsItem]:
        """没有具体日期的池子：anytime / someday / suspended。"""
        filters: dict[str, Any] = {
            "type": ["task", "event"],
            "schedule": ["anytime", "someday", "suspended"],
            "limit": 500,
        }
        return self._items_for_status(filters, status)

    def browse(self, *, status: str = "open") -> ThingsBrowse:
        """事项工作台左侧使用的全量集合，不受日历日期窗口限制。"""
        filters: dict[str, Any] = {
            "type": ["task", "event"],
            "schedule": ["scheduled", "anytime", "someday", "suspended"],
            "limit": 5000,
        }
        clean_status = _clean_status(status)
        container_kinds = (
            ("domain", "list", "section")
            if clean_status == "archived"
            else ("domain", "list")
        )
        return ThingsBrowse(
            status=clean_status,
            items=self._items_for_status(filters, status),
            containers=self.containers(container_kinds, status=status),
        )

    def overview(self, *, day: str = "") -> ThingsOverview:
        """首页概览使用的事项计数，日期按用户时区解释。"""
        target_day = str(day or "").strip()[:10] or user_now().date().isoformat()
        open_items = self._items_for_status(
            {
                "type": ["task", "event"],
                "schedule": ["scheduled", "anytime", "someday", "suspended"],
                "limit": 5000,
            },
            "open",
        )
        today_items = self.agenda(
            start=target_day,
            end=target_day,
            status="open",
        ).items
        repo = ThingsRepository.for_agent_dir(self.agent_dir)
        try:
            completed = self._flatten(
                repo.list(
                    {
                        "type": ["task"],
                        "status": "completed",
                        "completed_in": {"from": target_day, "to": target_day},
                        "limit": 5000,
                    }
                )
            )
        finally:
            repo.close()
        return ThingsOverview(
            open=sum(1 for item in open_items if item.type == "task"),
            scheduled_today=sum(
                1 for item in today_items if item.when[:10] == target_day
            ),
            overdue=sum(
                1
                for item in open_items
                if item.type == "task"
                and "scheduled_overdue" in item.flags
                and "deadline_overdue" not in item.flags
            ),
            deadline_passed=sum(
                1
                for item in open_items
                if item.type == "task" and "deadline_overdue" in item.flags
            ),
            completed_today=len(
                {(item.id, item.occurrence_date) for item in completed}
            ),
        )

    def detail(self, item_id: str) -> dict[str, Any] | None:
        """单项完整信息（含描述/位置/标签/截止/清单路径/重复/依赖），供详情面板。"""
        repo = ThingsRepository.for_agent_dir(self.agent_dir)
        try:
            result = repo.detail([{"id": str(item_id)}])
        finally:
            repo.close()
        items = result.get("items") or []
        return items[0] if items else None

    def containers(
        self,
        kinds: tuple[str, ...] = ("domain", "list"),
        *,
        status: str = "open",
    ) -> list[ThingsContainer]:
        """指定状态的容器（默认 domain + list），供侧栏和归档页展示。"""
        result: list[ThingsContainer] = []
        clean_status = _clean_status(status)
        repository_status = clean_status if clean_status in {"archived", "all"} else "open"
        parent_titles: dict[str, tuple[str, str]] = {}
        if any(kind in {"list", "section"} for kind in kinds):
            parent_payload = self._list({"type": ["domain", "list"], "status": "all", "limit": 1000})
            parent_titles = {
                str(node.get("id") or ""): (
                    str(node.get("type") or ""),
                    str(node.get("title") or ""),
                )
                for node in parent_payload.get("items") or []
            }
        for kind in kinds:
            payload = self._list({"type": [kind], "status": repository_status, "limit": 500})
            for node in payload.get("items") or []:
                result.append(
                    ThingsContainer(
                        id=str(node.get("id") or ""),
                        type=str(node.get("type") or kind),
                        title=str(node.get("title") or ""),
                        count=int(node.get("count") or 0),
                        position=int(node.get("position") or 0),
                        domain_id=str(node.get("domain_id") or ""),
                        domain_title=parent_titles.get(str(node.get("domain_id") or ""), ("", ""))[1],
                        list_id=str(node.get("list_id") or ""),
                        list_title=parent_titles.get(str(node.get("list_id") or ""), ("", ""))[1],
                        status=str(node.get("status") or repository_status),
                    )
                )
        return result

    def form_meta(self) -> dict[str, Any]:
        """建/改表单所需的级联选择数据：domain›list›section 树 + 开启态任务（供
        parent_task / blocked_by 依赖选择器）和全部未删除事项/日程标签。"""
        containers = self.containers(("domain", "list", "section"))
        domains = [{"id": c.id, "title": c.title} for c in containers if c.type == "domain"]
        lists = [
            {"id": c.id, "title": c.title, "domain_id": c.domain_id}
            for c in containers
            if c.type == "list"
        ]
        sections = [
            {"id": c.id, "title": c.title, "list_id": c.list_id}
            for c in containers
            if c.type == "section"
        ]
        # schedule 全档 → 绕过 list() 的默认 7 天窗口，拿到全部开启态 task（供依赖选择）。
        tasks = [
            {"id": item.id, "title": item.title, "list_id": item.list_id}
            for item in self._flatten(
                self._list(
                    {
                        "type": ["task"],
                        "status": "open",
                        "schedule": ["scheduled", "anytime", "someday", "suspended"],
                        "limit": 500,
                    }
                )
            )
        ]
        tag_items = self._items_for_status(
            {
                "type": ["task", "event"],
                "schedule": ["scheduled", "anytime", "someday", "suspended"],
                "limit": 5000,
            },
            "all",
        )
        tags = sorted({tag for item in tag_items for tag in item.tags}, key=str.casefold)
        archived_items = self._items_for_status(
            {
                "type": ["task", "event"],
                "schedule": ["scheduled", "anytime", "someday", "suspended"],
                "limit": 5000,
            },
            "archived",
        )
        archived_containers = self.containers(("domain", "list", "section"), status="archived")
        return {
            "domains": domains,
            "lists": lists,
            "sections": sections,
            "tasks": tasks,
            "tags": tags,
            "archived_count": len(archived_items) + len(archived_containers),
        }

    def create(self, item: dict[str, Any]) -> dict[str, Any]:
        """直连写入：把表单 item dict 交给仓库创建（人在 GUI 的直接操作，不经 agent 审批）。"""
        repo = ThingsRepository.for_agent_dir(self.agent_dir)
        try:
            return repo.create([dict(item or {})])
        finally:
            repo.close()

    def update(self, item_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """直连更新：patch 里省略的字段不改，传 null 清空可清空字段。"""
        payload = {k: v for k, v in dict(patch or {}).items() if k != "target"}
        payload["target"] = {"id": str(item_id)}
        repo = ThingsRepository.for_agent_dir(self.agent_dir)
        try:
            return repo.update([payload])
        finally:
            repo.close()

    def set_status(self, item_id: str, status: str) -> dict[str, Any]:
        """完成 / 归档 / 重开：走 update 的 status 字段。"""
        return self.update(item_id, {"status": status})

    def delete(self, item_id: str) -> dict[str, Any]:
        """通过专用入口把对象标记为 deleted。"""
        repo = ThingsRepository.for_agent_dir(self.agent_dir)
        try:
            return repo.delete([{"id": str(item_id)}])
        finally:
            repo.close()

    def restore_deleted(self, item_id: str, status: str = "open") -> dict[str, Any]:
        """恢复 deleted 对象到删除前状态。"""
        repo = ThingsRepository.for_agent_dir(self.agent_dir)
        try:
            return repo.restore_deleted([{"id": str(item_id), "status": status}])
        finally:
            repo.close()

    def _list(self, filters: dict[str, Any]) -> dict[str, Any]:
        repo = ThingsRepository.for_agent_dir(self.agent_dir)
        try:
            return repo.list(filters)
        finally:
            repo.close()

    def _items_for_status(self, filters: dict[str, Any], status: str) -> list[ThingsItem]:
        items: list[ThingsItem] = []
        seen: set[tuple[str, str, str, str]] = set()
        for value in _repository_statuses(status):
            payload = dict(filters)
            payload["status"] = value
            for item in self._flatten(self._list(payload)):
                key = (item.id, item.occurrence_date, item.when, item.status)
                if key in seen:
                    continue
                seen.add(key)
                items.append(item)
        return items

    def _flatten(self, payload: dict[str, Any]) -> list[ThingsItem]:
        """把 domain/list/section 树拍平成带容器上下文的 item 列表。"""
        items: list[ThingsItem] = []
        for node in payload.get("items") or []:
            _walk_tree(node, domain=None, list_ref=None, section=None, out=items)
        return items


def _walk_tree(
    node: dict[str, Any],
    *,
    domain: dict[str, str] | None,
    list_ref: dict[str, str] | None,
    section: dict[str, str] | None,
    out: list[ThingsItem],
) -> None:
    node_type = str(node.get("type") or "")
    ctx = {"id": str(node.get("id") or ""), "title": str(node.get("title") or "")}
    if node_type == "domain":
        domain = ctx
    elif node_type == "list":
        list_ref = ctx
    elif node_type == "section":
        section = ctx
    for leaf in node.get("items") or []:
        out.append(_item_from_leaf(leaf, domain, list_ref, section))
    for child in node.get("children") or []:
        _walk_tree(child, domain=domain, list_ref=list_ref, section=section, out=out)


def _item_from_leaf(
    leaf: dict[str, Any],
    domain: dict[str, str] | None,
    list_ref: dict[str, str] | None,
    section: dict[str, str] | None,
) -> ThingsItem:
    duration = leaf.get("duration")
    minutes = int(duration.get("minutes") or 0) if isinstance(duration, dict) else 0
    return ThingsItem(
        id=str(leaf.get("id") or ""),
        type=str(leaf.get("type") or ""),
        title=str(leaf.get("title") or ""),
        status=str(leaf.get("status") or "open"),
        position=int(leaf.get("position") or 0),
        when=str(leaf.get("when") or ""),
        duration_minutes=minutes,
        deadline=str(leaf.get("deadline") or ""),
        occurrence_date=str(leaf.get("occurrence_date") or ""),
        flags=[str(flag) for flag in (leaf.get("flags") or [])],
        tags=[str(tag) for tag in (leaf.get("tags") or [])],
        domain_id=(domain or {}).get("id", ""),
        domain_title=(domain or {}).get("title", ""),
        list_id=(list_ref or {}).get("id", ""),
        list_title=(list_ref or {}).get("title", ""),
        section_id=(section or {}).get("id", ""),
        section_title=(section or {}).get("title", ""),
        location=str(leaf.get("location") or ""),
        end_at=str(leaf.get("end_at") or ""),
    )


def _clean_status(status: str) -> str:
    text = str(status or "").strip() or "open"
    return text if text in {"open", "completed", "archived", "all", "visible"} else "open"


def _repository_statuses(status: str) -> tuple[str, ...]:
    clean = _clean_status(status)
    if clean == "visible":
        return ("open", "completed")
    return (clean,)


def default_window(days: int = 7) -> tuple[str, str]:
    """给未带参的调用一个合理默认：今天起 N 天。"""
    today = user_now().date()
    return today.isoformat(), (today + timedelta(days=max(1, days))).isoformat()
