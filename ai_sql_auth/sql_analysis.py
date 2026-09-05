"""
基于 SQL AST 的引用分析：这条 SQL 到底碰了哪些表、哪些列。

原来的实现是 `\b(?:from|join)\s+` 正则加一个把 `schema.table` 砍成 `table` 的
归一化，2026-09-04 的审查复现了三条绕过：

    SELECT s.value FROM public_data p, secret_data s   逗号连接的第二张表不在正则里
    SELECT private_value FROM public_data              没有列级检查
    SELECT * FROM public_data                          通配符同上

正则拿不到的是**结构**：作用域、别名、CTE、子查询、UNION、LATERAL。所以这里换成
sqlglot 按方言解析，然后按作用域解析每个列引用的归属。

三条硬规则，都是「宁可误杀不可漏放」的方向：

1. **解析不出来的 SQL 一律拒绝。** 「提取不到表」不等于「没有越权」——
   前者是我方不知道，后者是我方知道且安全，把两者混为一谈正是旧实现的病根。
2. **列归属不确定时，按作用域里所有候选表逐个判**，任一张拒就拒。
   join 里一个不带前缀的 `salary`，两张表都要能给它放行。
3. **`SELECT *` 遇到有列级限制的表就拒。** 执行器这一层没有 schema，
   展不开通配符；展不开就不能证明它没读到禁列。

误杀的方向也如实写下来：sqlglot 的 `Scope.columns` 会把关联子查询里的列同时
算进外层作用域，所以 `SELECT (SELECT max(salary) FROM emp) FROM dept` 会拿
`salary` 去问 `dept` 一次。这是保守方向的多判一次，不是漏判。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

# sqlglot 方言名；执行器的 `dialect` 属性直接对得上，认不出来就用通用解析。
_KNOWN_DIALECTS = {"sqlite", "postgres", "postgresql", "mysql", "duckdb", "snowflake"}


@dataclass(frozen=True)
class TableRef:
    """一次表引用。`schema` 为空表示 SQL 里没写限定名。"""

    name: str
    schema: str = ""
    catalog: str = ""

    @property
    def qualified(self) -> str:
        return ".".join(part for part in (self.catalog, self.schema, self.name) if part)

    @property
    def identities(self) -> Tuple[str, ...]:
        """
        策略要匹配的名字集合。

        带 schema 时同时给出裸名与限定名，**两个都要被放行**才算放行：
        裸名保证老策略（`erp_db.sales_order`）继续生效，限定名让
        `secret.orders` 与 `public.orders` 在策略里终于能被区分开——
        旧实现把两者都砍成 `orders`，同名不同 schema 的表因此共用一条规则。
        """
        if self.qualified != self.name:
            return (self.name, self.qualified)
        return (self.name,)


@dataclass(frozen=True)
class ColumnRef:
    """
    一次列引用。

    `candidates` 是这个列**可能**属于的真实表。带前缀且前缀能解析时只有一张；
    不带前缀时是当前作用域里的全部真实表。判定时逐个问，任一张拒就拒。
    """

    name: str
    candidates: Tuple[TableRef, ...]


@dataclass(frozen=True)
class StarRef:
    """一次通配符引用（`*` 或 `t.*`），展开后覆盖 `tables` 里的每一张表。"""

    tables: Tuple[TableRef, ...]


@dataclass(frozen=True)
class SqlAnalysis:
    parsed: bool
    error: str = ""
    tables: Tuple[TableRef, ...] = ()
    columns: Tuple[ColumnRef, ...] = ()
    stars: Tuple[StarRef, ...] = ()


def _sqlglot_dialect(dialect: str) -> Optional[str]:
    name = (dialect or "").strip().lower()
    if name in _KNOWN_DIALECTS:
        return "postgres" if name == "postgresql" else name
    return None


def _table_ref(node) -> Optional[TableRef]:
    """把 sqlglot 的 Table 节点转成 TableRef；表函数之类没有名字的返回 None。"""
    name = (node.name or "").strip()
    if not name:
        return None
    return TableRef(
        name=name.lower(),
        schema=(node.db or "").strip().lower(),
        catalog=(node.catalog or "").strip().lower(),
    )


def analyze_sql(sql: str, dialect: str = "") -> SqlAnalysis:
    """解析 SQL，返回它引用的表与列。解析失败时 `parsed=False`，调用方必须据此拒绝。"""
    text = (sql or "").strip()
    if not text:
        return SqlAnalysis(parsed=False, error="SQL 为空。")

    try:
        import sqlglot
        from sqlglot import exp
        from sqlglot.optimizer.scope import build_scope
    except ImportError as exc:  # pragma: no cover - 依赖缺失属于部署事故
        return SqlAnalysis(
            parsed=False,
            error=f"缺少 sqlglot，无法做授权解析（不解析即拒绝）：{exc}",
        )

    read = _sqlglot_dialect(dialect)
    try:
        statements = sqlglot.parse(text, read=read)
    except Exception as exc:
        return SqlAnalysis(parsed=False, error=f"SQL 无法解析（{type(exc).__name__}）：{exc}")

    statements = [item for item in statements if item is not None]
    if not statements:
        return SqlAnalysis(parsed=False, error="SQL 无法解析成任何语句。")
    if len(statements) > 1:
        return SqlAnalysis(parsed=False, error="一次只允许执行一条 SQL 语句。")

    tree = statements[0]
    try:
        root = build_scope(tree)
    except Exception as exc:
        return SqlAnalysis(parsed=False, error=f"无法解析 SQL 的作用域（{type(exc).__name__}）：{exc}")

    if root is None:
        # build_scope 只认 SELECT / UNION 一类的查询表达式；其余形状交给
        # sql_guard 拒绝，这里不假装看得懂。
        return SqlAnalysis(parsed=False, error="SQL 不是可分析的查询语句。")

    tables: List[TableRef] = []
    columns: List[ColumnRef] = []
    stars: List[StarRef] = []

    for scope in root.traverse():
        # 这个作用域里 alias -> 真实表；值是 Scope 的（CTE / 子查询）交给它自己那一轮。
        local: Dict[str, TableRef] = {}
        for alias, source in scope.sources.items():
            if not isinstance(source, exp.Table):
                continue
            ref = _table_ref(source)
            if ref is None:
                continue
            local[(alias or ref.name).lower()] = ref
            local.setdefault(ref.name, ref)
            tables.append(ref)

        scope_tables = tuple(dict.fromkeys(local.values()))

        for column in scope.columns:
            qualifier = (column.table or "").lower()
            if qualifier:
                target = local.get(qualifier)
                if target is None:
                    # 前缀在本作用域解析不了：要么是 CTE / 子查询（它的列在自己
                    # 那一轮判），要么是关联子查询引用的外层别名（外层那一轮会
                    # 收到同一个列）。两种都不是漏判，跳过。
                    continue
                candidates: Tuple[TableRef, ...] = (target,)
            else:
                candidates = scope_tables
            if not candidates:
                continue
            columns.append(ColumnRef(name=column.name.lower(), candidates=candidates))

        stars.extend(_scope_stars(scope, local, scope_tables, exp))

    return SqlAnalysis(
        parsed=True,
        tables=tuple(dict.fromkeys(tables)),
        columns=tuple(columns),
        stars=tuple(stars),
    )


def _scope_stars(scope, local, scope_tables, exp) -> List[StarRef]:
    """
    收集本作用域里的通配符。

    `COUNT(*)` 不读任何列，排除掉——把它算成「读了全表所有列」会让带列限制的
    角色连计数都做不了，那是纯粹的误杀。
    """
    found: List[StarRef] = []
    expression = scope.expression

    for node in expression.find_all(exp.Star):
        if node.parent_select is not expression:
            continue
        parent = node.parent
        if isinstance(parent, exp.Count):
            continue
        if isinstance(parent, exp.Column):
            continue  # `t.*` 由下面的 Column 分支处理，避免重复
        if scope_tables:
            found.append(StarRef(tables=scope_tables))

    for node in expression.find_all(exp.Column):
        if node.parent_select is not expression:
            continue
        if not isinstance(node.this, exp.Star):
            continue
        qualifier = (node.table or "").lower()
        target = local.get(qualifier) if qualifier else None
        if target is not None:
            found.append(StarRef(tables=(target,)))
        elif not qualifier and scope_tables:
            found.append(StarRef(tables=scope_tables))

    return found


def extract_referenced_tables(sql: str, dialect: str = "") -> Set[str]:
    """
    提取 SQL 引用的表名（小写裸名，去 schema 前缀）。

    **这不是授权判据**——授权走 `ai_sql_auth.enforcement.authorize_sql`，它拿的是
    完整的 `TableRef`（带 schema）和列引用。这个函数只服务两处非安全用途：
    跨库静态校验（「这张表不在这个库里」）与审计日志里的表清单，两处都要的是
    「模型写了哪张表」而不是「他有没有权限」。

    解析失败时返回空集合。调用方是启发式检查，拿不到结构就跳过，不影响安全性。
    """
    analysis = analyze_sql(sql, dialect=dialect)
    if not analysis.parsed:
        return set()
    return {table.name for table in analysis.tables}
