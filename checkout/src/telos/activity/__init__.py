"""Activity graph: timeline run details 之上的活动层。

事实库是智能体目录下的 `runtime/activity.sqlite`，`repository.py` 是它的仓储，
`assign.py` 是快环路，`consolidate.py` 是慢环路，`projection.py` 把改动同步到
memory space 的活动投影。

设计见 `tasks/activity-graph-v2.md`，术语见 `docs/development/technical-naming.md` §5。
"""
