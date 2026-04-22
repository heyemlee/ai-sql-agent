from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

SHARED_FILE_STEM = "_shared"
"""目录里以下划线开头的文件是跨库共享的语义（关联键、口径消歧、指标定义）。"""

CONNECTION_KEYS = ("dialect", "db_path", "dsn", "dsn_env")


@dataclass
class DatabaseSemantics:
    """
    一个业务库的语义元数据：它是什么系统、怎么连、每张表每个字段是什么意思。

    `tables` 沿用 ai_sql_pipeline.demo_data 里 business_meta 的结构
    （表 → description / aliases / columns → 字段 → description / aliases /
    semantic_role / business_usage / value_range / samples / keyword_text ...），
    检索、术语表、规划校验都直接读这份字典，不做二次转换——
    元数据文件里写什么，模型就看到什么。
    """

    name: str
    description: str = ""
    aliases: List[str] = field(default_factory=list)

    dialect: str = "sqlite"
    db_path: str = ""
    """SQLite 库文件路径；dialect 为 postgres 时忽略。"""

    dsn: str = ""
    """PostgreSQL 连接串。生产上别写进文件，用 dsn_env 指向环境变量。"""

    dsn_env: str = ""
    """存放连接串的环境变量名，例如 ERP_DSN。连接串含密码，文件要进 git，密码不能。"""

    tables: Dict[str, dict] = field(default_factory=dict)

    def resolved_dsn(self) -> str:
        if self.dsn:
            return self.dsn
        if self.dsn_env:
            return os.environ.get(self.dsn_env, "")
        return ""

    def has_connection(self) -> bool:
        if self.dialect == "postgres":
            return bool(self.resolved_dsn())
        return bool(self.db_path)

    def catalog_entry(self) -> dict:
        """库级检索（Tier 1）用的目录条目。"""
        return {
            "name": self.name,
            "description": self.description,
            "aliases": list(self.aliases),
        }

    def to_dict(self) -> dict:
        """文件表示：连接信息单独放在 connection 下，业务人员改表字段时不用碰它。"""
        connection: Dict[str, Any] = {"dialect": self.dialect}
        if self.db_path:
            connection["db_path"] = str(self.db_path)
        if self.dsn:
            connection["dsn"] = self.dsn
        if self.dsn_env:
            connection["dsn_env"] = self.dsn_env
        return {
            "name": self.name,
            "description": self.description,
            "aliases": list(self.aliases),
            "connection": connection,
            "tables": self.tables,
        }

    @classmethod
    def from_dict(cls, payload: dict, fallback_name: str = "") -> "DatabaseSemantics":
        name = str(payload.get("name") or fallback_name).strip()
        if not name:
            raise ValueError("数据库元数据缺少 name")
        connection = payload.get("connection") or {}
        if not isinstance(connection, dict):
            raise ValueError(f"{name}: connection 必须是键值对")
        # 也接受把连接字段直接写在顶层，少一层缩进
        for key in CONNECTION_KEYS:
            if key in payload and key not in connection:
                connection[key] = payload[key]
        tables = payload.get("tables") or {}
        if not isinstance(tables, dict):
            raise ValueError(f"{name}: tables 必须是「表名 → 说明」的键值对")
        aliases = payload.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        return cls(
            name=name,
            description=str(payload.get("description") or "").strip(),
            aliases=[str(item) for item in aliases],
            dialect=str(connection.get("dialect") or "sqlite"),
            db_path=str(connection.get("db_path") or ""),
            dsn=str(connection.get("dsn") or ""),
            dsn_env=str(connection.get("dsn_env") or ""),
            tables=tables,
        )


@dataclass
class SemanticLayer:
    """
    整个系统的语义层：所有库的元数据 + 跨库共享的口径。

    这是模型认识业务的唯一入口。检索靠它的别名与描述召回字段，
    规划靠它的跨库关联键拆步，问答靠它的指标定义解释口径。
    元数据缺了哪一块，对应那一类问题就答不对——这是数据劳动，不是模型能力。
    """

    databases: Dict[str, DatabaseSemantics] = field(default_factory=dict)
    cross_database_links: List[dict] = field(default_factory=list)
    metric_disambiguation: List[dict] = field(default_factory=list)
    metric_definitions: Dict[str, str] = field(default_factory=dict)
    """业务指标定义：「超预算」「良率」这类词到底怎么算，对话问答链路直接引用。"""

    source: str = "demo"
    """来源：demo 表示内置演示元数据，否则是加载的目录路径。"""

    @property
    def is_demo(self) -> bool:
        return self.source == "demo"

    def database_names(self) -> List[str]:
        return list(self.databases.keys())

    def business_metas(self) -> Dict[str, dict]:
        return {name: database.tables for name, database in self.databases.items()}

    def catalog(self) -> List[dict]:
        return [database.catalog_entry() for database in self.databases.values()]

    def shared_to_dict(self) -> dict:
        return {
            "cross_database_links": list(self.cross_database_links),
            "metric_disambiguation": list(self.metric_disambiguation),
            "metric_definitions": dict(self.metric_definitions),
        }

    def merge_shared(self, payload: dict) -> None:
        """把一个共享文件的内容合并进来；多个共享文件按文件名顺序叠加。"""
        links = payload.get("cross_database_links") or []
        disambiguation = payload.get("metric_disambiguation") or []
        definitions = payload.get("metric_definitions") or {}
        if not isinstance(links, list) or not isinstance(disambiguation, list):
            raise ValueError("cross_database_links / metric_disambiguation 必须是列表")
        if not isinstance(definitions, dict):
            raise ValueError("metric_definitions 必须是「指标名 → 定义」的键值对")
        self.cross_database_links.extend(dict(item) for item in links)
        self.metric_disambiguation.extend(dict(item) for item in disambiguation)
        self.metric_definitions.update(
            {str(key): str(value) for key, value in definitions.items()}
        )
