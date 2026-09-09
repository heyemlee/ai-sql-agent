from __future__ import annotations

import re
from typing import Optional, Protocol

from .objects import AnomalyType, ValidationFinding


class ParaphraseClient(Protocol):
    """让模型用自然语言复述 SQL 实际算了什么。"""

    def generate(self, prompt: str) -> str: ...


_PARAPHRASE_PROMPT = """你是一个SQL审阅助手。

请用一句中文说明下面这条SQL**实际**做了什么：筛选了什么、按什么分组、输出了什么。
只描述SQL字面执行的内容，不要推测意图，不要评价对错，不要输出SQL。

SQL：
{sql}

输出格式：
复述：<一句话>
"""

_COMPARE_PROMPT = """你是一个语义一致性审阅助手。

判断「SQL 的实际行为」是否回答了「用户的问题」。
只看是否答非所问（例如问的是金额却统计了数量、问的是A项目却查了B项目、
漏掉了问题里明确要求的筛选条件）。措辞差异、字段命名差异不算不一致。

用户问题：{query}
SQL 的实际行为：{paraphrase}

输出格式（严格遵守，只输出两行）：
结论：一致 或 不一致
理由：<一句话>
"""

_MULTI_STEP_COMPARE_PROMPT = """你是一个语义一致性审阅助手。

这个问题被拆成了多个步骤在**不同数据库**里分别执行，下面是**最后一步**。
前面的步骤已经执行完毕，它们的筛选结果已经以字面量（如 IN ('A','B')）的形式
写进了最后一步的 SQL 里。

因此：最后一步的 SQL **本来就不会**再出现前置步骤的筛选条件和表，
这是正确的设计，不是遗漏。看到 SQL 里有一串看不出来历的字面量取值，
应当理解为「前置步骤算出来的结果」，不要因此判为不一致。

判断标准只有一条：在前置步骤已经完成的前提下，最后一步是否正确地
完成了剩下的工作（该聚合的聚合、该输出的字段输出）。

用户问题：{query}
前置步骤已完成：{preceding}
最后一步 SQL 的实际行为：{paraphrase}

输出格式（严格遵守，只输出两行）：
结论：一致 或 不一致
理由：<一句话>
"""


class SemanticSelfChecker:
    """
    语义自检。

    做法分两步，而不是直接问模型「这条SQL对不对」：
    1. 先让模型**只复述** SQL 字面行为——不给它看问题，避免它顺着问题脑补；
    2. 再把复述与原问题一起给模型判一致性。

    这样拆开的原因：一步到位地问「对不对」时，模型倾向于回答「对」，
    因为它同时看到了问题和SQL，会自动把两者对齐。先隔离复述能显著减少这种偏袒。
    """

    def __init__(self, client: Optional[ParaphraseClient] = None):
        self.client = client

    @property
    def available(self) -> bool:
        return self.client is not None

    def check(
        self,
        query: str,
        sql: str,
        preceding_summary: str = "",
    ) -> tuple[Optional[ValidationFinding], Optional[str]]:
        """
        返回 (发现, 复述文本)。模型不可用时返回 (None, None)，不阻断主链路。

        preceding_summary 非空表示这是多步计划的最后一步，比对时要告诉审阅者
        前面已经做完了什么。不告诉它的后果是系统性误报：跨库查询的最后一步
        只做「后半段」，拿原问题去比必然判成「漏掉了筛选条件」——
        实测跨库样本的误报率因此达到 100%，等于把提示变成噪音。
        """
        if not self.client or not sql.strip():
            return None, None

        try:
            raw = self.client.generate(_PARAPHRASE_PROMPT.format(sql=sql.strip()))
            paraphrase = self._extract(raw, "复述") or raw.strip()

            if preceding_summary.strip():
                compare_prompt = _MULTI_STEP_COMPARE_PROMPT.format(
                    query=query,
                    preceding=preceding_summary.strip(),
                    paraphrase=paraphrase,
                )
            else:
                compare_prompt = _COMPARE_PROMPT.format(
                    query=query, paraphrase=paraphrase
                )
            verdict_raw = self.client.generate(compare_prompt)
        except Exception:
            # 自检是增强项，不能因为它挂掉就让整条查询失败
            return None, None

        conclusion = self._extract(verdict_raw, "结论") or ""
        reason = self._extract(verdict_raw, "理由") or verdict_raw.strip()

        if "不一致" in conclusion:
            return (
                ValidationFinding(
                    anomaly=AnomalyType.SEMANTIC_MISMATCH,
                    detail=f"SQL 实际行为与问题不一致：{reason}（复述：{paraphrase}）",
                    severity="block",
                ),
                paraphrase,
            )

        return None, paraphrase

    @staticmethod
    def _extract(text: str, label: str) -> str:
        match = re.search(rf"{label}\s*[:：]\s*(.+)", text)
        return match.group(1).strip() if match else ""
