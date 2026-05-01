"""
跨仓库契约用例（CONSUMER-TESTS.md 的 A/B/C/D/E/F 六组）。

**fixtures 不拷进本仓库。** 拷贝就是副本，副本会漂，这份契约已经为此翻过一次车
（契约附录 B）。CI 把 a2a-contract checkout 到固定 tag，这里按
`fixtures/index.json` 消费——用例清单只有一份，在契约仓库里。

契约仓库位置按这个顺序找：`A2A_CONTRACT_DIR` → `./.contract` → `../a2a-contract`。
一个都没有就整组跳过（本地开发不必 clone 契约仓库），CI 里那一步是必跑的。
"""
from __future__ import annotations

import json
import os
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.testclient import TestClient

from ai_sql_api.trust import assess
from ai_sql_core import get_settings

CONTRACT_TAG = "v1.6"
"""CI 钉在这个 tag 上。契约仓库改了版本，这里也要跟着改——那是一次有意的动作。"""


def _find_contract() -> Optional[Path]:
    candidates = [
        os.environ.get("A2A_CONTRACT_DIR", ""),
        ".contract",
        str(Path(__file__).resolve().parents[2] / "a2a-contract"),
    ]
    for item in candidates:
        if not item:
            continue
        path = Path(item)
        if (path / "fixtures" / "index.json").exists():
            return path
    return None


CONTRACT = _find_contract()
SKIP_REASON = (
    "找不到 a2a-contract。设 A2A_CONTRACT_DIR，或 "
    f"git clone --depth 1 --branch {CONTRACT_TAG} <repo> .contract"
)


def _index() -> dict:
    return json.loads((CONTRACT / "fixtures" / "index.json").read_text(encoding="utf-8"))


