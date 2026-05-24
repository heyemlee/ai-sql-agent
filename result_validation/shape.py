"""输出形状检查：不调模型、不看结果，只比对「问题要什么」与「SQL 给什么」。

实测静默错误的头号形态就是形状不符——问「台面类供应商的采购金额」，
模型答一个 `SUM(po_amt)`。数字本身算得没错，但用户以为看到的是每一单的金额，
拿到的是总额，而且全程没有任何迹象提示这一点。21 条静默错误里 4 条是这一类。

两条检查都是静态的（正则 + 计划文本），成本接近零，因此可以对每条查询都跑。
"""

from __future__ import annotations

import re
from typing import Optional

from .cardinality import parse_expectation
from .objects import AnomalyType, ValidationFinding

_AGGREGATE = re.compile(r"\b(SUM|COUNT|AVG|MIN|MAX)\s*\(", re.I)
_GROUP_BY = re.compile(r"\bGROUP\s+BY\b", re.I)

_AGGREGATION_INTENT = (
    "统计", "合计", "总共", "一共", "总数", "总额", "总金额", "总量", "总数量",
    "平均", "多少", "占比", "比例", "排名", "最多", "最少", "各", "每个", "每家",
    "每张", "分别", "汇总", "求和", "计数",
)
"""问句里出现这些词，说明用户确实要一个汇总/分组结果。"""


def wants_aggregation(query: str) -> bool:
    return any(word in query for word in _AGGREGATION_INTENT)


def is_structurally_single_row(sql: str) -> bool:
    """SQL 结构上只可能返回一行：有聚合函数且没有 GROUP BY。"""
    return bool(_AGGREGATE.search(sql)) and not _GROUP_BY.search(sql)


def check_unrequested_aggregation(query: str, sql: str) -> Optional[ValidationFinding]:
    """
    用户没要求汇总，SQL 却聚合了。

    只报 warn 不阻断：把明细汇总成一个数**有时**正是用户想要的，
    只是他没说清楚。提示的价值在于让他看一眼是不是这个意思，
    而不是替他判定对错。
    """
    if not sql.strip() or wants_aggregation(query):
        return None
    if not _AGGREGATE.search(sql):
        return None

    # 只报**整表折叠**（无 GROUP BY）。按维度分组时，模型多半是按用户提到的实体
    # 在分组，那通常正是他要的；而无分组聚合是把一张明细拍成一个数——
    # 用户以为看到的是一张列表，拿到的是一个数字。
    #
    # 实测这条限制把精度从 38% 提到 71%：10 条误报里 8 条是分组聚合，
    # 去掉它们只漏 1 条真阳。误报率一旦超过护栏线，提示会被当噪音忽略，
    # 那时真正的静默错误也跟着漏过去——所以宁可少报。
    if _GROUP_BY.search(sql):
        return None

    detail = (
        "问题里没有出现「统计/合计/平均/每个」这类汇总要求，"
        "但 SQL 做了聚合且没有分组——明细被折叠成了一个数。"
        "如果你要的是每一条明细，这个结果回答的不是同一个问题。"
    )
    return ValidationFinding(
        anomaly=AnomalyType.AGGREGATION_UNREQUESTED,
        detail=detail,
        severity="warn",
    )


def check_shape_against_plan(expected_label: str, sql: str) -> Optional[ValidationFinding]:
    """
    计划声明的预期量级与 SQL 的**结构性**行数矛盾。

    与基数校验的区别：基数校验比的是「实际跑出多少行」，会被数据量影响；
    这里比的是「这条 SQL 结构上最多能出几行」，与数据无关，因此更确定。
    计划说要出数条、SQL 却是个无分组聚合，那不是数据少，是 SQL 写错了形状。
    """
    expectation = parse_expectation(expected_label)
    if expectation is None or not sql.strip():
        return None
    if expectation.high <= 1:
        return None
    if not is_structurally_single_row(sql):
        return None
    return ValidationFinding(
        anomaly=AnomalyType.SHAPE_CONTRADICTS_PLAN,
        detail=(
            f"计划预期输出「{expectation.label}」（至少 {max(expectation.low, 2)} 行），"
            "但这条 SQL 有聚合且没有分组，结构上只能返回 1 行。"
            "多半是漏了 GROUP BY，或者本来就不该聚合。"
        ),
        severity="warn",
    )
