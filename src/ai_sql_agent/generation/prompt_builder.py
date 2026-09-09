from __future__ import annotations

from ai_sql_agent.execution.dialects import get_dialect

from .objects import SqlGenerationRequest


class SqlPromptBuilder:
    """
    SQL 生成 Prompt 构建器。

    注意：
    - Prompt 中不放数据库名称，数据库名称只用于后续执行路由。
    """

    def build(self, request: SqlGenerationRequest) -> str:
        """
        构建 Coder 模型 Prompt。
        """
        cot_step = request.cot_step
        schema_context = request.local_schema.to_prompt_context()
        dialect_rules = get_dialect(request.sql_dialect).to_prompt_rules()

        certified_section = ""
        if request.certified_examples.strip():
            certified_section = f"\n{request.certified_examples.strip()}\n"

        previous_results_section = ""
        if request.previous_results.strip():
            previous_results_section = f"""
# 上一步结果（已在另一个数据库执行完毕）
下面是上一步的结果。**上一步的筛选条件已经生效，本步骤不要重复实现它**，
只需要把结果里的取值当成已知常量用掉。

用法：直接抄下面「可直接用作本步骤的字面量筛选条件」那一行放进 WHERE。
禁止：跨库 JOIN、跨库子查询、在本步骤重新推导上一步的筛选逻辑——
另一个库的表在这里不存在，写了必定报 no such table。

{request.previous_results}
"""

        return f"""你是一个SQL生成助手。

请根据当前步骤的CoT规划和相关Schema信息生成SQL语句。

要求：
1. 只能使用Schema中提供的表、字段和表关联关系。
2. 严格按照CoT中的操作指令生成SQL。
3. 不得编造不存在的表、字段或关联关系。
4. 只输出SQL语句，不输出解释性内容。
5. SQL需要符合下方「方言约束」中指定的语法。
6. 不要输出Markdown代码块，不要输出```sql。
7. **表名前不要加数据库名前缀**。执行时已经连到目标库，写 `pm_db.project`
   会报「no such table」，直接写 `project`。
8. 只输出一条SQL语句，不要输出多条或附加说明。

# 方言约束
{dialect_rules}
{certified_section}
# 当前步骤CoT
处理对象：{cot_step.processing_objects}
操作指令：{cot_step.operation_instruction}
输出目标：{cot_step.output_target}
{previous_results_section}
# Schema信息
{schema_context}
"""
