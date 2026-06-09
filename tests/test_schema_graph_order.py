"""Schema 图里表的先后：按相关性排。

表的顺序以前来自一个字符串集合，随 PYTHONHASHSEED 变。跨进程一致由
`test_prompt_snapshot.PromptsIgnoreHashSeedTest` 守着；这里守另一半：排出来的得是
相关性顺序，不是随便哪个确定的顺序（比如按表名）。
"""

import unittest

from ai_sql_agent.retrieval.graph_builder import build_schema_graph
from ai_sql_agent.retrieval.objects import ColumnSchema, SchemaHit, TableSchema

_TABLES = ("alpha", "mid", "zeta")


def _column(table, name):
    return ColumnSchema(database="erp_db", table_name=table, column_name=name, data_type="TEXT")


class TableOrderTest(unittest.TestCase):
    def test_tables_follow_their_best_ranked_hit(self):
        # 检索排序：zeta 的字段最相关，其次 alpha，再次 mid。
        # alpha 的第二个字段排在 mid 之后，不影响 alpha 排在 mid 前面。
        ranked = [("zeta", "name"), ("alpha", "id"), ("mid", "name"), ("alpha", "name"), ("zeta", "id")]
        hits = [
            SchemaHit(doc_id=f"erp_db.{table}.{name}", score=1.0 - index / 10,
                      column=_column(table, name))
            for index, (table, name) in enumerate(ranked)
        ]

        graph = build_schema_graph(
            hits,
            tables={name: TableSchema(database="erp_db", table_name=name) for name in _TABLES},
            all_columns=[_column(table, name) for table in _TABLES for name in ("id", "name")],
            relations=[],
        )

        self.assertEqual(list(graph.tables), ["zeta", "alpha", "mid"])
        rendered = [
            line.split("：", 1)[1]
            for line in graph.to_prompt_context().splitlines()
            if line.startswith("表名：")
        ]
        self.assertEqual(rendered, ["zeta", "alpha", "mid"])
        self.assertEqual([column.column_name for column in graph.columns["alpha"]], ["id", "name"])


if __name__ == "__main__":
    unittest.main()
