# mcp_router

MCP 路由执行模块。

## 目录结构

```text
mcp_router/
├── __init__.py
├── objects.py                # MCP执行请求和执行结果
├── sqlite_executor.py        # 进程内SQLite执行器（默认）
├── mcp_protocol_executor.py  # 真实MCP stdio协议执行器
└── router.py                 # MCP路由器：按database路由到执行器
```

## 执行治理

两种执行器共享同一套治理约束：

- **只读**：仅允许 SELECT / WITH（模拟只读账号）；
- **行数上限**：单查询最多返回 `max_rows` 行（默认 1000），超出截断并置 `truncated`；
- **执行超时**：`max_execution_seconds`（默认 10s）后由 progress handler 中止查询。

## 两种执行路径

```text
默认（离线快速）：
  Pipeline → MCPRouter → SQLiteMCPExecutor → SQLite

MCP 协议（PipelineConfig(use_mcp_protocol=True)）：
  Pipeline → MCPRouter → MCPProtocolExecutor
    → MCP stdio tool call → mcp_server.execute_sql → SQLite
```

## 执行请求格式

```json
{
  "database": "showroom_db",
  "sql": "SELECT ..."
}
```
