from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict

from .logging_setup import get_logger, log_event
from .metrics import CIRCUIT_STATE, CIRCUIT_TRIPS

_logger = get_logger("circuit")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


_STATE_CODE = {CircuitState.CLOSED: 0, CircuitState.HALF_OPEN: 1, CircuitState.OPEN: 2}


@dataclass
class CircuitConfig:
    enabled: bool = True
    failure_threshold: int = 5
    """连续多少次基础设施类失败后跳闸。"""

    reset_seconds: float = 30.0
    """跳闸后多久放一次试探请求。"""


class CircuitBreaker:
    """
    单个组件的熔断器。

    模型服务整体挂掉时，每个请求都要等满超时（默认 60s）再重试两次，
    一个人问一句话要卡三分钟，还把连接池占满。熔断器让失败在第一时间
    返回，交给上层降级链路处理：

        closed --连续失败达阈值--> open --冷却结束--> half_open
          ^                                             |
          +----------------- 试探成功 -------------------+
                             试探失败 --> open

    只有基础设施类失败（超时、连接失败、5xx、限流）计数；鉴权失败、
    参数错误这类确定性错误不跳闸——它们本来就是立即返回的。
    """

    def __init__(
        self,
        component: str,
        config: CircuitConfig,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.component = component
        self.config = config
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._publish_state()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    def allow(self) -> bool:
        """是否放行本次调用；返回 False 表示应当直接短路。"""
        if not self.config.enabled:
            return True

        with self._lock:
            if self._state is CircuitState.CLOSED:
                return True

            if self._state is CircuitState.OPEN:
                if self._clock() - self._opened_at < self.config.reset_seconds:
                    return False
                # 冷却结束，放一个试探请求进去
                self._state = CircuitState.HALF_OPEN
                self._publish_state_locked()
                log_event(
                    _logger,
                    logging.INFO,
                    "circuit.half_open",
                    component=self.component,
                )
                return True

            # HALF_OPEN：已经有试探在跑，其余请求继续短路
            return False

    def record_success(self) -> None:
        if not self.config.enabled:
            return

        with self._lock:
            previous = self._state
            self._failures = 0
            self._state = CircuitState.CLOSED
            self._publish_state_locked()

        if previous is not CircuitState.CLOSED:
            log_event(
                _logger, logging.INFO, "circuit.closed", component=self.component
            )

    def record_failure(self) -> None:
        if not self.config.enabled:
            return

        with self._lock:
            self._failures += 1
            should_open = (
                self._state is CircuitState.HALF_OPEN
                or self._failures >= self.config.failure_threshold
            )
            if not should_open or self._state is CircuitState.OPEN:
                return

            self._state = CircuitState.OPEN
            self._opened_at = self._clock()
            self._publish_state_locked()
            failures = self._failures

        CIRCUIT_TRIPS.inc(component=self.component)
        log_event(
            _logger,
            logging.ERROR,
            "circuit.opened",
            component=self.component,
            consecutive_failures=failures,
            reset_seconds=self.config.reset_seconds,
        )

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {
                "component": self.component,
                "state": self._state.value,
                "consecutive_failures": self._failures,
            }

    def reset(self) -> None:
        """仅用于测试与运维手动恢复。"""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._publish_state_locked()

    def _publish_state(self) -> None:
        with self._lock:
            self._publish_state_locked()

    def _publish_state_locked(self) -> None:
        CIRCUIT_STATE.set_labeled(
            _STATE_CODE[self._state], component=self.component
        )


class CircuitRegistry:
    """按组件维度持有熔断器（模型服务分开熔断，Rerank 挂了不影响 SQL 生成）。"""

    def __init__(self) -> None:
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get(self, component: str) -> CircuitBreaker:
        from .settings import get_settings

        with self._lock:
            breaker = self._breakers.get(component)
            if breaker is None:
                settings = get_settings()
                breaker = CircuitBreaker(
                    component,
                    CircuitConfig(
                        enabled=settings.circuit_breaker_enabled,
                        failure_threshold=settings.circuit_failure_threshold,
                        reset_seconds=settings.circuit_reset_seconds,
                    ),
                )
                self._breakers[component] = breaker
            return breaker

    def snapshot(self) -> Dict[str, Dict[str, object]]:
        with self._lock:
            return {
                name: breaker.snapshot()
                for name, breaker in sorted(self._breakers.items())
            }

    def reset_all(self) -> None:
        with self._lock:
            for breaker in self._breakers.values():
                breaker.reset()


REGISTRY = CircuitRegistry()
