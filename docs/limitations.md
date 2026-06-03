# Known limits

[English](limitations.md) | [简体中文](limitations.zh-CN.md)

## About the benchmark numbers

The README quotes the 2026-09-09 measurement: 150 demo cases, three-run aggregate, model identifier `claude-sonnet-5`, built-in demo metadata. **The release gate has not passed.**

These numbers do not establish current-commit accuracy on real business databases. The three-run record used an earlier primary-key prompt rule; the current expanded rule has only one full run. Demo metadata also differs from a deployment's semantic layer. See the [measurement record](../benchmark/baseline/2026-09-09/README.md) for conditions, artifacts, and attribution.

## Known limits and unfinished verification (updated 2026-10-14)

- **Conversation-summary input limits are verified offline only.** The model reads bounded per-message excerpts while stored results remain complete. There is no total prompt budget, and the explicit save-to-knowledge-base summary path remains unbounded. Real multi-turn summary quality has not been verified. See [memory verification and limits](modules/memory.md#verification-and-remaining-limits-2026-10-06).
- **Model regression for the English adaptation is incomplete.** Two full gate rounds per side plus a partial third were run before the model key was disabled; the gate fails on the same three metrics before and after, and no full gate has been run on the final code. Record: [benchmark/baseline/2026-10-04](../benchmark/baseline/2026-10-04/README.md). Do not cite those numbers as a baseline.
- **PostgreSQL is not verified on a real database** for anything added here: the identifier-case rules in authorization, the preview SQL (not schema-qualified; structure is read from `public`, names resolve through `search_path`), and the least-privilege binding; and, from the 2026-10-14 authorization fixes, the view catalog scan and view expansion, whole-row references (`e.*`), and the refusal of functions that run a string as SQL (`query_to_xml`, `dblink`, `set_config`, …). Unit tests only.
- **`AI_SQL_MAX_ROWS`, `AI_SQL_MAX_EXECUTION_SECONDS`, `AI_SQL_MAX_RESULT_BYTES` and `AI_SQL_MAX_VALUE_BYTES` are applied only to the API's in-process executors** (defaults 1000 rows / 10 s / 8 MiB / 8 MiB; the first two are the limits that were already in force while the variables were ignored). That is verified on SQLite; the PostgreSQL branch has unit tests against a stubbed driver only. On PostgreSQL the byte budget bounds what is returned, not what the driver has already received, and there is no per-value limit. The MCP stdio server keeps a fixed 1000 rows / 10 s / 8 MiB. Preview pages shrink to the row cap when it is lower than the page size.
- **Views under a restricted policy:** for a role with any deny rule, reading a view counts as reading all its base tables, so it is refused if a base table is denied, has column restrictions, or the view definition cannot be parsed. A table or view created after the last scan is refused until the next schema poll or `POST /admin/schema/reload`; while a database's scan is failing, such roles are refused everything in that database. An output alias that matches a denied column (`1 AS salary … ORDER BY salary`) is refused too. See [authorization model §3.1–3.2 (中文)](authz-model.md).
- **The catalog reads table structure before checking the caller's policy** (cached, single-flight); nothing is returned to a caller who may not see it.
- **Handling of a rejected model credential is verified with mock transports only.** The wording lists that recognise "quota exhausted", "key disabled" and "model not permitted" are heuristics; state is per process; requests already in flight when the first rejection arrives cannot be recalled.
- **English UI:** dictionary text is shown as written when the semantic metadata has no English keys, and server-side free text (routing reason, validator notes, error messages) is Chinese.
- **Routing fallback:** when the routing model is unavailable, the rule-based fallback sends most colloquial Chinese questions (30 of the 40 hold-out phrasings) to conversation instead of a query.
- **Joint integration with the Helpdesk over A2A has not been run** for this change set.
