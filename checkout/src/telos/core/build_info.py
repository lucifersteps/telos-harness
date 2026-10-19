"""App 版本与构建标识。

版本号来自包元数据，由 `scripts/check_versions.py` 统一维护。构建标识由
`desktop/build-sidecar.sh` 在打包时生成 `telos.core._build`，内容是构建时间和当次
提交的短哈希；直接从源码运行时该模块不存在，构建标识为空，界面显示「开发运行」。
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as package_version


@dataclass(frozen=True)
class BuildInfo:
    version: str
    # 形如 20260729-1724，取构建时的本地时间，用来区分同一版本号的多次打包。
    build_id: str
    # 构建时的提交短哈希；工作区有未提交改动时带 "-dirty" 后缀。
    commit: str
    built_at: str


def _package_version() -> str:
    try:
        return package_version("telos")
    except PackageNotFoundError:
        return "0.0.0"


def build_info() -> BuildInfo:
    try:
        from telos.core import _build  # type: ignore[attr-defined]
    except ImportError:
        return BuildInfo(version=_package_version(), build_id="", commit="", built_at="")
    return BuildInfo(
        version=_package_version(),
        build_id=str(getattr(_build, "BUILD_ID", "")),
        commit=str(getattr(_build, "COMMIT", "")),
        built_at=str(getattr(_build, "BUILT_AT", "")),
    )
