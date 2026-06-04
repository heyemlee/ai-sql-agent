<div align="center">

<img src="docs/logo.svg" width="64" height="64" alt="" />

# AI SQL Agent

**An LLM agent that routes, plans, acts through a guarded tool, repairs and checks its own work, and works with other agents.**

Its one tool is read-only SQL over SQLite and PostgreSQL. Around it: model routing with structured output, multi-step
planning, self-repair, self-verification, short- and long-term memory, A2A and MCP interfaces, evals and tracing, in
one FastAPI service.

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688?style=flat-square&logo=fastapi&logoColor=white)](src/ai_sql_agent/api)
[![Agents](https://img.shields.io/badge/Agents-A2A_%2B_MCP-6E56CF?style=flat-square)](src/ai_sql_agent/a2a)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?style=flat-square&logo=postgresql&logoColor=white)](deploy/docker-compose.prod.yml)
[![CI](https://img.shields.io/badge/CI-GitHub_Actions-2088FF?style=flat-square&logo=githubactions&logoColor=white)](https://github.com/heyemlee/ai-sql-agent/actions/workflows/ci.yml)

**English** · [简体中文](README.zh-CN.md)

[Highlights](#highlights) · [Architecture](#architecture) · [Tech stack](#tech-stack) · [Features](#features) · [Develop](#develop)

</div>

<p align="center">
  <img src="docs/screenshots/ask.webp" width="960" alt="With learning mode on, the console asks which rush orders are still in production and when they are due; stage events stream in while the agent works, the answer lists six orders with customer, delivery date and contract amount, and the walkthrough shows step 1 reading one database and step 2 reading another with step 1's order numbers written into its SQL" />
</p>

<p align="center"><sub>Made-up data in four SQLite databases. The plan and SQL are replayed for the recording; the rows come from running that SQL.</sub></p>

## Highlights

- **Routes and plans.** A model router with JSON-schema output decides whether a request needs the tool or can be
  answered from memory; a planner splits the rest into steps and hands each step's results to the next. Cross-database
  answers are 85.3% correct over three runs of the 150-task benchmark, single-database 93.3%.
- **One tool, behind one gate.** The agent acts only through read-only SQL. An SQLGlot AST gate checks every table and
  column against the caller's permissions and refuses what it can't parse, within 1,000 rows, 10 s and 8 MiB per call;
  213 tests cover it.
- **Repairs and checks its own work.** Failed calls go through a log-driven repair loop, at most twice, and results
  pass rule checks and a model self-check before they are returned. 5.3% of answers are wrong without a warning (gate:
  ≤ 3%), and 5.6% of correct ones are flagged.
- **Context and memory, engineered.** Hybrid retrieval (BM25 and vectors fused with RRF, re-ranked, trimmed to a token
  budget) recalls 100% of the needed columns on 40 held-out phrasings while sending 43.6% of the schema; sessions,
  optional pgvector long-term memory and verified past answers carry context forward.
- **Interoperable, measured, traced.** Other agents call it over A2A (JSON-RPC 2.0 with SSE stage events, contract
  tested in CI), and an MCP server offers its read-only SQL as tools. A release gate with eight thresholds measures it
  over three runs, and every request carries a `trace_id`.

## Architecture

<p align="center">
  <img src="docs/architecture.svg" width="960" alt="The web console and other agents call one FastAPI service, which maps API keys to table and column policies, applies rate limits and token quotas, and can switch the model and embedding connections live. Inside it an agent loop routes each question with an LLM router, retrieves tables and columns with BM25 and vectors fused by RRF, a reranker and a YAML semantic layer, plans one step per data source, writes SQL, runs it read-only through an SQLGlot gate, repairs it at most twice and checks the result with rules and an LLM self-check, keeping conversation memory and an audit trail under a trace_id. Model calls go to an OpenAI-compatible, Anthropic, vLLM or Ollama provider; SQL runs on SQLite or PostgreSQL data sources; memory, audit and vectors are kept in SQLite or pgvector. A separate MCP stdio server uses the same gate, Prometheus scrapes the metrics, and the project ships with Docker Compose, pytest, GitHub Actions and a release gate of 150 + 40 cases." />
</p>

One FastAPI process hosts the agent, its HTTP, A2A and admin endpoints and the console. A question enters through the
API; the agent loop has the model route it, retrieves the tables, has the model plan and write SQL, runs it through the
gate and has the model check the answer, and each card lights up while the request is in it. Orange marks model calls,
blue the request and its data, teal memory, audit and metrics. State lives in SQLite; with a PostgreSQL DSN, audit,
feedback and both vector indexes move to PostgreSQL + pgvector.

- **The model decides, code orchestrates.** The model routes, plans, writes and repairs SQL and judges results; code
  fixes the step order, the permission gate and the limits (one data source per step, two repairs at most), so every
  run is bounded and can be replayed from its `trace_id`.
- **One tool, one gate, fail closed.** Answers, previews and the MCP server all pass the same authorization gate.
  Unparseable SQL is refused, and so is all SQL without SQLGlot; when the model is down, the user's last successful SQL
  for the question is rerun, authorized again and marked degraded.

## Tech stack

| Layer               | Technology                                                                 | Role                                                                                       |
| ------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Runtime             | Python 3.11 · FastAPI · Uvicorn · Pydantic v2                              | One process hosts the agent, its HTTP and A2A endpoints and the console                    |
| Models              | HTTPX · OpenAI-compatible and Anthropic APIs · vLLM · Ollama               | Routing, planning, SQL, repair and self-check calls, with a circuit breaker per model role |
| Orchestration       | Model router with JSON-schema output · step planner · short path           | Decides what to do next and in which order; simple requests skip planning                  |
| Tool and guardrails | SQLGlot AST gate · SQLite authorizer · read-only connections               | The agent's only action, authorized against the caller's policy and capped                 |
| Context             | BM25 · NumPy vectors · RRF · reranker · token budget · YAML semantic layer | Finds the tables and columns a request needs                                               |
| Memory              | Session memory · pgvector long-term memory · summarizer                    | Carries context across turns and sessions                                                  |
| Interop             | A2A (JSON-RPC 2.0 + SSE) · MCP Python SDK                                  | Serves other agents; read-only SQL as MCP tools over stdio                                 |
| Evals               | Release gate · 150 + 40 cases · eight thresholds, three runs               | Accuracy, silent errors, false alarms and recall against gate lines                        |
| Observability       | `trace_id` audit · token accounting · Prometheus · JSON logs               | Every run traceable, every metric scrapeable                                               |
| Console             | HTML · CSS · ES modules, no build step                                     | Ask, Data and History pages; learning mode shows each step                                 |
| Delivery            | pytest · GitHub Actions · Docker Compose                                   | Tests, CI, image smoke test, deployment                                                    |

## Features

### The agent loop

<p align="center">
  <img src="docs/screenshots/agent.webp" width="960" alt="After an answer about rush orders, the follow-up “Which of these is due first?” is answered from the conversation without a new query; then a question about materials below safety stock fails on a column the table does not have, is repaired, and the query process shows the SQL before and after the repair" />
</p>

<p align="center"><sub>Made-up data. The plan, SQL and follow-up answer are replayed; the repair is the project's own repair loop run on that data, on its rule-based path.</sub></p>

- **Understand.** The router chooses between querying and answering from the conversation. Before retrieval, pronouns
  are resolved against earlier turns and phrases like "last quarter" become dates.
- **Plan.** The model writes one step per data source. Each step gets the previous step's first 50 rows as ready-made
  `IN` lists, which keeps prompts bounded and caps a hand-off at 50 rows.
- **Act and repair.** The tool gate authorizes and runs each statement. On failure the error is parsed from the
  execution log, and the failing clause is located and rewritten, by the model or by a nearest-name rule, at most twice.
- **Check.** Empty results, row counts far from the plan, totals nobody asked for and shapes the plan can't produce are
  flagged, then the model checks the result against the question. Findings travel with the answer as risk flags.
- **Remember and learn.** Sessions and optional long-term memory carry context. Answers marked right become examples
  for similar requests; answers marked wrong export as evaluation cases.

Execution accuracy on the 150-task benchmark with the built-in demo databases and `claude-sonnet-5`, mean and range over
three runs:

| Metric                                 |             Result |  Gate |
| -------------------------------------- | -----------------: | ----: |
| Single-database accuracy               | 93.3% [92.8, 93.6] | ≥ 85% |
| Cross-database accuracy                | 85.3% [84.0, 88.0] | ≥ 90% |
| Silent errors (wrong, with no warning) |    5.3% [5.3, 5.3] |  ≤ 3% |
| Correct answers flagged as risky       |    5.6% [4.3, 6.6] | ≤ 20% |
| Column recall, 40 held-out phrasings   |    100% [100, 100] | ≥ 85% |
| Share of columns sent to the model     | 43.6% [43.6, 43.8] | ≤ 40% |

Three lines are missed (cross-database accuracy, silent errors, column share), so this measurement fails the gate.
Annotating JOIN cardinality and de-duplicating across one-to-many joins lifted cross-database accuracy from 73.3% and
cut silent errors from 10.0%. The benchmark measures the agent on its own fixture, not on your data. Records: [accuracy](benchmark/baseline/2026-09-09/README.md) and
[retrieval](benchmark/baseline/2026-09-08/README.md) (in Chinese), [how each threshold was set](benchmark/README.md),
[known limits](docs/limitations.md).

### Interfaces

- **Console.** Ask, Data and History pages in English and Chinese, with no build step. Learning mode explains each
  request from its stage events, and the Data page shows only what the caller may see.
- **A2A.** `message/send` returns a finished task, and `message/stream` sends stage events over SSE before the same
  result as `POST /query`. The agent card is published, and the contract's cases run in CI at a pinned tag.
- **MCP and HTTP.** An MCP stdio server offers `list_databases` and `execute_sql` over the demo databases with the
  same caps; HTTP serves `/query`, `/catalog`, `/history` and `/metrics` ([API](docs/api.md)).

## Develop

```bash
python3 -m venv .venv && source .venv/bin/activate  # a virtual environment for Python 3.11+
pip install -e ".[dev]"                             # the service, its pinned dependencies and test tools
python examples/end_to_end.py                       # single- and cross-database questions, mock model
./scripts/run_api.sh                                # console and API on http://localhost:8000, demo data
cp .env.example .env                                # then set the model provider, key and model names
python -m ai_sql_agent.semantic export semantic/    # a semantic-layer template for your databases
python -m ai_sql_agent.semantic validate semantic/  # check it against the live schema
```

Without model credentials the service answers from a mock model. Point `AI_SQL_SEMANTIC_DIR` at the semantic directory
to query your own databases; setup is in [docs/deployment.md](docs/deployment.md#2-configuration), and another semantic
layer with sample data is in [tools/simple_erp](tools/simple_erp/README.md) (in Chinese).

## Test

```bash
make test        # 1,199 tests with pytest; the 24 that need PostgreSQL skip without AI_SQL_PGVECTOR_DSN
make test-ci     # the unittest entry point CI uses
make gate-smoke  # the release gate on its offline smoke sample
make gate        # the full gate: 150 + 40 tasks, three runs; needs a model and spends tokens
```

CI runs the tests against a PostgreSQL 16 + pgvector service, the A2A contract cases at the pinned tag, the demos, the
offline gate and its negative check, the read-only role tool both ways, and a Docker image smoke test with API keys on.

## Deploy

```bash
cp deploy/api_keys.example.json deploy/api_keys.json  # API keys, roles and table and column policies
make up                                               # the app and PostgreSQL + pgvector, in Docker Compose
make vllm-up                                          # the same with a self-hosted vLLM model server
```

Model credentials, read-only database roles, metrics and backups are in [docs/deployment.md](docs/deployment.md).
Design decisions: [docs/design.md](docs/design.md) · API: [docs/api.md](docs/api.md) · Known limits:
[docs/limitations.md](docs/limitations.md) · Walkthrough: [docs/walkthrough.zh-CN.md](docs/walkthrough.zh-CN.md) (in
Chinese).
