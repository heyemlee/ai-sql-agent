from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List

from .authorization import enforce
from .data_time import QUERY_TIME, now_rfc3339
from .objects import MCPExecutionRequest, MCPExecutionResult
from .sql_guard import check_readonly_sql, readonly_sqlite_uri


class SQLiteMCPExecutor:
    """
    SQLite MCP 执行器。

    这里模拟 MCP 路由到数据库 API 后的执行过程，并落实执行治理：
    - 只读约束（双防线）：应用层用 sql_guard 校验语句，连接层用
      `file:...?mode=ro` URI 打开只读连接，写操作在数据库层同样失败；
    - 行数上限：单条查询最多返回 max_rows 行，超出即截断并标记；
    - 执行超时：通过 progress handler 中止超过 max_execution_seconds 的查询。
    """

    dialect = "sqlite"

    # progress handler 回调间隔（虚拟机指令数），值越小超时检查越频繁
    _PROGRESS_HANDLER_INTERVAL = 10_000

    def __init__(
        self,
        database: str,
        db_path: str | Path,
        timeout: int = 30,
        readonly: bool = True,
        max_rows: int = 1000,
        max_execution_seconds: float = 10.0,
        as_of_source: str = QUERY_TIME,
    ):
        self.database = database
        self.db_path = str(db_path)
        self.timeout = timeout
        self.readonly = readonly
        self.max_rows = max_rows
        self.max_execution_seconds = max_execution_seconds
        self.as_of_source = as_of_source
        """默认 query_time（读主库）。挂只读副本或快照库时改成 snapshot。"""

    def execute(self, request: MCPExecutionRequest) -> MCPExecutionResult:
        """
        执行 SQL。
        """
        denied = enforce(request, self.dialect)
        if denied is not None:
            return denied

        if request.database != self.database:
            return MCPExecutionResult(
                database=request.database,
                sql=request.sql,
                success=False,
                error=f"数据库路由错误：当前执行器只处理 {self.database}",
            )

        sql = request.sql.strip()

        if self.readonly:
            guard = check_readonly_sql(sql)
            if not guard.allowed:
                return MCPExecutionResult(
                    database=request.database,
                    sql=sql,
                    success=False,
                    error=guard.reason,
                )

        deadline = time.monotonic() + self.max_execution_seconds

        conn = None
        try:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            conn.set_progress_handler(
                lambda: 1 if time.monotonic() > deadline else 0,
                self._PROGRESS_HANDLER_INTERVAL,
            )

            # 时点在真正执行 SQL 之前打：读到的就是这一刻的库状态
            as_of = now_rfc3339()
            cursor = conn.execute(sql)
            # 多取一行以区分“刚好取满”和“被截断”
            rows = cursor.fetchmany(self.max_rows + 1)
            truncated = len(rows) > self.max_rows
            rows = rows[: self.max_rows]

            columns = list(rows[0].keys()) if rows else []
            result_rows: List[Dict[str, Any]] = [dict(row) for row in rows]

            return MCPExecutionResult(
                database=request.database,
                sql=sql,
                success=True,
                columns=columns,
                rows=result_rows,
                row_count=len(result_rows),
                truncated=truncated,
                row_limit=self.max_rows,
                as_of=as_of,
                as_of_source=self.as_of_source,
            )

        except sqlite3.OperationalError as exc:
            message = str(exc)
            if "interrupt" in message.lower():
                message = (
                    f"查询执行超时（超过 {self.max_execution_seconds} 秒），已中止。"
                )
            return MCPExecutionResult(
                database=request.database,
                sql=sql,
                success=False,
                error=message,
            )

        except Exception as exc:
            return MCPExecutionResult(
                database=request.database,
                sql=sql,
                success=False,
                error=str(exc),
            )

        finally:
            if conn is not None:
                conn.close()

    def _connect(self) -> sqlite3.Connection:
        """
        建立数据库连接。

        readonly=True 时用 `file:...?mode=ro` URI 打开，让只读约束真正落到
        数据库层——本地对应生产环境的只读账号。
        """
        if not self.readonly:
            return sqlite3.connect(self.db_path, timeout=self.timeout)

        uri = readonly_sqlite_uri(self.db_path)
        if uri is None:
            raise sqlite3.OperationalError(
                f"数据库文件不存在，只读模式不会自动创建：{self.db_path}"
            )
        return sqlite3.connect(uri, uri=True, timeout=self.timeout)
