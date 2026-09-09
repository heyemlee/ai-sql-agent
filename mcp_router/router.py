from __future__ import annotations

from typing import Dict

from .objects import MCPExecutionRequest, MCPExecutionResult


class MCPRouter:
    """
    MCP 路由器：按 execution_request.database 找到对应执行器并转发。

    **授权不在这里判，在执行器入口判**（`mcp_router.authorization.enforce`）。
    路由器只是四条执行路径中最常走的那一条，绕开它直接调执行器一样能打到库——
    审查的复现脚本就是这么干的。关卡要放在绕不开的地方。
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
        路由并执行请求。授权由执行器自己把关。
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
