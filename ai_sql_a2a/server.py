from __future__ import annotations

import asyncio
import json
import logging
import queue
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ai_sql_audit import AuditRecord
from ai_sql_core import get_settings, get_trace_id, log_event
from ai_sql_core.logging_setup import get_logger
from ai_sql_core.trace import use_stage_sink

# ai_sql_api 在导入本包（app.include_router），所以这里的反向导入必须延迟到调用时，
# 否则是循环导入。函数级 import 在这里不是坏味道，是方向使然。

_logger = get_logger("a2a")
router = APIRouter()

PROTOCOL_VERSION = "0.2"
CARD_VERSION = "1.1.0"          # 契约 §6.1：从 1.0.0 提到 1.1.0 作为「有了流式」的标记

# JSON-RPC 错误码。-32004 是 A2A 规范的 UnsupportedOperationError，
# 调用方读到它就退回 message/send（契约 §6.2 的回滚路径）。
E_PARSE, E_INVALID_REQUEST, E_METHOD_NOT_FOUND, E_INVALID_PARAMS = -32700, -32600, -32601, -32602
E_UNSUPPORTED = -32004
E_INTERNAL = -32603

# ── 契约 §4.5：stage 映射表 ──────────────────────────────────────────────────
#
# 契约面是右边这一列，不是 pipeline 内部的名字。内部把 sql_generation 改名不影响
# 契约；改这张表就是一次契约变更。memory_context 刻意不发（内部管道，对用户没有
# 展示价值）。normalization 今天还不是真 stage（契约 §8.2 待办），所以不会出现。
_STAGE_MAP = {
    "routing": "routing",
    "retrieval": "retrieval",
    "planning": "planning",
    "sql_generation": "generation",
    "execution": "execution",
    "repair": "repair",
    "validation": "validation",
    "data_qa": "answering",
}
_STAGE_TEXT = {
    "normalization": "正在理解问题",
    "routing": "正在判断问题类型",
    "retrieval": "正在检索知识库",
    "planning": "正在规划查询步骤",
    "generation": "正在生成查询",
    "execution": "正在查询数据库",
    "repair": "查询出错，正在重试",
    "validation": "正在校验结果",
    "answering": "正在组织回答",
}
# 进度事件只带进入 stage 时就已知的字段（契约 §4.5「附带字段」的诚实说明）。
_STAGE_FIELDS = ("step", "database", "dialect", "with_memory", "short_path")


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, http_status: int = 200,
                 headers: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.code, self.message, self.http_status, self.headers = code, message, http_status, headers or {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _error_response(req_id: Any, err: JsonRpcError) -> JSONResponse:
    body = {"jsonrpc": "2.0", "id": req_id, "error": {"code": err.code, "message": err.message}}
    return JSONResponse(body, status_code=err.http_status, headers=err.headers)


# ── Agent Card ────────────────────────────────────────────────────────────────

def build_agent_card(base_url: str) -> Dict[str, Any]:
    settings = get_settings()
    return {
        "name": "ai-sql-agent",
        "description": "把自然语言问题翻成只读 SQL 去业务库里查，返回答案与执行的 SQL。",
        "url": f"{base_url.rstrip('/')}/a2a",
        "version": CARD_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {
            # 由开关决定（契约 §6.2）。关 = message/stream 返回 -32004。
            "streaming": bool(settings.a2a_streaming),
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [{
            "id": "text2sql",
            "name": "业务数据查询",
            "description": "订单、生产、库存、账款等业务问题，查真实数据后回答。",
            "tags": ["sql", "erp", "orders"],
        }],
    }


@router.get("/.well-known/agent.json", include_in_schema=False)
def agent_card(request: Request) -> Dict[str, Any]:
    return build_agent_card(str(request.base_url))


# ── 请求解析与准入（流打开之前）──────────────────────────────────────────────

@dataclass
class ParsedMessage:
    query: str
    session_id: str
    user_id: str
    message_id: str


def _parse_message(params: Any) -> ParsedMessage:
    """
    `params.message` 的解析规则，send 与 stream 完全一致（契约 §3.1）：
    parts 里的 text 拼接、contextId 当会话、metadata.user_id 当身份。
    """
    if not isinstance(params, dict) or not isinstance(params.get("message"), dict):
        raise JsonRpcError(E_INVALID_PARAMS, "params.message 缺失或不是对象")
    msg = params["message"]
    parts = msg.get("parts") or []
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("kind") == "text"]
    query = "\n".join(t for t in texts if t).strip()
    if not query:
        raise JsonRpcError(E_INVALID_PARAMS, "message.parts 里没有任何 text part")
    meta = msg.get("metadata") or {}
    return ParsedMessage(
        query=query,
        session_id=str(msg.get("contextId") or "").strip() or uuid.uuid4().hex[:12],
        user_id=str(meta.get("user_id") or "").strip() or "anonymous",
        message_id=str(msg.get("messageId") or uuid.uuid4().hex),
    )


def _admit(request: Request, user_id: str):
    """
    鉴权 + 限流，**一律发生在流打开之前**（契约 §3.2 / §5.5）。
    失败返回普通 JSON-RPC 错误，不是 SSE——调用方不该为了知道「我没通过鉴权」
    而去解析一个事件流。
    """
    from ai_sql_api.security import require_api_key

    try:
        record = require_api_key(request)
        principal = request.app.state.auth.resolve_principal(record, user_id)
        request.app.state.limiter.check(principal)
        return principal
    except HTTPException as exc:
        headers = {k: v for k, v in (exc.headers or {}).items()}
        code = E_INVALID_REQUEST if exc.status_code in (401, 403) else -32001
        raise JsonRpcError(code, str(exc.detail), http_status=exc.status_code, headers=headers) from exc


# ── 事件构造（契约 §4）───────────────────────────────────────────────────────

def _envelope(req_id: Any, result: Dict[str, Any]) -> str:
    return "data: " + json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result},
                                 ensure_ascii=False) + "\n\n"


