from __future__ import annotations

import json
from typing import Any, Protocol, Sequence

from .objects import MemoryKind, MemoryMessage, MessageRole


class MemorySummarizer(Protocol):
    def summarize_conversation(
        self, previous_summary: str, messages: Sequence[MemoryMessage]
    ) -> str: ...

    def summarize_memory(
        self,
        *,
        kind: MemoryKind,
        content: Any,
        metadata: dict[str, Any],
    ) -> str: ...


class ExtractiveMemorySummarizer:
    """无模型配置时的可运行降级实现，保留事实而非生成新结论。"""

    def __init__(self, max_chars: int = 4000):
        self.max_chars = max_chars

    def summarize_conversation(
        self, previous_summary: str, messages: Sequence[MemoryMessage]
    ) -> str:
        lines: list[str] = []
        if previous_summary:
            lines.append(f"已有背景：{previous_summary}")
        for message in messages:
            role = "用户" if message.role == MessageRole.USER else "助手"
            content = " ".join(message.content.split())
            if message.message_type in {"sql_result", "analysis"} and message.payload:
                payload = json.dumps(message.payload, ensure_ascii=False, default=str)
                content = f"{content}；关键数据：{payload}"
            lines.append(f"{role}：{content}")
        return self._truncate("\n".join(lines))

    def summarize_memory(
        self,
        *,
        kind: MemoryKind,
        content: Any,
        metadata: dict[str, Any],
    ) -> str:
        query = str(metadata.get("query") or metadata.get("title") or "").strip()
        if isinstance(content, str):
            body = " ".join(content.split())
        else:
            body = json.dumps(content, ensure_ascii=False, default=str)
        prefix = "结构化查询记忆" if kind == MemoryKind.STRUCTURED else "业务知识记忆"
        database = metadata.get("database")
        details = [prefix]
        if query:
            details.append(f"问题：{query}")
        if database:
            details.append(f"数据库：{database}")
        details.append(f"核心内容：{body}")
        return self._truncate("；".join(details))

    def _truncate(self, text: str) -> str:
        if len(text) <= self.max_chars:
            return text
        marker = "\n……（中间内容已压缩）……\n"
        available = self.max_chars - len(marker)
        head_size = max(1, available * 2 // 5)
        tail_size = max(1, available - head_size)
        return text[:head_size] + marker + text[-tail_size:]


class TextGenerationClient(Protocol):
    def generate(self, prompt: str) -> str: ...


class ModelMemorySummarizer:
    """通过项目使用的任意文本生成 Client 完成摘要。"""

    def __init__(self, client: TextGenerationClient):
        self.client = client

    def summarize_conversation(
        self, previous_summary: str, messages: Sequence[MemoryMessage]
    ) -> str:
        records = [
            {
                "role": message.role.value,
                "content": message.content,
                "message_type": message.message_type,
                "payload": message.payload,
            }
            for message in messages
        ]
        prompt = f"""你是会话记忆压缩器。请把已有摘要与新增对话合并成一份事实性摘要。
必须保留：用户目标、已确认业务口径、筛选条件、关键 SQL/结果、分析结论、未完成任务。
不得补充原文没有的信息。直接输出摘要正文。

已有摘要：
{previous_summary or '无'}

新增对话：
{json.dumps(records, ensure_ascii=False, default=str)}
"""
        return self.client.generate(prompt).strip()

    def summarize_memory(
        self,
        *,
        kind: MemoryKind,
        content: Any,
        metadata: dict[str, Any],
    ) -> str:
        prompt = f"""你是长期记忆索引摘要器。请生成适合向量检索的业务摘要。
保留问题目标、业务口径、筛选条件、指标数值、关键结论；不要编造信息。
记忆类型：{kind.value}
元信息：{json.dumps(metadata, ensure_ascii=False, default=str)}
原始内容：{json.dumps(content, ensure_ascii=False, default=str)}
直接输出摘要正文。
"""
        return self.client.generate(prompt).strip()
