<div align="center">

# AI SQL Agent

**用自然语言查询业务数据，让每条 SQL 都可控、可追溯。**

面向 SQLite / PostgreSQL 的 Text2SQL 服务，支持跨库查询、业务语义、会话记忆与结果校验。

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)](src/ai_sql_agent/api)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?style=flat-square&logo=postgresql&logoColor=white)](deploy/docker-compose.pgvector.yml)
[![CI](https://github.com/heyemlee/ai-sql-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/heyemlee/ai-sql-agent/actions/workflows/ci.yml)

[English](README.md) · **简体中文**

[快速开始](#快速开始) · [系统架构](#系统架构) · [技术栈](#技术栈) · [关键设计](#关键设计) · [评测](#评测与当前边界)

</div>

## 项目预览

<!-- 截图准备好后：放到 docs/assets/screenshot.png，并把下方 src 改为 docs/assets/screenshot.png。 -->
<p align="center">
  <img src="docs/assets/screenshot-placeholder.svg" alt="项目主界面截图预留位置" width="960" />
</p>

> 例如：**“查询准时率低于 0.9 的供应商名称”** → 检索业务 Schema → 生成并执行只读 SQL → 返回结果、SQL 与风险提示。

- **跨库查数**：把问题拆成多个目标库上的查询，上一步结果作为下一步筛选条件。
- **连续追问**：区分“查新数据”和“解释上次结果”，结合会话上下文与可选个人长期记忆。
- **受控执行**：表列权限、只读连接、超时与行数限制；失败修复与结果风险检查分别处理。
- **可复现验证**：内置四个演示库、离线 Mock、评测集与发布门禁，可本地运行或内网部署。

## 系统架构

![AI SQL Agent 系统架构](docs/assets/architecture.svg)

使用 [Archify](https://github.com/tt-a1i/archify) 生成 · [交互版 HTML](docs/diagrams/architecture.html)（下载后用浏览器打开）· [可编辑 JSON](docs/diagrams/architecture.json)

图中展示逻辑模块，主体运行在同一个 Python 服务内。模型调用贯穿检索、规划、生成等阶段；会话、记忆与审计按配置使用 SQLite 或 PostgreSQL，向量索引可选 pgvector。

一次查数依次经过 **问题归一化 → 库 / 表 / 字段检索 → 分步规划 → SQL 生成 → 受控执行 → 结果校验**。解释历史结果走记忆问答分支；简单单库问题可跳过独立规划调用。默认使用进程内执行器，也提供可选的 MCP stdio 执行路径。

## 技术栈

| 层次 | 技术 | 在项目中的作用 |
| --- | --- | --- |
| 运行时与 API | Python 3.11+ · FastAPI · Uvicorn · Pydantic v2 | 类型化请求响应、服务入口、配置校验 |
| Web Console | 原生 HTML · CSS · ES Module（无构建步骤） | 业务查数、数据浏览、历史查询三个入口，由 FastAPI 托管 |
| 模型接入 | HTTPX · OpenAI 兼容协议 · Anthropic 协议 | 规划、SQL 生成与问答；可连接 vLLM / Ollama 等兼容服务 |
| Schema 检索 | 自实现 BM25 · NumPy 向量索引 · RRF · Rerank 客户端 | 融合关键词和语义召回，定位相关库、表与字段 |
| 业务语义 | PyYAML · SchemaGraph | 定义字段含义、别名、指标口径、关联键与 JOIN 基数 |
| SQL 与存储 | SQLite · PostgreSQL · psycopg · pgvector | 业务查询、状态持久化与可选向量检索后端 |
| 权限与集成 | SQLGlot · MCP Python SDK · A2A / SSE | 基于 SQL AST 的授权检查、只读工具与 Agent 流式接口 |
| 工程与运维 | pytest / unittest · GitHub Actions · Docker Compose | 自动化验证、镜像构建；结构化日志与 Prometheus 格式指标 |

依赖版本以 [pyproject.toml](pyproject.toml) 为准。默认无需向量数据库；未配置模型凭证时使用 Mock，适合验证链路，真实问答能力需要接入模型。

## 关键设计

| 工程问题 | 实现方式 | 源码入口 |
| --- | --- | --- |
| 业务问法与字段名不一致 | 用 YAML 管理业务描述、别名与指标口径，供检索、规划和口径解释共享 | [semantic](src/ai_sql_agent/semantic) |
| Schema 太大，直接塞入 Prompt 成本高 | 库 → 表 → 字段分层召回，BM25 + 向量经 RRF 融合和精排；按相关性裁剪 SchemaGraph，优先保留关联键 | [retrieval](src/ai_sql_agent/retrieval) · [预算裁剪](src/ai_sql_agent/pipeline/schema_budget.py) |
| 跨库不能直接执行一条 JOIN | 每步绑定目标库，通过中间结果回填条件；检查步骤与目标库是否一致，阻止 SQL 引用其他库的表 | [pipeline](src/ai_sql_agent/pipeline/text2sql_pipeline.py) |
| SQL 能执行，却算错了 | SchemaGraph 标注 JOIN 基数，生成时约束去重与主键投影；执行后检查空结果、基数与语义一致性 | [graph_builder](src/ai_sql_agent/retrieval/graph_builder.py) · [validation](src/ai_sql_agent/validation) |
| 模型生成了越权查询 | 在模型上下文中过滤无权 Schema；执行前用 SQLGlot 解析表、列与作用域，解析失败则拒绝，再叠加 SQL Guard 与数据库只读连接 | [AST 分析](src/ai_sql_agent/auth/sql_analysis.py) · [execution](src/ai_sql_agent/execution) |
| 模型或 SQL 执行失败 | 错误定位与局部修复，默认最多 2 次；模型不可用时尝试重放该用户同题成功 SQL，重新授权并显式标记降级 | [repair](src/ai_sql_agent/repair) · [降级逻辑](src/ai_sql_agent/pipeline/text2sql_pipeline.py) |
| 线上问题难定位、难复现 | `trace_id` 关联 SQL、库表、耗时、修复和 Token 用量；用户反馈可导出评测用例，确认正确的查询可用作生成范例 | [audit](src/ai_sql_agent/audit) · [feedback](src/ai_sql_agent/feedback) |

执行默认限制为 **1,000 行、10 秒**，可通过 [PipelineConfig](src/ai_sql_agent/pipeline/objects.py) 调整。结果风险检查提供提示，不代表业务正确性保证。

## 快速开始

需要 **Python 3.11+**。在全新目录中运行：

```bash
git clone https://github.com/heyemlee/ai-sql-agent.git
cd ai-sql-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 无模型凭证时自动使用 Mock
python examples/end_to_end.py
./scripts/run_api.sh
```

打开 [http://localhost:8000](http://localhost:8000) 使用查询页面，或直接请求 API：

```bash
curl -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"u1","session_id":"s1","query":"查询准时率低于 0.9 的供应商名称"}'
```

### 接入真实模型

首次配置时复制 `.env.example` 为 `.env`，按服务商填写凭证、地址和模型名；已有 `.env` 时直接编辑。

```dotenv
AI_SQL_MODEL_PROVIDER=openai
OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=https://api.openai.com/v1
AI_SQL_CHAT_MODEL=your-chat-model
AI_SQL_CODER_MODEL=your-coder-model
```

Anthropic 使用 `AI_SQL_MODEL_PROVIDER=anthropic` 与 `ANTHROPIC_API_KEY`。Embedding 与 Rerank 的凭证和端点独立配置，详见 [.env.example](.env.example) 与 [部署指南](docs/deployment.zh-CN.md)。修改后重启服务。

### 接入自己的数据库

```bash
python -m ai_sql_agent.semantic export semantic/
# 编辑 semantic/*.yaml：连接、表列、业务别名、关联键与指标口径
python -m ai_sql_agent.semantic validate semantic/
```

设置 `AI_SQL_SEMANTIC_DIR=semantic/` 后重启。校验会将元数据与真实库结构比对；未配置目录时使用内置演示语义层。

部署到共享环境前，开启 `AI_SQL_AUTH_MODE=api_key` 并配置权限文件与业务库只读账号。Docker Compose、pgvector 与内网 vLLM 配置见 [部署指南](docs/deployment.zh-CN.md)。

## 评测与当前边界

以下为 **2026-09-09 历史基线**：150 条演示数据集，三轮汇总，模型标识 `claude-sonnet-5`，使用内置演示元数据。**发布门禁尚未通过。**

| 指标 | 测量结果 | 门禁要求 |
| --- | --- | --- |
| 单库执行准确率 | **93.3%** | ≥ 85% |
| 跨库执行准确率 | **85.3%** | ≥ 90%，未达标 |
| 静默错误率 | **5.3%** | ≤ 3%，未达标 |
| 风险提示误报率 | **5.6%** | ≤ 20% |
| 总体执行准确率 | **92.0%** | — |

这不是当前提交在真实业务库上的准确率承诺：三轮记录对应旧版主键提示规则，当前扩写版只有一轮全量数据；演示元数据也不等于实际部署语义层。条件、原始记录与改动归因见 [测量报告](benchmark/baseline/2026-09-09/README.md)。

```bash
python -m pytest tests -q   # 本地测试；部分集成用例需要外部服务 / 契约仓库
make gate-smoke            # 离线冒烟，不代表完整发布门禁通过
make gate                  # 完整门禁：需要模型凭证
```

CI 配置覆盖单元测试、pgvector 集成、固定版本的 A2A 契约、演示链路与容器冒烟。详见 [工作流](.github/workflows/ci.yml) 和 [评测说明](benchmark/README.zh-CN.md)。

### 已知限制与尚未完成的验证（截至 2026-10-04）

- **英文适配的模型回归没有跑齐。** 模型 Key 被停用之前，改动前后各跑完 2 轮完整门禁加不完整的第 3 轮；门禁改动前后卡在同样三项上，最终代码上没有跑过完整门禁。记录见 [benchmark/baseline/2026-10-04](benchmark/baseline/2026-10-04/README.md)，其中的数字不能当作基线引用。
- **PostgreSQL 没有在真实库上验证过**这次新增的任何东西：授权里的标识符大小写规则、预览 SQL（不带 schema 限定；表结构从 `public` 读，表名靠 `search_path` 解析）、最小权限脚本的方言绑定。只有单元测试。
- **`AI_SQL_MAX_ROWS` 与 `AI_SQL_MAX_EXECUTION_SECONDS` 只对 API 进程内的执行器生效**（默认 1000 行 / 10 秒，就是这两个变量没生效时实际在用的上限）。这一点在 SQLite 上验证过；PostgreSQL 分支只有用假驱动的单元测试。MCP stdio 服务固定 1000 行 / 10 秒。上限比每页行数小时，预览的一页按上限给。
- **数据目录先读表结构、再按调用方权限裁剪**（有缓存与单飞）；看不到的东西不会返回给调用方。
- **模型凭证被拒之后的处理只用 mock transport 验证过。** 识别「额度耗尽」「Key 停用」「模型无权限」靠的是字样表；状态是进程级的；第一个拒绝回来之前已经发出的并发请求收不回来。
- **英文界面**：语义元数据没有英文键时，数据字典的文字照原文显示；服务端的自由文本（路由原因、校验说明、错误信息）是中文。
- **路由兜底**：模型路由不可用时，规则兜底会把大部分口语化中文问法（留出集 40 条里的 30 条）判成对话而不是查数。
- **与 Helpdesk 的 A2A 共同联调尚未针对这批改动进行。**

<details>
<summary><strong>接口一览</strong></summary>

| 接口 | 用途 |
| --- | --- |
| `POST /query` | 自然语言查询，返回答案、分步 SQL / 结果、风险与追踪信息 |
| `GET /history` | 查询历史，默认仅自己的记录 |
| `GET /whoami` | 本次请求解析出的身份（与 `/query` 同一套解析）与服务能力 |
| `GET /catalog` | 当前身份可见的数据目录：数据域、表、字段、单位与业务术语 |
| `POST /catalog/{database}/{table}/rows` | 只读分页预览，支持搜索 / 筛选 / 排序；不接受 SQL |
| `POST /memory/save` | 保存个人长期记忆 |
| `POST /feedback` · `GET /feedback/stats` | 回答反馈与管理员统计 |
| `GET /health` · `GET /readyz` | 存活与就绪检查 |
| `GET /metrics` | Prometheus 格式指标 |
| `POST /admin/schema/reload` | 管理员重建 Schema 索引 |

`POST /query` 响应包含 `answer`、`has_risk`、`degraded`、`trace_id` 及分步执行信息。

目录接口沿用 `/query` 的调用方身份与表列权限。预览语句由服务端按「真实存在且可见的列」拼出，值全部走绑定参数，执行走生成 SQL 用的同一个只读执行器与授权关卡。取回的内容在数据库里就设了上限，不是取回来再截：长文本截到 2000 个字符，二进制字段只取大小。

</details>

<details>
<summary><strong>项目结构</strong></summary>

```text
src/ai_sql_agent/
  api/ · a2a/ · mcp_server/       网页、HTTP API 与 Agent 接口
  pipeline/ · normalization/     意图路由、问题归一化与编排
  semantic/ · retrieval/         业务语义、分层检索与 SchemaGraph
  indexing/ · memory/            向量索引、会话与长期记忆
  planning/ · generation/        跨库计划与 SQL 生成
  execution/ · repair/           受控执行与失败修复
  validation/ · feedback/        结果校验与反馈回路
  auth/ · audit/ · core/ · ops/   权限、审计、运行时与只读账号工具
tests/ · benchmark/              测试、评测集、基线与门禁
examples/ · tools/sim_erp/        分模块示例与模拟多库 ERP
deploy/                         Docker 镜像与 Compose 编排
docs/                           深入文档、架构图与截图素材
```

</details>

## 深入阅读

[项目详解](docs/walkthrough.zh-CN.md) · [部署与运维](docs/deployment.zh-CN.md) · [授权模型](docs/authz-model.md) · [最小权限配置](docs/least-privilege-runbook.md) · [检索设计](docs/modules/retrieval.md) · [执行器设计](docs/modules/execution.md) · [A2A 契约反馈](docs/contract-feedback.md)
