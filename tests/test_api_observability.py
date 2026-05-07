import unittest

from fastapi.testclient import TestClient

from ai_sql_agent.api.app import TRACE_HEADER, app


class ApiTraceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_ctx = TestClient(app)
        cls.client = cls.client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_ctx.__exit__(None, None, None)

    def test_health(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})

    def test_query_returns_trace_id_in_body_and_header(self):
        response = self.client.post(
            "/query",
            json={
                "user_id": "u_test",
                "session_id": "s_trace",
                "query": "查询准时率低于0.9的供应商名称",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertTrue(body["trace_id"])
        self.assertEqual(response.headers[TRACE_HEADER], body["trace_id"])
        self.assertEqual(body["route"], "database_query")
        self.assertTrue(body["step_logs"])
        self.assertTrue(body["step_logs"][0]["success"])

    def test_incoming_trace_id_is_propagated(self):
        response = self.client.post(
            "/query",
            headers={TRACE_HEADER: "upstream-trace-1"},
            json={"user_id": "u_test", "session_id": "s_trace", "query": "你好"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["trace_id"], "upstream-trace-1")
        self.assertEqual(response.headers[TRACE_HEADER], "upstream-trace-1")

    def test_invalid_request_is_rejected(self):
        response = self.client.post(
            "/query", json={"user_id": "", "session_id": "s", "query": "x"}
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()


class FrontendTest(unittest.TestCase):
    """页面由 FastAPI 直接托管，没有构建步骤。"""

    @classmethod
    def setUpClass(cls):
        cls.client_ctx = TestClient(app)
        cls.client = cls.client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_ctx.__exit__(None, None, None)

    def test_index_is_served(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])

    def test_page_wires_the_query_api(self):
        html = self.client.get("/").text
        for marker in ["/query", "X-API-Key", "trace_id", "degraded", "step_logs"]:
            self.assertIn(marker, html)

    def test_page_escapes_untrusted_values(self):
        # 结果集内容来自数据库，直接插 innerHTML 会有 XSS 风险。
        # 不锁转义函数叫什么名字（改版换过一次），只锁两件必须成立的事：
        # 转义覆盖到这几个字符，且单元格与 SQL 确实走了转义再拼进模板。
        html = self.client.get("/").text
        for entity in ("&amp;", "&lt;", "&gt;", "&quot;"):
            self.assertIn(entity, html, entity)
        # 不锁变量名，只锁「插值前过了一层函数」——裸插 ${cell} / ${row[c]}
        # 这种写法匹配不上，正是要拦的那种
        self.assertRegex(html, r"<td[^>]*>\$\{\s*\w+\(")
        self.assertRegex(html, r"<pre[^>]*>\$\{\s*\w+\(")
