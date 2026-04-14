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
    ) -> Principal:
        """把 Key 配置 + 请求里的 user_id 解析成本次请求的身份。"""
        resolved_policy = _resolve_policy(self.roles, policies or {})
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
    roles: Sequence[str], policies: Dict[str, DataPolicy]
) -> DataPolicy:
    """把多个角色的策略合并；没有配置策略的角色视为全放开。"""
    matched: List[DataPolicy] = [
        policies[role] for role in roles if role in policies
    ]
    if not matched:
        return DataPolicy.full_access()

    merged = matched[0]
    for policy in matched[1:]:
        merged = merged.merge(policy)
    return merged
