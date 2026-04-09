from __future__ import annotations

from typing import Optional


class SqlAgentError(RuntimeError):
    """AI SQL Agent 统一异常基类。"""


class ModelCallError(SqlAgentError):
    """
    模型调用失败的统一异常。

    把散落在各 Client 里的 `RuntimeError(f"调用失败: {exc}")` 收敛成有类型、
    可判断是否可重试的错误，便于上层按类别处理（限流退避 / 直接失败 / 告警）。
    """

    #: 该类错误是否值得重试
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        component: str = "",
        model: str = "",
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
    ):
        super().__init__(message)
        self.component = component
        self.model = model
        self.status_code = status_code
        self.retry_after = retry_after

    @property
    def category(self) -> str:
        return type(self).__name__

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.component:
            parts.append(f"component={self.component}")
        if self.model:
            parts.append(f"model={self.model}")
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        return " | ".join(parts)


class ModelAuthError(ModelCallError):
    """鉴权失败（401/403）：API Key 无效或无权限，重试无意义。"""

    retryable = False


class ModelBadRequestError(ModelCallError):
    """请求本身不合法（4xx，不含 429）：参数、模型名或内容问题，重试无意义。"""

    retryable = False


class ModelRateLimitError(ModelCallError):
    """触发限流（429）：需要退避后重试。"""

    retryable = True


class ModelServerError(ModelCallError):
    """服务端错误（5xx）：可退避重试。"""

    retryable = True


class ModelTimeoutError(ModelCallError):
    """请求超时：可退避重试。"""

    retryable = True


class ModelConnectionError(ModelCallError):
    """网络连接失败：可退避重试。"""

    retryable = True


class ModelCircuitOpenError(ModelCallError):
    """
    熔断器断开，请求被直接短路。

    重试没有意义（本地就拒绝了），上层应据此走降级链路。
    """

    retryable = False


class ModelResponseError(ModelCallError):
    """返回体结构不符合预期（解析失败）：重试通常无意义。"""

    retryable = False