def _task_frame(task_id: str, context_id: str, state: str) -> Dict[str, Any]:
    return {"kind": "task", "id": task_id, "contextId": context_id,
            "status": {"state": state, "timestamp": _now()}}


def _progress_frame(task_id: str, context_id: str, stage: str, fields: Dict[str, Any],
                    trace_id: str) -> Dict[str, Any]:
    text = _STAGE_TEXT.get(stage, stage)
    db = fields.get("database")
    if stage in ("generation", "execution") and db:
        text = f"{text}（{db}）"
    metadata = {"stage": stage, "trace_id": trace_id}
    for key in _STAGE_FIELDS:
        if key in fields and fields[key] is not None:
            metadata[key] = fields[key]
    return {
        "kind": "status-update", "taskId": task_id, "contextId": context_id, "final": False,
        "status": {"state": "working", "timestamp": _now(),
                   "message": {"kind": "message", "role": "agent", "messageId": uuid.uuid4().hex,
                               "taskId": task_id, "contextId": context_id,
                               "parts": [{"kind": "text", "text": text}]}},
        "metadata": metadata,
    }


def _artifact(task_id: str, summary, trace_id: str) -> Dict[str, Any]:
    """结果载荷：文本 part（答案）+ data part。send 与 stream **完全同构**（契约 §4.4）。"""
    return {
        "artifactId": uuid.uuid4().hex, "name": "answer",
        "parts": [
            {"kind": "text", "text": summary.answer},
            {"kind": "data", "data": {
                "route": summary.route, "confidence": summary.confidence,
                "queried_database": summary.queried_database,
                "has_risk": summary.has_risk, "degraded": summary.degraded,
                "degrade_reason": summary.degrade_reason,
                "selected_databases": summary.selected_databases,
                "trace_id": trace_id,
                "steps": [s.model_dump() for s in summary.step_logs],
            }},
        ],
    }


def _artifact_frame(task_id: str, context_id: str, artifact: Dict[str, Any]) -> Dict[str, Any]:
    return {"kind": "artifact-update", "taskId": task_id, "contextId": context_id,
            "artifact": artifact, "lastChunk": True}


def _terminal_frame(task_id: str, context_id: str, state: str, trace_id: str,
                    error: str = "") -> Dict[str, Any]:
    metadata: Dict[str, Any] = {"trace_id": trace_id}
    if error:
        metadata["error"] = error
    frame = {"kind": "status-update", "taskId": task_id, "contextId": context_id, "final": True,
             "status": {"state": state, "timestamp": _now()}, "metadata": metadata}
    return frame


# ── 执行 ─────────────────────────────────────────────────────────────────────

def _run_and_audit(request: Request, principal, parsed: ParsedMessage, started: float):
    """同步跑 pipeline 并写审计。一次 stream = 一次 /query = 一条审计（契约 §5.5）。"""
    from ai_sql_api.result_summary import apply_summary_to_audit, summarize_result, write_audit

    audit = AuditRecord(trace_id=get_trace_id(), caller=principal.caller,
                        user_id=principal.user_id, session_id=parsed.session_id, query=parsed.query)
    try:
        result = request.app.state.service.run(
            user_id=principal.user_id, session_id=parsed.session_id, query=parsed.query,
            enable_long_term=False, access=principal.policy,
        )
    except Exception as exc:
        audit.success = False
        audit.status_code = 400 if isinstance(exc, ValueError) else 500
        audit.error = f"{type(exc).__name__}: {exc}"
        write_audit(request.app, audit, started, principal)
        raise
    summary = summarize_result(result)
    apply_summary_to_audit(summary, audit)
    write_audit(request.app, audit, started, principal)
    return summary


def _task_result(task_id: str, context_id: str, state: str, artifact: Optional[Dict[str, Any]],
                 trace_id: str, error: str = "") -> Dict[str, Any]:
    """message/send 的返回：一个完整 Task。"""
    task = {"kind": "task", "id": task_id, "contextId": context_id,
            "status": {"state": state, "timestamp": _now()},
            "artifacts": [artifact] if artifact else [],
            "metadata": {"trace_id": trace_id, **({"error": error} if error else {})}}
    return task


