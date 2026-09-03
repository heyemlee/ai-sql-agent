# AI SQL Agent 部署与运维

[English](README.md) | **简体中文**

面向公司内部自用部署：单实例、无多租户、服务自校验身份。

## 一、首次部署

```bash
# 1. 生成 API Key 配置
cp deploy/api_keys.example.json deploy/api_keys.json
python -c "import secrets; print(secrets.token_urlsafe(32))"   # 每个调用方生成一个
# 编辑 deploy/api_keys.json，替换 key 并按团队实际情况改 roles / policies

# 2. 配置模型凭证
cat > .env <<'ENV'
AI_SQL_MODEL_PROVIDER=openai          # 或 anthropic
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://relay.example.com/v1
# ANTHROPIC_API_KEY=sk-ant-xxx
# ANTHROPIC_BASE_URL=https://relay.example.com
POSTGRES_PASSWORD=改成强密码
ENV

# 3. 起服务
docker compose -f docker-compose.prod.yml up -d --build

# 4. 验收
curl -s localhost:8000/health
curl -s localhost:8000/readyz | python -m json.tool
```

`deploy/api_keys.json` 已在 `.dockerignore` 里，不会进镜像，只以只读方式挂载。
换 Key 只需改文件并 `docker compose restart app`。

## 二、配置项

| 环境变量 | 生产建议 | 说明 |
| --- | --- | --- |
| `AI_SQL_MODEL_PROVIDER` | `openai` | `openai`=OpenAI 兼容协议，`anthropic`=Messages 协议 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | 按厂商填 | provider=openai 时的凭证与端点 |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | 按厂商填 | provider=anthropic 时的凭证与端点 |
| `AI_SQL_AUTH_MODE` | `api_key` | `disabled` 会放行所有请求，只允许本地开发 |
| `AI_SQL_API_KEYS_FILE` | `/run/secrets/api_keys.json` | Key 与数据权限策略 |
| `AI_SQL_SEMANTIC_DIR` | `semantic/` | 业务语义元数据目录。**接真实库必配**，见 §二·六 |
| `AI_SQL_FEEDBACK_ENABLED` | `true` | 收集用户对回答的对错判定 |
| `AI_SQL_CERTIFIED_SQL_ENABLED` | `true` | 把确认正确的查询作为参考写法复用 |
| `AI_SQL_PGVECTOR_DSN` | 指向内网 PG | Schema 向量、长期记忆、审计共用 |
| `AI_SQL_MAX_ROWS` | `1000` | 单查询返回行数上限 |
| `AI_SQL_MAX_EXECUTION_SECONDS` | `10` | 单查询执行超时（数据库侧中断） |
| `AI_SQL_HTTP_MAX_RETRIES` | `2` | 模型 429/5xx 退避重试次数 |
| `AI_SQL_DEFAULT_RATE_LIMIT_PER_MINUTE` | `60` | 未在 Key 上单独配置时的默认限流 |
| `AI_SQL_DEFAULT_DAILY_TOKEN_QUOTA` | 按预算设 | 0 表示不限，建议设上限保护模型账单 |
| `AI_SQL_SCHEMA_REFRESH_SECONDS` | `300` | Schema 变更轮询间隔，0 为关闭 |
| `AI_SQL_LOG_FORMAT` | `json` | 便于日志系统采集 |

## 二·五、模型部署形态（公网 API / 内网 vLLM）

本项目支持两套协议：**OpenAI 兼容**（`POST /chat/completions` + `Authorization: Bearer`）
与 **Anthropic Messages**（`POST /v1/messages` + `x-api-key`），由 `AI_SQL_MODEL_PROVIDER`
选择；服务地址与模型名全部由配置项决定。因此模型跑在哪里、用哪家对应用代码是透明的，
换厂商或换部署形态**不需要改任何代码**，只改环境变量。
自建推理服务（vLLM / SGLang / Ollama）都走 OpenAI 兼容那一套。

