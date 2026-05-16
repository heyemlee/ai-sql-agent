"""
强制授权关卡：SQL 打到数据库之前**必然**经过的那一道。

原来的检查写在 `text2sql_pipeline` 的执行分支里（`_check_table_access`），
于是四条执行路径里漏了一条：修复回调直接调执行器，完全没有重新鉴权
（2026-09-04 审查复现：让修复模型返回一条禁用表的 SQL，直接检查会拒，
走完整修复路径却成功返回了 1 行）。

**在每个调用点各写一次检查，就是这次漏掉一处的原因。** 所以这里换个结构：
授权不再由调用点主动发起，而是下沉到每个执行器的 `execute()` 入口
（`ai_sql_agent.execution.authorization.enforce`）—— 首次生成、失败修复、
缓存/历史重放、MCP 调用四条路最终都要调执行器，不存在「忘了加一句」的可能。

关卡靠一个 contextvar 拿当前身份的策略：

    with authorization_scope(principal.policy, caller=..., user_id=...):
        service.run(...)          # 里面怎么绕都出不去这个作用域

**没有绑定作用域时一律拒绝。** 这一条是整个设计的支点：如果「没绑定」等于
「放行」，那么任何新增的执行路径只要忘了绑，就会静默地拿到全权限——这正是
`_resolve_policy` 那条 `if not matched: return full_access()` 犯过的错误。
contextvar 会随 `asyncio.to_thread` 一起复制到工作线程，A2A 的 send / stream
两条路因此都在作用域内。
"""
from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Iterator, List, Optional, Tuple

from ai_sql_agent.core import log_event
from ai_sql_agent.core.logging_setup import get_logger
from ai_sql_agent.core.metrics import ACCESS_DENIED

from .identifiers import bind_dialect
from .sql_analysis import SqlAnalysis, TableRef, analyze_sql

_logger = get_logger("auth")


@dataclass(frozen=True)
class AuthorizationScope:
    """一次请求的授权上下文。`access` 就是 `Principal.policy`。"""

    access: Any
    caller: str = ""
    user_id: str = ""
    trace_id: str = ""


_ACTIVE: "contextvars.ContextVar[Optional[AuthorizationScope]]" = contextvars.ContextVar(
    "ai_sql_authorization_scope", default=None
)


@contextmanager
def authorization_scope(
    access: Any, *, caller: str = "", user_id: str = "", trace_id: str = ""
) -> Iterator[AuthorizationScope]:
    """绑定当前身份的数据权限；作用域内的所有 SQL 执行都按它判定。"""
    scope = AuthorizationScope(
        access=access, caller=caller, user_id=user_id, trace_id=trace_id
    )
    token = _ACTIVE.set(scope)
    try:
        yield scope
    finally:
        _ACTIVE.reset(token)


def current_scope() -> Optional[AuthorizationScope]:
    return _ACTIVE.get()


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason: str = ""
    denied_tables: Tuple[str, ...] = ()
    denied_columns: Tuple[str, ...] = ()

    @property
    def audit_error(self) -> str:
        """执行结果里那句错误文案。表名要带上，调用方才知道该申请什么权限。"""
        if self.allowed:
            return ""
        if self.denied_tables:
            return (
                f"没有访问权限：{', '.join(self.denied_tables)}。"
                "请联系管理员申请该表的查询权限。"
            )
        if self.denied_columns:
            return (
                f"没有访问权限：{', '.join(self.denied_columns)}。"
                "请联系管理员申请该字段的查询权限。"
            )
        return self.reason


_ALLOWED = AuthorizationDecision(allowed=True)


def restricts_columns(access: Any, database: str, table: str) -> bool:
    """
    这个策略对 `database.table` 有没有列级限制？

    只在判 `SELECT *` 时用得上：执行器这一层没有 schema，展不开通配符，
    所以只能反过来问「这张表有没有禁列」，有就拒。

    三种取值来源，最后一种是兜底方向：

    1. 策略自己实现了 `restricts_columns`（`DataPolicy` / `AllowAll` 都实现了）；
    2. 策略暴露了 `deny_columns` 规则，按规则的前两段（库.表）判;
    3. 都没有——**按有限制处置**。这里必须保守：一个自定义的权限过滤器如果
       真的限制了列却没声明，按「没限制」放行就是静默越权。要放行就显式实现
       `restricts_columns` 说一声。
    """
    hook = getattr(access, "restricts_columns", None)
    if callable(hook):
        return bool(hook(database, table))

    rules = getattr(access, "deny_columns", None)
    if rules is not None:
        return _rules_restrict_columns(tuple(rules), database, table)

    return True


