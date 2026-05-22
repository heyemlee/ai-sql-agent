from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ai_sql_agent.core.model_client import FollowsModelSettings


@dataclass
class CoderModelConfig:
    """
    Coder 模型配置。

    协议由 provider 决定：openai（OpenAI 兼容）或 anthropic（Messages）。
    留空的字段在构造 Client 时从全局 Settings 补齐（见 ai_sql_agent.core.settings），
    之后跟着页面保存的模型连接走。
    没有 API Key 时默认走 Mock，方便先跑通流程。
    """

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    provider: str = ""
    temperature: float = 0.0
    timeout: Optional[float] = None
    use_mock_when_no_api_key: bool = True


class CoderModelClient(FollowsModelSettings):
    """
    Coder 模型 Client。

    HTTP 调用统一走 ai_sql_agent.core.ModelTransport：超时、限流退避重试、
    错误分类与 Token 用量统计都在那里处理。
    """

    COMPONENT = "coder"
    MODEL_SETTING = "coder_model"

    def __init__(self, config: CoderModelConfig | None = None):
        self._bind_connection(config or CoderModelConfig())

    def generate_sql(self, prompt: str) -> str:
        """
        生成 SQL。
        """
        connection = self._connection()
        if connection.has_key:
            return connection.transport.chat(
                connection.url,
                [{"role": "user", "content": prompt}],
                model=connection.model,
                temperature=self.config.temperature,
            )

        if self.config.use_mock_when_no_api_key:
            return self._mock_generate(prompt)

        raise ValueError(
            "缺少模型 API Key（OPENAI_API_KEY / ANTHROPIC_API_KEY），无法调用 Coder 模型。"
        )

    def _mock_generate(self, prompt: str) -> str:
        """Mock SQL 生成，仅用于离线跑通流程。"""
        # 跨库第二步：把上一步的 project_code 回填成 ERP 的 job_no
        if "purchase_order" in prompt and "# 上一步结果" in prompt:
            codes = re.findall(r'"project_code":\s*"([^"]+)"', prompt)
            if codes:
                in_list = ", ".join(f"'{c}'" for c in dict.fromkeys(codes))
                return (
                    "SELECT job_no, SUM(po_amt) AS total_po_amt\n"
                    "FROM purchase_order\n"
                    f"WHERE job_no IN ({in_list})\n"
                    "GROUP BY job_no;"
                )

        # 跨库第一步 / 单库：超预算项目
        if "actual_cost" in prompt and "budget_amt" in prompt:
            target = prompt.split("输出目标：")[-1].split("\n")[0] if "输出目标：" in prompt else ""
            field = "project_code" if "project_code" in target else "project_name"
            return f"SELECT {field}\nFROM project\nWHERE actual_cost > budget_amt;"

        # 库存低于安全库存
        if "on_hand_qty" in prompt and "safety_stock" in prompt:
            return """SELECT material.material_name
FROM material
JOIN stock_snapshot
  ON material.material_code = stock_snapshot.material_code
WHERE stock_snapshot.on_hand_qty < material.safety_stock;"""

        # 供应商准时率
        if "on_time_rate" in prompt:
            m = re.search(r"(0\.\d+)", prompt)
            threshold = m.group(1) if m else "0.9"
            return f"SELECT supplier_name\nFROM supplier\nWHERE on_time_rate < {threshold};"

        return "SELECT 1;"

    def clean_sql(self, text: str) -> str:
        """
        清理模型输出，只保留 SQL。

        处理：
        - 去掉 Markdown 代码块
        - 去掉“输出SQL语句：”之类的前缀
        - 提取 SELECT/WITH 开头的 SQL
        """
        text = text.strip()

        text = re.sub(
            r"^```(?:sql)?\s*",
            "",
            text,
            flags=re.I,
        )
        text = re.sub(
            r"\s*```$",
            "",
            text,
            flags=re.I,
        )

        text = re.sub(
            r"^(输出SQL语句|SQL语句|SQL)\s*[:：]\s*",
            "",
            text,
            flags=re.I,
        ).strip()

        match = re.search(
            r"((?:SELECT|WITH)\b[\s\S]*?;?)\s*$",
            text,
            flags=re.I,
        )

        if match:
            sql = match.group(1).strip()
        else:
            sql = text

        if sql and not sql.endswith(";"):
            sql += ";"

        return sql
