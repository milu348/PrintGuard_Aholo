"""前端渲染契约测试：用真实页面脚本 + 最小 DOM stub 在 node 里跑。

覆盖「下载按钮只给模型、图片只内联显示」这条硬要求（线上曾出现图片也可下载）。
node 不可用时自动跳过。
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "modelscope_agent" / "static" / "index.html"

NODE_SCRIPT = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = html.match(/<script>\n([\s\S]*)\n<\/script>/)[1];

function el(tag) {
  return {
    tagName: tag, children: [], attrs: {}, style: {}, dataset: {},
    innerHTML: '', textContent: '', className: '', disabled: false, src: '',
    classList: { add() {}, remove() {} },
    appendChild(c) { this.children.push(c); return c; },
    replaceWith() {}, querySelectorAll() { return []; },
    querySelector() { return null; }, setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener() {}, remove() {}, click() {}, focus() {},
  };
}
global.document = { createElement: el, getElementById: () => el('div'),
                    querySelectorAll: () => [], querySelector: () => null,
                    body: el('body') };
global.window = { scrollTo() {}, open() {} };
global.URL = { createObjectURL: () => 'blob:x', revokeObjectURL() {} };

const sandbox = {};
new Function('sandbox', script +
  '\n; sandbox.renderRich = renderRich;' +
  ' sandbox.downloadButton = downloadButton;' +
  ' sandbox.pollJob = pollJob;')(sandbox);

const arts = [
  { key: 'model', url: 'https://h/d/m1', filename: 'printable.3mf',
    size_bytes: 1290, kind: 'model' },
];
const text = '✅ 模型已生成并修复！\n\n' +
             '**下载链接：** [printable.3mf](https://h/d/m1)（5 分钟有效）\n\n' +
             '另外这里也有一份裸链接 https://h/d/m1 可以直接复制';
const box = el('div');
sandbox.renderRich(box, text, arts);
const out = box.children.map(p => p.innerHTML).join('\n');
const btn = sandbox.downloadButton(arts[0], true);

const result = {
  model_becomes_button: /data-dl="https:\/\/h\/d\/m1"/.test(out),
  no_image_rendered: !/<img /.test(out),
  button_tag: btn.tagName,
  button_label: btn.textContent.trim(),
};
// ---- 轮询韧性：502 / 网络抖动都要重试，不能废掉整轮 ----
(async () => {
  let calls = 0;
  global.fetch = async () => {
    calls += 1;
    if (calls === 1) return { ok: false, status: 502, json: async () => ({}) };
    if (calls === 2) throw new Error('network down');
    return { ok: true, status: 200,
             json: async () => ({ status: 'succeeded',
                                  result: { reply: 'ok', artifacts: [] } }) };
  };
  global.setTimeout = (fn) => fn();
  const rec = await sandbox.pollJob('abc', el('div'));
  result.poll_fetch_calls = calls;
  result.poll_terminal = rec.status;
  console.log(JSON.stringify(result));
})();
"""


@unittest.skipUnless(shutil.which("node"), "未安装 node，跳过长 upstream 渲染检查")
class FrontendRenderContractTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(PAGE.is_file(), f"缺少前端页面：{PAGE}")

    def run_page_script(self) -> dict:
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "check.js"
            script.write_text(NODE_SCRIPT, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(script), str(PAGE)],
                capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr[:500])
        import json
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_only_model_gets_download_button(self):
        result = self.run_page_script()
        self.assertTrue(result["model_becomes_button"],
                        "模型链接必须变成下载按钮")
        self.assertTrue(result["no_image_rendered"],
                        "产品已不出图，正文不应出现任何图片节点")
        self.assertEqual(result["button_tag"], "button")
        self.assertIn("下载模型", result["button_label"])
        self.assertIn("printable.3mf", result["button_label"])
        self.assertEqual(result["poll_terminal"], "succeeded")
        self.assertEqual(result["poll_fetch_calls"], 3,
                        "502 与网络错误都必须重试，第三次才落终态")


if __name__ == "__main__":
    unittest.main()
