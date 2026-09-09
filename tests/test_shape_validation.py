"""输出形状检查：静默错误的头号形态。

实测 21 条静默错误里，最大的一类是**形状不符**——问「台面类供应商的采购金额」，
模型答一个 `SUM(po_amt)`。数字本身算得没错，但用户以为看到的是每一单的金额、
拿到的是总额，而且全程没有任何迹象提示这一点。

两条检查都是静态的（正则 + 计划文本），不调模型、不看结果，成本接近零，
所以可以对每条查询都跑——这正是「覆盖不足」的解法：
语义自检准（误报率 1.7%）但贵且抓得少，静态检查便宜到可以全量铺开。
"""

import unittest

from ai_sql_agent.validation import (
    check_shape_against_plan,
    check_unrequested_aggregation,
    wants_aggregation,
)
from ai_sql_agent.validation.objects import AnomalyType


class UnrequestedAggregationTest(unittest.TestCase):
    def test_flags_sum_when_question_asks_for_detail(self):
        finding = check_unrequested_aggregation(
            "查询台面类供应商的采购金额",
            "SELECT SUM(p.po_amt) FROM purchase_order p JOIN supplier s ON ...",
        )
        self.assertIsNotNone(finding)
        self.assertEqual(finding.anomaly, AnomalyType.AGGREGATION_UNREQUESTED)
        self.assertEqual(finding.severity, "warn", "汇总有时正是用户想要的，只提示不阻断")

    def test_silent_when_question_explicitly_asks_to_aggregate(self):
        self.assertIsNone(check_unrequested_aggregation(
            "统计每个供应商的采购单数量",
            "SELECT supplier_id, COUNT(*) FROM purchase_order GROUP BY supplier_id",
        ))

    def test_silent_on_plain_detail_query(self):
        self.assertIsNone(check_unrequested_aggregation(
            "查询准时率低于0.9的供应商名称",
            "SELECT supplier_name FROM supplier WHERE on_time_rate < 0.9",
        ))

    def test_grouped_aggregation_is_not_flagged(self):
        """
        按维度分组时模型多半在按用户提到的实体分组，那通常正是他要的。
        实测 10 条误报里 8 条是分组聚合——报它们只会把误报率顶过护栏线，
        而提示一旦被当噪音忽略，真正的静默错误也跟着漏过去。
        """
        self.assertIsNone(check_unrequested_aggregation(
            "查询板材类供应商的采购金额",
            "SELECT s.supplier_name, SUM(p.po_amt) FROM x GROUP BY s.supplier_name",
        ))

    def test_aggregation_intent_words(self):
        for query in ("统计订单总额", "平均准时率是多少", "各项目的采购额", "每家供应商的单量"):
            self.assertTrue(wants_aggregation(query), query)
        for query in ("查询超预算的项目名称", "台面类供应商的采购金额"):
            self.assertFalse(wants_aggregation(query), query)


class ShapeAgainstPlanTest(unittest.TestCase):
    """
    与基数校验的区别：基数校验比「实际跑出多少行」，会被数据量影响；
    这里比「这条 SQL 结构上最多能出几行」，与数据无关，因此更确定。
    计划说要出数条、SQL 却是无分组聚合，那不是数据少，是形状写错了。
    """

    def test_flags_single_row_sql_when_plan_expects_many(self):
        finding = check_shape_against_plan("数条", "SELECT SUM(x) FROM t WHERE y > 1")
        self.assertIsNotNone(finding)
        self.assertEqual(finding.anomaly, AnomalyType.SHAPE_CONTRADICTS_PLAN)

    def test_silent_when_plan_expects_one_row(self):
        self.assertIsNone(check_shape_against_plan("单条", "SELECT SUM(x) FROM t"))

    def test_silent_when_sql_groups(self):
        self.assertIsNone(
            check_shape_against_plan("数条", "SELECT a, SUM(x) FROM t GROUP BY a")
        )

    def test_silent_without_a_plan_estimate(self):
        self.assertIsNone(check_shape_against_plan("", "SELECT SUM(x) FROM t"))


class IntermediateSelfCheckTest(unittest.TestCase):
    """中间步骤要拿**该步骤的目标**比对，不能拿用户原问题。"""

    class Recorder:
        def __init__(self):
            self.prompts = []

        def generate(self, prompt):
            self.prompts.append(prompt)
            return "复述：筛选了超预算项目" if "复述" in prompt and "结论" not in prompt \
                else "结论：一致\n理由：符合"

    def test_intermediate_step_is_compared_against_its_own_objective(self):
        from ai_sql_agent.validation import ResultValidator, SemanticSelfChecker

        client = self.Recorder()
        ResultValidator(SemanticSelfChecker(client)).validate(
            query="查询超预算项目的采购总金额",
            sql="SELECT project_code FROM project WHERE actual_cost > budget_amt",
            row_count=3,
            is_final_step=False,
            step_objective="筛选超预算项目并输出 project_code（输出：project_code）",
        )
        compare = client.prompts[-1]
        self.assertIn("project_code", compare)
        self.assertNotIn("采购总金额", compare, "中间步骤不该拿整个问题去比")

    def test_intermediate_check_is_skipped_when_objective_missing(self):
        """开关关掉时传空目标，自检应当整个跳过，而不是退回用原问题比。"""
        from ai_sql_agent.validation import ResultValidator, SemanticSelfChecker

        client = self.Recorder()
        ResultValidator(SemanticSelfChecker(client)).validate(
            query="查询超预算项目的采购总金额",
            sql="SELECT project_code FROM project",
            row_count=3,
            is_final_step=False,
            step_objective="",
        )
        self.assertEqual(client.prompts, [])
