from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ai_sql_audit import AuditRecord, create_audit_store
from ai_sql_auth import ApiKeyRecord
from ai_sql_feedback import (
    CertifiedSqlLibrary,
    FeedbackRecord,
    Verdict,
    create_feedback_store,
)
from ai_sql_core.metrics import FEEDBACK_SUBMITTED, REQUEST_DURATION, REQUESTS
from ai_sql_core import (
    CIRCUIT_REGISTRY,
    configure_logging,
    get_logger,
    get_settings,
    get_trace_id,
    log_event,
    render_metrics,
    usage_summary,
    use_trace_id,
)
from ai_sql_memory import ConversationMemoryService
from ai_sql_pipeline import Text2SQLPipeline, DynamicSqlAgentService

from .limits import RequestLimiter
from .result_summary import StepLogResponse, parse_denied_tables, summarize_result, apply_summary_to_audit
from .schema_watcher import SchemaWatcher
from .security import AuthGuard, require_admin, require_api_key

_logger = get_logger("api")

TRACE_HEADER = "X-Trace-Id"
STATIC_DIR = Path(__file__).resolve().parent / "static"


class QueryRequest(BaseModel):
    """自然语言查询请求。"""

    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    enable_long_term: bool = False


# StepLogResponse 挪到了 result_summary：/query、A2A send、A2A stream 三处共用同一份（契约 §4.4）


class QueryResponse(BaseModel):
    trace_id: str = ""
    """本次查询的链路 ID，与服务端日志一一对应，排查时直接拿它检索。"""

    degraded: bool = False
    """模型不可用时走了降级链路，结果可能来自历史 SQL 重放。"""

    degrade_reason: str = ""

    route: str
    confidence: float
    reason: str
    answer: str
    queried_database: bool
    selected_databases: List[str] = []

    steps: List[StepLogResponse] = []
    """
    逐步骤日志。**契约面的名字是 `steps`**（§4.4.1）。

    A2A 那两条一直叫 `steps`，只有直连 `/query` 叫 `step_logs`——同一个
    `StepLogResponse`，两个名字。后果不是「字段名不好看」：调用方按 §4.6.7
    处理缺失字段，`steps` 取不到就按「缺失 = 不可信」处置，
    **于是每一次直连查询都不再是出处，而链路看起来完全正常**——
    没有报错、没有降级事件、没有任何症状。而 §6.2 的回滚恰好把流量切到这条路上。
    """

    step_logs: List[StepLogResponse] = []
    """
    **已弃用，与 `steps` 内容完全相同。** 过渡期两个字段同时发，不破坏现有消费方；
    删除它要走契约 §6 的变更流程，届时两侧同时删。

    契约明确拒绝了「在契约里承认第三种形状、写一张 step_logs ↔ steps 翻译表」
    ——那是把分叉制度化，此后每个新消费方都要重踩一次。
    """

    has_risk: bool = False
    """任一步骤带有软异常提示。前端据此提醒用户「这条结果需要核对」。"""

    # ---- 契约 §4.6 业务终态语义 ----
    #
    # 与 A2A artifact 的 data part 是同一套字段（都由 ResultSummary.data_part()
    # 产出）。契约 §4.4 要求两条 transport 能做的信任判断一模一样，「走哪条路
    # 信息更多」本身就是缺陷。
    execution_status: str = "failed"
    """§4.6.1：这次查库到底跑成什么样。缺失/算不出来按 failed 处置。"""

    succeeded_steps: int = 0
    failed_steps: int = 0
    repaired_steps: int = 0
    result_truncated: bool = False
    """§4.6.3：任一步被截断。为真时「共有 N 条 / 全部 / 一共」这类说法都不成立。"""

    as_of: Optional[str] = None
    """§4.6.4：数据时点。为空时调用方不得表述「截至今天 / 目前 / 最新」。"""

    as_of_source: Optional[str] = None
    currency: Optional[str] = None
    """§4.6.5：本轮金额列的币种。多币种时为空，逐列读 step_logs[].column_units。"""


class HistoryItem(BaseModel):
    """一条历史查询（不含结果集，结果不落审计）。"""

    trace_id: str
    created_at: str
    query: str
    route: str = ""
    databases: List[str] = []
    tables: List[str] = []
    sqls: List[Dict[str, str]] = []
    success: bool = True
    degraded: bool = False
    duration_ms: float = 0.0
    total_tokens: int = 0
    error: Optional[str] = None


