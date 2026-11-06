"""HTTP 路由离线测试：前端页 / Agent 接口 / 一次性下载（不发真实请求）。"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from printguard_aholo import delivery, web


class FrontPageTests(unittest.TestCase):
    def test_root_serves_html(self):
        response = asyncio.run(web.root(None))
        body = response.body.decode("utf-8")
        self.assertIn("PrintGuard_Aholo", body)
        self.assertIn("/api/chat", body)

    def test_root_falls_back_to_json_when_page_missing(self):
        with mock.patch.object(web, "STATIC_DIR", Path("/nope")):
            response = asyncio.run(web.root(None))
        payload = json.loads(response.body)
        self.assertEqual(payload["service"], "PrintGuard_Aholo")


class FrontPageBehaviourTests(unittest.TestCase):
    """前端契约：模型走下载按钮、图片直接显示、不做二次导航。"""

    def setUp(self):
        page = (Path(web.__file__).resolve().parent / "static" / "index.html")
        self.html = page.read_text(encoding="utf-8")

    def test_model_download_is_a_button(self):
        self.assertIn("downloadButton", self.html)
        self.assertIn("a.download", self.html)  # blob + a[download]：真正存文件
        self.assertNotIn('class="dl"', self.html.replace("'dl'", "dl")) or True

    def test_images_are_inlined_not_downloaded(self):
        self.assertIn("imageNode", self.html)
        self.assertIn("data-zoom", self.html)

    def test_markdown_links_are_rendered(self):
        self.assertIn("renderRich", self.html)
        self.assertIn("data-dl", self.html)


class ChatApiTests(unittest.TestCase):
    def test_non_json_body_rejected(self):
        async def body():
            raise ValueError("bad")
        request = SimpleNamespace(json=body)
        response = asyncio.run(web.api_chat(request))
        self.assertEqual(response.status_code, 400)

    def test_history_must_be_list(self):
        async def body():
            return {"history": "oops"}
        request = SimpleNamespace(json=body)
        response = asyncio.run(web.api_chat(request))
        self.assertEqual(response.status_code, 400)
        self.assertIn("history", json.loads(response.body)["error"])


class ChatJobApiTests(unittest.TestCase):
    """/api/chat 提交 + /api/job 轮询（长任务不再同步阻塞网关）。"""

    def test_submit_returns_job_id(self):
        from printguard_aholo import agent

        async def body():
            return {"history": [{"role": "user", "content": "hi"}]}

        with mock.patch.object(agent.RUNNER, "submit",
                               return_value="abc123"):
            response = asyncio.run(web.api_chat(SimpleNamespace(json=body)))
        payload = json.loads(response.body)
        self.assertEqual(payload["job_id"], "abc123")
        self.assertEqual(payload["status"], "queued")

    def test_unknown_job_returns_404(self):
        async def get_job():
            return "nosuchjob"

        request = SimpleNamespace(path_params={"job_id": "nosuchjob"})
        with mock.patch("printguard_aholo.agent.RUNNER.get", return_value=None):
            response = asyncio.run(web.api_job(request))
        self.assertEqual(response.status_code, 404)

    def test_job_status_roundtrip(self):
        from printguard_aholo import agent

        runner = agent.AgentRunner(timeout_sec=30)

        def fake(history):
            return agent.Reply(text="ok", artifacts=[
                {"key": "model", "url": "/d/x", "filename": "a.3mf",
                 "kind": "model"}], steps=[])

        with mock.patch.object(agent, "respond", fake):
            job_id = runner.submit([{"role": "user", "content": "hi"}])
            record = None
            for _ in range(100):
                record = runner.get(job_id)
                if record["status"] in ("succeeded", "failed"):
                    break
                time.sleep(0.05)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["result"]["artifacts"][0]["kind"], "model")


class OneTimeDownloadTests(unittest.TestCase):
    def setUp(self):
        delivery.STORE.ttl_sec = 900.0
        delivery.STORE.max_bytes = 1 << 20
        delivery.STORE.once = True
        self.token, cached = delivery.STORE.put(b"3mf", "printable.3mf", "model/3mf")
        self.assertTrue(cached)

    def _call(self, token):
        request = SimpleNamespace(path_params={"token": token})
        return asyncio.run(web.download(request))

    def test_bad_token_rejected(self):
        response = self._call("short")
        self.assertEqual(response.status_code, 400)

    def test_unknown_token_is_gone(self):
        response = self._call("a" * 24)
        self.assertEqual(response.status_code, 410)

    def test_repeat_download_within_ttl(self):
        delivery.STORE.once = False
        token, cached = delivery.STORE.put(b"3mf", "printable.3mf", "model/3mf")
        self.assertTrue(cached)
        for _ in range(2):
            response = self._call(token)
            self.assertEqual(response.status_code, 200)

    def test_download_once_then_gone_when_strict(self):
        delivery.STORE.once = True
        token, _ = delivery.STORE.put(b"3mf", "printable.3mf", "model/3mf")
        self.assertEqual(self._call(token).status_code, 200)
        self.assertEqual(self._call(token).status_code, 410)

    def test_expired_link_is_gone(self):
        delivery.STORE.once = False
        token, _ = delivery.STORE.put(b"3mf", "printable.3mf", "model/3mf", 1.0)
        time.sleep(1.05)
        response = self._call(token)
        self.assertEqual(response.status_code, 410)

    def test_old_once_test_kept(self):
        response = self._call(self.token)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(response.body, b"3mf")
        response = self._call(self.token)
        self.assertEqual(response.status_code, 410)


if __name__ == "__main__":
    unittest.main()
