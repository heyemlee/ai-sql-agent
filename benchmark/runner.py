from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from collections import Counter
from typing import Any, Dict, List, Optional


@dataclass
class BenchmarkItemResult:
    """单条评测结果。"""

    item_id: str
    category: str
    query: str
    correct: bool
    predicted_sql: str = ""
    error: Optional[str] = None
    repair_used: bool = False
    recall_hit: Optional[bool] = None
    """
    gold 字段是否全部**进入 Prompt**；无 gold_columns 时为 None。

    口径刻意是「进 Prompt」而不是「被检索到」：Schema 图受 Token 预算裁剪，
    检索命中但被裁掉的字段，模型压根看不到，对下游等于不存在。
    """
    retrieval_recall_hit: Optional[bool] = None
    """gold 字段是否全部被**检索**命中（裁剪之前）。只用于定位瓶颈，不进门禁。"""
    retrieved_columns: int = 0
    """实际进入 Prompt 的字段数。"""

    answered: bool = False
    """系统给出了一个看起来正常的答案：最终步骤执行成功、且没有走降级链路。"""

    flagged: bool = False
    """任一步骤带上了风险信号（软异常校验发现 / 修复痕迹以外的显式提示）。"""

    degraded: bool = False
    """模型不可用时的降级答复。用户看得到，属于显性失败。"""

    flag_reasons: List[str] = field(default_factory=list)
    """触发风险提示的软异常类型，用于定位误报是哪一类校验打出来的。"""

    steps: List[Dict[str, Any]] = field(default_factory=list)
    """
    每一步的库、SQL 与行数。

    只存最后一步会让多步查询无法事后归因：跨库失败里最常见的一种是
    「步骤 1 返回空 → 步骤 2 退化成 WHERE 0」，而报告里只看得到步骤 2，
    看上去像是步骤 2 写错了。缺了这一段，每次定位都要重跑一次评测。
    """

    @property
    def silent_error(self) -> bool:
        """
        静默错误：答案是错的，但用户全程看不到任何异常信号。

        这是四项落地指标里唯一「越低越好」的一项，也是最贵的一类错误——
        执行失败、空结果告警、降级答复用户都能看见，会自己去核对；
        而一个跑通了、有结果、没有任何提示的错误数字，会被当成事实带进决策。
        """
        return (not self.correct) and self.answered and not self.flagged

    @property
    def false_alarm(self) -> bool:
        """误报：答案本来是对的，却被打上了风险提示。"""
        return self.correct and self.flagged