### 两种形态对比

| | 公网 API | 内网自建 vLLM |
|---|---|---|
| 编排文件 | `docker-compose.prod.yml` | `docker-compose.vllm.yml` |
| 数据出网 | 问题原文与 Schema 会发给外部服务 | **数据不出内网** |
| 硬件成本 | 无 | 需要 GPU（14B bf16 约 32GB 显存） |
| 单次延迟 | 受公网与厂商排队影响 | 取决于本地 GPU，通常更稳定 |
| 模型能力 | 可用最新商业模型 | 受限于可私有化部署的开源模型 |
| 适用 | 数据敏感度低、不想维护 GPU | 客户信息、成本价等不允许出内网 |

选择依据是**数据合规要求**，不是性能。合同金额、客户与供应商信息、批次成本单价
这类数据如果不允许离开内网，就只能用自建形态。

### 切到内网 vLLM

```bash
# 1. 起 vLLM（OpenAI 兼容 server）
docker compose -f docker-compose.vllm.yml up -d

# 2. 或者对已有服务，只改这几个环境变量
AI_SQL_CHAT_URL=http://vllm-chat:8000/v1/chat/completions
AI_SQL_EMBEDDING_URL=http://vllm-embed:8000/v1/embeddings
AI_SQL_MODEL_PROVIDER=openai                  # vLLM 是 OpenAI 兼容 server
OPENAI_API_KEY=sk-internal-vllm               # vLLM --api-key 的值
AI_SQL_CHAT_MODEL=Qwen2.5-14B-Instruct        # 需与 --served-model-name 一致
AI_SQL_CODER_MODEL=Qwen2.5-Coder-14B-Instruct
AI_SQL_EMBEDDING_MODEL=bge-m3
AI_SQL_EMBEDDING_DIMENSIONS=1024
```

### 注意事项

- **模型名必须与 vLLM 的 `--served-model-name` 完全一致**，否则 vLLM 返回 404。
- **`--max-model-len` 要覆盖 Token 预算**。本项目单次查询预算约 8k，建议设 16384 留一倍余量。
- **Embedding 维度要和索引一致**。换 Embedding 模型后维度若变化，必须重建向量索引
  （`POST /admin/schema/reload`），否则维度不匹配会报错。
- **Rerank 不是 OpenAI 标准协议**，走的是厂商自定义格式，vLLM 不直接兼容。
  留空 `AI_SQL_RERANK_URL` 时字段级精排退化为本地词面覆盖度打分——可用，
  但弱于交叉编码精排；要恢复需自建 rerank 服务并加一层格式适配。
- **超时可以收紧**。本地推理没有公网抖动，`AI_SQL_HTTP_TIMEOUT_SECONDS` 从 60 降到 45 更合适。

### 兼容性验证

`tests/test_openai_compatible_endpoint.py` 起了一个最小 OpenAI 兼容服务作为被测端点，
验证四件事：配置可整体切换、chat 按 OpenAI 格式收发且鉴权头正确、embedding 同理、
以及**整条 Text2SQL 链路跑在自建端点上能产出可执行 SQL 与真实结果**。
改动模型接入相关代码后跑这个测试即可确认没有破坏兼容性。

```bash
python -m pytest tests/test_openai_compatible_endpoint.py -v
```

## 二·六、业务语义元数据：模型对业务的全部认知

接真实业务库时，这是工作量最大的一块，而且它不是编码工作——需要懂业务的人
把每张表每个字段是什么意思、业务上怎么称呼、几个库之间靠什么对应写下来。

服务自带四个演示库的元数据。换成真实库之后那份元数据就不适用了：检索退化成
按字段名字面匹配，「料快没了」再也匹配不到 `safety_stock`。现象是「模型答不准」，
实际是没人告诉过它业务是什么。

