from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class DataPolicy:
    """
    数据权限策略：某个角色能看哪些库表、不能看哪些列。

    内部自用场景下真正要拦的不是租户之间的越界，而是「估价员不能看
    批次成本单价」这类表级/列级授权。规则用 glob 匹配，写法：

        allow_tables = ["erp_db.*", "mes_db.work_order"]
        deny_tables  = ["hr_db.*"]
        deny_columns = ["wms_db.batch.unit_cost", "*.*.id_card_no"]

    deny 优先于 allow。
    """

    allow_tables: Tuple[str, ...] = ("*",)
    deny_tables: Tuple[str, ...] = ()
    deny_columns: Tuple[str, ...] = ()

    def allows_table(self, database: str, table: str) -> bool:
        target = f"{database}.{table}"
        if any(fnmatch.fnmatch(target, rule) for rule in self.deny_tables):
            return False
        return any(fnmatch.fnmatch(target, rule) for rule in self.allow_tables)

    def allows_column(self, database: str, table: str, column: str) -> bool:
        if not self.allows_table(database, table):
            return False
        target = f"{database}.{table}.{column}"
        return not any(
            fnmatch.fnmatch(target, rule) for rule in self.deny_columns
        )

    def allows_table_reference(self, database: str, names: Sequence[str]) -> bool:
        """
        判定一次**表引用**。`names` 是它可能被叫到的所有名字：
        `("orders",)`，或者带 schema 时 `("orders", "secret.orders")`。

        规则是「任一名字命中 deny 就拒，任一名字命中 allow 才放」：

        - `erp_db.*` 放行两个 schema 里的 orders —— `*` 本来就是这个意思；
        - `erp_db.secret.orders` **只**放行 secret 那张，public 那张不沾光；
        - `erp_db.orders`（不带 schema 的老规则）继续放行 orders，
          **包括别的 schema 里的同名表**——一条不带 schema 的规则说的就是
          「叫 orders 的那张表」，这是它一直以来的含义，不在这次改动里悄悄收紧；
          要区分 schema 就写带 schema 的规则，或者加一条 `deny_tables`。

        写成独立方法而不是 `all(allows_table(...))`：后者会让「只写限定名的策略」
        永远拒绝一切（裸名对不上限定名的 pattern），2026-09-04 在真实 PostgreSQL
        上验出来的就是这个。
        """
        candidates = [f"{database}.{name}" for name in names]
        if any(
            fnmatch.fnmatch(target, rule)
            for target in candidates
            for rule in self.deny_tables
        ):
            return False
        return any(
            fnmatch.fnmatch(target, rule)
            for target in candidates
            for rule in self.allow_tables
        )

    def allows_column_reference(
        self, database: str, names: Sequence[str], column: str
    ) -> bool:
        """列级同理：任一名字命中 deny_columns 就拒。"""
        if not self.allows_table_reference(database, names):
            return False
        return not any(
            fnmatch.fnmatch(f"{database}.{name}.{column}", rule)
            for name in names
            for rule in self.deny_columns
        )

    def restricts_columns(self, database: str, table: str) -> bool:
        """
        这张表上有没有列级限制？

        执行器那一层没有 schema，展不开 `SELECT *`，只能反过来问这个问题——
        有禁列就不许写通配符。判定规则见 `enforcement.restricts_columns`。
        """
        if not self.allows_table(database, table):
            return True
        from .enforcement import _rules_restrict_columns

        return _rules_restrict_columns(self.deny_columns, database, table)

    def merge(self, other: "DataPolicy") -> "DataPolicy":
        """多角色取并集：allow 合并，deny 也合并（deny 始终优先）。"""
        return DataPolicy(
            allow_tables=tuple(dict.fromkeys(self.allow_tables + other.allow_tables)),
            deny_tables=tuple(dict.fromkeys(self.deny_tables + other.deny_tables)),
            deny_columns=tuple(dict.fromkeys(self.deny_columns + other.deny_columns)),
        )

    @classmethod
    def full_access(cls) -> "DataPolicy":
        return cls()

    @classmethod
    def deny_all(cls) -> "DataPolicy":
        """
        一张表都不给。`allow_tables` 为空时 `allows_table` 的 `any(...)` 恒为 False。

        这是「没有配置策略的角色」的默认值——见 `_resolve_policy`。
        """
        return cls(allow_tables=())

    @classmethod
    def from_dict(cls, payload: dict) -> "DataPolicy":
        return cls(
            allow_tables=tuple(payload.get("allow_tables", ["*"])),
            deny_tables=tuple(payload.get("deny_tables", [])),
            deny_columns=tuple(payload.get("deny_columns", [])),
        )


