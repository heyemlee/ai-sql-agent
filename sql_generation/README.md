# sql_generation

SQL 生成模块，位于 CoT 规划之后、SQL 执行之前。

## 目录结构

```text
sql_generation/
├── __init__.py
├── objects.py              # SQL生成数据结构
├── cot_parser.py           # 正则解析CoT四元组
├── schema_store.py         # 局部Schema存储与提取
├── prompt_builder.py       # Coder Prompt构建
├── coder_client.py         # Coder模型调用
├── sql_generator.py        # SQL生成主流程
└── sql_generation_demo.py  # Demo
```

## 运行

```bash
python -m sql_generation.sql_generation_demo
```

不配置环境变量时走 Mock。

配置真实模型：

```bash
export DASHSCOPE_API_KEY="你的APIKey"
export DASHSCOPE_CODER_MODEL="qwen-plus"

python -m sql_generation.sql_generation_demo
```

## 执行请求格式

```json
{
  "database": "showroom_db",
  "sql": "SELECT ..."
}
```
