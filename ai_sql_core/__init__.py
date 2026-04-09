"""
AI SQL Agent 横切基础设施：配置、日志与链路追踪、模型调用治理。

刻意不引入 Agent 框架：编排逻辑留在 ai_sql_pipeline，这里只解决
「生产可运维性」——配置收口、结构化日志、超时重试、错误分类、用量统计。
"""

from .errors import (
    SqlAgentError,
    ModelAuthError,
    ModelBadRequestError,
    ModelCallError,
    ModelCircuitOpenError,
    ModelConnectionError,
    ModelRateLimitError,
    ModelResponseError,
    ModelServerError,
    ModelTimeoutError,
)
from .logging_setup import configure_logging, get_logger, log_event
from .metrics import REGISTRY, render_metrics
from .settings import SqlAgentSettings, get_settings, reload_settings
from .trace import (
    get_trace_id,
    new_trace_id,
    record_usage,
    stage,
    usage_summary,
    use_trace_id,
)

__all__ = [
    "SqlAgentError",
    "SqlAgentSettings",
    "ModelAuthError",
    "ModelBadRequestError",
    "ModelCallError",
    "ModelCircuitOpenError",
    "ModelConnectionError",
    "ModelRateLimitError",
    "ModelResponseError",
    "ModelServerError",
    "ModelTimeoutError",
    "REGISTRY",
    "configure_logging",
    "get_logger",
    "get_settings",
    "get_trace_id",
    "log_event",
    "new_trace_id",
    "record_usage",
    "render_metrics",
    "reload_settings",
    "stage",
    "usage_summary",
    "use_trace_id",
]
