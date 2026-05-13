"""
查询界面（`api/static`）的静态检查。

页面没有构建步骤，也就没有编译器替我们发现「引用了一个不存在的模块」
「英文加了一句文案、中文忘了加」这类错误——它们只会在某个人点到那一页时才冒出来。
这里用读文件的方式把这几类错误提前到测试里。检查的是不变量，不是具体长相：

- 页面与它引用的每个模块、每条文案都真实存在；
- 来自数据库与模型的文本进不了 innerHTML（唯一的出口在 dom.js，默认转义）；
- 页面里没有写死的业务数据：表名、字段名、练习库的答案都只能来自服务端；
- 学习模式里引用的源码路径没有过期；
- 镜像里装得全（package-data 覆盖每一个静态文件）。
"""
import fnmatch
import re
import tomllib
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ai_sql_agent.api.app import STATIC_DIR, app

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "src" / "ai_sql_agent"
JS_DIR = STATIC_DIR / "js"


def js_modules():
    return sorted(JS_DIR.rglob("*.js"))


def app_modules():
    """除文案字典之外的脚本。"""
    return [path for path in js_modules() if path.parent.name != "i18n"]


def bundle_text() -> str:
    files = [STATIC_DIR / "index.html", STATIC_DIR / "app.css", *js_modules()]
    return "\n".join(path.read_text(encoding="utf-8") for path in files)


def dictionary(language: str) -> list:
    text = (JS_DIR / "i18n" / f"{language}.js").read_text(encoding="utf-8")
    return re.findall(r"^\s*'([^']+)':\s", text, flags=re.M)


class ConsoleIsServedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_ctx = TestClient(app)
        cls.client = cls.client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_ctx.__exit__(None, None, None)

    def test_shell_and_every_asset_are_served(self):
        shell = self.client.get("/")
        self.assertEqual(shell.status_code, 200)
        self.assertIn("text/html", shell.headers["content-type"])

        for path in [STATIC_DIR / "app.css", *js_modules()]:
            url = "/static/" + path.relative_to(STATIC_DIR).as_posix()
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)

    def test_assets_are_revalidated_not_cached_blindly(self):
        # 没有带哈希的文件名：不让浏览器回来确认的话，升级后会出现「新页面配旧脚本」
        for url in ("/", "/static/app.css", "/static/js/main.js"):
            self.assertEqual(self.client.get(url).headers.get("cache-control"), "no-cache", url)

    def test_unknown_asset_is_404(self):
        self.assertEqual(self.client.get("/static/js/nope.js").status_code, 404)
        self.assertEqual(self.client.get("/static/../app.py").status_code, 404)

    def test_shell_loads_the_module_entry_and_explains_a_failed_boot(self):
        shell = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('type="module" src="static/js/main.js"', shell)
        self.assertIn('href="static/app.css"', shell)
        # 页面文件更新了、服务进程还是旧的时候，脚本取不到：要有一段不依赖脚本和样式的说明
        self.assertIn('id="bootFallback"', shell)
        self.assertIn("Restart the service", shell)


class ModuleGraphTest(unittest.TestCase):
    def test_every_import_resolves_to_a_file(self):
        for module in js_modules():
            source = module.read_text(encoding="utf-8")
            for target in re.findall(r"""from\s+'(\.{1,2}/[^']+)'""", source):
                resolved = (module.parent / target).resolve()
                self.assertTrue(resolved.is_file(), f"{module.name} imports missing {target}")

    def test_named_imports_are_exported_by_their_module(self):
        for module in js_modules():
            source = module.read_text(encoding="utf-8")
            for names, target in re.findall(r"import\s*\{([^}]*)\}\s*from\s*'(\.{1,2}/[^']+)'", source):
                exported = (module.parent / target).resolve().read_text(encoding="utf-8")
                for name in (item.strip().split(" as ")[0] for item in names.split(",")):
                    if not name:
                        continue
                    pattern = (
                        rf"export\s+(?:async\s+)?(?:function|const|let|class)\s+{re.escape(name)}(?![\w$])"
                    )
                    self.assertRegex(exported, pattern, f"{module.name}: {name} from {target}")


