# cot_planning

Chain-of-thought planning, sitting between schema retrieval and SQL generation.

## Layout

```text
cot_planning/
├── __init__.py
├── objects.py             # Plan data structures
├── prompt_builder.py      # Prompt assembly
├── thinking_client.py     # Thinking-model calls, streaming supported
├── cot_planner.py         # Planning entry point: plan and plan_stream
└── cot_planning_demo.py   # Streaming demo
```

## Running

```bash
python -m cot_planning.cot_planning_demo
```

With no environment variables set, the demo streams from a mock.

To use a real model:

```bash
export DASHSCOPE_API_KEY="your-api-key"
export DASHSCOPE_COT_MODEL="qwen-plus"

python -m cot_planning.cot_planning_demo
```