```bash
python -m ai_sql_semantic export semantic/     # 导出演示元数据当模板
# 编辑 semantic/*.yaml：一个库一个文件，外加 _shared.yaml
python -m ai_sql_semantic validate semantic/   # 与真实库比对结构
python -m ai_sql_semantic show                 # 当前生效的是哪一份
```

每个库的文件里是表和字段的业务描述、别名、语义角色和样例值；`_shared.yaml`
放跨库的部分：关联键（这些系统之间没有外键，不显式声明规划就没法正确拆步）、
指标口径消歧（「采购金额」到底指哪个字段）、指标定义（「超预算」怎么算）。

`validate` 会连上真实库双向比对：

| 发现 | 后果 |
| --- | --- |
| 元数据里有、库里没有的表或字段 | 检索会把它推给模型，生成的 SQL 必定报错 |
| 库里有、元数据没写的表 | 对模型完全不存在，涉及它的问题永远答不出来 |
| 跨库关联键指向不存在的字段 | 跨库规划会静默失效 |
| 连接串直接写在文件里 | 文件要进版本库，密码不能——改用 `dsn_env` |

有错误时返回非 0，可以直接卡在发布流水线里。每次业务库改结构后都要重跑一次，
和 `POST /admin/schema/reload` 一起做。

生产部署把目录只读挂进去：

```yaml
volumes:
  - ./semantic:/app/semantic:ro
environment:
  AI_SQL_SEMANTIC_DIR: /app/semantic
```

## 二·七、反馈回路：让工具越用越准

页面上每条回答下面都有「这条回答对吗」。这个判定是唯一不依赖我们自己出题的
数据来源。

- **答错**进待改进清单。绝大多数答错都能追溯到元数据缺失，而用户那句补充说明
  往往直接点出缺的是哪一条；
- **答对**成为认证范例：下次遇到相似问题，它会作为参考写法进生成 Prompt，
  把这家公司的 join 路径和口径固化下来，而不是每次重新猜。

```bash
python -m ai_sql_feedback stats                    # 线上准确率与静默错误率
python -m ai_sql_feedback wrong                    # 待处理清单，带用户说明
python -m ai_sql_feedback export-dataset new.json  # 把答错的问题导成评测集草稿
python -m ai_sql_feedback resolve <trace_id>       # 标记某条已处理
```

`export-dataset` 导出的是草稿：问题原文、模型答错的 SQL、用户的说明都摆好了，
但正确 SQL 得人来补——模型答错了，系统里没有任何地方知道正确答案。补完并进
`benchmark/dataset.json` 再重跑门禁，真实用户答错过的题就成了以后不能悄悄
退化回去的红线。

运维上主要看两个数（都来自 `feedback stats`）：

| 指标 | 怎么读 |
| --- | --- |
| 线上准确率 | 有明确判定的样本里答对的占比。与 benchmark 对照，两边差得远时以这个为准 |
| 线上静默错误率 | 答错且当时没有任何风险提示的占比。最贵的一类——用户没有任何理由去核对 |

反馈按人隔离：只能对自己的查询表态，认证范例也只回给确认它的那个人——
一条查询里带着表名，等于绕过权限层看 Schema。

## 三、权限配置

`api_keys.json` 里的 `policies` 决定每个角色能看哪些表。生效在两处：
检索阶段（无权表不会进入 Prompt，模型看不到）与执行阶段
（SQL 里引用了无权表直接拒绝，不落到数据库）。

```json
"policies": {
  "analyst": {
    "allow_tables": ["erp_db.*", "pm_db.*", "mes_db.*", "wms_db.*"],
    "deny_columns": ["*.*.id_card_no"]
  },
  "hr": { "allow_tables": ["hr_db.*"] }
}
```

- `deny` 优先于 `allow`；
- 一个 Key 配多个角色时权限取并集；
- `can_impersonate: true` 只给服务端调用方（如 BI 门户），
  个人 Key 不要开——否则改一下请求体的 `user_id` 就能读到别人的个人知识库。

## 四、探针

