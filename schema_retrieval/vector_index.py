from __future__ import annotations

from typing import List, Tuple

import numpy as np

from .embedding_client import AliyunEmbeddingClient


class VectorIndex:
    """
    内存向量索引。

    当前 Demo 使用 numpy 矩阵保存字段向量，并通过 cosine similarity 完成向量召回。
    生产环境走 pgvector（见 pgvector_index.py），由 vector_backend 工厂按配置切换。
    """

    def __init__(self, embedding_client: AliyunEmbeddingClient):
        self.embedding_client = embedding_client
        self.documents: List[str] = []
        self.embeddings: np.ndarray | None = None

    def build(self, documents: List[str]) -> None:
        """
        构建向量索引。

        Args:
            documents: 待索引的字段级向量文本列表。
        """
        self.documents = documents
        self.embeddings = self.embedding_client.embed_texts(documents)

    def search(self, query: str, top_k: int = 5) -> List[Tuple[int, float]]:
        """
        根据 Query 进行向量召回。

        Args:
            query: 用户自然语言查询。
            top_k: 返回的候选数量。

        Returns:
            List[Tuple[int, float]]: 文档下标与相似度得分。
        """
        if self.embeddings is None:
            raise RuntimeError("VectorIndex 尚未构建，请先调用 build()。")

        query_vector = self.embedding_client.embed_texts([query])[0]

        # embeddings 和 query_vector 都已经归一化，点积等价于 cosine similarity。
        scores = self.embeddings @ query_vector

        top_indices = np.argsort(-scores)[:top_k]

        return [
            (int(index), float(scores[index]))
            for index in top_indices
        ]