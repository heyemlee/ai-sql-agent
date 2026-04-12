"""测试期强制离线。

自从关键词抽取改成「有凭证走模型」，构造 Text2SQLPipeline 的测试会真的
去调模型 API——本机 .env 里配了 key 时尤其明显：测试变慢、要联网、还花钱，
而且结果不再确定（模型输出会变）。

这里在整个测试会话开始前清空模型凭证，让所有测试走 Mock。
需要真实 HTTP 行为的测试（如 OpenAI 兼容端点测试）会在用例内部自行设置
指向本地 mock 服务的凭证，优先级高于这里。
"""

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def force_offline_models():
    keys = (
        "AI_SQL_OPENAI_API_KEY",
        "OPENAI_API_KEY",
        "AI_SQL_ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY",
    )
    saved = {key: os.environ.get(key) for key in keys}
    for key in keys:
        os.environ[key] = ""

    from ai_sql_core import reload_settings

    reload_settings()
    yield

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reload_settings()