| 端点 | 用途 | 编排配置 |
| --- | --- | --- |
| `/health` | 存活：只看进程，不查依赖 | livenessProbe |
| `/readyz` | 就绪：查记忆库、审计库、pgvector | readinessProbe |
| `/metrics` | Prometheus 抓取 | ServiceMonitor / scrape_config |
| `/` | 查询界面（单页，无构建步骤） | 内网直接访问 |

依赖抖动时 `/readyz` 返回 503 摘掉流量，但 `/health` 仍然 200，
避免编排系统把进程反复重启。

Kubernetes 片段：

```yaml
livenessProbe:
  httpGet: { path: /health, port: 8000 }
  initialDelaySeconds: 15
  periodSeconds: 20
readinessProbe:
  httpGet: { path: /readyz, port: 8000 }
  initialDelaySeconds: 10
  periodSeconds: 10
```

副本扩容优先加 replica，不要加 uvicorn worker：
Pipeline 与 Schema 索引在进程内共享，多 worker 会各建一份。

## 五、监控指标

`GET /metrics` 暴露（Prometheus 文本格式，零外部依赖自实现）：

| 指标 | 类型 | 用途 |
| --- | --- | --- |
| `ai_sql_requests_total{method,path,status}` | counter | QPS、错误率 |
| `ai_sql_request_duration_seconds` | histogram | P95/P99 延迟 |
| `ai_sql_stage_duration_seconds{stage,status}` | histogram | 哪一段慢（检索/规划/生成/执行/修复） |
| `ai_sql_model_calls_total{component,status,error_category}` | counter | 模型失败率、限流次数 |
| `ai_sql_model_tokens_total{component,kind}` | counter | Token 消耗与成本 |
| `ai_sql_sql_executions_total{database,status}` | counter | SQL 成功率、越权次数 |
| `ai_sql_sql_repairs_total{result}` | counter | 修复回路的实际收益 |
| `ai_sql_access_denied_total{database}` | counter | 数据权限拦截 |
| `ai_sql_limit_rejections_total{reason}` | counter | 限流/配额拒绝 |
| `ai_sql_degraded_responses_total{reason,failed_stage}` | counter | 模型不可用触发的降级 |
| `ai_sql_schema_reloads_total{result}` | counter | Schema 索引重建 |
| `ai_sql_circuit_state{component}` | gauge | 熔断状态 0=闭合 1=半开 2=断开 |
| `ai_sql_circuit_trips_total{component}` | counter | 熔断跳闸次数 |

常用 PromQL：

```promql
# P95 查询延迟
histogram_quantile(0.95, sum(rate(ai_sql_request_duration_seconds_bucket{path="/query"}[5m])) by (le))

# SQL 执行成功率
sum(rate(ai_sql_sql_executions_total{status="ok"}[5m]))
  / sum(rate(ai_sql_sql_executions_total[5m]))

# 修复回路救回来的比例
sum(rate(ai_sql_sql_repairs_total{result="repaired"}[1h]))
  / sum(rate(ai_sql_sql_repairs_total[1h]))

# 今日 Token 消耗
sum(increase(ai_sql_model_tokens_total[24h])) by (component)
```

建议告警：模型失败率 > 5%、`/query` P95 > 10s、`ai_sql_limit_rejections_total`
突增（说明配额设小了或有人写了循环）、`/readyz` 连续失败、
`ai_sql_circuit_state > 0` 持续 5 分钟（模型侧真的挂了）。

## 六、排查一次失败查询

每个响应都带 `trace_id`（响应体字段与 `X-Trace-Id` 响应头），日志与审计都按它检索：

```bash
# 1. 从日志捞全链路（JSON 格式下用 jq）
docker compose -f docker-compose.prod.yml logs app | grep <trace_id>

# 2. 从审计表看这次查询做了什么
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U ai_sql -d ai_sql -c \
  "SELECT created_at, caller, user_id, query, route, databases, sqls, success, error
     FROM ai_sql_audit_log WHERE trace_id = '<trace_id>'"
```

