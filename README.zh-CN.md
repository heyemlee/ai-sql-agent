<div align="center">

<img src="docs/logo.svg" width="64" height="64" alt="" />

# AI SQL Agent

**一个会路由、会规划、通过受控工具行动、能修复和检查自己的结果，还能与其他 agent 协作的 LLM agent。**

它唯一的工具是对 SQLite 和 PostgreSQL 执行只读 SQL。围绕这个工具的有：结构化输出的模型路由、多步规划、自我修复、自我校验、短期与长期记忆、A2A 与 MCP 接口、评测和全链路追踪，全部在一个 FastAPI 服务里。

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688?style=flat-square&logo=fastapi&logoColor=white)](src/ai_sql_agent/api)
[![Agents](https://img.shields.io/badge/Agents-A2A_%2B_MCP-6E56CF?style=flat-square)](src/ai_sql_agent/a2a)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?style=flat-square&logo=postgresql&logoColor=white)](deploy/docker-compose.prod.yml)
[![CI](https://img.shields.io/badge/CI-GitHub_Actions-2088FF?style=flat-square&logo=githubactions&logoColor=white)](https://github.com/heyemlee/ai-sql-agent/actions/workflows/ci.yml)

[English](README.md) · **简体中文**

[技术亮点](#技术亮点) · [系统架构](#系统架构) · [技术栈](#技术栈) · [功能](#功能) · [本地开发](#本地开发)

</div>

<p align="center">
  <img src="docs/screenshots/ask.webp" width="960" alt="打开学习模式，在控制台问哪些加急订单还在生产、什么时候交付；agent 工作时阶段事件陆续到达，答案列出六张订单的客户、交付日期和合同金额，讲解部分显示第 1 步查一个库、第 2 步查另一个库，并把第 1 步得到的订单号写进了自己的 SQL" />
</p>

<p align="center"><sub>虚构数据，放在四个 SQLite 库里。规划和 SQL 是为录屏回放的，结果行是实际执行这些 SQL 得到的。</sub></p>

## 技术亮点

- **会路由，会规划。** 模型路由器用 JSON Schema 输出，判断一条请求需要调用工具，还是可以直接用记忆回答；需要查数的请求由规划器拆成多步，每一步的结果交给下一步。在 150 题评测集上跑三轮，跨库答案正确率 85.3%，单库 93.3%。
- **一个工具，一道关卡。** agent 只能通过只读 SQL 行动。SQLGlot AST 关卡按调用方权限检查每一张表、每一个字段，解析不了的一律拒绝；每次调用上限 1,000 行、10 秒、8 MiB。这部分有 213 个测试。
- **自己修复，自己检查。** 执行失败的调用进入基于日志的修复循环，最多两次；结果返回之前要经过规则检查和模型自检。答错且没有提示的答案占 5.3%（门禁线 ≤ 3%），正确答案被标为有风险的占 5.6%。
- **上下文与记忆都经过设计。** 混合检索（BM25 与向量用 RRF 融合、重排、按 token 预算裁剪）在 40 条留出问法上找全了 100% 需要的字段，只送出 43.6% 的 Schema；会话记忆、可选的 pgvector 长期记忆和验证过的历史答案把上下文延续下去。
- **能互联，可度量，可追溯。** 其他 agent 通过 A2A 调用它（JSON-RPC 2.0，SSE 推送阶段事件，契约在 CI 里测），MCP 服务把只读 SQL 作为工具提供出去。发布门禁用八条阈值、三轮评测来衡量它，每次请求都带 `trace_id`。

## 系统架构

<p align="center">
  <img src="docs/architecture.svg" width="960" alt="网页控制台和其他 agent 把请求发给同一个 FastAPI 进程；它的 agent 工作流先判断请求类型，借助 YAML 语义层找到相关的表，再让模型给出计划和 SQL，每步一个数据源；工具关卡按调用方权限检查每条语句并只读执行，先查一个数据源，再把上一步的值写进 SQL 查下一个，失败的语句送回修复；自检、记忆和每次请求的审计记录写入状态存储" />
</p>

一个 FastAPI 进程同时承载 agent、它的 HTTP 与 A2A 接口和控制台。请求先判断类型，再通过混合检索和 YAML 语义层匹配到表，拆成多步并由模型写出 SQL；工具关卡逐步只读执行，失败的送回修复，结果经过检查，答案带着 SQL、风险提示和 `trace_id` 返回。

- **模型做决定，代码管流程。** 路由、规划、写 SQL、修复和判断结果由模型完成；步骤顺序、权限关卡和各项上限由代码固定，比如每步只查一个数据源、最多修复两次。所以每次运行都有边界，并能凭 `trace_id` 复盘。
- **一个工具，一道关卡，出错就拒绝。** 通往数据的每条路径，无论是回答、预览还是 MCP 服务，都经过同一道授权关卡。解析不了的 SQL 一律拒绝；没装 SQLGlot 时服务拒绝所有 SQL；模型不可用时，服务重新执行该用户同一问题上次成功的 SQL，重新授权并标为降级。

## 技术栈

| 层         | 技术                                                        | 作用                                                         |
| ---------- | ----------------------------------------------------------- | ------------------------------------------------------------ |
| 运行时     | Python 3.11 · FastAPI · Uvicorn · Pydantic v2               | 一个进程承载 agent、HTTP 与 A2A 接口和控制台                 |
| 模型       | HTTPX · OpenAI 兼容与 Anthropic API · vLLM · Ollama         | 路由、规划、写 SQL、修复和自检的模型调用，按模型角色分别熔断 |
| 编排       | JSON Schema 输出的模型路由 · 分步规划器 · 短路径            | 决定下一步做什么、按什么顺序；简单请求跳过规划               |
| 工具与护栏 | SQLGlot AST 关卡 · SQLite authorizer · 只读连接             | agent 唯一的行动方式，按调用方权限授权并设上限               |
| 上下文     | BM25 · NumPy 向量 · RRF · Rerank · token 预算 · YAML 语义层 | 找出请求需要的表和字段                                       |
| 记忆       | 会话记忆 · pgvector 长期记忆 · 摘要                         | 在多轮对话和多次会话之间延续上下文                           |
| 互联       | A2A（JSON-RPC 2.0 + SSE）· MCP Python SDK                   | 为其他 agent 提供服务；通过 stdio 以 MCP 工具提供只读 SQL    |
| 评测       | 发布门禁 · 150 + 40 题 · 八条阈值、三轮                     | 准确率、静默错误、误报和召回率，对照门禁线                   |
| 可观测     | `trace_id` 审计 · token 计量 · Prometheus · JSON 日志       | 每次运行可追溯，每项指标可采集                               |
| 控制台     | HTML · CSS · ES Module，无构建步骤                          | 提问、数据、历史三个页面；学习模式展示每一步                 |
| 交付       | pytest · GitHub Actions · Docker Compose                    | 测试、CI、镜像冒烟、部署                                     |

## 功能

### Agent 循环

<p align="center">
  <img src="docs/screenshots/agent.webp" width="960" alt="在一次关于加急订单的回答之后，追问「哪张最早交付」直接用对话记忆回答，没有重新查库；接着问低于安全库存的物料，第一次执行因为用了表里不存在的字段而失败，随后被修复，查询过程里能看到修复前后的 SQL" />
</p>

<p align="center"><sub>虚构数据。规划、SQL 和追问的回答是回放的；修复是项目自己的修复循环在这份数据上实际跑出来的，走的是不调模型的规则分支。</sub></p>

- **理解。** 路由器在「重新查库」和「用对话记忆回答」之间做选择。检索之前，先把代词换成前几轮里的对象，把「上季度」这类说法换成具体日期。
- **规划。** 模型为每个数据源写一步。每一步拿到上一步的前 50 行，按列整理成现成的 `IN` 列表；Prompt 长度因此有上限，代价是一次交接最多 50 行。
- **行动与修复。** 工具关卡授权并执行每一条语句。执行失败时，从执行日志里解析出错误，定位出错的子句并改写（由模型改写，或者按最近的字段名替换），最多两次。
- **检查。** 空结果、行数远离计划、没人要的汇总、计划给不出的结果形状都会被标出来，然后由模型对照问题检查结果。发现的问题作为风险提示随答案返回。
- **记忆与学习。** 会话记忆和可选的长期记忆延续上下文。标为正确的答案会成为相似请求的范例；标为错误的可以导出成评测用例。

用内置演示库和 `claude-sonnet-5` 在 150 题评测集上的执行准确率，三轮的均值与区间：

| 指标                        |               结果 | 门禁线 |
| --------------------------- | -----------------: | -----: |
| 单库准确率                  | 93.3% [92.8, 93.6] |  ≥ 85% |
| 跨库准确率                  | 85.3% [84.0, 88.0] |  ≥ 90% |
| 静默错误（答错且没有提示）  |    5.3% [5.3, 5.3] |   ≤ 3% |
| 正确答案被标为有风险        |    5.6% [4.3, 6.6] |  ≤ 20% |
| 字段召回率（40 条留出问法） |    100% [100, 100] |  ≥ 85% |
| 送进模型的字段占比          | 43.6% [43.6, 43.8] |  ≤ 40% |

三条线没过（跨库准确率、静默错误、字段占比），所以这次测量没通过门禁。标注 JOIN 基数、要求一对多 JOIN 之后去重，把跨库准确率从 73.3% 提了上来，静默错误率从 10.0% 降了下来。评测集衡量的是 agent 在自己的评测夹具上的表现，不代表在你的数据上的准确率。测量记录：[准确率](benchmark/baseline/2026-09-09/README.md)、[检索](benchmark/baseline/2026-09-08/README.md)；[每条门禁线是怎么定的](benchmark/README.zh-CN.md)；[已知限制](docs/limitations.zh-CN.md)。

### 接口

- **控制台。** 提问、数据、历史三个页面，中英双语，无构建步骤。学习模式根据阶段事件讲解每次请求，数据页只显示调用方有权看的内容。
- **A2A。** `message/send` 返回一个已完成的任务；`message/stream` 先通过 SSE 推送阶段事件，再给出与 `POST /query` 相同的结果。服务公开 Agent Card，契约用例在 CI 里按固定 tag 运行。
- **MCP 与 HTTP。** MCP stdio 服务在演示库上提供 `list_databases` 和 `execute_sql` 两个工具，上限相同；其余由 HTTP 接口 `/query`、`/catalog`、`/history`、`/metrics` 提供（[接口文档](docs/api.zh-CN.md)）。

## 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate  # 建一个 Python 3.11+ 的虚拟环境
pip install -e ".[dev]"                             # 安装服务、固定版本的依赖和测试工具
python examples/end_to_end.py                       # 离线跑一个单库问题和一个跨库问题，模型用 Mock
./scripts/run_api.sh                                # 控制台和 API 在 http://localhost:8000，用内置演示数据
cp .env.example .env                                # 然后填模型服务商、Key 和模型名
python -m ai_sql_agent.semantic export semantic/    # 导出语义层模板，用来描述你自己的库
python -m ai_sql_agent.semantic validate semantic/  # 和真实库结构比对
```

没有模型凭证时服务用 Mock 模型作答。把 `AI_SQL_SEMANTIC_DIR` 指向语义层目录，就能查你自己的库；配置见 [docs/deployment.zh-CN.md](docs/deployment.zh-CN.md#二配置项)，另一套带示例数据的语义层见 [tools/simple_erp](tools/simple_erp/README.md)。

## 测试

```bash
make test        # 用 pytest 跑 1,199 个测试；需要 PostgreSQL 的 24 个在没有 AI_SQL_PGVECTOR_DSN 时跳过
make test-ci     # CI 用的 unittest 入口
make gate-smoke  # 发布门禁的离线冒烟样本
make gate        # 完整门禁：150 + 40 题，跑三轮；需要模型，会消耗 token
```

CI 在 PostgreSQL 16 + pgvector 服务上跑测试，还跑固定 tag 的 A2A 契约用例、演示脚本、离线门禁及其反例检查、只读账号工具的正反两面，以及开启 API Key 的 Docker 镜像冒烟。

## 部署

```bash
cp deploy/api_keys.example.json deploy/api_keys.json  # API Key、角色和表列权限
make up                                               # 用 Docker Compose 起应用和带 pgvector 的 PostgreSQL
make vllm-up                                          # 同上，外加内网自建的 vLLM 模型服务
```

模型凭证、业务库只读账号、监控指标和备份见 [docs/deployment.zh-CN.md](docs/deployment.zh-CN.md)。关键设计见 [docs/design.zh-CN.md](docs/design.zh-CN.md) · 接口见 [docs/api.zh-CN.md](docs/api.zh-CN.md) · 已知限制见 [docs/limitations.zh-CN.md](docs/limitations.zh-CN.md) · [项目详解](docs/walkthrough.zh-CN.md)。
