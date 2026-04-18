# ai_sql_pipeline

End-to-end Text2SQL orchestration, with runnable demos.

## Layout

```text
ai_sql_pipeline/
├── __init__.py
├── demo_data.py             # Demo database creation and business schema metadata
├── create_demo_db.py        # Create the SQLite demo database on its own
├── sql_execution_demo.py    # Minimal MCP SQL execution demo
├── local_clients.py         # Local mock keyword extraction and embedding
├── objects.py               # Pipeline data structures
├── text2sql_pipeline.py     # End-to-end orchestration
└── end_to_end_demo.py       # Full Text2SQL demo
```

## 1. Create the demo database

```bash
python -m ai_sql_pipeline.create_demo_db
```

Produces:

```text
runtime_data/erp_demo.db
```

The seed SQL lives in:

```text
sql/create_erp_demo.sql
```

## 2. Test the execution path only

```bash
python -m ai_sql_pipeline.sql_execution_demo
```

This demo calls no model; it only exercises MCP routing and SQLite execution.

## 3. Run the end-to-end pipeline

```bash
python -m ai_sql_pipeline.end_to_end_demo
```

## 4. Run dynamic routing with short- and long-term memory

```bash
python ai_sql_pipeline/memory_end_to_end_demo.py
```

`DynamicSqlAgentService` is the single entry point:

- `database_query`: runs schema retrieval, planning, SQL generation and execution;
- `data_qa`: explains results, defines metrics, answers follow-ups about past
  results, and handles analysis and general business questions — no SQL is run;
- short-term context is loaded before routing, and long-term memory is recalled
  only if the user has enabled it;
- with a model API key configured, routing is model-classified, falling back to
  rules when no model is available or its output is malformed;
- the routing decision is written into the assistant message's memory metadata.

Example:

```python
from ai_sql_memory import ConversationMemoryService
from ai_sql_pipeline import Text2SQLPipeline, DynamicSqlAgentService

memory = ConversationMemoryService()
service = DynamicSqlAgentService(Text2SQLPipeline(), memory)

response = service.run(
    user_id="user-001",
    session_id="session-001",
    query="list suppliers whose on-time rate is below 0.9",
)

print(response.decision.route.value)
print(response.decision.reason)
print(response.queried_database)
print(response.answer)
```

Routing tests on their own:

```bash
python tests/test_routing.py
```

## 5. Run the memory-enabled pipeline via script

```bash
bash run_memory.sh
```