@dataclass
class BenchmarkReport:
    """评测报告。"""

    total: int
    correct: int
    category_totals: Dict[str, int] = field(default_factory=dict)
    category_correct: Dict[str, int] = field(default_factory=dict)
    items: List[BenchmarkItemResult] = field(default_factory=list)
    total_columns: int = 0
    """全库字段总数，用于把「平均进图字段数」换算成占比。"""

    @property
    def column_recall(self) -> float:
        """字段召回率：gold 字段全部被检索命中的条目占比。"""
        scored = [i for i in self.items if i.recall_hit is not None]
        if not scored:
            return 0.0
        return sum(1 for i in scored if i.recall_hit) / len(scored)

    @property
    def retrieval_recall(self) -> float:
        """
        检索口径召回率（裁剪之前）。

        与 column_recall 的差值就是「检索到了、但被 Token 预算裁掉」的部分。
        两者拉开差距时，瓶颈不在检索而在预算分配，加大召回只会让情况更糟。
        """
        scored = [i for i in self.items if i.retrieval_recall_hit is not None]
        if not scored:
            return 0.0
        return sum(1 for i in scored if i.retrieval_recall_hit) / len(scored)

    @property
    def recall_gap(self) -> float:
        """
        检索召回率 − 进 Prompt 召回率：**检索捞到了、但模型没看到**的那部分。

        比单看任一个更能定位问题：落差拉开说明瓶颈在 Token 预算分配而不在检索，
        这时继续加大召回只会让情况更糟——多召回的字段一样进不去，
        还会把本来进得去的挤掉。实测这个落差从 0 涨到 6.6pp 时，
        单库准确率掉了 2.4pp、静默错误率涨了 3.3pp，而召回率本身毫无反应。
        """
        return max(0.0, self.retrieval_recall - self.column_recall)

    @property
    def silent_error_rate(self) -> float:
        """
        静默错误率：答错且无任何风险提示的条目 / 全部条目。

        分母刻意用「全部条目」而不是「答错的条目」：用户关心的是
        「我问 100 次会被悄悄骗几次」，不是「错误里有多大比例是隐蔽的」。
        """
        if not self.total:
            return 0.0
        return sum(1 for i in self.items if i.silent_error) / self.total

    @property
    def false_alarm_rate(self) -> float:
        """
        风险提示误报率：正确答案里被打上风险提示的占比。

        必须和静默错误率一起看：把每条结果都标成「可疑」，静默错误率能刷到 0，
        但提示会被用户当噪音忽略，等于没有提示。这一项是它的对偶护栏。
        """
        if not self.correct:
            return 0.0
        return sum(1 for i in self.items if i.false_alarm) / self.correct

    @property
    def avg_retrieved_columns(self) -> float:
        """平均进图字段数。"""
        counts = [i.retrieved_columns for i in self.items if i.retrieved_columns]
        if not counts:
            return 0.0
        return sum(counts) / len(counts)

    @property
    def avg_column_ratio(self) -> float:
        """
        平均进图字段占比。

        字段召回率的对偶护栏：把全库字段一股脑塞进 Schema 图，召回率必然 100%，
        但三级检索就失去意义，Prompt 也被无关字段稀释。两个数必须一起报。
        """
        if not self.total_columns:
            return 0.0
        return self.avg_retrieved_columns / self.total_columns

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    def category_accuracy(self, category: str) -> float:
        total = self.category_totals.get(category, 0)
        if not total:
            return 0.0
        return self.category_correct.get(category, 0) / total

    def grouped_accuracy(self) -> Dict[str, float]:
        """
        汇总口径：cross_db 单独一桶，其余五类合并为 single_db。

        与文档的「单库 / 多库执行准确率」两个指标对齐。
        """
        single_total = sum(
            total
            for category, total in self.category_totals.items()
            if category != "cross_db"
        )
        single_correct = sum(
            correct
            for category, correct in self.category_correct.items()
            if category != "cross_db"
        )

        grouped = {}
        if single_total:
            grouped["single_db"] = single_correct / single_total
        if self.category_totals.get("cross_db"):
            grouped["cross_db"] = self.category_accuracy("cross_db")
        return grouped

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "correct": self.correct,
            "accuracy": round(self.accuracy, 4),
            "column_recall": round(self.column_recall, 4),
            "retrieval_recall": round(self.retrieval_recall, 4),
            "recall_gap": round(self.recall_gap, 4),
            "silent_error_rate": round(self.silent_error_rate, 4),
            "false_alarm_rate": round(self.false_alarm_rate, 4),
            "avg_retrieved_columns": round(self.avg_retrieved_columns, 2),
            "avg_column_ratio": round(self.avg_column_ratio, 4),
            "grouped_accuracy": {
                key: round(value, 4)
                for key, value in self.grouped_accuracy().items()
            },
            "category_accuracy": {
                category: round(self.category_accuracy(category), 4)
                for category in sorted(self.category_totals)
            },
            "items": [
                {
                    "id": item.item_id,
                    "category": item.category,
                    "query": item.query,
                    "correct": item.correct,
                    "predicted_sql": item.predicted_sql,
                    "error": item.error,
                    "repair_used": item.repair_used,
                    "recall_hit": item.recall_hit,
                    "retrieval_recall_hit": item.retrieval_recall_hit,
                    "answered": item.answered,
                    "flagged": item.flagged,
                    "degraded": item.degraded,
                    "silent_error": item.silent_error,
                    "flag_reasons": item.flag_reasons,
                    "steps": item.steps,
                }
                for item in self.items
            ],
        }


class DatasetSchemaMismatch(RuntimeError):
    """评测夹具引用的库在当前语义层里不存在。见 GoldExecutor 的说明。"""


