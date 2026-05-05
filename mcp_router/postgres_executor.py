from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ai_sql_core import get_settings

from .authorization import enforce
from .data_time import QUERY_TIME, now_rfc3339
from .dialects import POSTGRES
from .pool import BoundedConnectionPool
from .objects import MCPExecutionRequest, MCPExecutionResult
from .sql_guard import check_readonly_sql


def _current_user_id() -> str:
    """当前授权作用域里的最终用户。没有作用域时返回空串（fail closed）。"""
    from ai_sql_auth.enforcement import current_scope

    scope = current_scope()
    return scope.user_id if scope is not None else ""


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
        as_of_source: str = QUERY_TIME,
        pool_max_connections: Optional[int] = None,
        pool_idle_seconds: Optional[float] = None,
    ):
        self.database = database
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.readonly = readonly
        self.max_rows = max_rows
        self.max_execution_seconds = max_execution_seconds
        self.as_of_source = as_of_source
        """默认 query_time（读主库）。DSN 指向只读副本或数仓时改成 snapshot。"""

        settings = get_settings()
        self._pool = BoundedConnectionPool(
            factory=self._connect,
            max_size=(
                settings.db_pool_max_connections
                if pool_max_connections is None
                else pool_max_connections
            ),
            idle_seconds=(
                settings.db_pool_idle_seconds
                if pool_idle_seconds is None
                else pool_idle_seconds
            ),
        )

    def _connect(self):
        import psycopg

        return psycopg.connect(
            self.dsn, connect_timeout=self.connect_timeout, autocommit=False
        )

    def close(self) -> None:
        """释放池里的空闲连接。进程退出或重建执行器时调用。"""
        self._pool.close_all()

    def execute(self, request: MCPExecutionRequest) -> MCPExecutionResult:
        """执行 SQL。"""
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

        conn = None
        pooled = False
        reusable = True
        try:
            conn, pooled = self._pool.acquire()
            timeout_ms = int(self.max_execution_seconds * 1000)
            conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
            if self.readonly:
                conn.execute("SET TRANSACTION READ ONLY")

            # 把最终用户身份放进事务，让行级安全（RLS）在**经由 agent 发起的
            # 查询**上真的生效——不然 RLS 只在有人手工连库时管用。
            #
            # SET LOCAL 是事务级的，随 rollback 一起还原，所以连接池复用不会串身份。
            # 没有授权作用域时设成空串：RLS 的
            # `USING (owner_id = current_setting('app.user_id', true))` 对空串
            # 不匹配任何行——fail closed，与执行器那道关卡方向一致。
            conn.execute(
                "SELECT set_config('app.user_id', %s, true)", (_current_user_id(),)
            )

            started = time.perf_counter()
            # 时点在真正执行 SQL 之前打：读到的就是这一刻的库状态
            as_of = now_rfc3339()
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
                row_limit=self.max_rows,
                as_of=as_of,
                as_of_source=self.as_of_source,
            )

        except Exception as exc:  # psycopg 未安装或执行失败都走这里
            # 出过错的连接不还回池：它可能停在一个半死的事务里，
            # 下一个人拿到它会收到一条与自己无关的错误，最难排查的那种。
            reusable = False
            return MCPExecutionResult(
                database=request.database,
                sql=sql,
                success=False,
                error=self._describe_error(exc),
            )

        finally:
            if conn is not None:
                # 事务级设置（statement_timeout / READ ONLY）随 rollback 一起还原，
                # 所以归还的连接是干净的，下一次执行会重新设一遍。
                try:
                    conn.rollback()
                except Exception:
                    reusable = False
                self._pool.release(conn, pooled, reusable=reusable)

    def _describe_error(self, exc: Exception) -> str:
        message = str(exc).strip()
        if "statement timeout" in message.lower() or "canceling statement" in message.lower():
            return f"查询执行超时（超过 {self.max_execution_seconds} 秒），已中止。"
        if "read-only" in message.lower():
            return "只读事务中不允许写操作。"
        return message
