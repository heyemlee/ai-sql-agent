from __future__ import annotations

from typing import Dict

from .objects import MCPExecutionRequest, MCPExecutionResult


class MCPRouter:
    """
    MCP 路由器。

    SQL 生成完成后，根据 execution_request.database
    找到对应数据库执行器，然后执行 SQL。
    """

    def __init__(self):
        self.executors: Dict[str, object] = {}

    def register_executor(self, database: str, executor: object) -> None:
        """
        注册数据库执行器。
        """
        self.executors[database] = executor

    def execute(self, request: MCPExecutionRequest | dict) -> MCPExecutionResult:
        """
        路由并执行请求。
        """
        if isinstance(request, dict):
            request = MCPExecutionRequest(
                database=request["database"],
                sql=request["sql"],
            )

        executor = self.executors.get(request.database)

        if executor is None:
            return MCPExecutionResult(
                database=request.database,
                sql=request.sql,
                success=False,
                error=f"未找到数据库 {request.database} 对应的 MCP 执行器。",
            )

        return executor.execute(request)
