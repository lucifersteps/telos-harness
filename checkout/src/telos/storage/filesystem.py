"""Filesystem storage."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from telos.storage.locks import file_lock


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in {path}: {error}") from error


def write_json_atomic(path: Path, data: Any) -> None:
    with file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            delete=False,
        ) as temp:
            json.dump(data, temp, ensure_ascii=False, indent=2, sort_keys=True)
            temp.write("\n")
            temp_path = Path(temp.name)
        temp_path.replace(path)
