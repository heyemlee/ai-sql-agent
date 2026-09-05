"""
生产模式启动闸（审查 P1-4 的「发布安全默认值」那一条）。

原来这些都是警告：readiness 明确把「缺模型凭证走 Mock」当成可就绪
（`app.py` 的 readyz），启动只读审计发现违规也只 log 一条 WARNING。
**警告在生产里等于没有**——没人会在凌晨看日志，而这几项里任何一项不合格，
服务都会以一种看起来完全正常的方式给出错误答案。
"""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

from ai_sql_api.app import ProductionReadinessError, check_production_readiness
from ai_sql_core import get_settings, reload_settings


class _FakeApp:
    def __init__(self, readonly_audits=None, records=None):
        store = SimpleNamespace(records=records or {})
        self.state = SimpleNamespace(
            auth=SimpleNamespace(store=store),
            readonly_audits=readonly_audits or {},
        )


class _EnvPatch:
    """临时改环境变量并 reload settings；测试之间不能互相污染。"""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()
        return False


class DevelopmentModeTest(unittest.TestCase):
    def test_problems_are_only_warnings_outside_production(self):
        """本地跑 demo 与测试不该被这些检查挡住。"""
        with _EnvPatch(AI_SQL_APP_ENV="development", AI_SQL_AUTH_MODE="disabled"):
            problems = check_production_readiness(_FakeApp())
        self.assertTrue(problems, "开发模式也应该把问题列出来，只是不阻断")


class ProductionModeTest(unittest.TestCase):
    def _assert_blocks(self, needle: str, **env):
        env.setdefault("AI_SQL_APP_ENV", "production")
        with _EnvPatch(**env):
            with self.assertRaises(ProductionReadinessError) as caught:
                check_production_readiness(_FakeApp())
        self.assertIn(needle, str(caught.exception))

    def test_mock_model_blocks_startup(self):
        self._assert_blocks("Mock", AI_SQL_ANTHROPIC_API_KEY="", OPENAI_API_KEY="",
                            AI_SQL_OPENAI_API_KEY="", ANTHROPIC_API_KEY="")

    def test_auth_disabled_blocks_startup(self):
        self._assert_blocks("AI_SQL_AUTH_MODE", AI_SQL_AUTH_MODE="disabled")

    def test_unknown_role_full_access_blocks_startup(self):
        self._assert_blocks("UNKNOWN_ROLE_POLICY",
                            AI_SQL_UNKNOWN_ROLE_POLICY="full_access")

    def test_demo_semantic_layer_blocks_startup(self):
        self._assert_blocks("AI_SQL_SEMANTIC_DIR", AI_SQL_SEMANTIC_DIR="")

    def test_writable_business_account_blocks_startup(self):
        with _EnvPatch(AI_SQL_APP_ENV="production"):
            app = _FakeApp(readonly_audits={"app_rw": "writable"})
            with self.assertRaises(ProductionReadinessError) as caught:
                check_production_readiness(app)
        self.assertIn("app_rw", str(caught.exception))
        self.assertIn("不是只读", str(caught.exception))

    def test_api_key_without_roles_blocks_startup(self):
        from ai_sql_auth import ApiKeyRecord

        with _EnvPatch(AI_SQL_APP_ENV="production"):
            app = _FakeApp(records={
                "k": ApiKeyRecord(key="k", caller="no-role-caller", roles=())
            })
            with self.assertRaises(ProductionReadinessError) as caught:
                check_production_readiness(app)
        self.assertIn("no-role-caller", str(caught.exception))

    def test_a_fully_configured_production_setup_passes(self):
        """反向断言：一个「永远拒绝」的闸门是没用的闸门。"""
        from ai_sql_auth import ApiKeyRecord

        with _EnvPatch(
            AI_SQL_APP_ENV="production",
            AI_SQL_AUTH_MODE="api_key",
            AI_SQL_UNKNOWN_ROLE_POLICY="deny",
            AI_SQL_SEMANTIC_DIR="sim_erp/semantic",
            AI_SQL_ANTHROPIC_API_KEY="test-key",
            AI_SQL_MODEL_PROVIDER="anthropic",
        ):
            app = _FakeApp(
                readonly_audits={"ai_sql_ro": "readonly"},
                records={"k": ApiKeyRecord(key="k", caller="bi", roles=("analyst",))},
            )
            self.assertEqual(check_production_readiness(app), [])


if __name__ == "__main__":
    unittest.main()
