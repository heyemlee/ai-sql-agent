"""
查询界面里几段纯逻辑的行为测试：用 Node 直接加载 `api/static/js` 下的真实模块来跑。

`test_console_frontend.py` 读文件查不变量（模块在不在、文案齐不齐），查不了「算得对不对」。
这里补的是后者，都是评审里实际跑出过错的地方：

- 跨库查询里哪一步算中间步骤（判错了，答案里会少一张表，或者多出一张不相干的）；
- 表格排序（数字以字符串到达页面时不能按字符比）；
- 数字显示（一个不是零的数不能显示成 0）；
- 路由（不认识的 hash 不该把人带去别的页面）；
- 请求层（响应体读不出来不是「成功」）与目录加载的先后次序、身份的备用来源。

没有装 Node 的环境整组跳过：页面本身不依赖 Node，这只是测试手段。
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_sql_agent.api.app import STATIC_DIR

JS_DIR = STATIC_DIR / "js"
NODE = shutil.which("node")

HARNESS = r"""
import { readFileSync } from 'node:fs';
const JS = process.argv[2];
const load = (name) => import(`${JS}/${name}`);
const out = {};

// ---- model.js：哪一步是中间步骤 ------------------------------------------------
{
  const { normalizeResult, passedOn, splitSteps } = await load('model.js');
  const make = (steps) => normalizeResult({
    route: 'database_query', queried_database: true, has_risk: false, degraded: false,
    execution_status: 'success', steps,
  }, { transport: 'query' });
  const step = (database, sql, rows) => ({ database, sql, final_sql: sql, success: true, row_count: rows.length, rows });
  const verdict = (result, question = '') => {
    const split = splitSteps(result, question);
    return {
      answers: split.answers.map((item) => item.index + 1),
      intermediate: split.intermediate.map((item) => item.index + 1),
      carried: passedOn(result, result.steps[0], question).map((item) => [item.column, item.values.length, item.intoStep + 1]),
    };
  };
  out.steps = {
    handoff: verdict(make([
      step('pm_db', 'SELECT "project_code" FROM "project" WHERE "actual_cost" > "budget_amt";',
        [{ project_code: 'J24001' }, { project_code: 'J24003' }, { project_code: 'J24006' }]),
      step('erp_db', "SELECT job_no, SUM(po_amt) AS total FROM purchase_order WHERE job_no IN ('J24001', 'J24003', 'J24006') GROUP BY job_no;",
        [{ job_no: 'J24001', total: 127200 }]),
    ])),
    handoffWithExtras: verdict(make([
      step('pm_db', 'SELECT project_code, project_name, actual_cost FROM project WHERE actual_cost > budget_amt',
        [{ project_code: 'J24001', project_name: 'Maple Kitchen', actual_cost: 91000 },
         { project_code: 'J24003', project_name: 'Quartz Bath', actual_cost: 45000 }]),
      step('erp_db', "SELECT job_no, SUM(po_amt) t FROM purchase_order WHERE job_no IN ('J24001', 'J24003') GROUP BY job_no",
        [{ job_no: 'J24001', t: 1 }]),
    ])),
    sharedFilter: verdict(make([
      step('erp_db', "SELECT order_no, status FROM sales_order WHERE status = 'open'",
        [{ order_no: 'SO-1', status: 'open' }, { order_no: 'SO-2', status: 'open' }]),
      step('mes_db', "SELECT wo_no, status FROM work_order WHERE status = 'open'", [{ wo_no: 'WO-9', status: 'open' }]),
    ])),
    valueFromQuestion: verdict(make([
      step('pm_db', "SELECT project_code FROM project WHERE project_code = 'J24001'", [{ project_code: 'J24001' }]),
      step('erp_db', "SELECT po_no FROM purchase_order WHERE job_no = 'J24001'", [{ po_no: 'PO-1' }]),
    ]), 'Show purchase orders for project J24001'),
    numericKeys: verdict(make([
      step('erp_db', 'SELECT supplier_id FROM supplier WHERE on_time_rate < 0.9',
        [{ supplier_id: 3 }, { supplier_id: 15 }, { supplier_id: 27 }]),
      step('wms_db', 'SELECT batch_no FROM batch WHERE supplier_id IN (3, 15, 27)', [{ batch_no: 'B1' }]),
    ])),
    loneNumberElsewhere: verdict(make([
      step('erp_db', 'SELECT COUNT(*) AS n FROM supplier', [{ n: 3 }]),
      step('wms_db', 'SELECT batch_no FROM batch ORDER BY received_on DESC LIMIT 3', [{ batch_no: 'B1' }]),
    ])),
    loneNumberAsKey: verdict(make([
      step('erp_db', "SELECT supplier_id FROM supplier WHERE supplier_name = 'Canyon Crest Quartz'", [{ supplier_id: 1042 }]),
      step('wms_db', 'SELECT batch_no FROM batch WHERE supplier_id = 1042', [{ batch_no: 'B1' }]),
    ])),
    pythonFloat: verdict(make([
      step('a', 'SELECT amount FROM t', [{ amount: 1200 }, { amount: 88.5 }]),
      step('b', 'SELECT x FROM u WHERE amount IN (1200.0, 88.5)', [{ x: 1 }]),
    ])),
    digitsInsideOtherNumbers: verdict(make([
      step('a', 'SELECT id FROM t', [{ id: 12 }, { id: 7 }]),
      step('b', 'SELECT x FROM u WHERE id IN (123, 77) AND rate > 1.12', [{ x: 1 }]),
    ])),
  };
}