class HistoryResponse(BaseModel):
    user_id: str
    items: List[HistoryItem] = []


class FeedbackRequest(BaseModel):
    """
    用户对一次回答的判定。

    只收 trace_id 与判定本身：问题原文与实际执行的 SQL 从审计记录回填，
    不接受客户端传。否则「这条 SQL 是对的」就成了客户端说了算，
    而认证 SQL 会被注入后续查询的生成 Prompt。
    """

    trace_id: str = Field(min_length=1)
    verdict: Literal["correct", "wrong", "unsure"]
    comment: str = Field(default="", max_length=2000)


class FeedbackResponse(BaseModel):
    recorded: bool
    trace_id: str
    verdict: str
    certified: bool = False
    """这条查询是否会作为认证范例参与后续生成。"""

    message: str = ""


class SaveMemoryRequest(BaseModel):
    """用户主动保存结构化结果到个人知识库。"""

    user_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    result: Dict[str, Any]
    database: str = ""
    tables: List[str] = []
    columns: List[str] = []
    sql: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()

    app.state.auth = AuthGuard()
    app.state.audit = create_audit_store()
    app.state.limiter = RequestLimiter(app.state.audit)

    log_event(
        _logger,
        logging.INFO,
        "api.startup",
        auth_mode=settings.auth_mode,
        pgvector=bool(settings.pgvector_dsn),
        model_provider=settings.model_provider,
        chat_model=settings.chat_model,
        coder_model=settings.coder_model,
        model_credentials=settings.has_model_credentials,
        audit=settings.audit_enabled,
        schema_refresh_seconds=settings.schema_refresh_seconds,
        rate_limit_per_minute=settings.default_rate_limit_per_minute,
        max_rows=settings.max_rows,
        max_execution_seconds=settings.max_execution_seconds,
    )

    memory = ConversationMemoryService()
    audit_store = app.state.audit
    app.state.feedback = create_feedback_store()

    # 用户点过「对」的查询 → 同类问题的生成范例。
    # 关掉开关时不注入：这条链路会占 Prompt 预算，值不值得要能对照测量。
    certified_library = (
        CertifiedSqlLibrary(
            loader=lambda user_id: app.state.feedback.certified(user_id=user_id),
            top_k=settings.certified_sql_top_k,
        )
        if settings.certified_sql_enabled and settings.feedback_enabled
        else None
    )
    app.state.certified_library = certified_library

    pipeline = Text2SQLPipeline(
        # 模型不可用时可以重放这位用户同一问题上次成功的 SQL
        sql_history_lookup=audit_store.last_successful_sqls,
        # 模型正常时，把确认过正确的相似查询当写法参考注入
        certified_lookup=certified_library.lookup if certified_library else None,
    )

    app.state.memory = memory
    app.state.pipeline = pipeline
    app.state.service = DynamicSqlAgentService(pipeline, memory)

    app.state.schema_watcher = SchemaWatcher(
        pipeline, settings.schema_refresh_seconds
    )
    app.state.schema_watcher.start()

    app.state.readonly_audits = _audit_business_databases(pipeline)

    # 生产模式的启动闸：不合格就**不启动**，不是打个警告继续跑。
    check_production_readiness(app)

    try:
        yield
    finally:
        _shutdown(app)


class ProductionReadinessError(RuntimeError):
    """生产模式下配置不合格。进程应当起不来。"""


