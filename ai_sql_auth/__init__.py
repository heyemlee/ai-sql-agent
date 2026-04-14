"""
鉴权与数据权限。

内部自用部署不做多租户，但仍然需要回答三个问题：
这条查询是谁发的（审计与配额）、他能查哪些表（数据权限）、
以及他有没有资格代别人查（防止随手改 user_id 读到别人的个人知识库）。
"""

from .key_store import ApiKeyStore
from .objects import ApiKeyRecord, DataPolicy, Principal
from .sql_tables import extract_referenced_tables

__all__ = [
    "ApiKeyRecord",
    "ApiKeyStore",
    "DataPolicy",
    "Principal",
    "extract_referenced_tables",
]
