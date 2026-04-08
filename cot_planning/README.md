# cot_planning

CoT 规划模块，位于 Schema 检索与 SQL 生成之间。

## 目录结构

```text
cot_planning/
├── __init__.py
├── objects.py             # CoT规划数据结构
├── prompt_builder.py      # Prompt构建
├── thinking_client.py     # 思考模型调用，支持流式输出
├── cot_planner.py         # CoT规划主流程，支持 plan 和 plan_stream
└── cot_planning_demo.py   # 流式输出 Demo
```

## 运行

```bash
python -m cot_planning.cot_planning_demo
```

不配置环境变量时走 Mock 流式输出。

配置真实模型：

```bash
export DASHSCOPE_API_KEY="你的APIKey"
export DASHSCOPE_COT_MODEL="qwen-plus"

python -m cot_planning.cot_planning_demo
```
