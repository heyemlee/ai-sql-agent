"""
提示词快照：证明一次重构没有改变模型面。

在离线 Mock 模型下跑一组固定的问题，按调用顺序记下每个"面向模型"的协作者收到了什么：

- 路由、抽词、检索、规划、SQL 生成、修复、结果校验、对话问答的输入；
- 两个模型客户端真正拼出来的提示词（长文本记 sha256 + 长度）；
- 每一条被执行的 SQL。

重构（拆 `Text2SQLPipeline._run`、加运行日志、加取消点）之后，这份记录必须与
`tests/fixtures/prompt_snapshot.json` 逐项相同。相同，就说明首次运行时每一次模型调用的
输入和顺序都没变，这次重构不需要跑模型门禁。

只有**有意**改变模型面（改提示词、改调用顺序）时才重录，重录之后照规矩跑模型回归：

    UPDATE_PROMPT_SNAPSHOT=1 python -m pytest tests/test_prompt_snapshot.py

离线 Mock 下路由、对话问答、语义自检走的是规则版，模型版的提示词不在这份快照里；
它们的提示词由各自模块拼装，输入则由这里记录的 `route` / `answer` / `validate` 参数覆盖。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any, Callable, List
from unittest import mock

from ai_sql_agent.core import get_settings

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "prompt_snapshot.json"

# 单表、多表 join、跨库两步、执行失败（会进修复阶段）、再加一条按规则路由到对话问答的追问。
QUESTIONS = (
    "查询准时率低于0.9的供应商名称",
    "查询库存低于安全库存的物料名称",
    "查询实际成本超过预算的各个项目的采购总金额",
    "查询采购状态为ordered的采购单号",
    "解释一下刚才的查询结果",
)

FROZEN_AS_OF = "2026-01-01T00:00:00Z"

_INLINE_LIMIT = 80


def _digest(text: str) -> Any:
    if len(text) <= _INLINE_LIMIT:
        return text
    return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], "len": len(text)}


def _norm(value: Any) -> Any:
    """把参数变成可比较、不随运行变化的 JSON。长文本只留指纹。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _digest(value)
    if isinstance(value, (list, tuple)):
        return [_norm(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _norm(value[key]) for key in sorted(value, key=str)}
    to_prompt = getattr(value, "to_prompt_context", None)
    if callable(to_prompt):
        return {"__type__": type(value).__name__, "prompt": _digest(to_prompt())}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {field.name: _norm(getattr(value, field.name)) for field in dataclasses.fields(value)}
        return {"__type__": type(value).__name__, **fields}
    if callable(value):
        return "<callable>"
    return f"<{type(value).__name__}>"


def _targets():
    """(类, 方法名, 记录参数时丢掉的形参) —— 都是模型面上的协作者。"""
    from ai_sql_agent.execution.router import MCPRouter
    from ai_sql_agent.generation.coder_client import CoderModelClient
    from ai_sql_agent.generation.sql_generator import SqlGenerator
    from ai_sql_agent.pipeline.data_qa import ModelDataQAClient, RuleBasedDataQAClient
    from ai_sql_agent.pipeline.local_clients import HybridKeywordExtractor
    from ai_sql_agent.pipeline.routing import DynamicIntentRouter
    from ai_sql_agent.planning.cot_planner import CotPlanner
    from ai_sql_agent.planning.thinking_client import ThinkingModelClient
    from ai_sql_agent.repair.repairer import SqlRepairService
    from ai_sql_agent.retrieval.hierarchical_service import ThreeTierSchemaRetrievalService
    from ai_sql_agent.validation.validator import ResultValidator

    return (
        (DynamicIntentRouter, "route", ()),
        (HybridKeywordExtractor, "extract", ()),
        (ThreeTierSchemaRetrievalService, "retrieve", ("access",)),
        (CotPlanner, "plan", ()),
        (ThinkingModelClient, "generate", ()),
        (ThinkingModelClient, "generate_structured", ()),
        (ThinkingModelClient, "generate_stream", ()),
        (SqlGenerator, "generate", ()),
        (CoderModelClient, "generate_sql", ()),
        (SqlRepairService, "repair", ("execute",)),
        (ResultValidator, "validate", ()),
        (RuleBasedDataQAClient, "answer", ()),
        (ModelDataQAClient, "answer", ()),
        (MCPRouter, "execute", ()),
    )


def _recorder(records: List[dict], owner: type, name: str, drop: tuple) -> Callable:
    original = getattr(owner, name)

    def wrapper(self, *args, **kwargs):
        kept = {key: value for key, value in kwargs.items() if key not in drop}
        records.append({
            "call": f"{owner.__name__}.{name}",
            "args": _norm(list(args)),
            "kwargs": _norm(kept),
        })
        return original(self, *args, **kwargs)

    return wrapper


def record_snapshot() -> List[dict]:
    """在 Mock 模型下跑 QUESTIONS，返回按调用顺序的记录。"""
    from ai_sql_agent.memory import ConversationMemoryService
    from ai_sql_agent.memory.conversation import MemoryServiceConfig
    from ai_sql_agent.memory.short_term import ShortTermMemoryConfig
    from ai_sql_agent.pipeline import Text2SQLPipeline
    from ai_sql_agent.pipeline.dynamic_service import DynamicSqlAgentService

    records: List[dict] = []
    with tempfile.TemporaryDirectory() as temp_dir:
        memory = ConversationMemoryService(
            MemoryServiceConfig(
                db_path=Path(temp_dir) / "memory.db",
                embedding_dimensions=128,
                use_model_summarizer_when_available=False,
                use_reranker_when_available=False,
                vector_backend="none",
                # 摘要同步做：后台线程什么时候做完是竞态，会话上下文就会忽有忽无一段摘要
                short_term=ShortTermMemoryConfig(async_summary=False),
            )
        )
        try:
            service = DynamicSqlAgentService(Text2SQLPipeline(), memory)
            patches = [
                mock.patch.object(owner, name, autospec=True,
                                  side_effect=_recorder(records, owner, name, drop))
                for owner, name, drop in _targets()
            ]
            patches.append(mock.patch(
                "ai_sql_agent.execution.sqlite_executor.now_rfc3339", return_value=FROZEN_AS_OF
            ))
            for patcher in patches:
                patcher.start()
            try:
                session = f"snap_{uuid.uuid4().hex[:8]}"
                for question in QUESTIONS:
                    records.append({"question": question})
                    result = service.run(user_id="u_snapshot", session_id=session, query=question)
                    records.append({
                        "route": result.decision.route.value,
                        "answer": _digest(result.answer),
                        "degraded": result.degraded,
                    })
            finally:
                for patcher in reversed(patches):
                    patcher.stop()
        finally:
            memory.close(wait=True)
    return records


_CHILD = """
import json, sys
sys.path.insert(0, sys.argv[1])
from ai_sql_agent.core import reload_settings
from ai_sql_agent.core.settings import SqlAgentSettings
SqlAgentSettings.model_config["env_file"] = None
reload_settings()
import test_prompt_snapshot as snapshot
runs = [snapshot.record_snapshot(), snapshot.record_snapshot()]
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(runs, handle, ensure_ascii=False)
"""


def record_in_fixed_hash_seed() -> List[List[dict]]:
    """
    在 PYTHONHASHSEED=0 的子进程里录两遍。

    Schema 上下文里表和字段的先后顺序取决于字符串哈希的随机种子：同一个问题，进程重启之后
    拼出来的提示词顺序可能不同（这是现状，不是这份测试造成的）。固定种子才能拿它当基线。
    子进程用和当前进程相同的 sys.path，所以测的是同一份代码；工作目录是临时目录，
    演示库建在那里，不碰仓库里的 runtime_data。
    """
    import subprocess
    import sys

    with tempfile.TemporaryDirectory() as work_dir:
        out_path = Path(work_dir) / "records.json"
        env = dict(os.environ)
        env.update({
            "PYTHONHASHSEED": "0",
            "PYTHONPATH": os.pathsep.join(path for path in sys.path if path),
            "AI_SQL_LOG_LEVEL": "ERROR",
            "AI_SQL_MODEL_SETTINGS_ENABLED": "false",
        })
        for key in ("AI_SQL_OPENAI_API_KEY", "OPENAI_API_KEY",
                    "AI_SQL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"):
            env[key] = ""
        completed = subprocess.run(
            [sys.executable, "-c", _CHILD, str(Path(__file__).parent), str(out_path)],
            cwd=work_dir, env=env, capture_output=True, text=True, timeout=300,
        )
        if completed.returncode != 0:
            raise AssertionError(f"录制子进程失败：\n{completed.stderr[-4000:]}")
        return json.loads(out_path.read_text(encoding="utf-8"))


class PromptSnapshotTest(unittest.TestCase):
    """首次运行的模型面与录下的快照逐项相同。"""

    @classmethod
    def setUpClass(cls):
        cls.runs = None
        if not get_settings().has_model_credentials:
            cls.runs = record_in_fixed_hash_seed()

    def setUp(self):
        if self.runs is None:
            self.skipTest("快照只在离线 Mock 模型下有意义；当前环境配置了模型凭证")

    def test_model_facing_calls_match_the_recorded_snapshot(self):
        records = self.runs[0]

        if os.environ.get("UPDATE_PROMPT_SNAPSHOT") == "1":
            SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
            SNAPSHOT_PATH.write_text(
                json.dumps(records, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
            )
            self.skipTest(f"已重录 {SNAPSHOT_PATH.name}（{len(records)} 条）")

        expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        for index, (got, want) in enumerate(zip(records, expected)):
            self.assertEqual(
                got, want,
                f"第 {index} 条记录与快照不同——模型面变了。有意变更才重录（见模块说明）。",
            )
        self.assertEqual(len(records), len(expected), "记录条数与快照不同")

    def test_two_runs_record_the_same_thing(self):
        """快照本身必须稳定，否则它证明不了任何事。"""
        self.assertEqual(self.runs[0], self.runs[1])


if __name__ == "__main__":
    unittest.main()