async def _handle_send(request: Request, req_id: Any, params: Any) -> JSONResponse:
    parsed = _parse_message(params)
    principal = _admit(request, parsed.user_id)
    task_id, trace_id, started = uuid.uuid4().hex, get_trace_id(), time.perf_counter()
    try:
        summary = await asyncio.to_thread(_run_and_audit, request, principal, parsed, started)
    except ValueError as exc:
        raise JsonRpcError(E_INVALID_PARAMS, str(exc)) from exc
    except Exception as exc:
        log_event(_logger, logging.ERROR, "a2a.send_failed", error=f"{type(exc).__name__}: {exc}")
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": _task_result(
            task_id, parsed.session_id, "failed", None, trace_id, error=str(exc) or type(exc).__name__)})
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": _task_result(
        task_id, parsed.session_id, "completed", _artifact(task_id, summary, trace_id), trace_id)})


async def _stream_events(request: Request, req_id: Any, principal, parsed: ParsedMessage,
                         task_id: str) -> AsyncIterator[str]:
    """
    事件顺序（契约 §4.2，**保证**）：
        1× task → N× status-update(working) → 1× artifact-update → 1× status-update(final)
    流中途出错不靠断连表示失败：补一个 failed 终态再关（§5.2）。
    不发心跳、不发空事件（§3.4）：静默本身就是信息。
    """
    trace_id, context_id, started = get_trace_id(), parsed.session_id, time.perf_counter()
    yield _envelope(req_id, _task_frame(task_id, context_id, "submitted"))

    events: "queue.Queue[Tuple[str, Any, Any]]" = queue.Queue()

    def sink(name: str, fields: Dict[str, Any]) -> None:
        stage = _STAGE_MAP.get(name)
        if stage is not None:              # memory_context 等内部 stage 刻意不发
            events.put(("stage", stage, fields))

    async def run() -> None:
        try:
            with use_stage_sink(sink):
                summary = await asyncio.to_thread(_run_and_audit, request, principal, parsed, started)
            events.put(("done", summary, None))
        except Exception as exc:
            events.put(("error", None, exc))

    worker = asyncio.create_task(run())
    loop = asyncio.get_running_loop()
    try:
        while True:
            kind, a, b = await loop.run_in_executor(None, events.get)
            if kind == "stage":
                yield _envelope(req_id, _progress_frame(task_id, context_id, a, b, trace_id))
            elif kind == "done":
                yield _envelope(req_id, _artifact_frame(task_id, context_id, _artifact(task_id, a, trace_id)))
                yield _envelope(req_id, _terminal_frame(task_id, context_id, "completed", trace_id))
                return
            else:
                err = str(b) or type(b).__name__
                log_event(_logger, logging.ERROR, "a2a.stream_failed", error=f"{type(b).__name__}: {err}")
                yield _envelope(req_id, _terminal_frame(task_id, context_id, "failed", trace_id, error=err))
                return
    finally:
        if not worker.done():
            worker.cancel()


async def _handle_stream(request: Request, req_id: Any, params: Any):
    if not get_settings().a2a_streaming:
        # 开关关着：和 Card 的 streaming:false 一致，调用方据此退回 send（§6.2）
        raise JsonRpcError(E_UNSUPPORTED, "message/stream 未启用（AI_SQL_A2A_STREAMING=false）")
    parsed = _parse_message(params)
    principal = _admit(request, parsed.user_id)          # 流打开之前
    task_id = uuid.uuid4().hex
    return StreamingResponse(
        _stream_events(request, req_id, principal, parsed, task_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/a2a", include_in_schema=True)
async def a2a_endpoint(request: Request):
    req_id: Any = None
    try:
        try:
            body = await request.json()
        except Exception as exc:
            raise JsonRpcError(E_PARSE, f"请求体不是合法 JSON: {exc}", http_status=400) from exc
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
            raise JsonRpcError(E_INVALID_REQUEST, "不是合法的 JSON-RPC 2.0 请求", http_status=400)
        req_id, method, params = body.get("id"), body["method"], body.get("params")

        if method == "message/send":
            return await _handle_send(request, req_id, params)
        if method == "message/stream":
            return await _handle_stream(request, req_id, params)
        if method in ("tasks/cancel", "tasks/resubscribe", "tasks/get",
                      "tasks/pushNotificationConfig/set", "tasks/pushNotificationConfig/get"):
            # 契约 §7：刻意不做。查询是同步 pipeline，没有取消点；不存任务状态。
            raise JsonRpcError(E_UNSUPPORTED, f"{method} 不支持")
        raise JsonRpcError(E_METHOD_NOT_FOUND, f"未知 method: {method}")
    except JsonRpcError as err:
        return _error_response(req_id, err)
