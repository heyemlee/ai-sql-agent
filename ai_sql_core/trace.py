from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from .logging_setup import get_logger, log_event
from .metrics import STAGE_DURATION

_trace_id: ContextVar[str] = ContextVar("ai_sql_trace_id", default="")
_usage: ContextVar[Optional["UsageAccumulator"]] = ContextVar(
    "ai_sql_usage", default=None
)

_logger = get_logger("trace")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def get_trace_id() -> str:
    return _trace_id.get()


@contextmanager
def use_trace_id(trace_id: str = "") -> Iterator[str]:
    """
    绑定一次请求的 trace_id。

    同时重置 Token 用量累加器，使一次请求的用量可以独立汇总。
    """
    resolved = trace_id or new_trace_id()
    trace_token = _trace_id.set(resolved)
    usage_token = _usage.set(UsageAccumulator())
    try:
        yield resolved
    finally:
        _trace_id.reset(trace_token)
        _usage.reset(usage_token)


@dataclass
class ModelUsage:
    """单次模型调用的用量。"""

    component: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class UsageAccumulator:
    """请求级 Token 用量累加器。"""

    records: List[ModelUsage] = field(default_factory=list)

    def add(self, usage: ModelUsage) -> None:
        self.records.append(usage)

    def summary(self) -> Dict[str, Any]:
        by_component: Dict[str, int] = {}
        for record in self.records:
            by_component[record.component] = (
                by_component.get(record.component, 0) + record.total_tokens
            )
        return {
            "calls": len(self.records),
            "prompt_tokens": sum(r.prompt_tokens for r in self.records),
            "completion_tokens": sum(r.completion_tokens for r in self.records),
            "total_tokens": sum(r.total_tokens for r in self.records),
            "by_component": by_component,
        }


def record_usage(
    component: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """登记一次模型调用用量；不在请求上下文中时静默忽略。"""
    accumulator = _usage.get()
    if accumulator is None:
        return
    accumulator.add(
        ModelUsage(
            component=component,
            model=model,
            prompt_tokens=int(prompt_tokens or 0),
            completion_tokens=int(completion_tokens or 0),
        )
    )


def usage_summary() -> Dict[str, Any]:
    accumulator = _usage.get()
    if accumulator is None:
        return {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "by_component": {},
        }
    return accumulator.summary()


class StageRecorder:
    """阶段内补充字段的句柄，见 `stage()`。"""

    def __init__(self) -> None:
        self.fields: Dict[str, Any] = {}

    def set(self, **fields: Any) -> None:
        self.fields.update(fields)


@contextmanager
def stage(
    name: str,
    logger: Optional[logging.Logger] = None,
    **initial_fields: Any,
) -> Iterator[StageRecorder]:
    """
    给一个处理阶段计时并输出结构化日志。

        with stage("retrieval", query=query) as st:
            result = service.retrieve(...)
            st.set(databases=result.selected_databases)

    正常结束记 INFO（status=ok），抛异常记 ERROR（status=error）后原样向上抛。
    """
    active_logger = logger or _logger
    recorder = StageRecorder()
    recorder.fields.update(initial_fields)
    started = time.perf_counter()

    try:
        yield recorder
    except Exception as exc:
        STAGE_DURATION.observe(
            time.perf_counter() - started, stage=name, status="error"
        )
        log_event(
            active_logger,
            logging.ERROR,
            f"stage.{name}",
            status="error",
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            error_type=type(exc).__name__,
            error=str(exc),
            **recorder.fields,
        )
        raise
    else:
        STAGE_DURATION.observe(
            time.perf_counter() - started, stage=name, status="ok"
        )
        log_event(
            active_logger,
            logging.INFO,
            f"stage.{name}",
            status="ok",
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            **recorder.fields,
        )