class TranslationTest(unittest.TestCase):
    def test_both_languages_define_the_same_keys(self):
        english, chinese = dictionary("en"), dictionary("zh")
        self.assertEqual(len(english), len(set(english)), "en.js 有重复的键")
        self.assertEqual(len(chinese), len(set(chinese)), "zh.js 有重复的键")
        self.assertEqual(sorted(set(english) ^ set(chinese)), [])

    def test_every_key_used_in_code_exists(self):
        defined = set(dictionary("en"))
        used = set()
        sources = [path.read_text(encoding="utf-8") for path in app_modules()]
        sources.append((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
        for source in sources:
            used.update(re.findall(r"""\btn?\(\s*'([\w.]+)'""", source))
            used.update(re.findall(r'data-i18n(?:-label|-placeholder)?="([^"]+)"', source))
        # 以点结尾的是拼接前缀（t('stage.' + name)），不是完整的键
        missing = sorted(key for key in used if not key.endswith(".") and key not in defined)
        self.assertEqual(missing, [])

    def test_dynamic_key_families_cover_the_server_vocabulary(self):
        """
        `t('stage.' + name)` 这种拼接的键，静态检查看不到。这里把服务端的闭集搬过来对一遍：
        服务端加了一个取值而字典没跟上，页面上显示的就是一个光秃秃的英文标识符。
        """
        from ai_sql_agent.a2a.server import _STAGE_MAP
        from ai_sql_agent.catalog.preview import OPERATORS
        from ai_sql_agent.semantic.validator import SEMANTIC_ROLES
        from ai_sql_agent.validation.objects import AnomalyType

        defined = set(dictionary("en"))
        expected = {f"stage.{stage}" for stage in _STAGE_MAP.values()}
        expected |= {f"filter.op.{operator}" for operator in OPERATORS}
        expected |= {f"anomaly.{anomaly.value}" for anomaly in AnomalyType}
        expected |= {f"role.{role}" for role in SEMANTIC_ROLES if role}
        expected |= {f"route.{route}" for route in ("database_query", "data_qa")}
        expected |= {
            f"data.reason.{reason}"
            for reason in ("not_configured", "file_not_found", "driver_missing", "connection_failed")
        }
        self.assertEqual(sorted(expected - defined), [])


class UntrustedTextTest(unittest.TestCase):
    """结果单元格、SQL、报错、问题原文都是不可信文本。"""

    def test_innerhtml_is_written_in_one_place_only(self):
        for module in app_modules():
            source = module.read_text(encoding="utf-8")
            if module.name == "dom.js":
                continue
            for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
                self.assertNotIn(sink, source, f"{module.name} 直接写了 {sink}")

    def test_the_template_escapes_by_default(self):
        dom = (JS_DIR / "dom.js").read_text(encoding="utf-8")
        for entity in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
            self.assertIn(entity, dom, entity)
        # 「原样输出」的口子不导出：别的模块拿不到它，也就没法绕过转义
        self.assertNotRegex(dom, r"export\s+(?:const|function)\s+raw\b")
        self.assertRegex(dom, r"return esc\(value\);")

    def test_no_inline_event_handlers(self):
        text = bundle_text()
        self.assertNotRegex(text, r"""\son(?:click|error|load|input|change|submit)\s*=\s*["']""")


class NoHardcodedBusinessDataTest(unittest.TestCase):
    """
    目录来自 /catalog，结果来自 /query。页面里不该出现任何一张表的名字、
    一个字段的名字，或者练习库里的答案——出现了，就说明有东西是写死的。
    """

    def test_page_names_no_database_table_or_column(self):
        from ai_sql_agent.semantic import demo_semantic_layer

        text = bundle_text()
        layer = demo_semantic_layer()
        names = set(layer.databases)
        for database in layer.databases.values():
            for table, meta in database.tables.items():
                names.add(table)
                names.update(meta.get("columns", {}))
        # 太短或本身就是常用英文词的名字（unit、category、project…）没法靠子串判断，跳过
        distinctive = sorted(name for name in names if "_" in name)
        self.assertGreater(len(distinctive), 40)
        leaked = [name for name in distinctive if re.search(rf"\b{re.escape(name)}\b", text)]
        self.assertEqual(leaked, [])

    def test_page_contains_no_practice_answers(self):
        text = bundle_text()
        for answer in (
            "Harborline", "Canyon Crest", "Clearbrook", "PO-24-0103", "SO-2024-001",
            "31600", "19800", "11800", "J24001", "MAT-00", "SQ_FT", "LIN_FT",
        ):
            self.assertNotIn(answer, text, answer)


class ContractWiringTest(unittest.TestCase):
    def test_page_talks_to_the_documented_endpoints(self):
        api = (JS_DIR / "api.js").read_text(encoding="utf-8")
        for marker in ("/query", "/history", "/feedback", "/catalog", "/a2a", "/health", "X-API-Key", "message/stream"):
            self.assertIn(marker, api, marker)

    def test_result_fields_are_read_by_their_contract_names(self):
        model = (JS_DIR / "model.js").read_text(encoding="utf-8")
        for field in (
            "trace_id", "execution_status", "has_risk", "degraded", "result_truncated",
            "risk_findings", "column_units", "as_of", "as_of_source", "currency",
            "repair_attempts", "final_sql", "row_limit",
        ):
            self.assertIn(field, model, field)
        # 契约 §4.4.1：steps 是契约面的名字，step_logs 是过渡期的旧名字，两个都要认
        self.assertRegex(model, r"payload\.steps")
        self.assertRegex(model, r"payload\.step_logs")

    def test_missing_trust_signals_lower_trust(self):
        # 契约 §4.6.7：has_risk / degraded 缺失按 true，queried_database 缺失按 false
        model = (JS_DIR / "model.js").read_text(encoding="utf-8")
        self.assertRegex(model, r"hasRisk:\s*strictBool\(payload\.has_risk,\s*true\)")
        self.assertRegex(model, r"degraded:\s*strictBool\(payload\.degraded,\s*true\)")
        self.assertRegex(model, r"queriedDatabase:\s*strictBool\(payload\.queried_database,\s*false\)")

    def test_feedback_sends_only_the_trace_and_verdict(self):
        # 问题原文与 SQL 由服务端按审计记录回填；页面把它们一起发上去就等于「客户端说了算」
        api = (JS_DIR / "api.js").read_text(encoding="utf-8")
        match = re.search(
            r"feedback:\s*\(traceId, verdict, comment\)\s*=>\s*"
            r"request\('POST', '/feedback', \{ body: \{([^}]*)\} \}\)",
            api,
        )
        self.assertIsNotNone(match, "找不到 /feedback 的请求体")
        fields = {item.split(":")[0].strip() for item in match.group(1).split(",") if item.strip()}
        self.assertEqual(fields, {"trace_id", "verdict", "comment"})


class LearningNotesTest(unittest.TestCase):
    def test_cited_source_paths_exist(self):
        cited = []
        for module in app_modules():
            source = module.read_text(encoding="utf-8")
            for chunk in re.findall(r"'(src/ai_sql_agent/[^']+)'", source):
                cited.append(chunk)
            for chunk in re.findall(r'class="learn-src">(src/ai_sql_agent/[^<]+)<', source):
                cited.append(chunk)
        self.assertGreater(len(cited), 6)

        for chunk in cited:
            for index, item in enumerate(chunk.split(" · ")):
                path = re.sub(r"\s*\(.*\)$", "", item.strip())      # 去掉「(符号名)」
                full = REPO / path if index == 0 else PACKAGE / path
                self.assertTrue(full.is_file(), f"学习讲解引用了不存在的文件：{item}")


class PackagingTest(unittest.TestCase):
    def test_package_data_covers_every_static_file(self):
        config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        patterns = config["tool"]["setuptools"]["package-data"]["ai_sql_agent"]
        for path in STATIC_DIR.rglob("*"):
            if not path.is_file() or path.name.startswith("."):
                continue
            relative = path.relative_to(PACKAGE).as_posix()
            # fnmatch 的 * 会跨目录，setuptools 的不会：按「同一层目录」比
            matched = any(
                Path(pattern).parent == Path(relative).parent
                and fnmatch.fnmatch(Path(relative).name, Path(pattern).name)
                for pattern in patterns
            )
            self.assertTrue(matched, f"{relative} 不在 package-data 里，镜像里会缺这个文件")


if __name__ == "__main__":
    unittest.main()
