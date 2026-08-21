# sql_generation

SQL generation, running after chain-of-thought planning and before execution.

## Layout

```text
sql_generation/
├── __init__.py
├── objects.py              # SQL generation data structures
├── cot_parser.py           # Regex parsing of the plan's four-field steps
├── schema_store.py         # Local schema storage and extraction
├── prompt_builder.py       # Coder prompt assembly
├── coder_client.py         # Coder model calls
├── sql_generator.py        # Generation entry point
└── sql_generation_demo.py  # Demo
```

## Running

```bash
python -m sql_generation.sql_generation_demo
```

With no environment variables set, the demo uses a mock.

To use a real model:

```bash
export DASHSCOPE_API_KEY="your-api-key"
export DASHSCOPE_CODER_MODEL="qwen-plus"

python -m sql_generation.sql_generation_demo
```

## Request format

```json
{
  "database": "erp_db",
  "sql": "SELECT ..."
}
```
