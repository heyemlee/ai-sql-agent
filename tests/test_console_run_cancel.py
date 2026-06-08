"""
控制台的停止按钮要让服务端也停下：流式请求从首帧拿到运行号（taskId），停止时
`POST /runs/{id}/cancel`，再断开连接。只断开的话，服务端照样把查询跑完、照样扣配额。

用 Node 直接加载 `api/static/js/api.js` 的真实模块，`fetch` 换成假的。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_sql_agent.api.app import STATIC_DIR

NODE = shutil.which("node")
JS_DIR = Path(STATIC_DIR) / "js"

HARNESS = r"""
const JS = process.argv[2];
const { state } = await import(`${JS}/state.js`);
const { api } = await import(`${JS}/api.js`);

const calls = [];
const sse = (frames) => frames
  .map((frame) => `data: ${JSON.stringify({ jsonrpc: '2.0', id: 'x', result: frame })}\n\n`)
  .join('');

globalThis.fetch = async (url, init = {}) => {
  calls.push({ url: String(url), method: init.method || 'GET' });
  if (String(url).includes('/a2a')) {
    const body = sse([
      { kind: 'task', id: 'run123', contextId: 'c',
        status: { state: 'submitted', timestamp: '2026-01-01T00:00:00.000Z' } },
      { kind: 'artifact-update', taskId: 'run123', contextId: 'c', lastChunk: true,
        artifact: { parts: [{ kind: 'text', text: 'ok' }, { kind: 'data', data: { route: 'database_query' } }] } },
      { kind: 'status-update', taskId: 'run123', contextId: 'c', final: true,
        status: { state: 'completed', timestamp: '2026-01-01T00:00:01.000Z' }, metadata: { trace_id: 't' } },
    ]);
    return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } });
  }
  if (String(url).includes('/runs/')) {
    return new Response(JSON.stringify({ run_id: 'run 1/2', status: 'cancel_requested' }),
      { status: 202, headers: { 'content-type': 'application/json' } });
  }
  throw new Error(`unexpected ${url}`);
};

const seen = [];
const streamed = await api.stream('q', { onTask: (id) => seen.push(id) });
state.userId = 'alice';
const cancelled = await api.cancelRun('run 1/2');
console.log(JSON.stringify({ seen, answer: streamed.payload && streamed.payload.answer, cancelled, calls }));
"""


@unittest.skipUnless(NODE, "没有安装 Node：跳过控制台停止按钮的行为测试")
class ConsoleRunCancelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmpdir:
            script = Path(tmpdir) / "harness.mjs"
            script.write_text(HARNESS, encoding="utf-8")
            completed = subprocess.run(
                [NODE, str(script), JS_DIR.as_uri()], capture_output=True, text=True, timeout=60,
            )
        if completed.returncode != 0:
            raise AssertionError(f"页面脚本在 Node 里跑不起来：\n{completed.stderr}")
        cls.out = json.loads(completed.stdout.strip().splitlines()[-1])

    def test_the_stream_hands_over_the_run_id_from_its_first_frame(self):
        self.assertEqual(self.out["seen"], ["run123"])
        self.assertEqual(self.out["answer"], "ok", "拿运行号不影响正常解析结果")

    def test_cancel_posts_to_the_run_on_behalf_of_the_acting_user(self):
        cancel = self.out["calls"][-1]
        self.assertEqual(cancel["method"], "POST")
        self.assertEqual(cancel["url"], "/runs/run%201%2F2/cancel?user_id=alice")
        self.assertEqual(self.out["cancelled"]["status"], "cancel_requested")

    def test_the_stop_handler_cancels_on_the_server_before_aborting(self):
        source = (JS_DIR / "views" / "ask.js").read_text(encoding="utf-8")
        body = source[source.index("function stopTurn"):]
        body = body[: body.index("\n}\n")]
        self.assertLess(body.index("api.cancelRun"), body.index("controller.abort()"))
        self.assertIn("onTask: (runId) => { turn.runId = runId; }", source)
        self.assertEqual(source.count("stopTurn("), 3, "两个停止入口都走 stopTurn")


if __name__ == "__main__":
    unittest.main()