// ---- table.js / format.js ------------------------------------------------------
{
  const { sortRows } = await load('table.js');
  const { formatNumber, recordDates } = await load('format.js');
  const column = (values, direction = 'asc') => sortRows(values.map((value) => [value]), 0, direction).map((row) => row[0]);
  out.sort = {
    numericStrings: column(['-0.50', '-9.00', '-10.00', '3', '12.5']),
    numericStringsDesc: column(['-0.50', '-9.00', '-10.00', '3', '12.5'], 'desc'),
    longIntegers: column(['9007199254740993', '9007199254740992', '9007199254740994']),
    mixedTypes: column([10, '9', 2.5, '-1']),
    emptiesLast: column([null, '2', '', '1'], 'desc'),
    text: column(['banana', 'Apple', 'cherry']),
  };
  out.numbers = {
    tiny: formatNumber(0.00001234),
    tinyNegative: formatNumber(-0.00001),
    tinyMoney: formatNumber(0.004, { unit: 'USD' }),
    zero: formatNumber(0),
    zeroMoney: formatNumber(0, { unit: 'USD' }),
    money: formatNumber(31600, { unit: 'USD' }),
    ratio: formatNumber(0.88),
    year: formatNumber(2024),
    large: formatNumber(127200),
  };
  const dates = (values) => recordDates([{ key: 'c' }], values.map((value) => ({ c: value })), (row, col) => row[col.key])
    .map((range) => [range.min, range.max]);
  out.dates = {
    plain: dates(['2024-01-25', '2024-04-22']),
    withTime: dates(['2024-03-31 10:00:00', '2024-03-30T09:00:00']),
    documentNumbers: dates(['2024-10-12345', '2024-10-98765']),
    impossible: dates(['2024-13-01']),
  };

  // 数据字典里的文字：有当前语言的版本用它；没有就给原文并标出来，不替它翻译
  const { localized } = await load('format.js');
  const { state } = await load('state.js');
  const entry = {
    term: '准时率', term_en: 'On-time rate',
    definition: '按约定交期到货的批次占比。',
    definition_en: 'Share of deliveries that arrived by the promised date.',
    usage: '用于筛选供应商', note: 'Same supplier', empty_en: '',
  };
  const read = (lang) => {
    state.lang = lang;
    return Object.fromEntries(['term', 'definition', 'usage', 'note', 'empty', 'absent']
      .map((key) => [key, localized(entry, key)]));
  };
  out.localized = { en: read('en'), zh: read('zh'), nothing: localized(null, 'term') };
  state.lang = 'en';
}

// ---- main.js：路由（函数取出来单独跑，整个模块要 DOM） ---------------------------
{
  const source = readFileSync(new URL(`${JS}/main.js`), 'utf8');
  const body = source.match(/function parseRoute\(\) \{[\s\S]*?\n\}/)[0];
  const parse = (hash) => new Function('location', `${body}; return parseRoute();`)({ hash });
  out.routes = Object.fromEntries(['', '#', '#/', '#/ask', '#/data', '#/data/erp_db/supplier', '#/history',
    '#main', '#/nope', '#/data/%E0%A4%A'].map((hash) => [hash, parse(hash)]));
}

