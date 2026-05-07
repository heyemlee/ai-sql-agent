from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from ai_sql_agent.core import ModelTransport, get_settings
from ai_sql_agent.core.errors import ModelResponseError


@dataclass
class AliyunEmbeddingConfig:
    """
    阿里云百炼 Embedding 接口配置。

    默认使用 OpenAI 兼容 Embedding 接口。留空字段在构造 Client 时从全局
    Settings 补齐（AI_SQL_EMBEDDING_URL / DASHSCOPE_EMBEDDING_URL 可覆盖，
    业务空间专属域名直接填完整 embeddings 地址即可）。
    """

    api_key: str = ""
    """阿里云百炼 API Key。为空时取 Settings（DASHSCOPE_API_KEY）。"""

    embedding_url: str = ""
    """Embedding HTTP 接口地址，为空时取 Settings。"""

    model: str = ""
    """Embedding 模型名称，为空时取 Settings（默认 text-embedding-v4）。"""

    dimensions: int = 0
    """向量维度，0 时取 Settings（默认 1024）。"""

    timeout: Optional[float] = None
    """HTTP 超时时间（秒），None 时取 Settings。"""


class AliyunEmbeddingClient:
    """
    阿里云百炼 Embedding Client。

    负责调用 Embedding API 把文本转成归一化向量；HTTP 超时、限流退避重试、
    错误分类与用量统计统一由 ai_sql_agent.core.ModelTransport 处理。
    """

    COMPONENT = "embedding"

    def __init__(self, config: AliyunEmbeddingConfig | None = None):
        self.config = config or AliyunEmbeddingConfig()
        settings = get_settings()

        if not self.config.api_key:
            self.config.api_key = settings.resolved_embedding_api_key()
        if not self.config.embedding_url:
            self.config.embedding_url = settings.embedding_url
        if not self.config.model:
            self.config.model = settings.embedding_model
        if not self.config.dimensions:
            self.config.dimensions = settings.embedding_dimensions

        if not self.config.api_key:
            raise ValueError(
                "缺少 Embedding API Key。请在 .env 里配置 OPENAI_API_KEY 或 AI_SQL_EMBEDDING_API_KEY，"
                "或在 AliyunEmbeddingConfig(api_key='...') 中传入。"
            )

        self.transport = ModelTransport(
            self.COMPONENT,
            api_key=self.config.api_key,
            timeout=self.config.timeout,
        )

    @property
    def dimensions(self) -> int:
        return self.config.dimensions

    def embed_texts(self, texts: List[str]) -> np.ndarray:
        """
        批量生成文本向量。

        Args:
            texts: 待向量化的文本列表。

        Returns:
            np.ndarray: 归一化后的向量矩阵，形状为 [文本数量, 向量维度]。
        """
        if not texts:
            return np.empty((0, self.config.dimensions), dtype=np.float32)

        result = self.transport.post_json(
            self.config.embedding_url,
            {
                "model": self.config.model,
                "input": texts,
                "dimensions": self.config.dimensions,
                "encoding_format": "float",
            },
            model=self.config.model,
        )

        try:
            embeddings = [
                item["embedding"]
                for item in sorted(result["data"], key=lambda x: x.get("index", 0))
            ]
        except (KeyError, TypeError) as exc:
            raise ModelResponseError(
                f"解析 Embedding 返回失败，原始返回: {str(result)[:500]}",
                component=self.COMPONENT,
                model=self.config.model,
            ) from exc

        vectors = np.array(embeddings, dtype=np.float32)

        # 归一化，后续可以直接用点积计算 cosine similarity。
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.maximum(norms, 1e-8)
