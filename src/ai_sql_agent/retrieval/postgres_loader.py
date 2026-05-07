from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from .objects import ColumnSchema, TableRelation, TableSchema

# AI SQL Agent 自身的基础设施表：向量索引、长期记忆、离线 Schema 索引。
# 它们和业务库可能落在同一个 Postgres 实例里，不应该被当成业务表召回。
DEFAULT_EXCLUDED_TABLE_PREFIXES = (
    "ai_sql_vectors_",
    "ai_sql_long_term_memory",
    "schema_field_index",
    "schema_relation_index",
)


class PostgresSchemaLoader:
    """
    PostgreSQL Schema 加载器。

    与 SQLiteSchemaLoader 同接口（load() 返回 tables / columns / relations），
    数据源换成 information_schema 与 pg_catalog：

    - 表与字段：information_schema.tables / columns；
    - 主键与外键：pg_constraint（contype='p' / 'f'）；
    - 字段样例：对每个字段取若干非空 DISTINCT 值，用于增强字段语义。
    """

    def __init__(
        self,
        dsn: str,
        database_name: str,
        business_meta: Dict[str, Any] | None = None,
        sample_size: int = 5,
        schema: str = "public",
        excluded_table_prefixes: Sequence[str] = DEFAULT_EXCLUDED_TABLE_PREFIXES,
    ):
        self.dsn = dsn
        self.database_name = database_name
        self.business_meta = business_meta or {}
        self.sample_size = sample_size
        self.schema = schema
        self.excluded_table_prefixes = tuple(excluded_table_prefixes)

    def load(self) -> Tuple[Dict[str, TableSchema], List[ColumnSchema], List[TableRelation]]:
        """加载数据库 Schema，返回表信息、字段信息和表关系。"""
        import psycopg

        conn = psycopg.connect(self.dsn, autocommit=True)
        try:
            table_names = self._get_table_names(conn)
            primary_keys = self._get_primary_keys(conn)
            foreign_keys = self._get_foreign_keys(conn)

            tables: Dict[str, TableSchema] = {}
            columns: List[ColumnSchema] = []
            relations: List[TableRelation] = []

            for table_name in table_names:
                table_meta = self.business_meta.get(table_name, {})
                table_pks = primary_keys.get(table_name, [])

                tables[table_name] = TableSchema(
                    database=self.database_name,
                    table_name=table_name,
                    description=table_meta.get("description", ""),
                    aliases=table_meta.get("aliases", []),
                    primary_keys=table_pks,
                )

                fk_map: Dict[str, str] = {}
                for source_col, target_table, target_col in foreign_keys.get(
                    table_name, []
                ):
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

                for col_name, data_type, is_nullable in self._get_columns(
                    conn, table_name
                ):
                    col_meta = table_meta.get("columns", {}).get(col_name, {})
                    columns.append(
                        ColumnSchema(
                            database=self.database_name,
                            table_name=table_name,
                            column_name=col_name,
                            data_type=data_type or "UNKNOWN",
                            nullable=is_nullable,
                            description=col_meta.get("description", ""),
                            aliases=col_meta.get("aliases", []),
                            table_description=table_meta.get("description", ""),
                            table_aliases=table_meta.get("aliases", []),
                            samples=self._get_column_samples(
                                conn, table_name, col_name
                            ),
                            is_primary_key=col_name in table_pks,
                            foreign_key_ref=fk_map.get(col_name),
                        )
                    )

            return tables, columns, relations

        finally:
            conn.close()

    def _get_table_names(self, conn) -> List[str]:
        rows = conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            (self.schema,),
        ).fetchall()
        return [
            row[0]
            for row in rows
            if not row[0].startswith(self.excluded_table_prefixes)
        ]

    def _get_columns(self, conn, table_name: str) -> List[Tuple[str, str, bool]]:
        rows = conn.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (self.schema, table_name),
        ).fetchall()
        return [(row[0], row[1], row[2] == "YES") for row in rows]

    def _get_primary_keys(self, conn) -> Dict[str, List[str]]:
        rows = conn.execute(
            """
            SELECT cls.relname AS table_name, att.attname AS column_name
            FROM pg_constraint AS con
            JOIN pg_class AS cls ON cls.oid = con.conrelid
            JOIN pg_namespace AS nsp ON nsp.oid = cls.relnamespace
            JOIN unnest(con.conkey) AS key_col(attnum) ON TRUE
            JOIN pg_attribute AS att
              ON att.attrelid = cls.oid AND att.attnum = key_col.attnum
            WHERE con.contype = 'p' AND nsp.nspname = %s
            """,
            (self.schema,),
        ).fetchall()

        result: Dict[str, List[str]] = {}
        for table_name, column_name in rows:
            result.setdefault(table_name, []).append(column_name)
        return result

    def _get_foreign_keys(self, conn) -> Dict[str, List[Tuple[str, str, str]]]:
        rows = conn.execute(
            """
            SELECT src_cls.relname   AS source_table,
                   src_att.attname   AS source_column,
                   tgt_cls.relname   AS target_table,
                   tgt_att.attname   AS target_column
            FROM pg_constraint AS con
            JOIN pg_class AS src_cls ON src_cls.oid = con.conrelid
            JOIN pg_class AS tgt_cls ON tgt_cls.oid = con.confrelid
            JOIN pg_namespace AS nsp ON nsp.oid = src_cls.relnamespace
            JOIN unnest(con.conkey, con.confkey) AS cols(src_attnum, tgt_attnum) ON TRUE
            JOIN pg_attribute AS src_att
              ON src_att.attrelid = src_cls.oid AND src_att.attnum = cols.src_attnum
            JOIN pg_attribute AS tgt_att
              ON tgt_att.attrelid = tgt_cls.oid AND tgt_att.attnum = cols.tgt_attnum
            WHERE con.contype = 'f' AND nsp.nspname = %s
            """,
            (self.schema,),
        ).fetchall()

        result: Dict[str, List[Tuple[str, str, str]]] = {}
        for source_table, source_column, target_table, target_column in rows:
            result.setdefault(source_table, []).append(
                (source_column, target_table, target_column)
            )
        return result

    def _get_column_samples(self, conn, table_name: str, column_name: str) -> List[str]:
        """抽取字段非空样例值；表名字段名来自库内元数据，用标识符引用防注入。"""
        from psycopg import sql as pgsql

        query = pgsql.SQL(
            "SELECT DISTINCT {column} AS value FROM {schema}.{table} "
            "WHERE {column} IS NOT NULL LIMIT %s"
        ).format(
            column=pgsql.Identifier(column_name),
            schema=pgsql.Identifier(self.schema),
            table=pgsql.Identifier(table_name),
        )

        try:
            rows = conn.execute(query, (self.sample_size,)).fetchall()
            return [str(row[0]) for row in rows if row[0] is not None]
        except Exception:
            return []