// ---- api.js / catalog.js：假的 fetch -------------------------------------------
{
  const calls = [];
  let handler = null;
  globalThis.fetch = (url, options) => { calls.push(String(url)); return handler(String(url), options); };
  const json = (status, body) => Promise.resolve({ ok: status < 400, status, json: async () => body, headers: { get: () => 'application/json' } });
  const { api } = await load('api.js');
  const { catalog, ensureIdentity, loadCatalog } = await load('catalog.js');
  const { state } = await load('state.js');
  const caught = async (promise) => { try { await promise; return null; } catch (error) { return { name: error.name, status: error.status, code: error.code }; } };

  handler = () => Promise.resolve({ ok: true, status: 200, json: async () => { throw new SyntaxError('not json'); } });
  out.nonJsonSuccess = await caught(api.health());
  handler = () => Promise.resolve({ ok: true, status: 200, json: async () => { const error = new Error('aborted'); error.name = 'AbortError'; throw error; } });
  out.abortedBody = await caught(api.health());

  // 先发的目录请求后回来：不能盖掉后发的那一份
  const gates = [];
  handler = (url) => new Promise((resolve) => {
    if (url.includes('/catalog')) gates.push(resolve);
    else resolve(json(404, {}));
  });
  const first = loadCatalog();
  const second = loadCatalog({ refresh: true });
  const page = (user) => json(200, { domains: [], identity: { user_id: user }, service: { streaming: false } });
  gates[1](await page('new-identity'));
  await second;
  gates[0](await page('old-identity'));
  await first;
  out.sequencing = { identity: state.identity && state.identity.user_id, status: catalog.status };

  // 数据浏览关掉了：身份从 /whoami 来，提问不受连累
  state.identity = null;
  state.service = null;
  calls.length = 0;
  handler = (url) => (url.includes('/catalog')
    ? json(404, { detail: { code: 'disabled', message: 'off' } })
    : json(200, { identity: { user_id: 'buyer' }, service: { streaming: true } }));
  await loadCatalog({ refresh: true });
  out.catalogOff = {
    status: catalog.status, identity: state.identity && state.identity.user_id,
    streaming: state.service && state.service.streaming, calls: calls.map((url) => url.split('?')[0]),
  };

  // 目录出错、当时连身份也没问到；发问前再问一次
  state.identity = null;
  let whoami = 0;
  handler = (url) => {
    if (url.includes('/catalog')) return json(500, { detail: 'boom' });
    whoami += 1;
    return whoami === 1 ? json(503, { detail: 'later' }) : json(200, { identity: { user_id: 'late' }, service: {} });
  };
  await loadCatalog({ refresh: true });
  const before = state.identity;
  const ensured = await ensureIdentity();
  out.lateIdentity = { before, ensured, identity: state.identity && state.identity.user_id, status: catalog.status };
}