def check_production_readiness(app: FastAPI) -> List[str]:
    """
    生产模式（`AI_SQL_APP_ENV=production`）的启动闸。

    以前这些都是「警告」：readiness 明确把「缺模型凭证走 Mock」当成可就绪，
    启动只读审计发现违规也只是 log 一条 WARNING。**警告在生产里等于没有**——
    没有人会在凌晨看日志，而这几项里任何一项不合格，服务都会以一种看起来
    完全正常的方式给出错误答案：Mock 模型编 SQL、示例库返回演示数据、
    权限没配就是全放开、账号能写就等着某天模型生成一条 DELETE。

    开发模式下全部降级为警告——本地跑 demo 与测试不该被这些挡住。
    返回问题清单，方便测试直接断言。
    """
    settings = get_settings()
    problems: List[str] = []

    if not settings.has_model_credentials:
        problems.append(
            "没有模型凭证，链路会走 Mock。Mock 会编出语法正确但内容错误的 SQL，"
            "而且不会有任何症状。"
        )
    if settings.auth_mode != "api_key":
        problems.append(
            f"AI_SQL_AUTH_MODE={settings.auth_mode}：鉴权关闭时所有请求都是"
            "匿名身份、全部权限。"
        )
    if settings.unknown_role_policy != "deny":
        problems.append(
            "AI_SQL_UNKNOWN_ROLE_POLICY=full_access：角色没匹配到策略就给全部权限，"
            "配错一个角色名的后果是全库可读。"
        )
    if not settings.semantic_dir:
        problems.append(
            "没有配置 AI_SQL_SEMANTIC_DIR，用的是内置演示元数据与演示库。"
        )

    unknown_databases = _policy_databases_not_in_semantic_layer(app)
    if unknown_databases:
        problems.append(
            "这些策略引用了语义层里没有的库标识符："
            + "、".join(unknown_databases)
            + "。deny by default 之下，这类配错的后果是那把 Key 什么都查不到，"
            "而且没有任何症状。"
        )

    keys_without_roles = _keys_without_roles(app)
    if keys_without_roles:
        problems.append(
            "这些 API Key 没有声明 roles，等于不做数据权限限制："
            + "、".join(keys_without_roles)
        )

    writable = [
        user
        for user, verdict in getattr(app.state, "readonly_audits", {}).items()
        if verdict != "readonly"
    ]
    if writable:
        problems.append(
            "这些业务库账号不是只读：" + "、".join(writable)
            + "。应用层校验是第二道防线，不能是唯一一道。"
        )

    if settings.app_env != "production":
        for problem in problems:
            log_event(_logger, logging.WARNING, "production_readiness.warning",
                      problem=problem, app_env=settings.app_env)
        return problems

    if problems:
        for problem in problems:
            log_event(_logger, logging.ERROR, "production_readiness.failed",
                      problem=problem)
        raise ProductionReadinessError(
            "生产模式启动检查未通过：\n  - " + "\n  - ".join(problems)
        )

    log_event(_logger, logging.INFO, "production_readiness.ok")
    return []


def _policy_databases_not_in_semantic_layer(app: FastAPI) -> List[str]:
    """
    策略里写的库标识符，语义层里有没有。

    这类错误现在是**完全静默**的：`deploy/api_keys.example.json` 一度还写着
    `pm_db` / `mes_db`（契约 v1.6 已标退役），配上 deny by default 之后
    那把 Key 什么都查不到——既没有报错，也没有「你没权限」的提示，
    因为策略语法完全合法，只是指向了一个不存在的库。

    返回 `角色:库名` 的列表。含通配符的库段跳过（`*` 本来就是「所有库」）。
    """
    store = getattr(getattr(app.state, "auth", None), "store", None)
    pipeline = getattr(app.state, "pipeline", None)
    if store is None or pipeline is None:
        return []

    known = {database.name for database in pipeline.databases}
    if not known:
        return []

    found: List[str] = []
    for role, policy in getattr(store, "policies", {}).items():
        rules = (
            list(policy.allow_tables)
            + list(policy.deny_tables)
            + list(policy.deny_columns)
        )
        for rule in rules:
            head = rule.split(".", 1)[0]
            if not head or any(ch in head for ch in "*?["):
                continue
            if head not in known:
                found.append(f"{role}:{head}")
    return sorted(set(found))


def _keys_without_roles(app: FastAPI) -> List[str]:
    store = getattr(getattr(app.state, "auth", None), "store", None)
    if store is None:
        return []
    return sorted(
        record.caller
        for record in store.records.values()
        if not record.roles
    )