日志里能看到每一段：`stage.retrieval`（命中哪些库表）、`stage.planning`
（拆了几步）、`stage.sql_generation`（生成的 SQL）、`stage.execution`
（成功与否、行数）、`repair.attempt`（修复过程）、`model.call`（模型耗时与重试）。

## 七、常用运维查询

```sql
-- 今天各用户用量（配额判断用的就是这张表）
SELECT user_id, COUNT(*) AS queries, SUM(prompt_tokens + completion_tokens) AS tokens
FROM ai_sql_audit_log
WHERE created_at >= date_trunc('day', now())
GROUP BY user_id ORDER BY tokens DESC;

-- 最近失败的查询
SELECT created_at, user_id, query, error
FROM ai_sql_audit_log
WHERE NOT success ORDER BY id DESC LIMIT 20;

-- 越权访问尝试（谁在试图查没权限的表）
SELECT created_at, caller, user_id, query, denied_tables
FROM ai_sql_audit_log
WHERE jsonb_array_length(denied_tables) > 0 ORDER BY id DESC;

-- 高频问题（值得沉淀成固定报表的）
SELECT query, COUNT(*) FROM ai_sql_audit_log
WHERE created_at > now() - interval '7 days'
GROUP BY query ORDER BY 2 DESC LIMIT 20;
```

## 八、Schema 变更后

设了 `AI_SQL_SCHEMA_REFRESH_SECONDS` 就不用管：后台按周期比对指纹，
变了自动重建（日志 `schema_watcher.change_detected` → `schema.reloaded`）。

不想等下一个周期，或想确认当前状态：

```bash
curl -H "X-API-Key: $ADMIN_KEY" localhost:8000/admin/schema/status
curl -XPOST -H "X-API-Key: $ADMIN_KEY" localhost:8000/admin/schema/reload
```

这两个接口要求 Key 带 `"is_admin": true`。彻底强制重算向量：

```sql
DELETE FROM ai_sql_vector_meta;
```

## 八之二、模型不可用时会发生什么

不会返回 5xx。规划/生成阶段模型失败时，系统会取该用户同一问题上次
成功的 SQL 重放（数据实时查、权限重新校验）；没有可重放的历史就返回
明确的降级说明。响应里的 `degraded` 与审计表的 `degraded` 列都能查到。

```promql
# 降级率突增说明模型侧有问题
sum(rate(ai_sql_degraded_responses_total[5m])) by (reason, failed_stage)
```

```sql
-- 最近发生过降级的查询
SELECT created_at, user_id, query, degraded FROM ai_sql_audit_log
WHERE degraded ORDER BY id DESC LIMIT 20;
```

## 八之三、业务库只读账号

只读的第三层防线是数据库账号本身。配错了平时看不出来，
所以服务启动会自动体检并在日志里喊（`readonly_audit.violation`），
`/readyz` 的 `business_db_accounts` 也能看到。

```bash
# 建最小权限只读角色
python -m ai_sql_ops provision \
  --admin-dsn "postgresql://postgres:***@db:5432/yourdb" \
  --database yourdb --host db

# 体检（只查系统目录，不写数据，退出码非 0 表示不合格，可以进 CI）
python -m ai_sql_ops audit --dsn "$AI_SQL_BUSINESS_DSN"
```

角色自带 `statement_timeout`、`idle_in_transaction_session_timeout`
和 `default_transaction_read_only = on`：应用层配置失效时数据库仍然兜底。

## 九、备份

需要备份的是 PostgreSQL 一个库（Schema 向量、长期记忆向量、审计）
加上容器卷里的 `runtime_data`（记忆原文 SQLite）：

```bash
docker compose -f docker-compose.prod.yml exec postgres \
  pg_dump -U ai_sql ai_sql | gzip > ai_sql_$(date +%F).sql.gz
```
