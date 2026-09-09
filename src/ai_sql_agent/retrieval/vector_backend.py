from __future__ import annotations

import logging

from ai_sql_agent.core import get_logger, get_settings, log_event
from ai_sql_agent.retrieval.vector_index import VectorIndex

_logger = get_logger("vector_backend")


def create_vector_index(
    embedding_client,
    namespace: str = "default",
    dsn: str | None = None,
):
    """
    向量索引工厂。

    配置了 pgvector DSN（参数或环境变量 AI_SQL_PGVECTOR_DSN）时返回
    PgVectorIndex；未配置、依赖缺失或连接失败时回退内存索引，
    保证离线开发环境可用。

    namespace 用于区分不同层级/不同库的索引表：
    三层检索中库级、表级、每个库的字段级各占一个命名空间。
    """
    resolved_dsn = dsn or get_settings().pgvector_dsn

    if resolved_dsn:
        try:
            from ai_sql_agent.retrieval.pgvector_index import PgVectorIndex

            index = PgVectorIndex(
                embedding_client=embedding_client,
                dsn=resolved_dsn,
                namespace=namespace,
            )
            log_event(
                _logger,
                logging.INFO,
                "vector_backend.selected",
                backend="pgvector",
                namespace=namespace,
            )
            return index
        except Exception as exc:
            log_event(
                _logger,
                logging.WARNING,
                "vector_backend.fallback",
                backend="memory",
                namespace=namespace,
                reason=str(exc),
            )

    log_event(
        _logger,
        logging.DEBUG,
        "vector_backend.selected",
        backend="memory",
        namespace=namespace,
    )
    return VectorIndex(embedding_client)
