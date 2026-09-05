# 交回 a2a-contract 的问题

> 契约是双边的，这一侧不改它（`a2a-streaming-contract.md` 在
> `heyemlee/a2a-contract`）。发现问题写在这里，交回契约仓库决定。
>
> 放在仓库根而不是 `docs/`：`docs/` 是本地私有草稿区（`.gitignore` 里写着
> 「不公开」），而反馈必须可复核——契约附录 B 自检表第 2 条。

---

## 第一轮（对照 v1.6，2026-09-04）

> 实现 §4.6 时撞到的四条，按「会不会让下一个人写错代码」排序。
> 前两条建议改契约，后两条只是记录。
>
> **结果：前三条已被采纳进契约 v1.7。**

## 1.（建议改）§4.6.7 的「缺失 = failed」与 A 组正例 fixture 直接矛盾

**事实。** §4.6.7 的表写着 `execution_status` 缺失时按 **`failed`** 处置。
而 `fixtures/data-part/valid/01-success-single-step.json`（`dp-valid-01`）里
**没有** `execution_status` 字段，`index.json` 却把它标成 `usable_as_source`。

按 §4.6.7 逐字实现，`dp-valid-01` 会被判成不可信，A 组直接红。
C 组的 `dp-trust-06`（`no_data`）同样如此。

**根因。** §4.6.7 那条规则的前提是这个字段**已经必填**——
`schema/artifact-data-part.schema.json` 的 `$comment` 自己写了：
「🔨 服务端尚未实现——所以不在 required 里。实现之后应当提级为必填」。
两件事之间有一个过渡期，契约没有描述这个过渡期该怎么读。

**这一侧现在怎么做的。** `ai_sql_api/trust.py` 的 `execution_status_of()`：
字段在就用字段，不在就按 §4.6.1 的判据从 `steps[]` 推。§4.6.1 明确说了
五个值「判据全部可由现有 `steps[]` 算出」，所以推导不是发明新规则。

**建议二选一：**

- **A：** §4.6.7 补一句过渡条款——「`execution_status` 提级为必填之前，
  缺失时按 §4.6.1 从 `steps[]` 推导；提级之后缺失即 `failed`」；
- **B：** 现在就把它提级为必填（走 §6），同时给四个 A/C 组 fixture 补上这个字段。

**这一侧已经在发这个字段了**，所以 B 随时可以做。倾向 B：过渡条款是要被忘记的。

## 2.（建议改）§4.5 的 stage 表按「normalization 在最前」排，实现里它在 routing 之后

**事实。** §4.5 的表第一行是 `normalization`，第二行 `routing`。
实际事件顺序是 `routing → normalization → retrieval → …`：
路由发生在 `DynamicSqlAgentService`（要先判断这问题该不该查库），
归一化发生在 `Text2SQLPipeline` 内部（只有查库路径才需要）。

§4.2 保证的是「进度事件之间保持 pipeline 的真实先后顺序」，
所以现在的实现是合规的——**表的行序不是顺序承诺**。

但下一个人照着表写消费端断言（比如「首个 stage 必须是 normalization」）会挂。

**建议：** §4.5 表下补一句「本表按契约面字母/逻辑分组排列，
**不表示事件顺序**；顺序保证见 §4.2」。或者干脆把 `normalization` 挪到
`routing` 后面，与实现一致。

## 3.（只是记录）`normalization` 缺口已关闭，`emitted-2026-09-04` 该更新了

`schema/stage.schema.json` 的 `$defs["emitted-2026-09-04"]` 是八个，
差一个 `normalization`。这一侧已经把它改成真 `stage()`，九个全发。

CONSUMER-TESTS 说「normalization 上线后改断言集合，改集合的那个提交就是
这条缺口关闭的证据」——这一侧的断言已经改成对 `contract-declared`
（`tests/test_contract_fixtures.py` 的 `EmittedStageSetTest`）。

契约侧可以把 `emitted-2026-09-04` 更新成九个，或改名成
`emitted-2026-09-05` 并保留旧的做历史。**这一条不阻塞任何人**，
只是两边的具名集合现在不同步了。

## 4.（只是记录）`dp-trust-05` 的 `truncated=true` 缺 `row_limit`

`fixtures/data-part/trust-negative/05-truncated.json` 里 `truncated: true`，
但没有 `row_limit`。schema 的那条 `if/then` 要求 `truncated` 与 `row_limit`
**同时存在**才校验，所以它现在合法。

§4.6.3 的正文说的是「`truncated == true` 时**必须**有值」。
两者松紧不一致：正文是「必须」，schema 是「有的话得是整数」。

**这一侧已经在发 `row_limit` 了**（执行器打点，见 `mcp_router/objects.py`）。
`row_limit` 提级为必填时，这条 fixture 要一起补。与第 1 条大概率是同一次变更。

---

## 这一侧已经关闭的缺口（供契约更新状态行）

| 契约里的缺口 | 状态 | 证据 |
| --- | --- | --- |
| §4.4 / §4.6：`execution_status` 与三个汇总数未下发 | **已关闭** | `ai_sql_api/result_summary.py` 的 `compute_execution_status`；`tests/test_business_outcome.py` |
| §4.5：九个 stage 实发八个 | **已关闭** | `ai_sql_pipeline/text2sql_pipeline.py` 的 `stage("normalization")`；`tests/test_contract_fixtures.py::EmittedStageSetTest` |
| §4.6.3：`result_truncated` / `row_limit` | **已关闭** | 执行器打点，两条 transport 同时下发 |
| §4.6.4：`as_of` / `as_of_source` | **已关闭** | `mcp_router/data_time.py`；今天全部是 `query_time`（读主库） |
| §4.6.5：`currency` / `column_units` | **已关闭** | 语义层新增 `unit` 键；`ai_sql_pipeline/provenance.py` |
| §4.6.6：`source_records` | **部分关闭** | 结果集带主键列时给得出来，聚合结果给不出来（那时退回 `trace_id` + `final_sql`） |
| §4.5：`database` 生效清单可拉取 | **已关闭** | Agent Card 的 `databases` 字段 + `GET /a2a/databases` |
