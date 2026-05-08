# ai_sql_agent.execution

MCP routing and execution.

## Layout

```text
src/ai_sql_agent/execution/
├── __init__.py
├── objects.py                # Execution request and result
├── sqlite_executor.py        # In-process SQLite executor (default)
├── mcp_protocol_executor.py  # Real MCP stdio protocol executor
└── router.py                 # Routes by database name to an executor
```

## Execution governance

Both executors share the same constraints:

- **Read-only**: only SELECT / WITH are allowed (mirroring a read-only account);
- **Row cap**: at most `max_rows` rows per query (default 1000); beyond that the
  result is truncated and `truncated` is set;
- **Execution timeout**: after `max_execution_seconds` (default 10s) a progress
  handler aborts the query.

## Two execution paths

```text
Default (fast, offline):
  Pipeline → MCPRouter → SQLiteMCPExecutor → SQLite

MCP protocol (PipelineConfig(use_mcp_protocol=True)):
  Pipeline → MCPRouter → MCPProtocolExecutor
    → MCP stdio tool call → ai_sql_agent.mcp_server.execute_sql → SQLite
```

## Request format

```json
{
  "database": "erp_db",
  "sql": "SELECT ..."
}
```
