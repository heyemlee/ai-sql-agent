"""测试期与本机配置隔离。

两件事会让测试读到开发者本机的配置，两件都得挡：

1. 项目根目录的 `.env`——pydantic-settings 会自动读它。里面钉死的
   provider / base_url / 模型名会盖过用例自己设的环境变量（`AI_SQL_`
   前缀的优先级高于不带前缀的别名），断言就变成了「你的 .env 配了什么」。
2. shell 里导出的模型凭证——自从关键词抽取改成「有凭证走模型」，构造
   Text2SQLPipeline 的测试会真的去调模型 API：变慢、要联网、还花钱，
   而且模型输出会变，结果不再确定。

需要真实 HTTP 行为的测试（如 OpenAI 兼容端点测试）会在用例内部自行设置
指向本地 mock 服务的凭证，优先级高于这里。
"""

import os

import pytest

_CREDENTIAL_VARS = (
    "AI_SQL_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "AI_SQL_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture(scope="session", autouse=True)
def isolate_from_local_config():
    from ai_sql_agent.core import reload_settings
    from ai_sql_agent.core.settings import SqlAgentSettings

    saved_env_file = SqlAgentSettings.model_config.get("env_file")
    SqlAgentSettings.model_config["env_file"] = None

    saved = {key: os.environ.get(key) for key in _CREDENTIAL_VARS}
    for key in _CREDENTIAL_VARS:
        os.environ[key] = ""

    reload_settings()
    yield

    SqlAgentSettings.model_config["env_file"] = saved_env_file
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reload_settings()
