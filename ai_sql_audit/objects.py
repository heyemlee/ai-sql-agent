from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AuditRecord:
    """
    一次查询的审计记录。

    内部自用系统同样需要事后追溯：谁、什么时候、问了什么、系统生成了
    什么 SQL、碰了哪些表、成功没有、烧了多少 token。出问题时用
    trace_id 就能把日志和这条记录对上。
    """

    trace_id: str
    caller: str
    user_id: str
    session_id: str
    query: str

    route: str = ""
    databases: List[str] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)
    sqls: List[Dict[str, str]] = field(default_factory=list)
    """每步实际执行的 {database, sql}；跨库查询各步的库不同，不能只存 SQL。"""
    denied_tables: List[str] = field(default_factory=list)

    success: bool = True
    degraded: bool = False
    """模型不可用时走了降级链路。"""

    status_code: int = 200
    failed_steps: int = 0
    repaired_steps: int = 0

    duration_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    error: Optional[str] = None
    created_at: str = field(default_factory=utc_now)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "created_at": self.created_at,
            "caller": self.caller,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "query": self.query,
            "route": self.route,
            "databases": self.databases,
            "tables": self.tables,
            "sqls": self.sqls,
            "denied_tables": self.denied_tables,
            "success": self.success,
            "degraded": self.degraded,
            "status_code": self.status_code,
            "failed_steps": self.failed_steps,
            "repaired_steps": self.repaired_steps,
            "duration_ms": self.duration_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "error": self.error,
        }


@dataclass
class DailyUsage:
    """某个用户当天的用量，用于配额判断。"""

    user_id: str
    queries: int = 0
    tokens: int = 0
