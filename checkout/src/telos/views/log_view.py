"""日志查询层：读运行时共享 JSONL 日志文件的尾部，给 web 控制台展示。

web 进程和 timeline collector 子进程都把日志（JSON 每行一条）写进同一个文件
（见 `core/logging.ensure_log_file_handler`）。HTTP 层只跟这个 view 说话，前端轮询
`/api/log/tail` 就能在浏览器里看到「截图 / 送审 / recap」等运行时活动。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LogView:
    log_file: Path

    def tail(self, limit: int = 400) -> list[dict[str, Any]]:
        """返回最后 `limit` 条可解析的日志记录（升序，最旧在前）。

        文件不存在或为空返回 `[]`；坏行（非 JSON）跳过。
        """
        if limit <= 0:
            return []
        try:
            text = Path(self.log_file).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return []
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(record, dict):
                records.append(record)
        return records[-limit:]
