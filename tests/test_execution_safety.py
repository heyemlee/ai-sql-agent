import sqlite3
import tempfile
import unittest
from pathlib import Path

from ai_sql_agent.auth import authorization_scope
from ai_sql_agent.execution import MCPExecutionRequest, SQLiteMCPExecutor, check_readonly_sql
from ai_sql_agent.retrieval.access import ALLOW_ALL
from ai_sql_agent.execution.dialects import POSTGRES, SQLITE, get_dialect


class SqlGuardTest(unittest.TestCase):
    def test_allows_plain_select_and_cte(self):
        self.assertTrue(check_readonly_sql("SELECT 1").allowed)
        self.assertTrue(check_readonly_sql("WITH x AS (SELECT 1) SELECT * FROM x").allowed)
        self.assertTrue(check_readonly_sql("-- 注释\nSELECT 2").allowed)
        self.assertTrue(check_readonly_sql("SELECT * FROM t /* 注释 */ LIMIT 1").allowed)

    def test_rejects_write_statements(self):
        for sql in [
            "UPDATE t SET a = 1",
            "DELETE FROM t",
            "INSERT INTO t VALUES (1)",
            "DROP TABLE t",
        ]:
            self.assertFalse(check_readonly_sql(sql).allowed, sql)

    def test_rejects_multi_statement(self):
        result = check_readonly_sql("SELECT 1; DROP TABLE t")
        self.assertFalse(result.allowed)
        self.assertIn("一条", result.reason)

    def test_rejects_write_hidden_behind_cte(self):
        # 只看首关键字会放行，这里必须靠全语句关键字扫描拦住
        result = check_readonly_sql("WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x")
        self.assertFalse(result.allowed)

    def test_rejects_attach_pragma_and_file_functions(self):
        self.assertFalse(check_readonly_sql("ATTACH DATABASE '/tmp/e.db' AS e").allowed)
        self.assertFalse(check_readonly_sql("PRAGMA table_info(t)").allowed)
        self.assertFalse(check_readonly_sql("SELECT load_extension('x')").allowed)
        self.assertFalse(check_readonly_sql("SELECT writefile('/tmp/x', 'y')").allowed)

    def test_semicolon_inside_string_literal_is_fine(self):
        self.assertTrue(
            check_readonly_sql("SELECT * FROM t WHERE name = 'a; DROP TABLE t'").allowed
        )

    def test_empty_sql_rejected(self):
        self.assertFalse(check_readonly_sql("   ").allowed)
        self.assertFalse(check_readonly_sql(";").allowed)


class SQLiteExecutorSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "safety.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany("INSERT INTO t VALUES (?, ?)", [(i, f"n{i}") for i in range(1, 11)])
        conn.commit()
        conn.close()
        self.executor = SQLiteMCPExecutor(
            database="test_db", db_path=self.db_path, max_rows=3
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def execute(self, sql):
        # 执行器入口现在有强制授权关卡：不绑作用域一律拒绝（这是有意的默认值，
        # 见 ai_sql_agent.auth.enforcement）。这组用例验的是只读约束与行数上限，
        # 所以显式绑一个全放开的策略，把权限这一维度排除掉。
        with authorization_scope(ALLOW_ALL):
            return self.executor.execute(
                MCPExecutionRequest(database="test_db", sql=sql)
            )

    def test_select_succeeds(self):
        result = self.execute("SELECT id FROM t ORDER BY id")
        self.assertTrue(result.success)
        self.assertEqual(result.row_count, 3)
        self.assertTrue(result.truncated)

    def test_row_cap_not_marked_truncated_when_exact(self):
        result = self.execute("SELECT id FROM t ORDER BY id LIMIT 3")
        self.assertTrue(result.success)
        self.assertFalse(result.truncated)

    def test_write_is_rejected_by_guard(self):
        result = self.execute("UPDATE t SET name = 'x'")
        self.assertFalse(result.success)

    def test_connection_layer_is_readonly(self):
        # 第二道防线：即使绕过应用层校验，连接本身也写不进去
        conn = self.executor._connect()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE t SET name = 'x'")
        finally:
            conn.close()

    def test_missing_database_file_reports_clearly(self):
        executor = SQLiteMCPExecutor(database="test_db", db_path="/nonexistent/x.db")
        with authorization_scope(ALLOW_ALL):
            result = executor.execute(
                MCPExecutionRequest(database="test_db", sql="SELECT 1")
            )
        self.assertFalse(result.success)
        self.assertIn("不存在", result.error)

    def test_wrong_database_is_not_executed(self):
        with authorization_scope(ALLOW_ALL):
            result = self.executor.execute(
                MCPExecutionRequest(database="other_db", sql="SELECT 1")
            )
        self.assertFalse(result.success)
        self.assertIn("路由错误", result.error)


class DialectTest(unittest.TestCase):
    def test_lookup_and_aliases(self):
        self.assertIs(get_dialect("postgres"), POSTGRES)
        self.assertIs(get_dialect("postgresql"), POSTGRES)
        self.assertIs(get_dialect("PG"), POSTGRES)
        self.assertIs(get_dialect("sqlite3"), SQLITE)

    def test_unknown_dialect_falls_back_to_sqlite(self):
        self.assertIs(get_dialect("oracle"), SQLITE)
        self.assertIs(get_dialect(""), SQLITE)

    def test_prompt_rules_mention_dialect_specifics(self):
        self.assertIn("PostgreSQL", POSTGRES.to_prompt_rules())
        self.assertIn("INTERVAL", POSTGRES.to_prompt_rules())
        self.assertIn("JULIANDAY", SQLITE.to_prompt_rules())


class DialectInPromptTest(unittest.TestCase):
    def test_generation_prompt_carries_target_dialect(self):
        from ai_sql_agent.generation.objects import CotStep, LocalSchema, SqlGenerationRequest
        from ai_sql_agent.generation.prompt_builder import SqlPromptBuilder

        request = SqlGenerationRequest(
            cot_step=CotStep(
                database="pm_db",
                processing_objects="work_order.schedule_date",
                operation_instruction="筛选排产日期在2024-04-01之后的工单",
                output_target="work_order.order_no",
            ),
            local_schema=LocalSchema(),
            sql_dialect="postgres",
        )
        prompt = SqlPromptBuilder().build(request)

        self.assertIn("PostgreSQL", prompt)
        self.assertIn("方言约束", prompt)
        self.assertNotIn("JULIANDAY", prompt)

    def test_generation_prompt_asks_for_readable_column_names(self):
        """
        结果的列名直接成了页面上的表头。规划是中文写的，模型顺手就给计算列起中文别名，
        用英文提问的人看到的是一排读不懂的表头——所以别名的写法得明说。
        """
        from ai_sql_agent.generation.objects import CotStep, LocalSchema, SqlGenerationRequest
        from ai_sql_agent.generation.prompt_builder import SqlPromptBuilder

        prompt = SqlPromptBuilder().build(
            SqlGenerationRequest(
                cot_step=CotStep(
                    database="erp_db",
                    processing_objects="payment.paid_amt",
                    operation_instruction="按采购单汇总已付金额",
                    output_target="已付金额合计",
                ),
                local_schema=LocalSchema(),
            )
        )

        self.assertIn("英文小写加下划线", prompt)
        self.assertIn("不要用中文别名", prompt)
        self.assertIn("直接输出的字段保持原名", prompt)


if __name__ == "__main__":
    unittest.main()


class SchemaFingerprintTest(unittest.TestCase):
    """Schema 指纹：只反映结构变化，不受数据变化影响。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "fp.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _fingerprint(self):
        from ai_sql_agent.retrieval.schema_fingerprint import sqlite_schema_fingerprint

        return sqlite_schema_fingerprint(str(self.db_path))

    def _execute(self, sql):
        conn = sqlite3.connect(self.db_path)
        conn.execute(sql)
        conn.commit()
        conn.close()

    def test_inserting_rows_does_not_change_fingerprint(self):
        before = self._fingerprint()
        self._execute("INSERT INTO t VALUES (1, 'x')")
        self.assertEqual(before, self._fingerprint())

    def test_adding_a_column_changes_fingerprint(self):
        before = self._fingerprint()
        self._execute("ALTER TABLE t ADD COLUMN age INTEGER")
        self.assertNotEqual(before, self._fingerprint())

    def test_adding_a_table_changes_fingerprint(self):
        before = self._fingerprint()
        self._execute("CREATE TABLE t2 (id INTEGER)")
        self.assertNotEqual(before, self._fingerprint())

    def test_unreachable_database_yields_empty_fingerprint(self):
        from ai_sql_agent.retrieval.schema_fingerprint import database_fingerprint

        # 取不到指纹时返回空串，调用方据此跳过重建而不是误判为“变了”
        self.assertEqual(
            database_fingerprint(dialect="sqlite", db_path="/nonexistent/x.db"), ""
        )


class SchemaHotReloadTest(unittest.TestCase):
    def test_pipeline_detects_and_reloads_schema_changes(self):
        from ai_sql_agent.pipeline import Text2SQLPipeline

        pipeline = Text2SQLPipeline()
        self.assertEqual(pipeline.detect_schema_changes(), [])

        conn = sqlite3.connect("runtime_data/erp_demo.db")
        conn.execute("ALTER TABLE sales_order ADD COLUMN hot_reload_probe TEXT")
        conn.commit()
        conn.close()

        self.assertEqual(pipeline.detect_schema_changes(), ["erp_db"])

        changed = pipeline.reload_schema(reason="unittest")
        self.assertEqual(changed, ["erp_db"])
        self.assertEqual(pipeline.detect_schema_changes(), [])

    def test_watcher_is_disabled_by_default(self):
        from ai_sql_agent.api.schema_watcher import SchemaWatcher

        watcher = SchemaWatcher(pipeline=None, interval_seconds=0)
        self.assertFalse(watcher.enabled)
        watcher.start()  # 不应启动线程，也不应报错
        watcher.stop()


class CrossDatabaseReferenceGuardTest(unittest.TestCase):
    """生成的 SQL 只能引用当前步骤目标库里的表。

    跨库问题里模型容易把两个库压进一条 SQL（在 erp 的步骤里写
    `... IN (SELECT project_code FROM project)`），执行时报 no such table。
    实测这类错误在跨库类目里占比很高，静态就能判出来。
    """

    def setUp(self):
        from ai_sql_agent.pipeline import Text2SQLPipeline

        self.pipeline = Text2SQLPipeline()

    def test_tables_of_returns_only_target_database_tables(self):
        erp = self.pipeline._tables_of("erp_db")
        self.assertIn("purchase_order", erp)
        self.assertNotIn("project", erp)      # project 属于 pm_db
        self.assertNotIn("work_order", erp)   # work_order 属于 mes_db

    def test_unknown_database_returns_empty_and_skips_check(self):
        self.assertEqual(self.pipeline._tables_of("nope_db"), set())

    def test_foreign_table_reference_triggers_regeneration(self):
        """引用外库表时应带提示重生成；改好了就用新 SQL。"""
        from ai_sql_agent.generation.objects import CotStep

        good_sql = "SELECT SUM(po_amt) FROM purchase_order WHERE job_no IN ('J24001')"

        class _Generator:
            def __init__(self):
                self.prompts = []

            def generate(
                self,
                cot_step,
                sql_dialect="",
                previous_results="",
                certified_examples="",
            ):
                self.prompts.append(cot_step.operation_instruction)
                return type("R", (), {"sql": good_sql})()

        generator = _Generator()
        bad = type("R", (), {
            "sql": "SELECT SUM(po_amt) FROM purchase_order "
                   "WHERE job_no IN (SELECT project_code FROM project)"
        })()

        fixed = self.pipeline._enforce_step_database(
            sql_result=bad,
            cot_step=CotStep(
                database="erp_db",
                processing_objects="purchase_order.po_amt",
                operation_instruction="汇总采购金额",
                output_target="purchase_order.po_amt",
            ),
            generator=generator,
            dialect="sqlite",
            previous_results='{"rows": [{"project_code": "J24001"}]}',
            step_index=2,
        )

        self.assertEqual(fixed.sql, good_sql)
        self.assertIn("project", generator.prompts[0])
        self.assertIn("不要写跨库子查询", generator.prompts[0])

    def test_clean_sql_is_left_untouched(self):
        from ai_sql_agent.generation.objects import CotStep

        class _Generator:
            def generate(self, *a, **k):
                raise AssertionError("SQL 合法时不应触发重生成")

        original = type("R", (), {"sql": "SELECT po_amt FROM purchase_order"})()
        result = self.pipeline._enforce_step_database(
            sql_result=original,
            cot_step=CotStep(
                database="erp_db",
                processing_objects="purchase_order.po_amt",
                operation_instruction="查询",
                output_target="purchase_order.po_amt",
            ),
            generator=_Generator(),
            dialect="sqlite",
            previous_results="",
            step_index=1,
        )
        self.assertIs(result, original)


class DockerImageHygieneTest(unittest.TestCase):
    """
    凭证不能进镜像。

    实测踩到过：`.dockerignore` 排除了 `deploy/api_keys.json`，却漏了 `.env`，
    于是带真实 API Key 的 `.env` 被 `COPY . /app` 打进了镜像——
    起容器后不传任何 key，`/readyz` 照样显示 `model_credentials: ok`。
    镜像一旦推到 registry，密钥就跟着分发到每台拉取的机器上。

    `docker build` 成功发现不了这个，只有真的起一个容器进去看才会发现。
    """

    IGNORE = Path(__file__).resolve().parents[1] / ".dockerignore"

    def setUp(self):
        self.patterns = [
            line.strip()
            for line in self.IGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def test_env_file_is_excluded_from_the_image(self):
        self.assertIn(".env", self.patterns, ".env 会带着真实 API Key 进镜像")

    def test_api_key_file_is_excluded(self):
        self.assertIn("deploy/api_keys.json", self.patterns)

    def test_env_example_is_still_shipped(self):
        """模板要留着，否则镜像里没有任何配置参考。"""
        self.assertIn("!.env.example", self.patterns)
