from __future__ import annotations

from typing import List

from .objects import CotPlanningRequest, FewShotExample


DEFAULT_OUTPUT_FORMAT_SPEC = """输出格式要求：

1. 仅输出步骤和五元组。
2. 每个步骤必须使用“步骤N：”作为标题。
3. 每个步骤下只输出一个五元组。
4. 五元组必须严格使用以下格式：

步骤N：
(
  数据库: xxx,
  处理对象: xxx,
  操作指令: 先xxx；再xxx；然后xxx；最后xxx,
  输出目标: xxx,
  预期输出量级: xxx
)

5. 预期输出量级从「单条 / 数条 / 数十条 / 数百条 / 数千条」中选一个，
   或直接写「约N条」。这是给执行后的基数校验用的：实际行数远超预期
   通常意味着关联扇出导致重复计数，SQL 能跑通但结果是错的。
"""


class CotPromptBuilder:
    """
    CoT 规划 Prompt 构建器。

    输入：
    - 用户 Query
    - Schema 图上下文
    - 输出格式规范
    - Few-shot 示例，可选
    """

    def build(self, request: CotPlanningRequest) -> str:
        """构建完整 CoT 规划 Prompt。"""
        sections: List[str] = [
            self._build_task_instruction(),
        ]

        if request.few_shots:
            sections.append(
                self._build_few_shot_section(request.few_shots)
            )

        output_format_spec = (
            request.output_format_spec.strip()
            if request.output_format_spec.strip()
            else DEFAULT_OUTPUT_FORMAT_SPEC
        )

        sections.extend(
            [
                output_format_spec,
                self._build_input_section(
                    user_query=request.user_query,
                    schema_context=request.schema_context,
                ),
            ]
        )

        if request.extra_instructions.strip():
            sections.append(
                "# 上一版规划的问题（必须修正）\n"
                + request.extra_instructions.strip()
            )

        return "\n\n".join(section.strip() for section in sections if section.strip())

    def _build_task_instruction(self) -> str:
        """构建任务说明。"""
        return """你是一个数据分析规划助手。

任务：
根据用户Query与提供的Schema信息，生成结构化CoT五元组，用于指导后续Coder模型生成SQL。

五元组格式：
（数据库，处理对象，操作指令，输出目标，预期输出量级）

字段说明：

1. 数据库
填写当前步骤需要访问的数据库名称。一个步骤只能对应一个数据库。

2. 处理对象
填写当前步骤涉及的数据表和字段。若一个数据库内涉及多张表，需要列出所有相关表、字段以及Schema中提供的表关联关系。

3. 操作指令
填写当前步骤的链式处理过程。操作指令不是简单列出筛选条件，而是按照数据处理顺序描述完整执行逻辑。

操作指令需要体现以下逻辑：
- 先确定用户Query中的筛选字段、输出字段以及两者所属的数据表。
- 若筛选字段和输出字段位于不同表中，需要根据Schema中的表关联关系确定关联键。
- 先从包含筛选字段的表中筛选满足条件的记录，并得到用于关联的键字段。
- 再基于关联键到包含输出字段的表中获取目标结果。
- 若涉及聚合、分组、排序或计算，需要按照执行顺序写入链式操作指令。
- 操作指令必须严格基于Schema中存在的表、字段和关联关系生成，不得编造不存在的信息。

4. 输出目标
填写当前步骤最终需要返回的字段或计算结果，即SQL中SELECT部分对应的内容。

5. 预期输出量级
估计当前步骤大约会返回多少行，从「单条 / 数条 / 数十条 / 数百条 / 数千条」中选一个。
聚合成一个数的步骤是「单条」，按条件筛选明细通常是「数条」到「数十条」。
这个估计只用于执行后的异常检测，不影响SQL本身，给个量级即可，不必精确。

规划要求：

1. 根据用户Query判断是否需要拆分多个执行步骤。
2. 若Query仅涉及一个数据库，可生成一个步骤。
3. 若Query涉及多个数据库，需要按数据库或执行顺序拆分为多个步骤。
4. 每个步骤只处理一个数据库中的查询任务，但一个数据库步骤中可以处理多张表。
   跨库查询必须拆步：不同数据库之间没有外键、也不允许 JOIN，
   前一步要把关联键（如项目编号）作为「输出目标」显式输出，
   后一步再拿这些取值当字面量条件用。
5. 操作指令必须以链式步骤形式描述，不得只输出条件列表。
6. 所有表、字段、关联关系和输出目标必须来自Schema。
7. 若Schema无法支撑用户Query，需要在对应步骤中说明缺失的信息。
8. 不生成SQL，不输出额外解释，仅输出结构化规划结果。"""

    def _build_few_shot_section(
        self,
        few_shots: List[FewShotExample],
    ) -> str:
        """构建 Few-shot 示例部分。"""
        lines: List[str] = ["# Few-shot示例"]

        for index, example in enumerate(few_shots, start=1):
            lines.append(f"\n## 示例{index}")
            lines.append("输入：")
            lines.append("用户Query：")
            lines.append(example.user_query)
            lines.append("")
            lines.append("Schema图：")
            lines.append(example.schema_context)
            lines.append("")
            lines.append("输出：")
            lines.append(example.output)

        return "\n".join(lines)

    def _build_input_section(
        self,
        user_query: str,
        schema_context: str,
    ) -> str:
        """构建当前用户输入部分。"""
        return f"""# 用户Query
{user_query}

# Schema
{schema_context}"""
