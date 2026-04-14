from __future__ import annotations

import re
from typing import List, Set

from mcp_router.sql_guard import strip_sql_noise

# FROM / JOIN 后面跟的表名；允许 schema.table 与引号包裹
# 表名的一段：裸标识符、双引号或反引号包裹
_IDENT_PART = r"""(?:"[^"]+"|`[^`]+`|[A-Za-z_][\w$]*)"""

# FROM / JOIN 后面跟的表名，支持 schema.table 及任意一段带引号
_TABLE_PATTERN = re.compile(
    rf"""\b(?:from|join)\s+
        (?P<table>{_IDENT_PART}(?:\s*\.\s*{_IDENT_PART})*)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# CTE 名字不是真实表，要从结果里排除
_CTE_PATTERN = re.compile(
    r"""(?:\bwith\b|,)\s*(?P<name>[A-Za-z_][\w$]*)\s+as\s*\(""",
    re.IGNORECASE,
)

_SQL_KEYWORDS = {"select", "lateral", "unnest", "values"}


def _normalize(identifier: str) -> str:
    """去掉引号，取最后一段（schema.table -> table）。"""
    cleaned = identifier.replace('"', "").replace("`", "")
    return cleaned.split(".")[-1].strip().lower()


def extract_referenced_tables(sql: str) -> Set[str]:
    """
    提取 SQL 中真正引用的表名（小写、去 schema 前缀、排除 CTE 别名）。

    用于执行前的数据权限二次校验：模型只看得到有权限的 Schema，
    但仍可能凭记忆写出无权表，这里在打到数据库之前拦一道。
    """
    skeleton = strip_sql_noise(sql)
    if not skeleton:
        return set()

    cte_names = {
        match.group("name").lower() for match in _CTE_PATTERN.finditer(skeleton)
    }

    tables: List[str] = []
    for match in _TABLE_PATTERN.finditer(skeleton):
        name = _normalize(match.group("table"))
        if not name or name in _SQL_KEYWORDS or name in cte_names:
            continue
        tables.append(name)

    return set(tables)
