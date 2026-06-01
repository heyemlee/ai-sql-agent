# AI SQL Agent 长短期记忆

该模块不依赖 Web 框架，可由后续 API、动态路由器或当前 Text2SQL Pipeline 直接调用。

## 短期记忆

- SQLite 持久化完整原始消息；
- 同时按最近消息数和估算 Token 数维护滑动窗口；
- 窗口外消息由后台线程增量压缩；
- 摘要带版本与 `through_message_id` 游标，避免重复或遗漏；
- 摘要运行时读取上一已完成版本，不阻塞当前请求；
- 摘要保留用户目标、业务口径、筛选条件、SQL 结果、分析结论和任务状态。

配置模型凭证（`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`）后，统一服务默认使用项目已有的模型 Client 生成摘要；没有 Key 时使用离线抽取式摘要，便于本地运行。也可以向 `ConversationMemoryService` 注入其他 `MemorySummarizer`。

## 长期记忆

- 只有显式调用保存方法时才会写入；
- 结构化与非结构化内容统一保存“检索摘要 + 原始内容 + 元信息 + 向量”；
- 结构化元信息支持数据库、表、字段、筛选条件、CoT 和 SQL；
- 召回严格按 `user_id` 隔离，并支持类型与元信息过滤；
- Rerank 为可选注入能力；未配置时按向量相似度排序；
- 只有 `enable_long_term=True` 时才会检索个人知识库。

默认使用 SQLite 保存原文、元信息与本地向量，适合 Demo。配置 `AI_SQL_PGVECTOR_DSN`（或 `MemoryServiceConfig(pgvector_dsn="...")`）后会启用 pgvector 向量索引——与 Schema 检索共用同一个 Postgres 实例，项目只维护一套向量栈。检索先在 pgvector 中按 `user_id` 和记忆类型过滤召回（HNSW + 余弦距离），再读取 SQLite 原文并做元信息过滤与可选 Rerank；连接失败时自动回退 SQLite 全量打分。

## 基本用法

```python
from ai_sql_memory import ConversationMemoryService

memory = ConversationMemoryService()

context = memory.begin_user_turn(
    user_id="user-1",
    session_id="session-1",
    query="上次查的门板材质是什么？",
    enable_long_term=True,
)

# 把 context.to_prompt_context() 交给对话模型或查询链路。
memory.record_assistant_message(
    user_id="user-1",
    session_id="session-1",
    content="查询结果为 3.5%",
    message_type="sql_result",
    payload={"door_material": "烤漆"},
)

# 仅响应用户的“保存到个人知识库”动作。
memory.save_structured_result(
    user_id="user-1",
    query="查询订单门板材质",
    result={"door_material": "烤漆"},
    database="showroom_db",
    tables=["order_item"],
    columns=["door_material"],
    sql="SELECT door_material FROM order_item ...",
)
```

接入现有 Text2SQL：

```python
from ai_sql_pipeline import Text2SQLPipeline, DynamicSqlAgentService

service = DynamicSqlAgentService(Text2SQLPipeline(), memory)
response = service.run(
    user_id="user-1",
    session_id="session-1",
    query="那这些订单的定金呢？",
    enable_long_term=True,
)

# 用户确认保存后再调用：
service.save_result_to_personal_knowledge_base(
    user_id="user-1",
    result=response.pipeline_result,
)
```

## 端到端 Demo

直接运行普通 Python 文件：

```bash
python ai_sql_pipeline/memory_end_to_end_demo.py
```

或按模块运行：

```bash
python -m ai_sql_pipeline.memory_end_to_end_demo
```

Demo 会依次展示第一轮 Text2SQL、短期窗口、用户主动保存长期记忆、第二轮长期召回、异步摘要完成后的最终上下文。默认记忆文件为 `runtime_data/memory_demo.db`。

Demo 同时启用了动态路由：第一轮获取新数据会进入 `database_query`；第二轮追问历史结果会进入 `data_qa`，不会重复访问数据库。

## 验证

```bash
python -m unittest discover -s tests -v
```
