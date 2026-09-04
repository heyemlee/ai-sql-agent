"""
A2A 服务端：契约 a2a-streaming-contract.md（v1.3）里能在单测里钉住的部分。

起整个 app（演示库 + Mock 模型），和 test_api_observability 同一套搭法。
"""
import json
import unittest

from fastapi.testclient import TestClient

from ai_sql_api.app import app
from ai_sql_core import get_settings


def _rpc(method, text="查询准时率低于0.9的供应商名称", user_id="u_a2a", **extra):
    return {"jsonrpc": "2.0", "id": "req-1", "method": method,
            "params": {"message": {"kind": "message", "role": "user", "messageId": "m1",
                                   "contextId": "s_a2a",
                                   "parts": [{"kind": "text", "text": text}],
                                   "metadata": {"user_id": user_id}, **extra}}}


def _events(resp):
    """把 SSE 正文拆成 result 事件列表。"""
    out = []
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            out.append(json.loads(line[5:].strip())["result"])
    return out


class A2ATest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        get_settings().a2a_streaming = False

    # ── Agent Card 与开关（契约 §6.2）──

    def test_card_reflects_the_streaming_flag(self):
        card = self.client.get("/.well-known/agent.json").json()
        self.assertEqual(card["version"], "1.1.0")
        self.assertEqual(card["protocolVersion"], "0.2")
        self.assertFalse(card["capabilities"]["streaming"])
        self.assertFalse(card["capabilities"]["pushNotifications"])
        get_settings().a2a_streaming = True
        self.assertTrue(self.client.get("/.well-known/agent.json").json()["capabilities"]["streaming"])

    def test_stream_returns_32004_when_flag_is_off(self):
        """开关关着：-32004 在**流打开之前**、以普通 JSON 返回（§3.2 / §6.2）。"""
        r = self.client.post("/a2a", json=_rpc("message/stream"))
        self.assertIn("application/json", r.headers["content-type"])
        self.assertEqual(r.json()["error"]["code"], -32004)
        self.assertEqual(r.json()["id"], "req-1")

    # ── 刻意不做的方法（契约 §7）──

    def test_unsupported_methods_are_32004(self):
        for m in ("tasks/cancel", "tasks/resubscribe", "tasks/get"):
            self.assertEqual(self.client.post("/a2a", json=_rpc(m)).json()["error"]["code"], -32004, m)

    def test_unknown_method_is_32601(self):
        self.assertEqual(self.client.post("/a2a", json=_rpc("nope/what")).json()["error"]["code"], -32601)

    def test_bad_params_are_32602_json(self):
        body = {"jsonrpc": "2.0", "id": "x", "method": "message/send", "params": {"message": {"parts": []}}}
        r = self.client.post("/a2a", json=body)
        self.assertIn("application/json", r.headers["content-type"])
        self.assertEqual(r.json()["error"]["code"], -32602)

    def test_non_jsonrpc_body_is_32600(self):
        self.assertEqual(self.client.post("/a2a", json={"hello": 1}).json()["error"]["code"], -32600)

    # ── message/send ──

    def test_send_returns_a_completed_task_with_isomorphic_artifact(self):
        r = self.client.post("/a2a", json=_rpc("message/send"))
        self.assertEqual(r.status_code, 200)
        task = r.json()["result"]
        self.assertEqual(task["kind"], "task")
        self.assertEqual(task["status"]["state"], "completed")
        self.assertEqual(task["contextId"], "s_a2a")
        parts = task["artifacts"][0]["parts"]
        text = next(p for p in parts if p["kind"] == "text")["text"]
        data = next(p for p in parts if p["kind"] == "data")["data"]
        self.assertTrue(text)
        # §4.4：data part 与 /query 的字段完全同构
        for key in ("route", "confidence", "queried_database", "has_risk", "degraded",
                    "degrade_reason", "selected_databases", "trace_id", "steps"):
            self.assertIn(key, data, key)
        self.assertEqual(data["route"], "database_query")
        self.assertTrue(data["steps"] and data["steps"][0]["success"])
        # 与 /query 跑同一个问题比对
        q = self.client.post("/query", json={"user_id": "u_a2a", "session_id": "s_cmp",
                                              "query": "查询准时率低于0.9的供应商名称"}).json()
        self.assertEqual(data["selected_databases"], q["selected_databases"])
        self.assertEqual(data["steps"][0]["final_sql"], q["step_logs"][0]["final_sql"])

    def test_trace_id_header_is_propagated_into_task(self):
        r = self.client.post("/a2a", headers={"X-Trace-Id": "up-1"}, json=_rpc("message/send"))
        self.assertEqual(r.headers["X-Trace-Id"], "up-1")
        self.assertEqual(r.json()["result"]["metadata"]["trace_id"], "up-1")

    # ── message/stream（契约 §4.2 的顺序保证）──

    def test_stream_event_order_is_guaranteed(self):
        get_settings().a2a_streaming = True
        with self.client.stream("POST", "/a2a", json=_rpc("message/stream"),
                                headers={"Accept": "text/event-stream"}) as r:
            self.assertIn("text/event-stream", r.headers["content-type"])
            r.read()
            events = _events(r)

        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds[0], "task", "首帧一定是 task")
        self.assertEqual(events[0]["status"]["state"], "submitted")
        finals = [e for e in events if e.get("kind") == "status-update" and e.get("final")]
        self.assertEqual(len(finals), 1, "终态有且只有一个")
        self.assertIs(events[-1], finals[0], "终态一定是最后一个")
        self.assertEqual(finals[0]["status"]["state"], "completed")
        self.assertIn("trace_id", finals[0]["metadata"])
        arts = [i for i, e in enumerate(events) if e["kind"] == "artifact-update"]
        self.assertEqual(len(arts), 1)
        self.assertLess(arts[0], len(events) - 1, "artifact 在终态之前")
        self.assertTrue(events[arts[0]]["lastChunk"])

        progress = [e for e in events if e["kind"] == "status-update" and not e.get("final")]
        self.assertTrue(progress, "查库路径至少有进度事件")
        stages = [e["metadata"]["stage"] for e in progress]
        # 契约面的 stage 名，不是内部名
        self.assertNotIn("sql_generation", stages)
        self.assertNotIn("memory_context", stages)
        self.assertTrue({"routing", "retrieval", "planning", "generation", "execution"} & set(stages))
        for e in progress:
            self.assertFalse(e["final"])
            self.assertEqual(e["status"]["state"], "working")
            self.assertIn("trace_id", e["metadata"])
            self.assertTrue(e["status"]["message"]["parts"][0]["text"])
        # 按 step 重复的 stage 带 step/database；顺序与 pipeline 一致
        exec_events = [e for e in progress if e["metadata"]["stage"] == "execution"]
        self.assertTrue(exec_events and "database" in exec_events[0]["metadata"])

    def test_stream_admission_failures_are_json_not_sse(self):
        """鉴权/参数错一律在流打开之前、普通 JSON（§3.2）。"""
        get_settings().a2a_streaming = True
        body = {"jsonrpc": "2.0", "id": "x", "method": "message/stream", "params": {"message": {"parts": []}}}
        r = self.client.post("/a2a", json=body, headers={"Accept": "text/event-stream"})
        self.assertIn("application/json", r.headers["content-type"])
        self.assertEqual(r.json()["error"]["code"], -32602)

    def test_mid_stream_failure_ends_with_a_failed_terminal(self):
        """流中途出错不靠断连表示失败：补一个 failed 终态再关（§5.2）。"""
        get_settings().a2a_streaming = True
        original = app.state.service.run

        def boom(**kw):
            raise RuntimeError("模拟中途炸了")
        app.state.service.run = boom
        try:
            with self.client.stream("POST", "/a2a", json=_rpc("message/stream")) as r:
                r.read()
                events = _events(r)
        finally:
            app.state.service.run = original
        self.assertEqual(events[0]["kind"], "task")
        last = events[-1]
        self.assertTrue(last["final"])
        self.assertEqual(last["status"]["state"], "failed")
        self.assertIn("模拟中途炸了", last["metadata"]["error"])
        self.assertFalse(any(e["kind"] == "artifact-update" for e in events))

    def test_stream_and_send_share_one_audit_shape(self):
        """
        §5.5：一次 stream = 一次 /query = 一条审计。

        用独立 user_id：/history 有 20 条上限，共用 u_a2a 时前面的用例攒满之后
        before 和 after 都是 20，这条断言就永远成立不了——单跑过、全量跑挂。
        """
        get_settings().a2a_streaming = True
        user = "u_audit_once"
        self.assertEqual(self.client.get("/history", params={"user_id": user}).json()["items"], [])
        with self.client.stream("POST", "/a2a", json=_rpc("message/stream", user_id=user)) as r:
            r.read()
        items = self.client.get("/history", params={"user_id": user}).json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["route"], "database_query")


if __name__ == "__main__":
    unittest.main()