def _registry():
    """把契约仓库的 schema 按文件名注册，让相对 $ref 能解析。"""
    from referencing import Registry, Resource

    resources = []
    for path in sorted((CONTRACT / "schema").glob("*.schema.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(doc)
        resources.append((path.name, resource))
        if "$id" in doc:
            resources.append((doc["$id"], resource))
    return Registry().with_resources(resources)


def _validator(schema_name: str):
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (CONTRACT / schema_name).read_text(encoding="utf-8")
    )
    return Draft202012Validator(schema, registry=_registry())


def _is_valid(schema_name: str, payload: Any) -> bool:
    return _validator(schema_name).is_valid(payload)


@unittest.skipIf(CONTRACT is None, SKIP_REASON)
class FixtureSchemaTest(unittest.TestCase):
    """A / B / D / E 组的形状断言：每条 fixture 的 expect 必须成立。"""

    def test_every_fixture_matches_its_expectation(self):
        cases = _index()["cases"]
        self.assertTrue(cases, "fixtures/index.json 里一条用例都没有")
        for case in cases:
            payload = json.loads(
                (CONTRACT / "fixtures" / case["file"]).read_text(encoding="utf-8")
            )
            valid = _is_valid(case["schema"], payload)
            self.assertEqual(
                valid,
                case["expect"] == "valid",
                f"{case['id']}（{case['why']}）：期望 {case['expect']}，实际 "
                f"{'valid' if valid else 'invalid'}",
            )

    def test_contract_version_is_the_one_we_pinned(self):
        # 契约仓库升了版本而这边没跟，属于「两侧不再跑同一组用例」，要显式发现
        self.assertEqual(_index()["contract_version"], CONTRACT_TAG)


@unittest.skipIf(CONTRACT is None, SKIP_REASON)
class TrustVerdictTest(unittest.TestCase):
    """
    C 组：信任反例。**这一组最容易被跳过，也最值钱**——它们全都是合法载荷，
    schema 拦不住，只能靠业务判据。

    其中 `dp-trust-01` 是核心：唯一一步 success=false，但三个信任布尔全「干净」。
    在 `execution_status` 上线之前它必须是红的（契约仓库让标 xfail）。
    **这个文件里没有 xfail——因为 execution_status 已经上线了。**
    判据见 ai_sql_api/trust.py，上线证据见 tests/test_business_outcome.py。
    """

    def test_every_fixture_gets_the_expected_trust_verdict(self):
        for case in _index()["cases"]:
            if case["trust"] == "n/a":
                continue
            payload = json.loads(
                (CONTRACT / "fixtures" / case["file"]).read_text(encoding="utf-8")
            )
            verdict = assess(
                payload, schema_valid=_is_valid(case["schema"], payload)
            )
            self.assertEqual(
                verdict,
                case["trust"],
                f"{case['id']}（{case['why']}）",
            )

    def test_failed_step_fixture_is_not_usable(self):
        """
        契约仓库点名的那条用例，单独钉一次。

        它是 §4.4 / §4.6 缺口的实际后果：`queried_database=true`、
        `has_risk=false`、`degraded=false`，今天的三布尔判据会把它判成可信，
        而唯一一步 `success=false`。
        """
        payload = json.loads(
            (CONTRACT / "fixtures/data-part/trust-negative/01-failed-step.json")
            .read_text(encoding="utf-8")
        )
        self.assertTrue(
            _is_valid("schema/artifact-data-part.schema.json", payload),
            "这条 fixture 是合法载荷，schema 本来就拦不住它",
        )
        self.assertEqual(assess(payload, schema_valid=True), "not_usable")

    def test_a_group_and_b_group_are_green_at_the_same_time(self):
        """
        反向断言：一个「永远返回不可信」的实现能过 B 组但会让 A 组挂。
        两组必须同时绿，只绿一组说明判据被写死了。
        """
        verdicts = set()
        for case in _index()["cases"]:
            if case["group"] not in ("A", "B"):
                continue
            payload = json.loads(
                (CONTRACT / "fixtures" / case["file"]).read_text(encoding="utf-8")
            )
            verdicts.add(
                assess(payload, schema_valid=_is_valid(case["schema"], payload))
            )
        self.assertIn("usable_as_source", verdicts)
        self.assertIn("not_usable", verdicts)


@unittest.skipIf(CONTRACT is None, SKIP_REASON)
class UnlistedDatabaseTest(unittest.TestCase):
    """
    D 组：v1.6 起**新增 `database` 标识符不算契约变更**，所以 `dp-valid-04`
    （`hr_db`，清单外但形状合法）是**常规路径不是异常路径**。
    """

    def test_unlisted_but_well_shaped_identifier_is_a_normal_path(self):
        payload = json.loads(
            (CONTRACT / "fixtures/data-part/valid/04-unlisted-database.json")
            .read_text(encoding="utf-8")
        )
        self.assertTrue(_is_valid("schema/artifact-data-part.schema.json", payload))
        self.assertEqual(assess(payload, schema_valid=True), "usable_as_source")

    def test_we_only_emit_identifiers_from_the_current_manifest(self):
        """
        服务端侧断言用 `current-*`，不是 `known`——退役的 mes_db / pm_db
        不会出现在任何部署里，拿 `known` 去断言运行时会发出什么是错的。
        """
        import re

        from ai_sql_a2a.server import active_database_manifest

        schema = json.loads(
            (CONTRACT / "schema/database-id.schema.json").read_text(encoding="utf-8")
        )
        pattern = re.compile(schema["pattern"])
        for name in active_database_manifest():
            self.assertRegex(name, pattern, f"{name} 不符合契约的形状约束")


@unittest.skipIf(CONTRACT is None, SKIP_REASON)
class EmittedStageSetTest(unittest.TestCase):
    """
    E 组的服务端额外断言：真跑一条流，看发出来的 stage 集合。

    **断言的目标集合从 `emitted-2026-09-04` 换成了 `contract-declared`。**
    CONSUMER-TESTS 说得很直白：「normalization 上线后改断言集合，改集合的
    那个提交就是『这条缺口关闭』的证据。」两者只差一个 `normalization`，
    它现在是真 stage（`text2sql_pipeline` 里那条裸 log_event 换成了 `stage()`）。
    """

    @classmethod
    def setUpClass(cls):
        from ai_sql_api.app import app

        cls.ctx = TestClient(app)
        cls.client = cls.ctx.__enter__()
        get_settings().a2a_streaming = True

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def _progress_events(self) -> List[dict]:
        body = {"jsonrpc": "2.0", "id": "stage-probe", "method": "message/stream",
                "params": {"message": {"kind": "message", "role": "user",
                                       "messageId": "m",
                                       "contextId": f"stage_{uuid.uuid4().hex[:8]}",
                                       "parts": [{"kind": "text",
                                                  "text": "查询准时率低于0.9的供应商名称"}],
                                       "metadata": {"user_id": "u_stage"}}}}
        with self.client.stream("POST", "/a2a", json=body,
                                headers={"Accept": "text/event-stream"}) as response:
            response.read()
            events = [
                json.loads(line[5:].strip())["result"]
                for line in response.text.splitlines()
                if line.startswith("data:")
            ]
        return [
            event for event in events
            if event.get("kind") == "status-update" and not event.get("final")
        ]

    def _stage_defs(self, name: str) -> set:
        schema = json.loads(
            (CONTRACT / "schema/stage.schema.json").read_text(encoding="utf-8")
        )
        return set(schema["$defs"][name]["enum"])

    def test_emitted_stages_are_all_contract_declared(self):
        declared = self._stage_defs("contract-declared")
        emitted = {e["metadata"]["stage"] for e in self._progress_events()}
        self.assertTrue(emitted, "查库路径至少要有进度事件")
        self.assertTrue(
            emitted <= declared,
            f"发出了契约没声明的 stage：{sorted(emitted - declared)}",
        )

    def test_normalization_is_actually_emitted_now(self):
        """契约 §4.5 缺口框的关闭证据：九个声明的 stage 里不再少这一个。"""
        emitted = {e["metadata"]["stage"] for e in self._progress_events()}
        self.assertIn("normalization", emitted)

    def test_internal_stages_are_never_emitted(self):
        emitted = {e["metadata"]["stage"] for e in self._progress_events()}
        self.assertNotIn("memory_context", emitted, "内部管道 stage 不得出现（§4.5）")
        self.assertNotIn("sql_generation", emitted, "契约面是 generation，不是内部名")

    def test_every_progress_event_matches_the_envelope_schema(self):
        """信封本身也是契约面（§4.1）：整条 JSON-RPC 响应过 progress-event schema。"""
        body = {"jsonrpc": "2.0", "id": "envelope-probe", "method": "message/stream",
                "params": {"message": {"kind": "message", "role": "user",
                                       "messageId": "m",
                                       "contextId": f"env_{uuid.uuid4().hex[:8]}",
                                       "parts": [{"kind": "text",
                                                  "text": "查询准时率低于0.9的供应商名称"}],
                                       "metadata": {"user_id": "u_env"}}}}
        with self.client.stream("POST", "/a2a", json=body,
                                headers={"Accept": "text/event-stream"}) as response:
            response.read()
            envelopes = [
                json.loads(line[5:].strip())
                for line in response.text.splitlines()
                if line.startswith("data:")
            ]
        progress = [
            env for env in envelopes
            if env["result"].get("kind") == "status-update"
            and not env["result"].get("final")
        ]
        self.assertTrue(progress)
        for envelope in progress:
            self.assertTrue(
                _is_valid("schema/progress-event.schema.json", envelope),
                f"进度事件不过契约 schema：{envelope}",
            )

    def test_progress_metadata_carries_no_post_hoc_fields(self):
        """
        route / confidence / tables / changed 要到 stage 结束才有值。
        出现在进度事件里 = 把事后信息当成了事前信息（§4.5 的诚实说明）。
        """
        for event in self._progress_events():
            for forbidden in ("route", "confidence", "tables", "changed"):
                self.assertNotIn(forbidden, event["metadata"], forbidden)


if __name__ == "__main__":
    unittest.main()
