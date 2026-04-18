from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .dialects import POSTGRES
from .objects import MCPExecutionRequest, MCPExecutionResult
from .sql_guard import check_readonly_sql


class PostgresMCPExecutor:
    """
    PostgreSQL 执行器。

    与 SQLiteMCPExecutor 同接口，执行治理对齐同一套规则，但每条都落到
    PostgreSQL 自己的机制上：

    - 只读（双防线）：应用层 sql_guard 校验语句 + 连接层
      `SET TRANSACTION READ ONLY`（生产上再叠加只读账号）；
    - 执行超时：会话级 `statement_timeout`，由数据库负责中断，
      不依赖应用轮询；
    - 行数上限：服务端游标按需取数，多取一行用于判断是否被截断，
      避免把整张表拉进内存。
    """

    dialect = POSTGRES.name

    def __init__(
        self,
        database: str,
        dsn: str,
        connect_timeout: int = 10,
        readonly: bool = True,
        max_rows: int = 1000,
        max_execution_seconds: float = 10.0,
    ):
        self.database = database
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.readonly = readonly
        self.max_rows = max_rows
        self.max_execution_seconds = max_execution_seconds

    def execute(self, request: MCPExecutionRequest) -> MCPExecutionResult:
        """执行 SQL。"""
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

        conn = None
        try:
            import psycopg

            conn = psycopg.connect(
                self.dsn,
                connect_timeout=self.connect_timeout,
                autocommit=False,
            )
            timeout_ms = int(self.max_execution_seconds * 1000)
            conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
            if self.readonly:
                conn.execute("SET TRANSACTION READ ONLY")

            started = time.perf_counter()
            with conn.cursor(name=f"ai_sql_{int(started * 1000)}") as cursor:
                cursor.itersize = min(self.max_rows, 1000)
                cursor.execute(sql)
                # 多取一行以区分「刚好取满」和「被截断」
                raw_rows = cursor.fetchmany(self.max_rows + 1)
                columns = [desc.name for desc in (cursor.description or [])]

            truncated = len(raw_rows) > self.max_rows
            raw_rows = raw_rows[: self.max_rows]
            rows: List[Dict[str, Any]] = [
                dict(zip(columns, row)) for row in raw_rows
            ]

            return MCPExecutionResult(
                database=request.database,
                sql=sql,
                success=True,
                columns=columns,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
            )

        except Exception as exc:  # psycopg 未安装或执行失败都走这里
            return MCPExecutionResult(
                database=request.database,
                sql=sql,
                success=False,
                error=self._describe_error(exc),
            )

        finally:
            if conn is not None:
                conn.rollback()
                conn.close()

    def _describe_error(self, exc: Exception) -> str:
        message = str(exc).strip()
        if "statement timeout" in message.lower() or "canceling statement" in message.lower():
            return f"查询执行超时（超过 {self.max_execution_seconds} 秒），已中止。"
        if "read-only" in message.lower():
            return "只读事务中不允许写操作。"
        return message
