"""
授权关卡的第二批越权——2026-10-14 审查用「SQLite 真实读了什么 vs. 关卡看见了什么」的差分测试找出来的。

和 `test_authorization_boundary.py` 同一个理由放进 CI：这类缺陷退化时没有症状，
越权成功和正常查询长得一模一样。

覆盖三条（编号对应审查报告）：

- S-01　`HAVING` / 与输出别名同名的 `ORDER BY` 表达式 / 表值函数实参里的列没被检查。
        sqlglot 的 `Scope.columns` 为了不把「引用别名」的名字当真实列而丢掉它们，
        `HAVING max(salary) > n` 因此能当布尔探针二分出被禁列的值。
- S-03　`rowid` / `oid` / `_rowid_` 是 INTEGER PRIMARY KEY 列的别名，策略写不出它的规则。
- S-05　`sqlite_master`（及别名 `sqlite_schema`）、`dbstat`、表值 `pragma_*` 函数：
        通配放行会一并放行，表值函数又不产生表名，原来被当成「不碰任何表」。

没有覆盖（已知缺口，S-04）：视图会遮住它读的基表。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ai_sql_agent.auth import DataPolicy, authorization_scope, check_sql_access
from ai_sql_agent.auth.sql_analysis import analyze_sql
from ai_sql_agent.execution import MCPExecutionRequest, SQLiteMCPExecutor

SCHEMA = """
CREATE TABLE employees(id INTEGER PRIMARY KEY, name TEXT, salary INT);
INSERT INTO employees VALUES (101,'ann',9000),(102,'bob',4500),(103,'cy',7200);
CREATE TABLE payroll(emp INT, amount INT);
INSERT INTO payroll VALUES (101,1),(102,1),(103,1);
"""

# 列级限制：salary 与主键 id 都是禁列
SALARY_POLICY = DataPolicy(deny_columns=("app.employees.salary", "app.employees.id"))


def _check(policy, sql, dialect="sqlite"):
    return check_sql_access(policy, database="app", sql=sql, dialect=dialect)


class HavingColumnsAreCheckedTest(unittest.TestCase):
    """S-01：不带前缀的 HAVING 列引用以前从没被问过策略。"""

    def test_having_aggregate_over_a_denied_column_is_denied(self):
        decision = _check(
            SALARY_POLICY,
            "SELECT count(*) AS c FROM employees HAVING max(salary) > 8000",
        )
        self.assertFalse(decision.allowed)
        self.assertIn("employees.salary", decision.denied_columns)

    def test_having_after_group_by_is_denied(self):
        decision = _check(
            SALARY_POLICY,
            "SELECT name FROM employees GROUP BY name HAVING sum(salary) > 7000",
        )
        self.assertFalse(decision.allowed)

    def test_qualified_having_was_always_denied_and_still_is(self):
        decision = _check(
            SALARY_POLICY,
            "SELECT count(*) FROM employees e HAVING max(e.salary) > 1",
        )
        self.assertFalse(decision.allowed)

    def test_having_inside_a_correlated_subquery_reaches_the_outer_table(self):
        # payroll 没有 salary 列：内层的无前缀 salary 绑到外层的 employees
        decision = _check(
            SALARY_POLICY,
            "SELECT name FROM employees e WHERE EXISTS ("
            "SELECT 1 FROM payroll p GROUP BY p.emp HAVING max(salary) > 0)",
        )
        self.assertFalse(decision.allowed)

    def test_having_in_a_plain_derived_table_does_not_reach_outer_tables(self):
        # 反向：普通派生表看不见外层，不能把它的列拿去问外层的表（否则是无谓的误杀）
        policy = DataPolicy(deny_columns=("app.employees.amount",))
        decision = _check(
            policy,
            "SELECT d.emp FROM (SELECT emp FROM payroll GROUP BY emp "
            "HAVING max(amount) > 0) d, employees e",
        )
        self.assertTrue(decision.allowed, decision.audit_error)

    def test_having_on_an_output_alias_of_a_permitted_column_is_allowed(self):
        decision = _check(
            SALARY_POLICY,
            "SELECT name, count(*) AS n FROM employees GROUP BY name "
            "HAVING n > 0 ORDER BY n DESC",
        )
        self.assertTrue(decision.allowed, decision.audit_error)


class OrderByAliasShadowTest(unittest.TestCase):
    """S-01：ORDER BY 表达式里的名字等于某个输出别名时被丢弃，真实列却被读了。"""

    def test_expression_over_a_shadowed_denied_column_is_denied(self):
        # SQLite 的 authorizer 回调显示这条真的读了 employees.salary
        decision = _check(
            SALARY_POLICY,
            "SELECT name AS salary FROM employees ORDER BY salary * 1",
        )
        self.assertFalse(decision.allowed)
        self.assertIn("employees.salary", decision.denied_columns)

    def test_a_bare_alias_with_a_denied_name_is_refused_conservatively(self):
        # 已知的误杀方向：分不清 `ORDER BY salary` 指别名还是真列时按真列问
        # （模块文档里写明）。别名和被禁列同名本来就少见，这个代价可以接受。
        decision = _check(
            SALARY_POLICY,
            "SELECT name, 1 AS salary FROM employees ORDER BY salary",
        )
        self.assertFalse(decision.allowed)

    def test_ordering_by_an_ordinary_alias_is_allowed(self):
        decision = _check(
            SALARY_POLICY,
            "SELECT name, count(*) AS total FROM employees GROUP BY name ORDER BY total",
        )
        self.assertTrue(decision.allowed, decision.audit_error)

    def test_union_order_by_names_an_output_column_not_a_table_column(self):
        decision = _check(
            SALARY_POLICY,
            "SELECT name AS n FROM employees UNION SELECT emp FROM payroll ORDER BY n",
        )
        self.assertTrue(decision.allowed, decision.audit_error)


class TableFunctionArgumentTest(unittest.TestCase):
    """S-01：表值函数的实参里的无前缀列也被丢。"""

    def test_column_inside_a_table_function_argument_is_denied(self):
        decision = _check(
            SALARY_POLICY, "SELECT 1 FROM employees e, json_each(salary)"
        )
        self.assertFalse(decision.allowed)
        self.assertIn("employees.salary", decision.denied_columns)

    def test_table_function_is_reported_by_the_analysis(self):
        analysis = analyze_sql("SELECT value FROM json_each('[1,2]')", dialect="sqlite")
        self.assertTrue(analysis.parsed)
        self.assertEqual(analysis.table_functions, ("json_each",))
        self.assertEqual(analysis.tables, ())

    def test_json_each_over_a_literal_is_still_allowed(self):
        # 不是引擎内部对象，也不碰任何表
        decision = _check(SALARY_POLICY, "SELECT value FROM json_each('[1,2]')")
        self.assertTrue(decision.allowed, decision.audit_error)


class RowidAliasTest(unittest.TestCase):
    """S-03。"""

    def test_rowid_of_a_column_restricted_table_is_denied(self):
        for name in ("rowid", "oid", "_rowid_", "ROWID"):
            with self.subTest(name=name):
                decision = _check(SALARY_POLICY, f"SELECT {name}, name FROM employees")
                self.assertFalse(decision.allowed)
                self.assertTrue(
                    any(item.startswith("employees.") for item in decision.denied_columns)
                )

    def test_qualified_rowid_is_denied_too(self):
        decision = _check(SALARY_POLICY, "SELECT e.rowid FROM employees e")
        self.assertFalse(decision.allowed)

    def test_rowid_of_a_table_without_column_restrictions_is_allowed(self):
        decision = _check(SALARY_POLICY, "SELECT rowid, amount FROM payroll")
        self.assertTrue(decision.allowed, decision.audit_error)

    def test_a_policy_without_any_column_rule_does_not_notice_rowid(self):
        decision = _check(DataPolicy.full_access(), "SELECT rowid FROM employees")
        self.assertTrue(decision.allowed, decision.audit_error)

    def test_postgres_has_no_rowid_alias(self):
        # PostgreSQL 里 rowid 只是个普通列名，不套 SQLite 的别名规则
        decision = _check(SALARY_POLICY, "SELECT rowid FROM employees", dialect="postgres")
        self.assertTrue(decision.allowed, decision.audit_error)


class EngineInternalObjectsTest(unittest.TestCase):
    """S-05：引擎内部对象对所有角色一律拒绝，通配放行也不例外。"""

    INTERNAL = [
        "SELECT sql FROM sqlite_master",
        "SELECT sql FROM sqlite_schema",
        "SELECT sql FROM main.sqlite_master",
        "SELECT name FROM sqlite_temp_schema",
        'SELECT * FROM "sqlite_master"',
        "SELECT * FROM sqlite_sequence",
        "SELECT * FROM sqlite_stat1",
        "SELECT name, pageno FROM dbstat",
        "SELECT name FROM pragma_table_info('payroll')",
        "SELECT name FROM main.pragma_table_info('payroll')",
        "SELECT * FROM pragma_index_list('payroll')",
        "SELECT name FROM pragma_table_list",
        "SELECT file FROM pragma_database_list",
        "SELECT t.name FROM employees e, pragma_table_info('payroll') t",
        "SELECT * FROM employees WHERE id IN (SELECT seq FROM sqlite_sequence)",
        "WITH x AS (SELECT sql FROM sqlite_master) SELECT * FROM x",
    ]

    def test_denied_even_for_full_access(self):
        for sql in self.INTERNAL:
            with self.subTest(sql=sql):
                decision = _check(DataPolicy.full_access(), sql)
                self.assertFalse(decision.allowed)
                self.assertTrue(decision.denied_internal)

    def test_denied_under_a_wildcard_allow_rule(self):
        policy = DataPolicy(allow_tables=("app.*",), deny_tables=("app.payroll",))
        for sql in self.INTERNAL:
            with self.subTest(sql=sql):
                self.assertFalse(_check(policy, sql).allowed)

    def test_message_does_not_tell_the_caller_to_ask_for_permission(self):
        decision = _check(DataPolicy.full_access(), "SELECT sql FROM sqlite_schema")
        self.assertIn("引擎内部对象", decision.audit_error)
        self.assertNotIn("申请", decision.audit_error)

    def test_postgres_catalogs_are_not_touched_by_this_rule(self):
        # 范围只到 SQLite 引擎自己的对象；PostgreSQL 的系统目录仍按策略判定
        decision = _check(
            DataPolicy.full_access(), "SELECT * FROM sqlite_master", dialect="postgres"
        )
        self.assertTrue(decision.allowed, decision.audit_error)

    def test_ordinary_tables_are_unaffected(self):
        for sql in (
            "SELECT name FROM employees",
            "SELECT count(*) FROM payroll",
            "SELECT 1",
        ):
            with self.subTest(sql=sql):
                self.assertTrue(_check(DataPolicy.full_access(), sql).allowed)


class ExecutorEndToEndTest(unittest.TestCase):
    """过真实执行器：以前返回数据的两条，现在不能再返回。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "app.db"
        with sqlite3.connect(path) as conn:
            conn.executescript(SCHEMA)
        self.executor = SQLiteMCPExecutor(database="app", db_path=path)

    def _run(self, policy, sql):
        with authorization_scope(policy, caller="audit", user_id="audit"):
            return self.executor.execute(MCPExecutionRequest(database="app", sql=sql))

    def test_having_probe_no_longer_answers(self):
        result = self._run(
            SALARY_POLICY, "SELECT count(*) AS c FROM employees HAVING max(salary) > 8000"
        )
        self.assertFalse(result.success)
        self.assertEqual(result.rows, [])
        self.assertIn("salary", result.error)

    def test_rowid_no_longer_returns_the_denied_primary_key(self):
        result = self._run(SALARY_POLICY, "SELECT rowid, name FROM employees")
        self.assertFalse(result.success)
        self.assertEqual(result.rows, [])

    def test_pragma_table_info_no_longer_lists_a_table_outside_the_allow_list(self):
        policy = DataPolicy(allow_tables=("app.employees",))
        result = self._run(policy, "SELECT name FROM pragma_table_info('payroll')")
        self.assertFalse(result.success)
        self.assertEqual(result.rows, [])

    def test_permitted_query_still_runs(self):
        result = self._run(SALARY_POLICY, "SELECT name FROM employees ORDER BY name")
        self.assertTrue(result.success, result.error)
        self.assertEqual([row["name"] for row in result.rows], ["ann", "bob", "cy"])


