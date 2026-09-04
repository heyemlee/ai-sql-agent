"""
把 pipeline 的结果归纳成响应 / 审计 / A2A artifact 共用的一份摘要。

以前这段逻辑只在 /query 里，A2A 来了之后 message/send、message/stream 都要同一份
——契约 §4.4 要求 artifact 的 data part 与 /query 的返回**完全同构**，「调用方走 send
还是走 stream，能做的信任判断必须一模一样」。三处各写一遍必然漂移，所以抽出来。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from ai_sql_audit import AuditRecord
from ai_sql_core import log_event, usage_summary
from ai_sql_core.logging_setup import get_logger

_logger = get_logger("api")


class StepLogResponse(BaseModel):
    database: str
    sql: str
    final_sql: str
    success: bool
    row_count: int
    truncated: bool = False
    rows: List[Dict[str, Any]] = []
    error: Optional[str] = None
    repair_attempts: List[Dict[str, Any]] = []
    risk_findings: List[Dict[str, Any]] = []
    """
    软异常校验发现（空结果 / 基数异常 / 语义不一致）。

    必须回传给调用方：这些错误 SQL 跑得通、有结果，不提示的话用户看不出异常，
    会把错的数字当成事实。指标口径上，「答错且没有任何提示」才算静默错误——
    如果提示只写进服务端日志，那对用户来说它依然是静默的。
    """


@dataclass
class ResultSummary:
    route: str
    confidence: float
    reason: str
    answer: str
    queried_database: bool
    step_logs: List[StepLogResponse] = field(default_factory=list)
    selected_databases: List[str] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)
    sqls: List[Dict[str, str]] = field(default_factory=list)
    denied_tables: List[str] = field(default_factory=list)
    failed_steps: int = 0
    repaired_steps: int = 0
    has_risk: bool = False
    degraded: bool = False
    degrade_reason: str = ""


def parse_denied_tables(error: str) -> List[str]:
    """从越权错误信息里取回被拒的表名，落进审计。（与 /query 原实现逐字相同）"""
    marker = "没有访问权限："
    if marker not in error:
        return []
    tail = error.split(marker, 1)[1]
    tail = tail.split("。", 1)[0]
    return [item.strip() for item in tail.split(",") if item.strip()]


def summarize_result(result) -> ResultSummary:
    step_logs: List[StepLogResponse] = []
    selected: List[str] = []
    tables: List[str] = []
    if result.pipeline_result is not None:
        selected = list(result.pipeline_result.selected_databases)
        for log in result.pipeline_result.step_logs:
            execution = log.execution_result
            step_logs.append(StepLogResponse(
                database=log.database, sql=log.sql, final_sql=log.final_sql or log.sql,
                success=bool(execution.get("success")),
                row_count=int(execution.get("row_count") or 0),
                truncated=bool(execution.get("truncated")),
                rows=list(execution.get("rows") or []),
                error=execution.get("error"),
                repair_attempts=list(log.repair_attempts),
                risk_findings=list((log.validation or {}).get("findings") or []),
            ))
        tables = sorted({t for ts in result.pipeline_result.selected_tables.values() for t in ts})

    failed = sum(1 for s in step_logs if not s.success)
    repaired = sum(1 for s in step_logs if s.repair_attempts)
    degraded = bool(result.pipeline_result is not None and result.pipeline_result.degraded)
    return ResultSummary(
        route=result.decision.route.value, confidence=result.decision.confidence,
        reason=result.decision.reason, answer=result.answer,
        queried_database=result.queried_database,
        step_logs=step_logs, selected_databases=selected, tables=tables,
        sqls=[{"database": s.database, "sql": s.final_sql} for s in step_logs if s.final_sql],
        denied_tables=sorted({t for s in step_logs if not s.success and s.error and "没有访问权限" in s.error
                              for t in parse_denied_tables(s.error)}),
        failed_steps=failed, repaired_steps=repaired,
        has_risk=any(s.risk_findings for s in step_logs),
        degraded=degraded,
        degrade_reason=(result.pipeline_result.degrade_reason if result.pipeline_result is not None else ""),
    )


def apply_summary_to_audit(summary: ResultSummary, audit: AuditRecord) -> None:
    audit.route = summary.route
    audit.databases = summary.selected_databases
    audit.tables = summary.tables
    audit.sqls = summary.sqls
    audit.denied_tables = summary.denied_tables
    audit.success = summary.failed_steps == 0 and not summary.degraded
    audit.degraded = summary.degraded
    audit.has_risk = summary.has_risk
    audit.failed_steps = summary.failed_steps
    audit.repaired_steps = summary.repaired_steps


def write_audit(app, audit: AuditRecord, started: float, principal) -> None:
    """写审计记录；审计失败绝不影响查询本身。（与 /query 的 _write_audit 同一实现）"""
    usage = usage_summary()
    audit.duration_ms = round((time.perf_counter() - started) * 1000, 2)
    audit.prompt_tokens = usage["prompt_tokens"]
    audit.completion_tokens = usage["completion_tokens"]
    try:
        app.state.audit.record(audit)
        app.state.limiter.quota_guard.invalidate(principal.user_id)
    except Exception as exc:
        log_event(_logger, logging.ERROR, "audit.write_failed",
                  error=f"{type(exc).__name__}: {exc}")
