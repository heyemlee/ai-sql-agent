from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from .objects import MCPExecutionRequest, MCPExecutionResult


class MCPProtocolExecutor:
    """
    通过真实 MCP 协议（stdio）调用远端 SQL 执行工具的执行器。

    与 SQLiteMCPExecutor 同接口，可直接注册进 MCPRouter，
    使整条链路的 SQL 执行走标准 MCP tool call：
        Pipeline → MCPRouter → MCPProtocolExecutor
        → (MCP stdio) → mcp_server.execute_sql → 数据库

    说明：Demo 实现每次调用拉起一个 stdio 会话，简单且无状态；
    生产部署应维护常驻 ClientSession 复用连接。
    """

    def __init__(
        self,
        database: str,
        server_command: Optional[list[str]] = None,
        working_dir: Optional[str] = None,
    ):
        self.database = database
        self.server_command = server_command or [
            sys.executable,
            "-m",
            "mcp_server.server",
        ]
        self.working_dir = working_dir or str(Path(__file__).resolve().parents[1])

    def execute(self, request: MCPExecutionRequest) -> MCPExecutionResult:
        """
        通过 MCP tool call 执行 SQL。
        """
        import anyio

        try:
            payload = anyio.run(self._call_tool_async, request)
        except Exception as exc:
            return MCPExecutionResult(
                database=request.database,
                sql=request.sql,
                success=False,
                error=f"MCP 调用失败：{exc}",
            )

        return MCPExecutionResult(
            database=payload.get("database", request.database),
            sql=payload.get("sql", request.sql),
            success=bool(payload.get("success")),
            columns=list(payload.get("columns") or []),
            rows=list(payload.get("rows") or []),
            row_count=int(payload.get("row_count") or 0),
            error=payload.get("error"),
            truncated=bool(payload.get("truncated")),
        )

    async def _call_tool_async(self, request: MCPExecutionRequest) -> dict:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        server_params = StdioServerParameters(
            command=self.server_command[0],
            args=self.server_command[1:],
            cwd=self.working_dir,
        )

        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "execute_sql",
                    {"database": request.database, "sql": request.sql},
                )

        text_blocks = [
            block.text
            for block in result.content
            if getattr(block, "type", "") == "text"
        ]
        if not text_blocks:
            raise RuntimeError("MCP 工具未返回文本内容")

        return json.loads(text_blocks[0])