class GateSeesEveryRealReadTest(unittest.TestCase):
    """
    差分：SQLite 自己说读了哪些 (表, 列)，关卡对「禁掉其中任何一个」都必须拒绝。

    这才是关卡该满足的性质，比枚举已知绕过更不容易漏。以后升级 sqlglot 后，
    `Scope.columns` 的丢列规则变了也会在这里露出来。
    视图不在语料里（S-04：视图遮住基表，是已知缺口）。
    """

    CORPUS = [
        "SELECT name FROM employees",
        "SELECT * FROM employees",
        "SELECT e.name FROM employees e, payroll p WHERE e.id = p.emp",
        "WITH c AS (SELECT * FROM employees) SELECT name FROM c",
        "SELECT (SELECT max(salary) FROM employees) FROM payroll",
        "SELECT name FROM employees UNION SELECT emp FROM payroll",
        "SELECT name FROM employees e WHERE EXISTS (SELECT 1 FROM payroll p WHERE p.emp = e.id)",
        "SELECT e.name FROM employees e JOIN payroll p ON p.emp = e.id",
        "SELECT * FROM employees NATURAL JOIN payroll",
        "SELECT name FROM employees WHERE salary > 5000",
        "SELECT name FROM employees ORDER BY salary",
        "SELECT name FROM employees GROUP BY name, salary",
        "SELECT count(*) FROM employees HAVING max(salary) > 8000",
        "SELECT name FROM employees GROUP BY name HAVING sum(salary) > 7000",
        "SELECT name FROM employees GROUP BY 1 HAVING salary = 9000",
        "SELECT name AS salary FROM employees ORDER BY salary * 1",
        "SELECT 1 AS id FROM employees ORDER BY id + 0",
        "SELECT sum(amount) OVER (ORDER BY emp) FROM payroll",
        "SELECT rowid FROM employees",
        "SELECT _rowid_, oid FROM employees",
        "SELECT 1 FROM employees e, json_each(e.salary)",
        "SELECT group_concat(name ORDER BY salary) FROM employees",
        "SELECT CASE WHEN salary > 1 THEN 1 END FROM employees",
        "SELECT name FROM employees LIMIT (SELECT count(*) FROM payroll)",
    ]

    @staticmethod
    def _real_reads(sql):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        reads = set()

        def authorizer(action, arg1, arg2, _db, _source):
            if action == sqlite3.SQLITE_READ and arg1 in ("employees", "payroll"):
                reads.add((arg1, arg2 or ""))
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        try:
            conn.execute(sql).fetchall()
        finally:
            conn.close()
        return reads

    def test_denying_any_column_sqlite_really_reads_denies_the_query(self):
        for sql in self.CORPUS:
            reads = self._real_reads(sql)
            self.assertTrue(reads, f"语料里这条没读到任何东西，写错了？{sql}")
            for table, column in sorted(reads):
                with self.subTest(sql=sql, table=table, column=column):
                    if column:
                        policy = DataPolicy(deny_columns=(f"app.{table}.{column}",))
                    else:
                        policy = DataPolicy(deny_tables=(f"app.{table}",))
                    decision = _check(policy, sql)
                    self.assertFalse(
                        decision.allowed,
                        f"SQLite 真实读了 {table}.{column or '*'}，关卡却放行了：{sql}",
                    )


if __name__ == "__main__":
    unittest.main()
