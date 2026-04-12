from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from ai_sql_memory import (
    ConversationMemoryService,
    ExtractiveMemorySummarizer,
    LocalMemoryEmbeddingClient,
    MemoryKind,
    MemoryServiceConfig,
    MessageRole,
    ShortTermMemoryConfig,
    ShortTermMemoryService,
    SQLiteMemoryStore,
)
from ai_sql_memory.long_term import LongTermMemoryService
from ai_sql_pipeline import (
    Text2SQLPipeline,
    MemoryAwareSqlAgentService,
    PipelineConfig,
)


class BlockingSummarizer(ExtractiveMemorySummarizer):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def summarize_conversation(self, previous_summary, messages):
        self.started.set()
        self.release.wait(timeout=5)
        return super().summarize_conversation(previous_summary, messages)


class MemoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "memory.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_async_summary_never_blocks_context_read(self) -> None:
        store = SQLiteMemoryStore(self.db_path)
        summarizer = BlockingSummarizer()
        service = ShortTermMemoryService(
            store=store,
            summarizer=summarizer,
            config=ShortTermMemoryConfig(
                max_window_messages=2,
                max_window_tokens=1000,
                async_summary=True,
            ),
        )
        try:
            service.add_message(user_id="u1", session_id="s1", role="user", content="第一问")
            service.add_message(user_id="u1", session_id="s1", role="assistant", content="第一答")
            service.add_message(user_id="u1", session_id="s1", role="user", content="第二问")
            self.assertTrue(summarizer.started.wait(timeout=2))

            context_while_running = service.get_context(user_id="u1", session_id="s1")
            self.assertTrue(context_while_running.summary_pending)
            self.assertIsNone(context_while_running.summary)
            self.assertEqual([m.content for m in context_while_running.messages], ["第一答", "第二问"])

            summarizer.release.set()
            service.wait_for_summary(user_id="u1", session_id="s1", timeout=5)
            completed = service.get_context(user_id="u1", session_id="s1")
            self.assertIn("第一问", completed.summary.content)
            self.assertEqual(completed.summary.through_message_id, 1)
        finally:
            summarizer.release.set()
            service.close()

    def test_token_limit_keeps_latest_message(self) -> None:
        store = SQLiteMemoryStore(self.db_path)
        service = ShortTermMemoryService(
            store=store,
            config=ShortTermMemoryConfig(
                max_window_messages=10,
                max_window_tokens=2,
                async_summary=False,
            ),
        )
        try:
            service.add_message(user_id="u1", session_id="s1", role=MessageRole.USER, content="很长的一条消息")
            service.add_message(user_id="u1", session_id="s1", role=MessageRole.ASSISTANT, content="最新消息")
            context = service.get_context(user_id="u1", session_id="s1")
            self.assertEqual([message.content for message in context.messages], ["最新消息"])
            self.assertIsNotNone(context.summary)
        finally:
            service.close()

    def test_long_term_recall_is_user_isolated_and_filterable(self) -> None:
        store = SQLiteMemoryStore(self.db_path)
        service = LongTermMemoryService(
            store=store,
            embedding_client=LocalMemoryEmbeddingClient(dimensions=128),
        )
        trade = service.save_structured(
            user_id="u1",
            query="查询供应商名称",
            result={"supplier_name": "烤漆"},
            database="erp_db",
            tables=["supplier"],
        )
        service.save_unstructured(
            user_id="u1",
            title="客服口径",
            content="退款审核通常需要三个工作日",
        )
        service.save_unstructured(
            user_id="u2",
            title="私有信息",
            content="供应商名称是内部保密信息",
        )

        hits = service.recall(user_id="u1", query="之前查询的供应商", top_k=2)
        self.assertEqual(hits[0].memory.id, trade.id)
        self.assertTrue(all(hit.memory.user_id == "u1" for hit in hits))

        filtered = service.recall(
            user_id="u1",
            query="门板",
            top_k=5,
            kinds=[MemoryKind.STRUCTURED],
            metadata_filters={"database": "erp_db", "tables": ["supplier"]},
        )
        self.assertEqual([hit.memory.id for hit in filtered], [trade.id])

    def test_long_term_is_only_written_by_explicit_save(self) -> None:
        memory = ConversationMemoryService(
            MemoryServiceConfig(
                db_path=self.db_path,
                embedding_dimensions=128,
                vector_backend="none",
                use_model_summarizer_when_available=False,
                short_term=ShortTermMemoryConfig(max_window_messages=4),
            )
        )
        try:
            memory.begin_user_turn(user_id="u1", session_id="s1", query="解释这个结果")
            memory.record_assistant_message(
                user_id="u1", session_id="s1", content="这是结果解释"
            )
            self.assertEqual(memory.store.list_long_term_memories(user_id="u1"), [])

            saved = memory.save_unstructured_content(
                user_id="u1", title="结果解释", content="这是结果解释"
            )
            self.assertEqual(
                memory.store.get_long_term_memory(user_id="u1", memory_id=saved.id).id,
                saved.id,
            )
        finally:
            memory.close()

    def test_memory_aware_pipeline_records_turn_and_supports_explicit_save(self) -> None:
        memory = ConversationMemoryService(
            MemoryServiceConfig(
                db_path=self.db_path,
                embedding_dimensions=128,
                vector_backend="none",
                use_model_summarizer_when_available=False,
                use_reranker_when_available=False,
            )
        )
        pipeline = Text2SQLPipeline(
            PipelineConfig(db_path=Path(self.temp_dir.name) / "showroom.db")
        )
        service = MemoryAwareSqlAgentService(pipeline, memory)
        try:
            response = service.run(
                user_id="u1",
                session_id="s1",
                query="查询准时率低于0.9的供应商名称",
                enable_long_term=False,
            )
            messages = memory.store.list_messages(user_id="u1", session_id="s1")
            self.assertEqual([message.role for message in messages], [MessageRole.USER, MessageRole.ASSISTANT])
            self.assertTrue(response.pipeline_result.step_logs[0].execution_result["success"])
            self.assertEqual(memory.store.list_long_term_memories(user_id="u1"), [])

            saved = service.save_result_to_personal_knowledge_base(
                user_id="u1", result=response.pipeline_result
            )
            self.assertEqual(saved.kind, MemoryKind.STRUCTURED)
            self.assertTrue(saved.metadata["sql"])
            self.assertEqual(saved.metadata["database"], "erp_db")
            self.assertIn("on_time_rate < 0.9", saved.metadata["filters"]["sql_where"][0])

            follow_up = service.run(
                user_id="u1",
                session_id="s1",
                query="之前查询的供应商是多少",
                enable_long_term=True,
            )
            self.assertEqual(follow_up.memory_context.long_term_hits[0].memory.id, saved.id)
        finally:
            memory.close()


if __name__ == "__main__":
    unittest.main()