def _rules_restrict_columns(rules: Tuple[str, ...], database: str, table: str) -> bool:
    for rule in rules:
        parts = rule.split(".")
        if len(parts) != 3:
            # 认不出形状的规则：当成可能命中，宁可多拒一次。
            return True
        db_pattern, table_pattern, _column = parts
        # 列限制是拒绝方向的规则：不区分大小写（见 identifiers 模块「拒绝从宽」）
        if fnmatchcase(database.casefold(), db_pattern.casefold()) and fnmatchcase(
            table.casefold(), table_pattern.casefold()
        ):
            return True
    return False


def _table_allowed(access: Any, database: str, table: TableRef) -> bool:
    """
    判定一次表引用。带 schema 的表有两个名字（裸名与限定名）。

    策略实现了 `allows_table_reference` 就交给它——`DataPolicy` 实现了，
    语义见那边的文档（任一名字命中 deny 就拒，任一命中 allow 才放）。
    只有两个方法的老式过滤器退回 `all(...)`：对它们没有别的办法既表达
    「限定名更具体」又不放松 deny，而 `all` 至少方向是保守的。
    """
    hook = getattr(access, "allows_table_reference", None)
    if callable(hook):
        return bool(hook(database, table.identities))
    return all(access.allows_table(database, name) for name in table.identities)


def _column_allowed(access: Any, database: str, table: TableRef, column: str) -> bool:
    hook = getattr(access, "allows_column_reference", None)
    if callable(hook):
        return bool(hook(database, table.identities, column))
    return all(
        access.allows_column(database, name, column) for name in table.identities
    )


def check_sql_access(
    access: Any, *, database: str, sql: str, dialect: str = ""
) -> AuthorizationDecision:
    """
    纯判定，不记日志、不打指标——给单测和 `authorize_sql` 共用。

    判定顺序是「先表后列」：表都过不了就没必要报列，错误文案里堆一串
    列名反而看不出根因。

    名字的大小写按 `dialect` 的规则处理（见 `identifiers` 模块）：`analyze_sql` 把
    SQL 里的标识符换成规范名，策略绑到同一个方言上去比。两边用的是同一条规则，
    所以「SQL 里怎么写」绕不开「策略里怎么写」。
    """
    access = bind_dialect(access, dialect)
    analysis: SqlAnalysis = analyze_sql(sql, dialect=dialect)
    if not analysis.parsed:
        # 提取不到 ≠ 没有越权。解析不了就拒，这是本次改动的核心一条。
        return AuthorizationDecision(
            allowed=False,
            reason=f"SQL 无法解析，出于安全拒绝执行：{analysis.error}",
        )

    if not analysis.tables:
        # `SELECT 1` 这类不碰表的查询：没有数据面，放行。
        return _ALLOWED

    denied_tables = [
        table.qualified
        for table in analysis.tables
        if not _table_allowed(access, database, table)
    ]
    if denied_tables:
        return AuthorizationDecision(
            allowed=False,
            reason="引用了无权访问的表。",
            denied_tables=tuple(sorted(dict.fromkeys(denied_tables))),
        )

    denied_columns: List[str] = []
    for column in analysis.columns:
        for table in column.candidates:
            if not _column_allowed(access, database, table, column.name):
                denied_columns.append(f"{table.qualified}.{column.name}")

    for star in analysis.stars:
        for table in star.tables:
            if restricts_columns(access, database, table.name):
                denied_columns.append(f"{table.qualified}.*")

    if denied_columns:
        return AuthorizationDecision(
            allowed=False,
            reason="引用了无权访问的字段。",
            denied_columns=tuple(sorted(dict.fromkeys(denied_columns))),
        )

    return _ALLOWED


def authorize_sql(*, database: str, sql: str, dialect: str = "") -> AuthorizationDecision:
    """
    执行器入口的强制关卡。拒绝时记审计日志并打指标。

    没有绑定授权作用域 = 拒绝。见模块文档。
    """
    scope = current_scope()
    if scope is None:
        decision = AuthorizationDecision(
            allowed=False,
            reason=(
                "本次执行没有绑定授权上下文，出于安全拒绝。"
                "所有执行路径都必须在 authorization_scope() 内调用。"
            ),
        )
        _audit_denied(decision, database=database, sql=sql, scope=None)
        return decision

    decision = check_sql_access(
        scope.access, database=database, sql=sql, dialect=dialect
    )
    if not decision.allowed:
        _audit_denied(decision, database=database, sql=sql, scope=scope)
    return decision


def _audit_denied(
    decision: AuthorizationDecision,
    *,
    database: str,
    sql: str,
    scope: Optional[AuthorizationScope],
) -> None:
    ACCESS_DENIED.inc(database=database)
    log_event(
        _logger,
        logging.WARNING,
        "access.denied",
        database=database,
        caller=(scope.caller if scope else ""),
        user_id=(scope.user_id if scope else ""),
        scoped=scope is not None,
        reason=decision.reason,
        tables=list(decision.denied_tables),
        columns=list(decision.denied_columns),
        sql=sql,
    )
