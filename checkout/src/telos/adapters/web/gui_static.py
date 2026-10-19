"""server 与 onboarding 共享的 GUI 构建产物静态服务（避免两 app 实现漂移）。"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

STATIC_DIR = Path(__file__).resolve().parent / "static"
GUI_APP_DIR = STATIC_DIR / "gui" / "app"
GUI_APP_MISSING_TEXT = "Telos GUI 构建产物缺失，请先构建 web 前端（static/gui/app）。"


def gui_app_response(path: str) -> FileResponse:
    root = GUI_APP_DIR.resolve()
    requested = str(path or "").strip().lstrip("/")
    candidate = (root / requested).resolve()
    if requested:
        if not candidate.is_relative_to(root):
            raise HTTPException(status_code=404, detail="GUI 文件不存在")
        if candidate.is_file():
            cache_control = (
                "no-store"
                if candidate.name == "index.html"
                else "public, max-age=31536000, immutable"
            )
            return FileResponse(str(candidate), headers={"Cache-Control": cache_control})
        if requested.startswith("assets/") or Path(requested).suffix:
            raise HTTPException(status_code=404, detail="GUI 文件不存在")

    index = root / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="GUI 尚未构建")
    return FileResponse(str(index), headers={"Cache-Control": "no-store"})
