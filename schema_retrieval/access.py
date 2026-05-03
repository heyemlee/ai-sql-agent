from __future__ import annotations

from typing import Dict, List, Protocol, runtime_checkable


@runtime_checkable
class SchemaAccessFilter(Protocol):
    """
    Schema 可见性判定。

    检索层不依赖鉴权模块，只要求传进来的对象能回答两个问题：
    这张表能不能看、这个字段能不能看。ai_sql_auth.DataPolicy 直接满足。
    """

    def allows_table(self, database: str, table: str) -> bool: ...

    def allows_column(self, database: str, table: str, column: str) -> bool: ...


class AllowAll:
    """默认放行，未接入权限时使用。"""

    def allows_table(self, database: str, table: str) -> bool:
        return True

    def allows_column(self, database: str, table: str, column: str) -> bool:
        return True


ALLOW_ALL = AllowAll()


def filter_schema_graph(graph, access: SchemaAccessFilter):
    """
    按权限裁剪 SchemaGraph：无权表整张移除，无权字段逐个移除。

    裁剪发生在进入 Prompt 之前，模型压根看不到越权的表和字段，
    也就不会把它们写进 SQL。
    """
    database = getattr(graph, "database", "")
    tables = getattr(graph, "tables", None)
    if not tables:
        return graph

    allowed_tables = {
        name: table
        for name, table in tables.items()
        if access.allows_table(database, name)
    }

    allowed_columns: Dict[str, List] = {}
    for table_name, columns in getattr(graph, "columns", {}).items():
        if table_name not in allowed_tables:
            continue
        allowed_columns[table_name] = [
            column
            for column in columns
            if access.allows_column(database, table_name, column.column_name)
        ]

    relations = [
        relation
        for relation in getattr(graph, "relations", [])
        if relation.source_table in allowed_tables
        and relation.target_table in allowed_tables
    ]

    graph.tables = allowed_tables
    graph.columns = allowed_columns
    graph.relations = relations
    return graph
