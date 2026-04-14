from __future__ import annotations

import hmac
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from ai_sql_core import get_logger, get_settings, log_event

from .objects import ApiKeyRecord, DataPolicy, Principal

_logger = get_logger("auth")


class ApiKeyStore:
    """
    API Key 与数据权限策略的加载与校验。

    没有网关的内部部署由服务自己校验身份。Key 来源两选一：

    - `AI_SQL_API_KEYS_FILE`：指向一个 JSON 文件（K8s Secret / Docker
      secret 直接挂进来，推荐，改 Key 不用重建镜像）；
    - `AI_SQL_API_KEYS`：JSON 字符串，适合本地或极简部署。

    文件格式：

        {
          "keys": [
            {"key": "...", "caller": "bi-portal", "roles": ["analyst"],
             "can_impersonate": true, "rate_limit_per_minute": 120},
            {"key": "...", "caller": "li", "user_id": "li", "roles": ["workshop"]}
          ],
          "policies": {
            "analyst": {"allow_tables": ["showroom_db.*", "factory_db.*"],
                        "deny_columns": ["*.*.id_card_no"]},
            "workshop": {"allow_tables": ["mes_db.*"]}
          }
        }
    """

    def __init__(
        self,
        records: Optional[List[ApiKeyRecord]] = None,
        policies: Optional[Dict[str, DataPolicy]] = None,
    ):
        self.records: Dict[str, ApiKeyRecord] = {}
        for record in records or []:
            if record.key in self.records:
                # 静默覆盖意味着有人会拿到别人的身份与权限，必须显式失败
                raise ValueError(
                    f"API Key 重复：{self.records[record.key].caller} 与 "
                    f"{record.caller} 配置了同一个 key。"
                )
            self.records[record.key] = record

        self.policies = policies or {}

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(cls) -> "ApiKeyStore":
        settings = get_settings()

        payload: dict = {}
        source = ""

        if settings.api_keys_file:
            path = Path(settings.api_keys_file)
            if not path.exists():
                raise FileNotFoundError(
                    f"AI_SQL_API_KEYS_FILE 指向的文件不存在：{path}"
                )
            payload = json.loads(path.read_text(encoding="utf-8"))
            source = str(path)
        elif settings.api_keys:
            payload = json.loads(settings.api_keys)
            source = "AI_SQL_API_KEYS"

        store = cls.from_dict(payload)

        log_event(
            _logger,
            logging.INFO,
            "auth.keys_loaded",
            source=source or "none",
            keys=len(store.records),
            policies=sorted(store.policies.keys()),
        )
        return store

    @classmethod
    def from_dict(cls, payload: dict) -> "ApiKeyStore":
        policies = {
            role: DataPolicy.from_dict(rule)
            for role, rule in (payload.get("policies") or {}).items()
        }

        records: List[ApiKeyRecord] = []
        for item in payload.get("keys") or []:
            key = str(item.get("key") or "").strip()
            caller = str(item.get("caller") or "").strip()
            if not key or not caller:
                raise ValueError("API Key 配置缺少 key 或 caller 字段。")
            records.append(
                ApiKeyRecord(
                    key=key,
                    caller=caller,
                    user_id=str(item.get("user_id") or ""),
                    roles=tuple(item.get("roles") or ()),
                    can_impersonate=bool(item.get("can_impersonate", False)),
                    is_admin=bool(item.get("is_admin", False)),
                    rate_limit_per_minute=int(
                        item.get("rate_limit_per_minute", 60)
                    ),
                    daily_query_quota=int(item.get("daily_query_quota", 0)),
                    daily_token_quota=int(item.get("daily_token_quota", 0)),
                )
            )

        return cls(records=records, policies=policies)

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def resolve(self, api_key: str, requested_user_id: str) -> Principal:
        """
        校验 Key 并解析出本次请求的身份。

        Key 比对用 `hmac.compare_digest` 做常数时间比较，避免计时侧信道；
        不允许代他人查询的 Key，只能使用自己绑定的 user_id。
        """
        record = self._match(api_key)
        if record is None:
            raise PermissionError("API Key 无效。")

        if not record.can_impersonate:
            bound_user = record.user_id or record.caller
            if requested_user_id and requested_user_id != bound_user:
                raise PermissionError(
                    f"该 API Key 只能以 {bound_user} 身份查询，"
                    f"不允许指定 user_id={requested_user_id}。"
                )
            requested_user_id = bound_user

        return record.to_principal(requested_user_id, self.policies)

    def _match(self, api_key: str) -> Optional[ApiKeyRecord]:
        """
        常数时间比对 Key。

        按字节比较：`hmac.compare_digest` 遇到非 ASCII 字符串会抛
        TypeError，而 Key 来自请求头——任何人发一个中文 Key 都能把
        401 变成 500。
        """
        if not api_key:
            return None

        provided = api_key.encode("utf-8")
        matched: Optional[ApiKeyRecord] = None

        # 不提前 break：命中与否都走完全部候选，避免用耗时反推 Key 是否存在
        for candidate, record in self.records.items():
            if hmac.compare_digest(candidate.encode("utf-8"), provided):
                matched = record
        return matched

    def __len__(self) -> int:
        return len(self.records)
