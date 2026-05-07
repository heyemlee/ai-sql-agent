from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MCPExecutionRequest:
    """
    MCP 执行请求。

    database 用于路由到对应数据库连接。
    sql 是当前步骤生成的 SQL。
    """

    database: str
    sql: str


@dataclass
class MCPExecutionResult:
    """
    MCP 执行结果。
    """

    database: str
    sql: str
    success: bool
    columns: List[str] = field(default_factory=list)
    rows: List[Dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    error: Optional[str] = None
    truncated: bool = False
    """结果行数达到 max_rows 上限并被截断时为 True。"""

    row_limit: Optional[int] = None
    """
    这次执行生效的行数上限（契约 §4.6.3）。

    `truncated=True` 时**必须**有值：调用方要能分清「10000 行封顶」和「被临时
    调小的上限截了」——两者对「共有 N 条」这句话的杀伤力不一样。
    """

    as_of: str = ""
    """
    数据时点，RFC 3339（契约 §4.6.4）。SQL 真正执行的时刻，由执行器打点。

    读主库时它就是「现在」；接了只读副本或数仓之后，主从延迟会让「最新状态」
    变成几分钟前的状态，而链路上原来没有任何字段能让调用方发现这件事。
    """

    as_of_source: str = ""
    """`query_time` / `snapshot` / `unknown`（契约 §4.6.4）。"""

    def to_dict(self) -> Dict[str, Any]:
        """转换为可序列化字典。"""
        return {
            "database": self.database,
            "sql": self.sql,
            "success": self.success,
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "error": self.error,
            "truncated": self.truncated,
            "row_limit": self.row_limit,
            "as_of": self.as_of,
            "as_of_source": self.as_of_source,
        }