def _audit_business_databases(pipeline) -> Dict[str, Any]:
    """
    启动时体检业务库账号是不是真的只读。

    只读约束有三层（应用层 SQL 校验、连接层只读事务、数据库账号权限），
    第三层配错了平时看不出来——直到某天模型生成了一条 DELETE。
    这里在启动时就把问题喊出来，而不是等出事。
    """
    from ai_sql_ops import audit_business_databases

    summary: Dict[str, Any] = {}
    try:
        audits = audit_business_databases(pipeline.databases)
    except Exception as exc:
        log_event(
            _logger,
            logging.WARNING,
            "readonly_audit.failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        return summary

    for audit in audits:
        summary[audit.user] = "readonly" if audit.is_readonly else "writable"
        if audit.is_readonly:
            log_event(
                _logger, logging.INFO, "readonly_audit.ok", user=audit.user
            )
        else:
            log_event(
                _logger,
                logging.WARNING,
                "readonly_audit.violation",
                user=audit.user,
                problems=audit.problems(),
                hint="用 python -m ai_sql_ops provision 建只读账号后改用它的 DSN",
            )
    return summary


def _shutdown(app: FastAPI) -> None:
    """优雅关闭：等待记忆摘要线程收尾，释放 HTTP 连接池。"""
    from ai_sql_a2a.server import shutdown_stream_executor
    from ai_sql_core.transport import close_http_client

    log_event(_logger, logging.INFO, "api.shutdown", stage="begin")
    shutdown_stream_executor()

    watcher = getattr(app.state, "schema_watcher", None)
    if watcher is not None:
        watcher.stop()

    memory = getattr(app.state, "memory", None)
    if memory is not None:
        memory.close(wait=True)

    close_http_client()

    log_event(_logger, logging.INFO, "api.shutdown", stage="done")


app = FastAPI(
    title="AI SQL Agent API",
    description="自然语言数据查询 Agent：动态路由 + 三层 Schema 检索 + "
    "CoT 规划 + SQL 生成 + MCP 路由执行 + 长短期记忆。",
    version="1.0.0",
    lifespan=lifespan,
)


# A2A：Agent Card + JSON-RPC /a2a（message/send、message/stream）。
# 与 /query 同一套鉴权、限流、审计；流式开关见 settings.a2a_streaming。
from ai_sql_a2a import router as _a2a_router  # noqa: E402

app.include_router(_a2a_router)


@app.middleware("http")
async def trace_requests(request: Request, call_next):
    """
    为每个请求绑定 trace_id，并记录一条请求级汇总日志。

    调用方可通过 X-Trace-Id 头透传上游链路 ID；响应头原样返回，
    便于前端把用户看到的一次查询和后端日志对上。
    """
    incoming = request.headers.get(TRACE_HEADER, "")

    with use_trace_id(incoming) as trace_id:
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[TRACE_HEADER] = trace_id
            return response
        finally:
            elapsed = time.perf_counter() - started
            path = request.url.path
            REQUESTS.inc(
                method=request.method, path=path, status=str(status_code)
            )
            REQUEST_DURATION.observe(elapsed, method=request.method, path=path)
            log_event(
                _logger,
                logging.INFO,
                "api.request",
                method=request.method,
                path=path,
                status_code=status_code,
                duration_ms=round(elapsed * 1000, 2),
                usage=usage_summary(),
            )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """
    查询界面。

    单页原生 HTML/JS，无构建步骤：接口怎么变，页面跟着改一个文件就行。
    页面自己不做鉴权，API Key 存在浏览器本地，请求时放在 X-API-Key 头里。
    """
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> Dict[str, str]:
    """
    存活探针：只回答“进程还在不在”，不查任何外部依赖。

    依赖抖动不应该让编排系统重启进程，那是就绪探针的职责。
    """
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> Dict[str, Any]:
    """
    就绪探针：检查外部依赖，任一不可用就摘流量但不重启。

    检查项：记忆库可读写、向量后端可用（配置了 pgvector 时真连一次）、
    审计存储可写、模型凭证是否配置（缺失只降级为 Mock，不算不就绪）。
    """
    settings = get_settings()
    checks: Dict[str, Any] = {}
    ready = True

    try:
        app.state.memory.store.list_messages(user_id="__probe__", session_id="__probe__")
        checks["memory_store"] = "ok"
    except Exception as exc:
        checks["memory_store"] = f"error: {exc}"
        ready = False

    try:
        app.state.audit.daily_usage("__probe__")
        checks["audit_store"] = "ok"
    except Exception as exc:
        checks["audit_store"] = f"error: {exc}"
        ready = False

    if settings.pgvector_dsn:
        try:
            import psycopg

            conn = psycopg.connect(settings.pgvector_dsn, connect_timeout=3)
            conn.close()
            checks["pgvector"] = "ok"
        except Exception as exc:
            checks["pgvector"] = f"error: {exc}"
            ready = False
    else:
        checks["pgvector"] = "not_configured (内存向量索引)"

    if settings.has_model_credentials:
        checks["model_credentials"] = "ok"
    else:
        # 生产模式下这不是「降级」，是不就绪。Mock 会编出语法正确、内容错误的
        # SQL，而且从响应上看不出区别——把它算成 ready 等于把编造当成服务。
        checks["model_credentials"] = "missing (模型走 Mock)"
        if settings.app_env == "production":
            ready = False
    checks["model_provider"] = f"{settings.model_provider} ({settings.chat_model})"
    checks["auth_mode"] = settings.auth_mode

    # 熔断状态只做展示：模型挂了会走降级链路，服务本身仍然可用，
    # 因此不把它算进 ready 判定，否则会被摘掉流量反而没法降级服务。
    circuits = {
        name: snapshot["state"]
        for name, snapshot in CIRCUIT_REGISTRY.snapshot().items()
    }
    if circuits:
        checks["model_circuits"] = circuits

    readonly_audits = getattr(app.state, "readonly_audits", {})
    if readonly_audits:
        checks["business_db_accounts"] = readonly_audits

    if not ready:
        raise HTTPException(status_code=503, detail={"ready": False, "checks": checks})

    return {"ready": True, "checks": checks}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    """Prometheus 抓取端点（免鉴权，按内网访问设计）。"""
    return render_metrics()


@app.post("/admin/schema/reload")
def reload_schema(principal=Depends(require_admin)) -> Dict[str, Any]:
    """
    手动重建 Schema 索引。

    业务库改了结构又不想等下一个轮询周期时用；
    定时轮询由 SchemaWatcher 负责（AI_SQL_SCHEMA_REFRESH_SECONDS）。
    """
    changed = app.state.pipeline.reload_schema(reason=f"manual:{principal.caller}")
    return {
        "reloaded": True,
        "changed_databases": changed,
        "fingerprints": {
            name: value[:12]
            for name, value in app.state.pipeline.schema_fingerprints.items()
        },
    }


@app.post("/admin/circuit/reset")
def reset_circuits(principal=Depends(require_admin)) -> Dict[str, Any]:
    """
    手动复位模型熔断器。

    模型侧确认恢复后不想等冷却时用。
    """
    before = CIRCUIT_REGISTRY.snapshot()
    CIRCUIT_REGISTRY.reset_all()
    log_event(
        _logger,
        logging.INFO,
        "circuit.manual_reset",
        caller=principal.caller,
        before={name: item["state"] for name, item in before.items()},
    )
    return {"reset": True, "before": before, "after": CIRCUIT_REGISTRY.snapshot()}


@app.get("/admin/schema/status")
def schema_status(principal=Depends(require_admin)) -> Dict[str, Any]:
    """查看各库当前指纹与是否有未生效的变更。"""
    watcher = app.state.schema_watcher
    return {
        "watcher_enabled": watcher.enabled,
        "interval_seconds": watcher.interval_seconds,
        "pending_changes": app.state.pipeline.detect_schema_changes(),
        "fingerprints": {
            name: value[:12]
            for name, value in app.state.pipeline.schema_fingerprints.items()
        },
    }


@app.post("/query", response_model=QueryResponse)
def query(
    request: QueryRequest,
    api_key: Optional[ApiKeyRecord] = Depends(require_api_key),
) -> QueryResponse:
    """
    自然语言查询入口。

    动态路由决定进入 Text2SQL 链路还是对话问答链路。
    每次调用都会落一条审计记录（含 trace_id、SQL、命中表、token 用量）。
    """
    principal = app.state.auth.resolve_principal(api_key, request.user_id)
    app.state.limiter.check(principal)

    started = time.perf_counter()
    audit = AuditRecord(
        trace_id=get_trace_id(),
        caller=principal.caller,
        user_id=principal.user_id,
        session_id=request.session_id,
        query=request.query,
    )

    try:
        result = app.state.service.run(
            user_id=principal.user_id,
            session_id=request.session_id,
            query=request.query,
            enable_long_term=request.enable_long_term,
            access=principal.policy,
        )
    except ValueError as exc:
        audit.success = False
        audit.status_code = 400
        audit.error = str(exc)
        _write_audit(audit, started, principal)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        audit.success = False
        audit.status_code = 500
        audit.error = f"{type(exc).__name__}: {exc}"
        _write_audit(audit, started, principal)
        raise

    summary = summarize_result(result)
    apply_summary_to_audit(summary, audit)
    step_logs = summary.step_logs
    selected_databases = summary.selected_databases
    failed_steps = summary.failed_steps
    repaired_steps = summary.repaired_steps
    has_risk = summary.has_risk
    degraded = summary.degraded
    degrade_reason = summary.degrade_reason
    _write_audit(audit, started, principal)

    log_event(
        _logger,
        logging.INFO,
        "api.query",
        caller=principal.caller,
        user_id=principal.user_id,
        session_id=request.session_id,
        route=result.decision.route.value,
        confidence=result.decision.confidence,
        queried_database=result.queried_database,
        databases=selected_databases,
        steps=len(step_logs),
        failed_steps=failed_steps,
        repaired_steps=repaired_steps,
        degraded=degraded,
        degrade_reason=degrade_reason,
        has_risk=has_risk,
        execution_status=summary.execution_status,
    )

    return QueryResponse(
        trace_id=get_trace_id(),
        degraded=degraded,
        degrade_reason=degrade_reason,
        route=result.decision.route.value,
        confidence=result.decision.confidence,
        reason=result.decision.reason,
        answer=result.answer,
        queried_database=result.queried_database,
        selected_databases=selected_databases,
        # 过渡期同一份数据序列化两次（契约 §4.4.1）
        steps=step_logs,
        step_logs=step_logs,
        has_risk=has_risk,
        execution_status=summary.execution_status,
        succeeded_steps=summary.succeeded_steps,
        failed_steps=failed_steps,
        repaired_steps=repaired_steps,
        result_truncated=summary.result_truncated,
        as_of=summary.as_of,
        as_of_source=summary.as_of_source,
        currency=summary.currency,
    )


_parse_denied_tables = parse_denied_tables  # 实现在 result_summary，这里保留名字


def _write_audit(audit: AuditRecord, started: float, principal) -> None:
    """写审计记录；审计失败绝不影响查询本身。"""
    usage = usage_summary()
    audit.duration_ms = round((time.perf_counter() - started) * 1000, 2)
    audit.prompt_tokens = usage["prompt_tokens"]
    audit.completion_tokens = usage["completion_tokens"]

    try:
        app.state.audit.record(audit)
        app.state.limiter.quota_guard.invalidate(principal.user_id)
    except Exception as exc:
        log_event(
            _logger,
            logging.ERROR,
            "audit.write_failed",
            error=f"{type(exc).__name__}: {exc}",
        )


@app.get("/history", response_model=HistoryResponse)
def history(
    request: Request,
    limit: int = 20,
    user_id: str = "",
    api_key: Optional[ApiKeyRecord] = Depends(require_api_key),
) -> HistoryResponse:
    """
    最近查询记录。

    默认只返回调用方自己的记录；管理员可以用 user_id 查他人，
    普通 Key 传别人的 user_id 会被拒绝——历史里有问题原文和 SQL，
    等同于数据访问面。
    """
    principal = app.state.auth.resolve_principal(api_key, "")

    target = user_id or principal.user_id
    if target != principal.user_id and not principal.is_admin:
        raise HTTPException(status_code=403, detail="只能查看自己的历史查询。")

    limit = max(1, min(int(limit), 100))
    rows = app.state.audit.recent(limit=limit, user_id=target)

    items = [
        HistoryItem(
            trace_id=row.get("trace_id", ""),
            created_at=str(row.get("created_at", "")),
            query=row.get("query", ""),
            route=row.get("route", ""),
            databases=list(row.get("databases") or []),
            tables=list(row.get("tables") or []),
            sqls=[
                item
                for item in (row.get("sqls") or [])
                if isinstance(item, dict)
            ],
            success=bool(row.get("success")),
            degraded=bool(row.get("degraded")),
            duration_ms=float(row.get("duration_ms") or 0),
            total_tokens=int(row.get("prompt_tokens") or 0)
            + int(row.get("completion_tokens") or 0),
            error=row.get("error"),
        )
        for row in rows
    ]

    return HistoryResponse(user_id=target, items=items)


@app.post("/feedback", response_model=FeedbackResponse)
def submit_feedback(
    request: FeedbackRequest,
    api_key: Optional[ApiKeyRecord] = Depends(require_api_key),
) -> FeedbackResponse:
    """
    记录用户对一次回答的判定（对 / 错 / 说不准）。

    这是让工具越用越准的那条回路：

    - 标记「错」的问题进反面教材清单，用 `python -m ai_sql_feedback export-dataset`
      导进评测集——评测集原本全是我们自己出的模板题，真实用户问法与它的差距，
      正是线上准确率与评测跑分的差距；
    - 标记「对」的查询成为认证范例，注入后续同类问题的生成 Prompt。

    问题原文与 SQL 都按 trace_id 从审计记录回填，不接受客户端传。
    """
    settings = get_settings()
    if not settings.feedback_enabled:
        raise HTTPException(status_code=404, detail="反馈功能未启用。")

    principal = app.state.auth.resolve_principal(api_key, "")

    audited = app.state.audit.get_by_trace(request.trace_id)
    if audited is None:
        raise HTTPException(
            status_code=404,
            detail=f"没有找到 trace_id={request.trace_id} 的查询记录。",
        )

    owner = str(audited.get("user_id") or "")
    if owner != principal.user_id and not principal.is_admin:
        # 反馈会影响别人的认证范例，越权表态等于替别人认证 SQL
        log_event(
            _logger,
            logging.WARNING,
            "feedback.denied",
            caller=principal.caller,
            trace_id=request.trace_id,
            owner=owner,
        )
        raise HTTPException(status_code=403, detail="只能对自己的查询提交反馈。")

    verdict = Verdict(request.verdict)
    feedback = FeedbackRecord(
        trace_id=request.trace_id,
        user_id=owner or principal.user_id,
        verdict=verdict,
        query=str(audited.get("query") or ""),
        databases=list(audited.get("databases") or []),
        tables=list(audited.get("tables") or []),
        sqls=[
            item for item in (audited.get("sqls") or []) if isinstance(item, dict)
        ],
        comment=request.comment.strip(),
        caller=principal.caller,
        route=str(audited.get("route") or ""),
        had_risk=bool(audited.get("has_risk")),
    )

    try:
        app.state.feedback.record(feedback)
    except Exception as exc:
        log_event(
            _logger,
            logging.ERROR,
            "feedback.write_failed",
            trace_id=request.trace_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise HTTPException(status_code=500, detail="反馈写入失败。") from exc

    certified = feedback.is_certifiable and get_settings().certified_sql_enabled

    log_event(
        _logger,
        logging.INFO,
        "feedback.recorded",
        trace_id=request.trace_id,
        user_id=feedback.user_id,
        verdict=verdict.value,
        had_risk=feedback.had_risk,
        certified=certified,
        has_comment=bool(feedback.comment),
    )
    FEEDBACK_SUBMITTED.inc(verdict=verdict.value, had_risk=str(feedback.had_risk).lower())

    if verdict == Verdict.CORRECT:
        message = (
            "已记录。这条查询会作为参考写法用于后续同类问题。"
            if certified
            else "已记录。"
        )
    elif verdict == Verdict.WRONG:
        message = "已记录。这条会进待改进清单，用于补评测集和排查元数据缺口。"
    else:
        message = "已记录，会有人来看一眼。"

    return FeedbackResponse(
        recorded=True,
        trace_id=request.trace_id,
        verdict=verdict.value,
        certified=certified,
        message=message,
    )


@app.get("/feedback/stats")
def feedback_stats(principal=Depends(require_admin)) -> Dict[str, Any]:
    """
    线上反馈汇总：真实用户口径的准确率与静默错误率。

    与 benchmark 的区别是题目来自真实用户，不是我们自己出的。
    两个数差得远时，以这个为准——评测集只代表我们想到的问法。
    """
    stats = app.state.feedback.stats()
    unresolved = app.state.feedback.recent(limit=10, verdict=Verdict.WRONG.value)
    return {
        "stats": stats.to_dict(),
        "recent_wrong": [
            {
                "trace_id": item.get("trace_id"),
                "query": item.get("query"),
                "comment": item.get("comment"),
                "had_risk": item.get("had_risk"),
                "resolved": item.get("resolved"),
            }
            for item in unresolved
        ],
    }


@app.post("/memory/save")
def save_memory(
    request: SaveMemoryRequest,
    api_key: Optional[ApiKeyRecord] = Depends(require_api_key),
) -> Dict[str, Any]:
    """
    响应用户“保存到个人知识库”动作。
    """
    principal = app.state.auth.resolve_principal(api_key, request.user_id)
    app.state.limiter.check(principal)

    memory = app.state.memory.save_structured_result(
        user_id=principal.user_id,
        query=request.query,
        result=request.result,
        database=request.database,
        tables=request.tables,
        columns=request.columns,
        sql=request.sql,
    )
    return {"memory_id": memory.id, "summary": memory.summary}
