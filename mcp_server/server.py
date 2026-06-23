from __future__ import annotations

import json
from pathlib import Path

from mcp.server import MCPServer

from ai_sql_pipeline.demo_data import (
    create_erp_demo_database,
    create_mes_demo_database,
    create_pm_demo_database,
    create_wms_demo_database,
)
from mcp_router import MCPExecutionRequest, MCPRouter, SQLiteMCPExecutor

server = MCPServer(
    name="ai-sql-agent",
    instructions="AI SQL Agent 只读 SQL 执行服务：先用 list_databases 查看可用数据库，"
    "再用 execute_sql 执行 SELECT / WITH 查询。",
)

# 数据库执行治理参数与 Pipeline 保持一致
_MAX_ROWS = 1000
_MAX_EXECUTION_SECONDS = 10.0

_DATABASES = {
    "erp_db": ("runtime_data/erp_demo.db", create_erp_demo_database),
    "pm_db": ("runtime_data/pm_demo.db", create_pm_demo_database),
    "mes_db": ("runtime_data/mes_demo.db", create_mes_demo_database),
    "wms_db": ("runtime_data/wms_demo.db", create_wms_demo_database),
}


def _build_router() -> MCPRouter:
    router = MCPRouter()
    for name, (db_path, creator) in _DATABASES.items():
        path = Path(db_path)
        if not path.exists():
            creator(path)
        router.register_executor(
            database=name,
            executor=SQLiteMCPExecutor(
                database=name,
                db_path=path,
                readonly=True,
                max_rows=_MAX_ROWS,
                max_execution_seconds=_MAX_EXECUTION_SECONDS,
            ),
        )
    return router


_router = _build_router()


def list_databases() -> str:
    """列出可查询的数据库。"""
    return json.dumps(
        {"databases": sorted(_DATABASES.keys())},
        ensure_ascii=False,
    )


def execute_sql(database: str, sql: str) -> str:
    """
    在指定数据库上执行只读 SQL 查询。

    受只读账号约束（仅 SELECT / WITH）、单查询行数上限与执行超时保护。
    返回 JSON：{success, columns, rows, row_count, truncated, error}。
    """
    result = _router.execute(
        MCPExecutionRequest(database=database, sql=sql)
    )
    return json.dumps(result.to_dict(), ensure_ascii=False, default=str)


server.add_tool(list_databases, name="list_databases")
server.add_tool(execute_sql, name="execute_sql")


if __name__ == "__main__":
    server.run("stdio")
