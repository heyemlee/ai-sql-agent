from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Set

# 只读查询允许的起始关键字
_ALLOWED_LEADING = ("select", "with")

# 语句体内出现即拒绝的关键字。
# `WITH x AS (...) INSERT INTO ...` 在 SQLite / PostgreSQL 都是合法写法，
# 只看首关键字会漏，因此对整条语句做关键字扫描。
_FORBIDDEN_KEYWORDS: Set[str] = {
    "insert",
    "update",
    "delete",
    "replace",
    "merge",
    "upsert",
    "drop",
    "create",
    "alter",
    "truncate",
    "grant",
    "revoke",
    "attach",
    "detach",
    "pragma",
    "vacuum",
    "reindex",
    "analyze",
    "begin",
    "commit",
    "rollback",
    "savepoint",
    "copy",
    "call",
    "do",
    "set",
    "into",
}

# SQLite / PostgreSQL 中可以落地到文件或执行代码的函数
_FORBIDDEN_FUNCTIONS: Set[str] = {
    "load_extension",
    "writefile",
    "readfile",
    "edit",
    "fts3_tokenizer",
    "pg_read_file",
    "pg_ls_dir",
    "lo_export",
    "lo_import",
    "dblink",
}

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_COMMENT_LINE = re.compile(r"--[^\n]*")
_WORD = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")


@dataclass
class SqlGuardResult:
    """只读校验结果。"""

    allowed: bool
    reason: str = ""
    normalized_sql: str = ""


def strip_sql_noise(sql: str) -> str:
    """去掉注释与字符串字面量，只留下用于关键字判断的骨架。"""
    without_comments = _COMMENT_LINE.sub(" ", _COMMENT_BLOCK.sub(" ", sql))
    without_strings = _STRING_LITERAL.sub(" '' ", without_comments)
    return re.sub(r"\s+", " ", without_strings).strip()


def check_readonly_sql(sql: str) -> SqlGuardResult:
    """
    判断 SQL 是否为安全的单条只读查询。

    这是应用层防线，与「数据库层只读连接 / 只读账号」配合形成纵深防御：
    应用层拦住绝大多数写操作与注入变体，数据库层兜住漏网的那一条。

    拒绝的情况：
    1. 空语句；
    2. 首关键字不是 SELECT / WITH；
    3. 一次提交多条语句（分号拼接）；
    4. 语句中出现写操作、事务控制、ATTACH/PRAGMA 等关键字；
    5. 调用可读写文件或加载扩展的函数。
    """
    skeleton = strip_sql_noise(sql)

    if not skeleton:
        return SqlGuardResult(False, "SQL 为空。")

    body = skeleton.rstrip(";").strip()

    if not body:
        return SqlGuardResult(False, "SQL 为空。")

    if ";" in body:
        return SqlGuardResult(False, "一次只允许执行一条 SQL 语句。")

    lowered = body.lower()

    if not lowered.startswith(_ALLOWED_LEADING):
        return SqlGuardResult(False, "只允许执行 SELECT / WITH 查询。")

    words = {word.lower() for word in _WORD.findall(body)}

    hit = sorted(words & _FORBIDDEN_KEYWORDS)
    if hit:
        return SqlGuardResult(
            False, f"SQL 中包含禁止的关键字：{', '.join(hit)}。"
        )

    function_hit = sorted(words & _FORBIDDEN_FUNCTIONS)
    if function_hit:
        return SqlGuardResult(
            False, f"SQL 中包含禁止的函数：{', '.join(function_hit)}。"
        )

    return SqlGuardResult(True, normalized_sql=body)


def readonly_sqlite_uri(db_path: str) -> Optional[str]:
    """
    生成 SQLite 只读连接 URI。

    文件不存在时返回 None：只读模式不会自动建库，需要调用方给出明确错误。
    """
    from pathlib import Path

    path = Path(db_path)
    if not path.exists():
        return None
    return f"{path.resolve().as_uri()}?mode=ro"
