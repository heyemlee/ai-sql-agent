from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .objects import ColumnSchema, TableRelation, TableSchema


class SQLiteSchemaLoader:
    """SQLite Schema 加载器，用于提取表、字段、主外键和字段样例。"""

    def __init__(
        self,
        db_path: str | Path,
        database_name: str = "main",
        business_meta: Dict[str, Any] | None = None,
        sample_size: int = 5,
    ):
        self.db_path = str(db_path)
        self.database_name = database_name
        self.business_meta = business_meta or {}  # 业务元信息补充，如表描述、字段描述、别名
        self.sample_size = sample_size  # 每个字段最多抽取的样例数量

    def load(self) -> Tuple[Dict[str, TableSchema], List[ColumnSchema], List[TableRelation]]:
        """加载数据库 Schema，返回表信息、字段信息和表关系。"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        try:
            table_names = self._get_table_names(conn)

            tables: Dict[str, TableSchema] = {}
            columns: List[ColumnSchema] = []
            relations: List[TableRelation] = []

            for table_name in table_names:
                table_meta = self.business_meta.get(table_name, {})

                table_info = conn.execute(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()

                foreign_keys = conn.execute(
                    f'PRAGMA foreign_key_list("{table_name}")'
                ).fetchall()

                primary_keys = [
                    row["name"]
                    for row in table_info
                    if row["pk"] and int(row["pk"]) > 0
                ]

                tables[table_name] = TableSchema(
                    database=self.database_name,
                    table_name=table_name,
                    description=table_meta.get("description", ""),
                    aliases=table_meta.get("aliases", []),
                    primary_keys=primary_keys,
                )

                fk_map: Dict[str, str] = {}

                for fk in foreign_keys:
                    source_col = fk["from"]
                    target_table = fk["table"]
                    target_col = fk["to"]

                    fk_map[source_col] = f"{target_table}.{target_col}"

                    relations.append(
                        TableRelation(
                            database=self.database_name,
                            source_table=table_name,
                            source_column=source_col,
                            target_table=target_table,
                            target_column=target_col,
                        )
                    )

                for row in table_info:
                    col_name = row["name"]
                    col_meta = table_meta.get("columns", {}).get(col_name, {})
                    samples = self._get_column_samples(conn, table_name, col_name)

                    columns.append(
                        ColumnSchema(
                            database=self.database_name,
                            table_name=table_name,
                            column_name=col_name,
                            data_type=row["type"] or "UNKNOWN",
                            nullable=not bool(row["notnull"]),
                            description=col_meta.get("description", ""),
                            aliases=col_meta.get("aliases", []),
                            table_description=table_meta.get("description", ""),
                            table_aliases=table_meta.get("aliases", []),
                            samples=samples,
                            is_primary_key=col_name in primary_keys,
                            foreign_key_ref=fk_map.get(col_name),
                        )
                    )

            return tables, columns, relations

        finally:
            conn.close()

    def _get_table_names(self, conn: sqlite3.Connection) -> List[str]:
        """获取业务表名，过滤 SQLite 系统表。"""
        rows = conn.execute(
            '''
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            '''
        ).fetchall()

        return [row["name"] for row in rows]

    def _get_column_samples(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
    ) -> List[str]:
        """抽取字段非空样例值，用于增强字段语义。"""
        try:
            sql = (
                f'SELECT DISTINCT "{column_name}" AS value '
                f'FROM "{table_name}" '
                f'WHERE "{column_name}" IS NOT NULL '
                f'LIMIT ?'
            )

            rows = conn.execute(sql, (self.sample_size,)).fetchall()
            return [str(row["value"]) for row in rows if row["value"] is not None]

        except Exception:
            return []