class GoldExecutor:
    """
    Gold SQL 执行器。

    在同一个连接上 ATTACH 四个业务库，
    统一执行 erp.xxx / pm.xxx / mes.xxx / wms.xxx 形式的 gold SQL。

    **夹具与语义层对不上时直接报错，不出分。** 这份数据集是照内置演示元数据
    （erp_db / pm_db / mes_db / wms_db）写的；换成 `tools/sim_erp/semantic`
    （erp_db / prod_db / svc_db / wms_db）之后 `pm` 和 `mes` 两个别名 ATTACH 不上，
    一半的 gold SQL 会直接抛异常——而以前的实现照样把它算成「答错」，
    最后给出一个看起来正常、实际毫无意义的准确率。

    契约 v1.6 已经把 `mes_db` / `pm_db` 标为退役（`mes_db` 是 `prod_db` 的旧名，
    `pm_db` 的职责本就在 `erp_db`）。**不为了让夹具成立去造这两个模拟库**——
    那会造出两个和现有库业务重复的库，语义检索选库时直接打架，而跨库准确率
    本来就只有 16%。夹具要跟着语义层重写，那是一件独立的数据工作；在它做完
    之前，这里的正确行为是**拒绝出分**。
    """

    ALIASES = {"erp_db": "erp", "pm_db": "pm", "mes_db": "mes", "wms_db": "wms"}

    def __init__(self, db_paths: Dict[str, str], required_aliases=None):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.attached = set()
        for database, alias in self.ALIASES.items():
            path = db_paths.get(database)
            if path:
                self.conn.execute(f"ATTACH DATABASE '{path}' AS {alias}")
                self.attached.add(alias)

        missing = sorted(set(required_aliases or ()) - self.attached)
        if missing:
            available = sorted(db_paths)
            raise DatasetSchemaMismatch(
                f"评测夹具引用了这些库前缀：{'、'.join(missing)}，"
                f"但当前语义层只提供 {'、'.join(available) or '（空）'}。\n"
                "这不是配置问题，是夹具与语义层对不上——继续跑只会给出一个"
                "看起来正常、实际毫无意义的准确率。\n"
                "要么把 AI_SQL_SEMANTIC_DIR 切回内置演示元数据（留空），"
                "要么按新语义层重写 benchmark/dataset.json。"
            )

    def execute(self, sql: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(sql).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self.conn.close()


def normalize_rows(rows: List[Dict[str, Any]]) -> List[tuple]:
    """
    结果集归一化：
    - 每行取值集合（忽略列名与列顺序，浮点四舍六入到 6 位）；
    - 整体按多重集比较（行顺序无关）。
    """
    normalized = []
    for row in rows:
        values = []
        for value in row.values():
            if isinstance(value, float):
                value = round(value, 6)
            values.append(str(value))
        normalized.append(tuple(sorted(values)))
    return sorted(normalized)


MAX_EXTRA_COLUMNS = 1
"""允许模型比 gold 多输出的列数上限。"""


def result_matches(gold_rows: List[Dict[str, Any]], predicted_rows: List[Dict[str, Any]]) -> bool:
    """
    判定模型结果是否回答了问题。

    规则（口径 C）：行数相同，且 gold 的每一行都能一一对应到模型的某一行、
    值多重集是其子集；模型的列数最多比 gold 多 `MAX_EXTRA_COLUMNS` 列。

    为什么不要求列集合完全相等：问「采购总金额超过 5 万的工程号」，
    模型答 `J24001 | 636000`——工程号、筛选、分组、行数全对，只是把金额
    一并显示了。严格相等会把它判错，而且因为 SQL 跑通、没有告警，
    这条还会被计进**静默错误率**，也就是把「答对了」算成「悄悄答错」。
    实测 32 条错题里有 6 条属于这一类，21 条静默错误里占 5~6 条。

    为什么仍然卡列数上限：不卡的话 `SELECT *` 能通吃所有题，判定就没有了。
    多一列对应「顺带把聚合值显示出来」这种真实且合理的行为，多得更多就不是了。

    值用多重集而不是集合比对：gold 一行是 (5, 5)、模型是 (5, 9) 时，
    集合口径会误判为命中。
    """
    if len(gold_rows) != len(predicted_rows):
        return False
    if not gold_rows:
        return True

    gold_width = max(len(row) for row in gold_rows)
    predicted_width = max(len(row) for row in predicted_rows)
    if predicted_width > gold_width + MAX_EXTRA_COLUMNS:
        return False

    pool = [Counter(_normalize_values(row)) for row in predicted_rows]
    for row in gold_rows:
        wanted = Counter(_normalize_values(row))
        index = next(
            (i for i, have in enumerate(pool) if not wanted - have),
            None,
        )
        if index is None:
            return False
        pool.pop(index)
    return True


def _normalize_values(row: Dict[str, Any]) -> List[str]:
    """行取值归一化：忽略列名与列顺序，浮点保留 6 位。"""
    return [
        str(round(value, 6) if isinstance(value, float) else value)
        for value in row.values()
    ]


class BenchmarkRunner:
    """
    执行准确率评测器。

    对每条评测数据：
    1. 走完整 Pipeline 得到最终步骤的执行结果；
    2. 在 ATTACH 双库连接上执行 gold SQL；
    3. 按归一化结果集比较判定正确性。
    """

    def __init__(
        self,
        pipeline=None,
        dataset_path: Optional[str | Path] = None,
        enable_repair: Optional[bool] = None,
    ):
        """
        enable_repair 显式传 False 时关闭回溯回路，用于测「回溯增益」：
        同一评测集分别开/关跑一次，两次准确率之差即增益。
        """
        if pipeline is None:
            from ai_sql_agent.pipeline import Text2SQLPipeline
            from ai_sql_agent.pipeline.objects import PipelineConfig

            config = PipelineConfig()
            if enable_repair is not None:
                config.enable_repair = enable_repair
            pipeline = Text2SQLPipeline(config)

        self.pipeline = pipeline

        resolved_path = Path(
            dataset_path or Path(__file__).parent / "dataset.json"
        )
        self.dataset: List[Dict] = json.loads(
            resolved_path.read_text(encoding="utf-8")
        )

        databases = {
            database.name: str(database.db_path)
            for database in self.pipeline.databases
        }
        self.gold_executor = GoldExecutor(
            databases, required_aliases=self._dataset_aliases()
        )

    def _dataset_aliases(self) -> set:
        """gold SQL 里真正用到的库前缀（`erp.` / `pm.` / …）。"""
        used = set()
        for item in self.dataset:
            used.update(
                re.findall(r"\b([a-z][a-z0-9_]*)\s*\.", item.get("gold_sql", ""))
            )
        return used & set(GoldExecutor.ALIASES.values())

    def run(
        self,
        categories: Optional[List[str]] = None,
        ids: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> BenchmarkReport:
        items = self.dataset

        if categories:
            items = [item for item in items if item["category"] in categories]
        if ids:
            wanted = set(ids)
            items = [item for item in items if item["id"] in wanted]
        if limit:
            items = items[:limit]

        report = BenchmarkReport(
            total=len(items), correct=0, total_columns=self._total_columns()
        )

        for item in items:
            result = self._run_item(item)
            report.items.append(result)
            report.category_totals[result.category] = (
                report.category_totals.get(result.category, 0) + 1
            )
            if result.correct:
                report.correct += 1
                report.category_correct[result.category] = (
                    report.category_correct.get(result.category, 0) + 1
                )

        return report

    def _total_columns(self) -> int:
        """全库字段总数，用于把平均进图字段数换算成占比。"""
        total = 0
        for database in self.pipeline.databases:
            meta = getattr(self.pipeline, "business_metas", {}).get(database.name, {})
            for table_meta in meta.values():
                total += len(table_meta.get("columns", {}))
        return total

    @staticmethod
    def _step_trace(pipeline_result) -> List[Dict[str, Any]]:
        """每一步的库 / SQL / 行数，供事后归因用。"""
        return [
            {
                "database": log.database,
                "sql": log.final_sql or log.sql,
                "success": bool(log.execution_result.get("success")),
                "row_count": int(log.execution_result.get("row_count") or 0),
            }
            for log in (getattr(pipeline_result, "step_logs", None) or [])
        ]

    @staticmethod
    def _risk_signals(pipeline_result) -> tuple:
        """
        用户这一侧看得到的风险信号，用于判定「静默错误」。

        answered：最终步骤执行成功、且不是降级答复——界面上呈现为一个正常答案。
        flagged：任一步骤的软异常校验有 finding（空结果 / 基数异常 / 语义不一致）。
                 只要有一条，用户就被提醒了，错误不算「静默」。
        reasons：具体是哪几类校验打出来的，用于定位误报来源。
        """
        degraded = bool(getattr(pipeline_result, "degraded", False))
        step_logs = getattr(pipeline_result, "step_logs", None) or []
        answered = bool(step_logs) and bool(
            step_logs[-1].execution_result.get("success")
        ) and not degraded
        reasons = [
            finding.get("anomaly", "")
            for log in step_logs
            for finding in (log.validation or {}).get("findings") or []
        ]
        return answered, bool(reasons), degraded, reasons

    @staticmethod
    def _collect_retrieved_columns(pipeline_result) -> set:
        """检索命中的 {db.table.column} 集合，用于算字段召回率。"""
        return set(getattr(pipeline_result, "retrieved_columns", None) or [])

    @staticmethod
    def _score_recall(item: Dict, pipeline_result) -> tuple:
        """返回 (进 Prompt 是否全中, 检索是否全中, 进 Prompt 字段数)。"""
        gold_columns = item.get("gold_columns") or []
        if not gold_columns:
            return None, None, 0
        retrieved = BenchmarkRunner._collect_retrieved_columns(pipeline_result)
        in_prompt = set(getattr(pipeline_result, "prompt_columns", None) or [])
        if not retrieved:
            return None, None, 0
        if not in_prompt:
            in_prompt = retrieved
        return (
            all(c in in_prompt for c in gold_columns),
            all(c in retrieved for c in gold_columns),
            len(in_prompt),
        )

    def _run_item(self, item: Dict) -> BenchmarkItemResult:
        gold_rows = self.gold_executor.execute(item["gold_sql"])

        try:
            pipeline_result = self.pipeline.run(item["query"])
        except Exception as exc:
            return BenchmarkItemResult(
                item_id=item["id"],
                category=item["category"],
                query=item["query"],
                correct=False,
                error=f"pipeline异常：{exc}",
            )

        if not pipeline_result.step_logs:
            return BenchmarkItemResult(
                item_id=item["id"],
                category=item["category"],
                query=item["query"],
                correct=False,
                error="未生成执行步骤",
            )

        final_log = pipeline_result.step_logs[-1]
        execution = final_log.execution_result

        if not execution.get("success"):
            recall_hit, retrieval_hit, retrieved_count = self._score_recall(
                item, pipeline_result
            )
            return BenchmarkItemResult(
                item_id=item["id"],
                category=item["category"],
                query=item["query"],
                correct=False,
                predicted_sql=final_log.final_sql or final_log.sql,
                error=execution.get("error"),
                repair_used=bool(final_log.repair_attempts),
                recall_hit=recall_hit,
                retrieval_recall_hit=retrieval_hit,
                retrieved_columns=retrieved_count,
            )

        predicted_rows = list(execution.get("rows") or [])
        correct = result_matches(gold_rows, predicted_rows)
        recall_hit, retrieval_hit, retrieved_count = self._score_recall(
            item, pipeline_result
        )
        answered, flagged, degraded, flag_reasons = self._risk_signals(pipeline_result)

        return BenchmarkItemResult(
            item_id=item["id"],
            category=item["category"],
            query=item["query"],
            correct=correct,
            predicted_sql=final_log.final_sql or final_log.sql,
            repair_used=bool(final_log.repair_attempts),
            recall_hit=recall_hit,
            retrieval_recall_hit=retrieval_hit,
            retrieved_columns=retrieved_count,
            answered=answered,
            flagged=flagged,
            degraded=degraded,
            flag_reasons=flag_reasons,
            steps=self._step_trace(pipeline_result),
        )


def run_holdout_recall(pipeline=None, holdout_path: Optional[str | Path] = None) -> float:
    """
    留出集字段召回率。

    与评测集分开跑：评测集的召回率被短语表、别名、枚举词表反复调过，
    留出集用完全不同的口语问法且不参与调参，才是「上线后遇到真实用户」的估计值。
    发布门禁卡的是这一项，评测集那一项只作为不许退化的下限。
    """
    if pipeline is None:
        from ai_sql_agent.pipeline import Text2SQLPipeline

        pipeline = Text2SQLPipeline()

    path = Path(holdout_path or Path(__file__).parent / "holdout.json")
    items = json.loads(path.read_text(encoding="utf-8"))
    if not items:
        return 0.0

    hit = 0
    for item in items:
        result = pipeline.run(item["query"])
        visible = set(result.prompt_columns or result.retrieved_columns)
        if all(column in visible for column in item["gold_columns"]):
            hit += 1
    return hit / len(items)
