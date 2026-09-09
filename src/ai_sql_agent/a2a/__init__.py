"""
A2A 服务端：把 ai-sql-agent 暴露成一个 Agent（Agent Card + JSON-RPC `/a2a`）。

契约在独立仓库 a2a-contract 的 `a2a-streaming-contract.md`（v1.3）。本包实现
`message/send` 与 `message/stream`；`tasks/cancel`、`tasks/resubscribe`、push
notifications 刻意不做（契约 §7）——查询是同步 pipeline，没有取消点。

流式进度的唯一来源是 `ai_sql_agent.core.trace.stage()` 的事件汇（契约 §8.2）：
pipeline 侧零改动，所有阶段本来就都穿过那一个上下文管理器。
"""
from .server import router, build_agent_card

__all__ = ["router", "build_agent_card"]