console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "没有安装 Node：跳过页面逻辑的行为测试")
class ConsoleLogicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmpdir:
            script = Path(tmpdir) / "harness.mjs"
            script.write_text(HARNESS, encoding="utf-8")
            completed = subprocess.run(
                [NODE, str(script), JS_DIR.as_uri()],
                capture_output=True, text=True, timeout=60,
            )
        if completed.returncode != 0:
            raise AssertionError(f"页面脚本在 Node 里跑不起来：\n{completed.stderr}")
        cls.out = json.loads(completed.stdout.strip().splitlines()[-1])

    # ---- 中间步骤 ----

    def test_a_step_whose_keys_all_went_into_the_next_sql_is_intermediate(self):
        steps = self.out["steps"]
        self.assertEqual(steps["handoff"]["intermediate"], [1])
        self.assertEqual(steps["handoff"]["answers"], [2])
        self.assertEqual(steps["handoff"]["carried"], [["project_code", 3, 2]])

    def test_numeric_keys_are_recognised_without_quotes(self):
        steps = self.out["steps"]
        # 服务端把数字写成不带引号的字面量；Python 的 1200.0 到了页面是 1200
        self.assertEqual(steps["numericKeys"]["intermediate"], [1])
        self.assertEqual(steps["loneNumberAsKey"]["intermediate"], [1])
        self.assertEqual(steps["pythonFloat"]["intermediate"], [1])

    def test_a_step_that_also_carries_other_columns_stays_in_the_answer(self):
        # 项目名称和成本没有被带进下一步：它们是答案的一部分，收进折叠里就丢了
        verdict = self.out["steps"]["handoffWithExtras"]
        self.assertEqual((verdict["answers"], verdict["intermediate"]), ([1, 2], []))

    def test_coincidences_are_not_handoffs(self):
        steps = self.out["steps"]
        for name in ("sharedFilter", "valueFromQuestion", "loneNumberElsewhere", "digitsInsideOtherNumbers"):
            self.assertEqual(steps[name]["intermediate"], [], name)
            self.assertEqual(steps[name]["answers"], [1, 2], name)
        # 问题里写着的值、LIMIT 后面的数字、别的数字里的一段，都不算「带过去了」
        for name in ("valueFromQuestion", "loneNumberElsewhere", "digitsInsideOtherNumbers"):
            self.assertEqual(steps[name]["carried"], [], name)

    # ---- 排序与显示 ----

    def test_numbers_sort_as_numbers_even_when_they_arrive_as_text(self):
        sort = self.out["sort"]
        self.assertEqual(sort["numericStrings"], ["-10.00", "-9.00", "-0.50", "3", "12.5"])
        self.assertEqual(sort["numericStringsDesc"], ["12.5", "3", "-0.50", "-9.00", "-10.00"])
        self.assertEqual(sort["mixedTypes"], ["-1", 2.5, "9", 10])
        # 超过 2^53 的整数：按 Number 比三个都相等
        self.assertEqual(
            sort["longIntegers"], ["9007199254740992", "9007199254740993", "9007199254740994"]
        )
        self.assertEqual(sort["emptiesLast"], ["2", "1", None, ""])
        self.assertEqual(sort["text"], ["Apple", "banana", "cherry"])

    def test_a_non_zero_number_is_never_shown_as_zero(self):
        numbers = self.out["numbers"]
        self.assertEqual(numbers["tiny"], "0.00001234")
        self.assertEqual(numbers["tinyNegative"], "-0.00001")
        self.assertEqual(numbers["tinyMoney"], "0.004")
        # 平常的写法不变
        self.assertEqual(numbers["zero"], "0")
        self.assertEqual(numbers["zeroMoney"], "0.00")
        self.assertEqual(numbers["money"], "31,600.00")
        self.assertEqual(numbers["ratio"], "0.88")
        self.assertEqual(numbers["year"], "2024")
        self.assertEqual(numbers["large"], "127,200")

    def test_dictionary_text_uses_the_readers_language_and_never_translates(self):
        english, chinese = self.out["localized"]["en"], self.out["localized"]["zh"]

        # 写了英文版：英文界面用它，不标「原文」
        self.assertEqual(english["term"], {"text": "On-time rate", "original": False})
        self.assertEqual(
            english["definition"]["text"], "Share of deliveries that arrived by the promised date."
        )
        # 没写英文版：照原文给，并标明这是原文——页面不替数据字典翻译口径
        self.assertEqual(english["usage"], {"text": "用于筛选供应商", "original": True})
        # 原文本来就是英文的不算「原文」
        self.assertEqual(english["note"], {"text": "Same supplier", "original": False})
        # 中文界面读原文，英文版不抢位置
        self.assertEqual(chinese["term"], {"text": "准时率", "original": False})
        self.assertEqual(chinese["usage"], {"text": "用于筛选供应商", "original": False})
        for reading in (english, chinese):
            self.assertEqual(reading["empty"], {"text": "", "original": False})
            self.assertEqual(reading["absent"], {"text": "", "original": False})
        self.assertEqual(self.out["localized"]["nothing"], {"text": "", "original": False})

    def test_record_dates_are_only_read_from_real_dates(self):
        dates = self.out["dates"]
        self.assertEqual(dates["plain"], [["2024-01-25", "2024-04-22"]])
        self.assertEqual(dates["withTime"], [["2024-03-30", "2024-03-31"]])
        # 2024-10-12345 是单据号，不是 10 月 12 日
        self.assertEqual(dates["documentNumbers"], [])
        self.assertEqual(dates["impossible"], [])

    # ---- 路由 ----

    def test_unknown_hashes_do_not_navigate(self):
        routes = self.out["routes"]
        for home in ("", "#", "#/", "#/ask"):
            self.assertEqual(routes[home], {"view": "ask"}, home)
        self.assertEqual(routes["#/data"], {"view": "data", "database": "", "table": ""})
        self.assertEqual(
            routes["#/data/erp_db/supplier"], {"view": "data", "database": "erp_db", "table": "supplier"}
        )
        self.assertEqual(routes["#/history"], {"view": "history"})
        # 页内锚点、不认识的路径、坏的转义：留在原地（null），不是跳去提问页
        for stray in ("#main", "#/nope", "#/data/%E0%A4%A"):
            self.assertIsNone(routes[stray], stray)

    # ---- 请求层与目录 ----

    def test_an_unreadable_success_body_is_an_error(self):
        self.assertEqual(
            self.out["nonJsonSuccess"], {"name": "Error", "status": 200, "code": "bad_response"}
        )
        self.assertEqual(self.out["abortedBody"]["name"], "AbortError")

    def test_a_late_catalog_response_does_not_overwrite_a_newer_one(self):
        self.assertEqual(self.out["sequencing"], {"identity": "new-identity", "status": "ready"})

    def test_identity_does_not_depend_on_the_catalog(self):
        off = self.out["catalogOff"]
        self.assertEqual((off["status"], off["identity"], off["streaming"]), ("disabled", "buyer", True))
        self.assertEqual(off["calls"], ["/catalog", "/whoami"])

        late = self.out["lateIdentity"]
        self.assertIsNone(late["before"])
        self.assertEqual((late["ensured"], late["identity"], late["status"]), (True, "late", "error"))


if __name__ == "__main__":
    unittest.main()
