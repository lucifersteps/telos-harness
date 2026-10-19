"""首次启动引导用的精简 web app（configs/telos.yaml 尚不存在时服务）。

设计要点见 plan：boot 信号是「主 config 是否存在」，而 onboarding 才是 config 的
唯一创建者。因此这个 app **不构建任何 runtime**（无 provider/coordinator/daemon），
只暴露 /api/setup/* 供向导采集 provider/微信/功能，用户点「完成」→ on_commit 原子写
config → 进程自重启走全量路径。中途退出不落 config，下次照常引导。

微信账号是 runtime 副产物（存 agents/main/runtime/...），与 configs 正交，不污染 boot
信号；反而让重进时能显示「已绑定」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from telos.adapters.web.gui_static import (
    GUI_APP_DIR,
    GUI_APP_MISSING_TEXT,
    gui_app_response,
)
from telos.adapters.web.permissions import (
    PERMISSION_KINDS,
    permissions_status,
    request_permission,
)
from telos.adapters.web.wechat_login import WeChatLoginSession
from telos.adapters.wechat.config import WeChatConfig
from telos.adapters.wechat.login import WeChatQrChallenge, run_wechat_qr_login
from telos.adapters.wechat.state import WeChatStateStore
from telos.core.config_bootstrap import ensure_user_config
from telos.core.logging import get_logger
from telos.memory_space.paths import AgentRuntimeLayout
from telos.providers.extra_body import RESERVED_EXTRA_BODY_KEYS
from telos.views.settings_ui import local_apps_payload
from telos.views.settings_view import update_agent_config_file

# provider 类型 → 默认 base_url / 是否需要 key（事实来源：providers/registry.py）。
PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "anthropic": {"base_url": "https://api.anthropic.com/v1", "needs_key": True},
    "openai": {"base_url": "https://api.openai.com/v1", "needs_key": True},
    "openai_responses": {"base_url": "https://api.openai.com/v1", "needs_key": True},
    "kimi": {"base_url": "https://api.moonshot.cn/v1", "needs_key": True},
    "ollama": {"base_url": "http://localhost:11434/v1", "needs_key": False},
    "fake": {"base_url": "", "needs_key": False},
}


def _wechat_state_store(agent_dir: Path) -> WeChatStateStore:
    layout = AgentRuntimeLayout.for_agent_dir(agent_dir)
    return WeChatStateStore(layout.runtime_wechat_dir)


def _positive_int(value: Any) -> int | None:
    """把表单值（数字或数字字符串）解析为正整数；非法返回 None。"""
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def validate_commit_payload(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """校验 commit payload；返回 (field, message) 列表，空 = 通过。"""
    raw = payload.get("providers") or []
    if not isinstance(raw, list):
        return [("providers", "providers 必须是数组")]
    problems: list[tuple[str, str]] = []
    if "providers" in payload and not raw:
        problems.append(("providers", "providers 不能为空数组"))
    for index, item in enumerate(raw):
        field = f"providers.{index}"
        if not isinstance(item, dict):
            problems.append((field, f"第 {index + 1} 个 provider 必须是对象"))
            continue
        ptype = str(item.get("type") or "").strip() or "fake"
        if ptype not in PROVIDER_DEFAULTS:
            problems.append((f"{field}.type", f"未知 provider 类型 {ptype!r}"))
            continue
        if not str(item.get("model") or "").strip():
            problems.append((f"{field}.model", f"{ptype} 需要填写模型名称"))
        if PROVIDER_DEFAULTS[ptype]["needs_key"] and not str(item.get("api_key") or "").strip():
            problems.append((f"{field}.api_key", f"{ptype} 需要 API key"))
        max_tokens = item.get("max_tokens")
        if max_tokens not in (None, "") and _positive_int(max_tokens) is None:
            problems.append((f"{field}.max_tokens", "max_tokens 必须是正整数"))
        extra_body = item.get("extra_body")
        if extra_body not in (None, {}) and not isinstance(extra_body, dict):
            problems.append((f"{field}.extra_body", "extra_body 必须是对象"))
        elif isinstance(extra_body, dict):
            reserved = sorted(RESERVED_EXTRA_BODY_KEYS.intersection(extra_body))
            if reserved:
                problems.append((f"{field}.extra_body", f"extra_body 不能覆盖 {reserved[0]!r}"))
    _validate_timeline_prefixes(payload, problems)
    _validate_assignments(payload, count=len(raw), problems=problems)
    return problems


def _validate_timeline_prefixes(
    payload: dict[str, Any], problems: list[tuple[str, str]]
) -> None:
    field = "features.timeline.exclude_bundle_prefixes"
    features = payload.get("features")
    timeline = features.get("timeline") if isinstance(features, dict) else None
    prefixes = timeline.get("exclude_bundle_prefixes") if isinstance(timeline, dict) else None
    if prefixes is None:
        return
    if not isinstance(prefixes, list) or any(not isinstance(item, str) for item in prefixes):
        problems.append((field, "exclude_bundle_prefixes 必须是字符串数组"))


def _validate_assignments(
    payload: dict[str, Any], *, count: int, problems: list[tuple[str, str]]
) -> None:
    assignments = payload.get("assignments")
    if assignments is None:
        return
    if not isinstance(assignments, dict):
        problems.append(("assignments", "assignments 必须是对象"))
        return
    for key in ("main", "timeline_summary"):
        field = f"assignments.{key}"
        value = assignments.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append((field, f"{field} 必须是整数下标"))
        elif not 0 <= value < count:
            problems.append((field, f"{field} 超出 providers 下标范围"))


def _providers_from_payload(
    raw: Any, *, default_index: int | None = None
) -> tuple[dict[str, Any], list[str]]:
    """多 provider payload → (落盘映射, 下标顺序的唯一化 key 列表)；fake 永驻。

    default 由 assignments.main（default_index）优先决定，其后是 entry 上的
    default:true 标记，最后回退第一个 entry。
    """
    items = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    providers: dict[str, Any] = {}
    keys: list[str] = []
    default_key = ""
    for item in items:
        ptype = str(item.get("type") or "").strip() or "fake"
        key = ptype
        suffix = 2
        while key in providers:
            key = f"{ptype}_{suffix}"
            suffix += 1
        entry: dict[str, Any] = {"type": ptype, "model": str(item.get("model") or "").strip()}
        label = str(item.get("label") or "").strip()
        if label:
            entry["label"] = label
        base_url = str(item.get("base_url") or "").strip()
        if base_url:
            entry["base_url"] = base_url
        api_key = str(item.get("api_key") or "").strip()
        if api_key:
            entry["api_key"] = api_key
        max_tokens = _positive_int(item.get("max_tokens"))
        if max_tokens is not None:
            entry["max_tokens"] = max_tokens
        extra_body = item.get("extra_body")
        if isinstance(extra_body, dict) and extra_body:
            entry["extra_body"] = dict(extra_body)
        providers[key] = entry
        keys.append(key)
        if not default_key and bool(item.get("default")):
            default_key = key
    providers.setdefault("fake", {"type": "fake", "model": "fake-echo"})
    if default_index is not None and 0 <= default_index < len(keys):
        default = keys[default_index]
    else:
        default = default_key or (keys[0] if keys else "fake")
    return {"default": default, **providers}, keys


def _resolve_timeline_summary_key(
    providers: dict[str, Any], keys: list[str], assignments: Any
) -> str:
    """timeline 摘要 provider key：assignments.timeline_summary 优先，否则 main 解析结果。

    该键在模板里指向被整体替换的旧 provider key，onboarding 必须显式写成有效 key。
    """
    index = assignments.get("timeline_summary") if isinstance(assignments, dict) else None
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(keys):
        return keys[index]
    return str(providers["default"])


def write_onboarding_config(config_dir: Path, payload: dict[str, Any]) -> None:
    """onboarding 的唯一 config 落地点：脚手架 + 覆盖用户选择。"""

    ensure_user_config(config_dir)  # profiles/skills 脚手架 + base telos.yaml/agents/main.yaml
    agent_config = Path(config_dir) / "agents" / "main.yaml"

    assignments = payload.get("assignments")
    main_index = assignments.get("main") if isinstance(assignments, dict) else None
    if not isinstance(main_index, int) or isinstance(main_index, bool):
        main_index = None
    providers, keys = _providers_from_payload(
        payload.get("providers") or [], default_index=main_index
    )
    values: dict[str, Any] = {"providers": providers}
    summary_key = _resolve_timeline_summary_key(providers, keys, assignments)
    values["daemon.timeline.summary.provider"] = summary_key
    # 模板里两条环路的 provider 指向模板自带的 key，而 providers 会被向导整体替换，
    # 因此一并改写为向导解析出的屏幕活动摘要 provider，避免指向不存在的配置。
    values["daemon.activity.assign.provider"] = summary_key
    values["daemon.activity.consolidate.provider"] = summary_key
    adapters = ["web"]
    if bool((payload.get("wechat") or {}).get("link")):
        adapters.append("wechat")
    values["adapters.enabled"] = adapters

    features = payload.get("features") or {}
    _apply_timeline(values, features.get("timeline"))
    _apply_activity(values, features.get("activity"))
    # recap 省略时整体继承模板默认，不写任何 memory.recap.* 覆盖。
    if isinstance(features, dict) and features.get("recap") is not None:
        _apply_recap(values, features.get("recap"))

    update_agent_config_file(agent_config, values=values)


def _as_bool(section: Any) -> bool:
    if not isinstance(section, dict):
        return False
    return bool(section.get("enabled"))


def _coerce(section: Any, key: str, cast) -> Any | None:
    if not isinstance(section, dict) or section.get(key) is None:
        return None
    try:
        return cast(section[key])
    except (TypeError, ValueError):
        return None


def _apply_timeline(values: dict[str, Any], section: Any) -> None:
    enabled = _as_bool(section)
    values["daemon.timeline.enabled"] = enabled
    if not enabled:
        return
    poll = _coerce(section, "poll_interval_seconds", float)
    if poll is not None:
        values["daemon.timeline.collector.poll_interval_seconds"] = poll
    summary_on = bool(section.get("summary_enabled")) if isinstance(section, dict) else False
    values["daemon.timeline.summary.enabled"] = summary_on
    if summary_on:
        interval = _coerce(section, "summary_check_interval_seconds", int)
        if interval is not None:
            values["daemon.timeline.summary.check_interval_seconds"] = interval
    prefixes = section.get("exclude_bundle_prefixes") if isinstance(section, dict) else None
    if isinstance(prefixes, list):
        values["daemon.timeline.collector.exclude_bundle_prefixes"] = [
            str(item) for item in prefixes
        ]


def _apply_activity(values: dict[str, Any], section: Any) -> None:
    """活动图谱只写总开关。两条环路的 provider 和参数继承模板默认。"""
    values["daemon.activity.enabled"] = _as_bool(section)


def _apply_recap(values: dict[str, Any], section: Any) -> None:
    enabled = _as_bool(section)
    values["memory.recap.enabled"] = enabled
    if not enabled:
        return
    # recap 仅当 idle_seconds > 0 才真正运行（见 daemon.py），故此处务必写入正值。
    idle = _coerce(section, "idle_seconds", int)
    if idle is not None:
        values["memory.recap.idle_seconds"] = idle
    min_messages = _coerce(section, "min_messages", int)
    if min_messages is not None:
        values["memory.recap.min_messages"] = min_messages


def create_onboarding_app(
    *,
    config_dir: Path,
    agent_dir: Path,
    on_commit: Callable[[dict[str, Any]], None],
) -> FastAPI:
    app = FastAPI(title="Telos Onboarding", docs_url=None, redoc_url=None)
    log = get_logger("telos.apps.web.onboarding")
    login = WeChatLoginSession()

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        # 必须含 provider/surfaces 键以满足桌面壳 looks_like_telos_health 探活。
        return {"ok": True, "provider": None, "surfaces": [], "needs_onboarding": True}

    @app.get("/api/setup/state")
    def setup_state() -> dict[str, Any]:
        account = _wechat_state_store(agent_dir).load_default_account()
        return {
            "wechat": {
                "linked": account is not None,
                "account": account.to_dict() if account is not None else None,
            },
        }

    @app.get("/api/setup/providers")
    def setup_providers() -> dict[str, Any]:
        return {
            "providers": [
                {"type": name, "base_url": meta["base_url"], "needs_key": meta["needs_key"]}
                for name, meta in PROVIDER_DEFAULTS.items()
            ],
        }

    @app.get("/api/setup/local-apps")
    def setup_local_apps() -> dict[str, Any]:
        # 纯扫描 /Applications，无 settings 依赖、无副作用，onboarding 直接复用。
        return {"apps": local_apps_payload()}

    @app.get("/api/setup/permissions")
    def setup_permissions() -> dict[str, Any]:
        return permissions_status()

    @app.post("/api/setup/permissions/request")
    async def setup_permissions_request(request: Request) -> Any:
        payload = await request.json()
        kind = payload.get("kind") if isinstance(payload, dict) else None
        if kind not in PERMISSION_KINDS:
            return JSONResponse(
                status_code=422,
                content={"error": "kind 必须是 screen_recording 或 accessibility", "fields": ["kind"]},
            )
        return request_permission(kind)

    @app.post("/api/setup/wechat/login/start")
    async def wechat_login_start() -> dict[str, Any]:
        # async：asyncio.create_task 需运行中的事件循环（sync 端点会跑在无 loop 的线程池里）。
        if login.task is not None and not login.task.done():
            return login.snapshot()
        login.phase = "starting"
        login.qr_image = ""
        login.account = None
        login.error = ""

        def on_challenge(challenge: WeChatQrChallenge) -> None:
            login.qr_image = challenge.image_url or challenge.scan_data or challenge.qrcode
            login.phase = "waiting"

        async def run() -> None:
            store = _wechat_state_store(agent_dir)
            result = await run_wechat_qr_login(
                WeChatConfig.from_env(),
                state_store=store,
                challenge_callback=on_challenge,
            )
            if result.ok and result.account is not None:
                login.phase = "confirmed"
                login.account = result.account.to_dict()
            else:
                login.phase = "error"
                login.error = result.error or "WeChat login failed"

        login.task = asyncio.create_task(run())
        return login.snapshot()

    @app.get("/api/setup/wechat/login/status")
    def wechat_login_status() -> dict[str, Any]:
        return login.snapshot()

    @app.post("/api/setup/commit")
    async def setup_commit(request: Request) -> Any:
        payload = await request.json()
        if not isinstance(payload, dict):
            return {"ok": False, "error": "invalid payload"}
        problems = validate_commit_payload(payload)
        if problems:
            return JSONResponse(
                status_code=422,
                content={
                    "error": problems[0][1],
                    "fields": [field for field, _ in problems],
                },
            )
        log.info("onboarding commit; writing config to %s", config_dir)
        on_commit(payload)  # 写 config + 触发重启（由 web.py 注入）
        return {"ok": True}

    # 引导页与主应用共用同一套 GUI 构建产物，直接在根路径提供；注册在 /api 之后。
    @app.get("/", include_in_schema=False)
    @app.get("/{path:path}", include_in_schema=False)
    def gui_app(path: str = "") -> Response:
        if not GUI_APP_DIR.is_dir():
            return PlainTextResponse(GUI_APP_MISSING_TEXT)
        return gui_app_response(path)

    return app
