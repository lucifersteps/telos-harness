"""onboarding 用的 macOS 权限检测/申请（ctypes 直调系统框架，无第三方依赖）。

与 ax_collector 的 PyObjC 加载器不同：onboarding 进程轻量、不构建 runtime，引入
PyObjC bundle 加载得不偿失，故这里用 ctypes 直接 dlopen 框架并调用无参 Boolean
函数。框架或符号缺失、调用异常一律归一为 "unknown"，绝不抛给路由层。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import platform
import subprocess
from functools import lru_cache
from typing import Any

State = str  # "granted" | "denied" | "unknown"

PERMISSION_KINDS = ("screen_recording", "accessibility")

# 辅助功能系统设置面板 URL（无法程序化触发授权弹窗，只能开面板）。
ACCESSIBILITY_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)


def is_macos() -> bool:
    return platform.system() == "Darwin"


@lru_cache(maxsize=None)
def _framework(name: str) -> ctypes.CDLL | None:
    path = ctypes.util.find_library(name) or f"/System/Library/Frameworks/{name}.framework/{name}"
    try:
        return ctypes.CDLL(path)
    except OSError:
        return None


def _boolean_call(framework: str, symbol: str) -> bool | None:
    """调用框架里无参、返回 Boolean 的函数；不可用/异常返回 None。"""
    lib = _framework(framework)
    if lib is None:
        return None
    try:
        func = getattr(lib, symbol)
    except AttributeError:
        return None
    func.restype = ctypes.c_bool
    func.argtypes = []
    try:
        return bool(func())
    except Exception:
        return None


def _state(value: bool | None) -> State:
    if value is None:
        return "unknown"
    return "granted" if value else "denied"


def screen_recording_state() -> State:
    """CGPreflightScreenCaptureAccess（macOS 10.15+）：仅检测，不触发弹窗。"""
    return _state(_boolean_call("CoreGraphics", "CGPreflightScreenCaptureAccess"))


def request_screen_recording() -> State:
    """CGRequestScreenCaptureAccess：未授权时触发系统授权弹窗，返回当前状态。"""
    return _state(_boolean_call("CoreGraphics", "CGRequestScreenCaptureAccess"))


def accessibility_state() -> State:
    """AXIsProcessTrusted：辅助功能权限检测（授权只能由用户在系统设置里勾选）。"""
    return _state(_boolean_call("ApplicationServices", "AXIsProcessTrusted"))


def open_accessibility_settings() -> bool:
    """打开辅助功能系统设置面板；open 缺失/超时/异常均兜底 False，绝不抛。"""
    try:
        subprocess.run(["open", ACCESSIBILITY_SETTINGS_URL], timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def permissions_status() -> dict[str, Any]:
    """两项权限的当前状态。初始设置向导与设置页共用。"""
    return {
        "supported": is_macos(),
        "screen_recording": screen_recording_state(),
        "accessibility": accessibility_state(),
    }


def request_permission(kind: str) -> dict[str, Any]:
    """发起系统授权。kind 由调用方先校验，非 macOS 返回 supported=False。"""
    if not is_macos():
        return {"supported": False}
    if kind == "screen_recording":
        return {"supported": True, "kind": kind, "state": request_screen_recording()}
    if accessibility_state() == "granted":
        return {"supported": True, "kind": kind, "state": "granted"}
    # 辅助功能无法程序化触发授权弹窗，只能打开系统设置面板；失败兜底 unknown。
    opened = open_accessibility_settings()
    return {"supported": True, "kind": kind, "state": "unknown", "opened_settings": opened}