@dataclass(frozen=True)
class Principal:
    """
    一次请求的调用方身份。

    内部自用不做多租户，但仍要知道「这条查询是谁发起的」：
    审计要记、配额要按人算、数据权限要按角色判。
    """

    caller: str
    """调用方标识（应用名或人名），来自 API Key 配置。"""

    user_id: str
    """记忆与配额的归属人。"""

    roles: Tuple[str, ...] = ()
    policy: DataPolicy = field(default_factory=DataPolicy.full_access)

    can_impersonate: bool = False
    """
    是否允许代其他 user_id 发起查询。

    内部 BI 门户这类服务端调用方需要（它替所有员工发请求），
    个人脚本用的 Key 不应该开。
    """

    is_admin: bool = False
    """是否可以调用运维接口（如手动重建 Schema 索引）。"""

    rate_limit_per_minute: int = 60
    daily_query_quota: int = 0
    """0 表示不限制。"""

    daily_token_quota: int = 0

    anonymous: bool = False
    """鉴权关闭时使用的占位身份。"""

    @classmethod
    def anonymous_principal(cls, user_id: str = "anonymous") -> "Principal":
        return cls(
            caller="anonymous",
            user_id=user_id,
            roles=("anonymous",),
            policy=DataPolicy.full_access(),
            can_impersonate=True,
            is_admin=True,
            anonymous=True,
        )


@dataclass(frozen=True)
class ApiKeyRecord:
    """一条 API Key 配置。"""

    key: str
    caller: str
    user_id: str = ""
    roles: Tuple[str, ...] = ()
    can_impersonate: bool = False
    is_admin: bool = False
    rate_limit_per_minute: int = 60
    daily_query_quota: int = 0
    daily_token_quota: int = 0

    def to_principal(
        self,
        requested_user_id: str,
        policies: Optional[Dict[str, DataPolicy]] = None,
        unknown_role_policy: Optional[DataPolicy] = None,
    ) -> Principal:
        """把 Key 配置 + 请求里的 user_id 解析成本次请求的身份。"""
        resolved_policy = _resolve_policy(
            self.roles, policies or {}, fallback=unknown_role_policy
        )
        return Principal(
            caller=self.caller,
            user_id=requested_user_id or self.user_id or self.caller,
            roles=self.roles,
            policy=resolved_policy,
            can_impersonate=self.can_impersonate,
            is_admin=self.is_admin,
            rate_limit_per_minute=self.rate_limit_per_minute,
            daily_query_quota=self.daily_query_quota,
            daily_token_quota=self.daily_token_quota,
        )


def _resolve_policy(
    roles: Sequence[str],
    policies: Dict[str, DataPolicy],
    fallback: Optional[DataPolicy] = None,
) -> DataPolicy:
    """
    把多个角色的策略合并。**没有匹配到任何策略时 deny by default。**

    这里原来写的是 `if not matched: return full_access()`，注释是「没有配置
    策略的角色视为全放开」。2026-09-04 的审查用一个根本不存在的角色拿到了全部
    权限——配错一个角色名、或者新加一个角色忘了配策略，后果就是全权限，而且
    没有任何症状。安全默认值必须是拒绝：配漏了要表现为「查不到」，不是「都能查」。

    `fallback` 只给显式开了宽松开关的开发环境用（`AI_SQL_UNKNOWN_ROLE_POLICY`），
    生产模式下 `ai_sql_api.app` 启动时就会拒绝那个开关。
    """
    if not roles:
        # 一个角色都没声明：这不是「未知角色」，是「这把 Key 不做角色限制」，
        # 而且只能是人手写出来的——打错角色名产生不了空列表。所以它是显式配置，
        # 不是默认值兜底。生产模式下仍然会在启动时被拒（见
        # `ai_sql_api.app.check_production_readiness`），那里要求每把 Key 都有角色。
        return DataPolicy.full_access()

    matched: List[DataPolicy] = [
        policies[role] for role in roles if role in policies
    ]
    if not matched:
        # 声明了角色，却一条策略都没匹配上——**这才是审查复现的那个洞**。
        # 「打错角色名」和「新加角色忘了配策略」都长这个样子，而旧代码把它翻译成
        # 全放开：配置错误静默升级成全部权限，没有任何症状。
        return fallback if fallback is not None else DataPolicy.deny_all()

    merged = matched[0]
    for policy in matched[1:]:
        merged = merged.merge(policy)
    return merged
