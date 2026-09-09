from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional

import httpx

from .circuit import REGISTRY as CIRCUIT_REGISTRY
from .errors import (
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
from .logging_setup import get_logger, log_event
from .metrics import MODEL_CALLS, MODEL_TOKENS
from .settings import get_settings
from .trace import record_usage

_logger = get_logger("model")

_shared_client: Optional[httpx.Client] = None
_client_lock = threading.Lock()


def get_http_client() -> httpx.Client:
    """
    进程内共享的 HTTP Client，复用连接池。

    FastAPI 的同步端点跑在线程池里，多个请求会并发走到这里，
    加锁避免重复创建 Client（httpx.Client 本身是线程安全的）。
    """
    global _shared_client
    if _shared_client is None:
        with _client_lock:
            if _shared_client is None:
                _shared_client = httpx.Client(
                    limits=httpx.Limits(
                        max_connections=32, max_keepalive_connections=16
                    )
                )
    return _shared_client


def close_http_client() -> None:
    """关闭共享 Client，释放连接池（进程退出前调用）。"""
    global _shared_client
    with _client_lock:
        if _shared_client is not None:
            _shared_client.close()
            _shared_client = None


@dataclass
class RetryPolicy:
    """可重试错误的退避策略。"""

    max_retries: int = 2
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0

    @classmethod
    def from_settings(cls) -> "RetryPolicy":
        settings = get_settings()
        return cls(
            max_retries=settings.http_max_retries,
            backoff_base_seconds=settings.http_backoff_base_seconds,
            backoff_max_seconds=settings.http_backoff_max_seconds,
        )

    def delay_for(self, attempt: int, retry_after: Optional[float] = None) -> float:
        """第 attempt 次失败后的等待秒数；服务端给了 Retry-After 就听它的。"""
        if retry_after is not None and retry_after > 0:
            return min(retry_after, self.backoff_max_seconds)
        exponential = self.backoff_base_seconds * (2 ** attempt)
        capped = min(exponential, self.backoff_max_seconds)
        # 抖动，避免多个并发请求在同一时刻重试把服务端再打挂一次
        return capped * random.uniform(0.5, 1.0)


def _parse_retry_after(response: httpx.Response) -> Optional[float]:
    raw = response.headers.get("Retry-After", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _classify_status(
    response: httpx.Response,
    *,
    component: str,
    model: str,
) -> ModelCallError:
    status = response.status_code
    body = response.text[:500]
    kwargs = {"component": component, "model": model, "status_code": status}

    if status in (401, 403):
        return ModelAuthError(f"模型服务鉴权失败：{body}", **kwargs)
    if status == 429:
        return ModelRateLimitError(
            f"模型服务限流：{body}",
            retry_after=_parse_retry_after(response),
            **kwargs,
        )
    if status == 408:
        return ModelTimeoutError(f"模型服务返回超时：{body}", **kwargs)
    if 400 <= status < 500:
        return ModelBadRequestError(f"模型服务拒绝请求：{body}", **kwargs)
    return ModelServerError(f"模型服务错误：{body}", **kwargs)


class ModelTransport:
    """
    模型 HTTP 调用统一入口。

    原先 6 个 Client 各自手写 urllib.request，没有重试、没有限流处理、
    错误全部塌缩成一句 RuntimeError。这里收敛为一处实现，负责：

    - 统一超时与连接池复用；
    - 429/5xx/超时/连接失败按指数退避重试（尊重 Retry-After）；
    - 错误按类别抛出（见 errors.py），上层可区分限流与参数错误；
    - 记录每次调用的结构化日志与 Token 用量。
    """

    def __init__(
        self,
        component: str,
        *,
        api_key: str = "",
        timeout: Optional[float] = None,
        provider: str = "openai",
        retry_policy: Optional[RetryPolicy] = None,
        client: Optional[httpx.Client] = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        settings = get_settings()
        self.component = component
        self.api_key = api_key
        self.provider = provider or "openai"
        """`openai` 走 Bearer + chat/completions，`anthropic` 走 x-api-key + messages。
        Embedding / Rerank 只有 OpenAI 兼容协议，因此保持默认值。"""
        self.anthropic_version = settings.anthropic_version
        self.max_output_tokens = settings.max_output_tokens
        self.timeout = timeout if timeout is not None else settings.http_timeout_seconds
        self.retry_policy = retry_policy or RetryPolicy.from_settings()
        self._client = client
        self._sleep = sleeper
        self.circuit = CIRCUIT_REGISTRY.get(component)

    @property
    def client(self) -> httpx.Client:
        return self._client or get_http_client()

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        if self.provider == "anthropic":
            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": self.anthropic_version,
            }
        else:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
        if extra:
            headers.update(extra)
        return headers

    def chat(
        self,
        url: str,
        messages: List[Dict[str, str]],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 0,
    ) -> str:
        """一次非流式对话，返回纯文本。两种协议的差异都收在这里。"""
        payload = build_chat_payload(
            messages,
            model=model,
            temperature=temperature,
            stream=False,
            provider=self.provider,
            max_tokens=max_tokens or self.max_output_tokens,
        )
        result = self.post_json(url, payload, model=model)
        return extract_chat_content(result, component=self.component, model=model)

    def chat_stream(
        self,
        url: str,
        messages: List[Dict[str, str]],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 0,
    ) -> Iterator[str]:
        """流式对话，逐段 yield 文本增量。"""
        payload = build_chat_payload(
            messages,
            model=model,
            temperature=temperature,
            stream=True,
            provider=self.provider,
            max_tokens=max_tokens or self.max_output_tokens,
        )
        yield from self.stream_sse(url, payload, model=model)

    def post_json(
        self,
        url: str,
        payload: Dict[str, Any],
        *,
        model: str = "",
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """POST 一个 JSON 请求并返回解析后的响应体，失败按类别抛 ModelCallError。"""
        self._guard_circuit(model)
        attempt = 0

        while True:
            started = time.perf_counter()
            try:
                response = self.client.post(
                    url,
                    content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers=self._headers(extra_headers),
                    timeout=self.timeout,
                )
                if response.status_code >= 400:
                    raise _classify_status(
                        response, component=self.component, model=model
                    )
                try:
                    result = response.json()
                except ValueError as exc:
                    raise ModelResponseError(
                        f"模型返回不是合法 JSON：{response.text[:500]}",
                        component=self.component,
                        model=model,
                        status_code=response.status_code,
                    ) from exc

            except httpx.TimeoutException as exc:
                error: ModelCallError = ModelTimeoutError(
                    f"模型调用超时（{self.timeout}s）：{exc}",
                    component=self.component,
                    model=model,
                )
            except httpx.HTTPError as exc:
                error = ModelConnectionError(
                    f"模型调用连接失败：{exc}",
                    component=self.component,
                    model=model,
                )
            except ModelCallError as exc:
                error = exc
            else:
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                usage = self._record_usage(result, model)
                self.circuit.record_success()
                MODEL_CALLS.inc(
                    component=self.component, status="ok", error_category=""
                )
                log_event(
                    _logger,
                    logging.INFO,
                    "model.call",
                    component=self.component,
                    model=model,
                    status="ok",
                    attempt=attempt,
                    duration_ms=duration_ms,
                    **usage,
                )
                return result

            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            can_retry = error.retryable and attempt < self.retry_policy.max_retries
            MODEL_CALLS.inc(
                component=self.component,
                status="error",
                error_category=error.category,
            )
            log_event(
                _logger,
                logging.WARNING if can_retry else logging.ERROR,
                "model.call",
                component=self.component,
                model=model,
                status="error",
                attempt=attempt,
                duration_ms=duration_ms,
                error_category=error.category,
                status_code=error.status_code,
                will_retry=can_retry,
                error=str(error),
            )

            if not can_retry:
                self._record_circuit_outcome(error)
                raise error

            self._sleep(self.retry_policy.delay_for(attempt, error.retry_after))
            attempt += 1

    def stream_sse(
        self,
        url: str,
        payload: Dict[str, Any],
        *,
        model: str = "",
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Iterator[str]:
        """
        流式调用，逐段 yield `choices[0].delta.content`。

        流式响应一旦开始下发就不能安全重放，因此只对「建连阶段」的失败重试。
        """
        self._guard_circuit(model)
        headers = self._headers(extra_headers)
        headers["Accept"] = "text/event-stream"
        attempt = 0

        while True:
            started = time.perf_counter()
            emitted = 0
            try:
                with self.client.stream(
                    "POST",
                    url,
                    content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers=headers,
                    timeout=self.timeout,
                ) as response:
                    if response.status_code >= 400:
                        response.read()
                        raise _classify_status(
                            response, component=self.component, model=model
                        )
                    for chunk in _iter_sse_content(response.iter_lines()):
                        emitted += 1
                        yield chunk

            except httpx.TimeoutException as exc:
                error: ModelCallError = ModelTimeoutError(
                    f"模型流式调用超时（{self.timeout}s）：{exc}",
                    component=self.component,
                    model=model,
                )
            except httpx.HTTPError as exc:
                error = ModelConnectionError(
                    f"模型流式调用连接失败：{exc}",
                    component=self.component,
                    model=model,
                )
            except ModelCallError as exc:
                error = exc
            else:
                self.circuit.record_success()
                log_event(
                    _logger,
                    logging.INFO,
                    "model.stream",
                    component=self.component,
                    model=model,
                    status="ok",
                    attempt=attempt,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
                return

            # 已经吐过内容的流不能重放，否则调用方会看到重复片段
            can_retry = (
                error.retryable
                and attempt < self.retry_policy.max_retries
                and emitted == 0
            )
            log_event(
                _logger,
                logging.WARNING if can_retry else logging.ERROR,
                "model.stream",
                component=self.component,
                model=model,
                status="error",
                attempt=attempt,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                error_category=error.category,
                status_code=error.status_code,
                will_retry=can_retry,
                error=str(error),
            )
            if not can_retry:
                self._record_circuit_outcome(error)
                raise error

            self._sleep(self.retry_policy.delay_for(attempt, error.retry_after))
            attempt += 1

    # ------------------------------------------------------------------
    # 熔断
    # ------------------------------------------------------------------

    def _guard_circuit(self, model: str) -> None:
        """熔断器断开时直接抛错，不再发起请求也不再等超时。"""
        if self.circuit.allow():
            return

        MODEL_CALLS.inc(
            component=self.component,
            status="error",
            error_category="ModelCircuitOpenError",
        )
        log_event(
            _logger,
            logging.WARNING,
            "model.short_circuited",
            component=self.component,
            model=model,
        )
        raise ModelCircuitOpenError(
            f"{self.component} 模型调用已熔断，正在冷却，暂不发起请求。",
            component=self.component,
            model=model,
        )

    @staticmethod
    def _is_infrastructure_failure(error: ModelCallError) -> bool:
        """
        只有基础设施类失败才计入熔断。

        鉴权失败、参数错误是确定性的：它们本来就秒回，熔断没有意义，
        反而会让配置修好之后还要等冷却。
        """
        return isinstance(
            error,
            (
                ModelTimeoutError,
                ModelConnectionError,
                ModelServerError,
                ModelRateLimitError,
            ),
        )

    def _record_circuit_outcome(self, error: ModelCallError) -> None:
        if self._is_infrastructure_failure(error):
            self.circuit.record_failure()
        else:
            # 能拿到明确的业务错误说明链路本身是通的
            self.circuit.record_success()

    def _record_usage(self, result: Dict[str, Any], model: str) -> Dict[str, int]:
        usage = result.get("usage") if isinstance(result, dict) else None
        if not isinstance(usage, dict):
            return {}
        # OpenAI 用 prompt/completion_tokens，Anthropic 用 input/output_tokens
        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )
        record_usage(
            component=self.component,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        MODEL_TOKENS.inc(prompt_tokens, component=self.component, kind="prompt")
        MODEL_TOKENS.inc(
            completion_tokens, component=self.component, kind="completion"
        )
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }


def _iter_sse_content(lines: Iterator[str]) -> Iterator[str]:
    """解析 SSE 流，只吐出增量文本（同时兼容 OpenAI 与 Anthropic 两种事件格式）。"""
    for raw_line in lines:
        line = raw_line.strip()
        if not line or not line.startswith("data:"):
            continue
        data_text = line[len("data:"):].strip()
        if data_text == "[DONE]":
            return
        try:
            event = json.loads(data_text)
        except json.JSONDecodeError:
            continue

        # Anthropic：message_start / content_block_delta / message_stop
        event_type = event.get("type")
        if event_type == "content_block_delta":
            text = (event.get("delta") or {}).get("text", "")
            if text:
                yield text
            continue
        if event_type == "message_stop":
            return
        if event_type is not None and "choices" not in event:
            continue

        # OpenAI：choices[0].delta.content
        choices: List[Dict[str, Any]] = event.get("choices") or [{}]
        content = (choices[0].get("delta") or {}).get("content", "")
        if content:
            yield content


def _anthropic_accepts_temperature(model: str) -> bool:
    """Claude 5 起 `temperature` 被废弃，带上这个字段整个请求会被 400 拒绝。

    老模型（claude-3-*/claude-sonnet-4 等）仍然接受，所以按模型名分流而不是
    一刀切删掉——本项目全链路用 temperature=0 求确定性，能带就带。
    认不出的模型名按「接受」处理，保持改动前的行为。
    """
    match = re.search(r"claude-(?:opus|sonnet|haiku|fable)-(\d+)", model or "")
    return not (match and int(match.group(1)) >= 5)


def build_chat_payload(
    messages: List[Dict[str, str]],
    *,
    model: str,
    temperature: float = 0.0,
    stream: bool = False,
    provider: str = "openai",
    max_tokens: int = 0,
) -> Dict[str, Any]:
    """按厂商协议构造请求体。

    OpenAI 兼容：`messages` 直接带 system 角色；
    Anthropic Messages：system 要提到顶层字段，`max_tokens` 必填，
    且 Claude 5 起不能带 `temperature`。
    """
    if provider == "anthropic":
        system_parts = [
            str(m.get("content", ""))
            for m in messages
            if m.get("role") == "system"
        ]
        turns = [m for m in messages if m.get("role") != "system"]
        payload: Dict[str, Any] = {
            "model": model,
            "messages": turns,
            "max_tokens": max_tokens or get_settings().max_output_tokens,
            "stream": stream,
        }
        if _anthropic_accepts_temperature(model):
            payload["temperature"] = temperature
        if system_parts:
            payload["system"] = "\n\n".join(part for part in system_parts if part)
        return payload

    return {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": stream,
    }


def extract_chat_content(result: Dict[str, Any], *, component: str, model: str) -> str:
    """从对话响应里取出文本内容（兼容 OpenAI 与 Anthropic 两种返回体）。"""
    try:
        if isinstance(result, dict) and result.get("choices"):
            return result["choices"][0]["message"]["content"].strip()
        # Anthropic：content 是内容块数组，取其中的 text 块
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            text = "".join(
                str(block.get("text", ""))
                for block in result["content"]
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if text.strip():
                return text.strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ModelResponseError(
            f"解析模型返回失败，原始返回：{str(result)[:500]}",
            component=component,
            model=model,
        ) from exc

    raise ModelResponseError(
        f"解析模型返回失败，原始返回：{str(result)[:500]}",
        component=component,
        model=model,
    )
