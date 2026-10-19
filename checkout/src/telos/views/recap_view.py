"""Recap 查询层：把所有 session 的滚动摘要连成"单条跨 session 的连续流"。

前端从上往下按时间渲染；相邻项 session_id 变化处画一条浅灰分隔提示。不做 session 选择器
——recap 是"把一切在低分辨率下讲成一条故事"。HTTP 层只跟这个 view 说话。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from telos.memory_space.recap import RecapRepository


@dataclass(frozen=True)
class RecapItem:
    recap_id: str
    session_id: str
    source_seq_start: int
    source_seq_end: int
    title: str
    content: str
    created_at: str
    agent_name: str


@dataclass(frozen=True)
class RecapView:
    memory_space_dir: Path

    def stream(self) -> list[RecapItem]:
        repo = RecapRepository.for_memory_space(self.memory_space_dir)
        try:
            records = repo.list()  # 全部 recap，按 created_at 升序
        finally:
            repo.close()
        return [
            RecapItem(
                recap_id=record.recap_id,
                session_id=record.session_id,
                source_seq_start=record.source_seq_start,
                source_seq_end=record.source_seq_end,
                title=record.title,
                content=record.content,
                created_at=record.created_at,
                agent_name=record.agent_name,
            )
            for record in records
        ]
