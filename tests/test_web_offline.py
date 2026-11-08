"""HTTP 路由离线测试：前端页 / Agent 接口 / 限时下载（零网络请求）。

v0.2：用 starlette TestClient 直接打 `ctx.mcp.streamable_http_app()`，
路由经真实 ASGI 栈（此前直接调函数的写法曾漏掉路由挂载错误）。
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from modelscope_agent import register_routes as register_agent_routes
from printguard_aholo.config import Settings
from printguard_aholo.pipeline.delivery import DeliveryStore
from printguard_aholo.service.app import build_app


class AppTestBase(unittest.TestCase):
    """每个用例一个独立装配的应用（自带临时 runs 目录）。

    默认注入 llm_token 并挂载演示层（modelscope_agent 提供聊天页与
    /api/chat、/api/job）；关闭态的隔离性见 AgentDisabledTests。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(
            runs_dir=Path(self.tmp.name) / "runs",
            delivery_ttl_min=5, public_base_url="", llm_token="test-token")
        self.app_ctx = build_app(self.settings)
        register_agent_routes(self.app_ctx)  # 组合根行为：token 存在才挂载
        self.client = TestClient(self.app_ctx.mcp.streamable_http_app())


class FrontPageTests(AppTestBase):
    def test_root_serves_html(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn("PrintGuard_Aholo", body)
        self.assertIn("/api/chat", body)

    def test_root_falls_back_to_json_when_page_missing(self):
        from modelscope_agent import routes
        with mock.patch.object(routes, "STATIC_DIR", Path("/nope")):
            response = self.client.get("/")
        payload = response.json()
        self.assertEqual(payload["service"], "PrintGuard_Aholo")

    def test_healthz(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("printguard_check_job", body["tools"])
        self.assertIn("total", body["jobs"])
        self.assertIn("runs_dir", body)

    def test_healthz_llm_nested_for_frontend(self):
        """前端读 llm.configured / llm.model——必须是嵌套结构
        （曾与平铺字段不一致，状态徽章永远显示「模型已就绪」）。"""
        from modelscope_agent.routes import STATIC_DIR
        body = self.client.get("/healthz").json()
        self.assertIsInstance(body["llm"]["configured"], bool)
        self.assertIsInstance(body["llm"]["model"], str)
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn("d.llm || {}", html)
        self.assertIn("llm.configured === true", html)


class FrontPageBehaviourTests(unittest.TestCase):
    """前端契约：模型走下载按钮；不做二次导航。"""

    def setUp(self):
        from modelscope_agent.routes import STATIC_DIR
        self.html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def test_model_download_is_a_button(self):
        self.assertIn("downloadButton", self.html)
        self.assertIn("a.download", self.html)  # blob + a[download]：真正存文件

    def test_no_image_rendering_code(self):
        # 产品只交付模型：前端不应再有任何图片渲染/放大代码
        for sym in ("imageNode", "data-zoom", "lightbox"):
            self.assertNotIn(sym, self.html)

    def test_markdown_links_are_rendered(self):
        self.assertIn("renderRich", self.html)
        self.assertIn("data-dl", self.html)

    def test_no_image_quick_prompts(self):
        # 快捷提示与提示条不应再提概念图/四视图（图生 3D 用上传，不是概念图）
        for word in ("概念图", "四视图", "concept"):
            self.assertNotIn(word, self.html)

    def test_image_upload_entry_exists(self):
        """图生 / 文图混合：前端要有图片上传入口。"""
        self.assertIn("imageInput", self.html)
        self.assertIn("pendingImages", self.html)


class ChatApiTests(AppTestBase):
    def test_non_json_body_rejected(self):
        response = self.client.post(
            "/api/chat", content=b"not-json",
            headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)

    def test_history_must_be_list(self):
        response = self.client.post("/api/chat", json={"history": "oops"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("history", response.json()["error"])


class ChatJobApiTests(AppTestBase):
    """/api/chat 提交 + /api/job 轮询（长任务不再同步阻塞网关）。"""

    def test_submit_returns_job_id(self):
        from modelscope_agent import agent

        with mock.patch.object(agent, "submit_turn", return_value="abc123"):
            response = self.client.post(
                "/api/chat", json={"history": [{"role": "user",
                                                "content": "hi"}]})
        payload = response.json()
        self.assertEqual(payload["job_id"], "abc123")
        self.assertEqual(payload["status"], "queued")

    def test_existing_job_returns_record(self):
        """必须走完整路由（含 STORE.purge_expired）——曾因引用已删除的
        agent.STORE 而 500，单测没覆盖到。"""
        from modelscope_agent import agent

        def fake(ctx, history):
            return agent.Reply(text="ok", artifacts=[], steps=[])

        with mock.patch.object(agent, "respond", fake):
            job_id = agent.submit_turn(self.app_ctx,
                                       [{"role": "user", "content": "hi"}])
            record = None
            for _ in range(200):
                record = agent.get_turn(self.app_ctx, job_id)
                if record["status"] in ("succeeded", "failed"):
                    break
                time.sleep(0.05)

        response = self.client.get(f"/api/job/{job_id}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["result"]["reply"], "ok")

    def test_unknown_job_returns_404(self):
        response = self.client.get("/api/job/nosuchjob")
        self.assertEqual(response.status_code, 404)

    def test_job_status_roundtrip(self):
        from modelscope_agent import agent

        def fake(ctx, history):
            return agent.Reply(text="ok", artifacts=[
                {"key": "model", "url": "/d/x", "filename": "a.3mf",
                 "kind": "model"}], steps=[])

        with mock.patch.object(agent, "respond", fake):
            job_id = agent.submit_turn(self.app_ctx,
                                       [{"role": "user", "content": "hi"}])
            record = None
            for _ in range(200):
                record = agent.get_turn(self.app_ctx, job_id)
                if record["status"] in ("succeeded", "failed"):
                    break
                time.sleep(0.05)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["result"]["artifacts"][0]["kind"], "model")


class AgentDisabledTests(unittest.TestCase):
    """Agent 是可选演示层的结构保证：不配 token 时不挂载演示路由，
    核心 MCP 服务照常运行（/api/chat 直接 404，而不是报半路错误）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(runs_dir=Path(self.tmp.name) / "runs")
        self.app_ctx = build_app(self.settings)  # 默认 llm_token=""
        self.client = TestClient(self.app_ctx.mcp.streamable_http_app())
        # 注意：组合根在 token 缺失时不会调用 register_agent_routes

    def test_core_tools_registered_without_agent(self):
        """删除演示层也不影响 MCP 核心：7 个工具照常注册。"""
        import asyncio
        names = {t.name for t in asyncio.run(self.app_ctx.mcp.list_tools())}
        self.assertIn("printguard_run", names)
        self.assertEqual(len(names), 7)

    def test_agent_routes_not_mounted(self):
        """未挂载演示层：聊天页与回合接口直接 404（路由不存在）。"""
        self.assertEqual(self.client.get("/").status_code, 404)
        response = self.client.post(
            "/api/chat", json={"history": [{"role": "user", "content": "hi"}]})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get("/api/job/whatever").status_code, 404)

    def test_core_routes_still_served(self):
        """/healthz 与交付路由不受演示层影响。"""
        body = self.client.get("/healthz").json()
        self.assertEqual(body["status"], "ok")
        self.assertFalse(body["llm"]["configured"])
        self.assertEqual(self.client.get("/d/short").status_code, 400)


class OneTimeDownloadTests(unittest.TestCase):
    """限时下载语义：once / TTL / 重复下载——直接对 DeliveryStore 实例断言，
    再经真实路由取件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(
            runs_dir=Path(self.tmp.name) / "runs", delivery_ttl_min=15,
            delivery_once=True)
        self.app_ctx = build_app(self.settings)
        self.app_ctx.store = DeliveryStore(ttl_sec=900.0, max_bytes=1 << 20,
                                           once=True)
        self.client = TestClient(self.app_ctx.mcp.streamable_http_app())
        token, cached = self.app_ctx.store.put(b"3mf", "printable.3mf",
                                               "model/3mf")
        self.assertTrue(cached)
        self.token = token

    def test_bad_token_rejected(self):
        response = self.client.get("/d/short")
        self.assertEqual(response.status_code, 400)

    def test_unknown_token_is_gone(self):
        response = self.client.get("/d/" + "a" * 24)
        self.assertEqual(response.status_code, 410)

    def test_repeat_download_within_ttl_when_not_once(self):
        self.app_ctx.store.once = False
        token, cached = self.app_ctx.store.put(b"3mf", "printable.3mf",
                                               "model/3mf")
        self.assertTrue(cached)
        for _ in range(2):
            response = self.client.get(f"/d/{token}")
            self.assertEqual(response.status_code, 200)

    def test_download_once_then_gone_when_strict(self):
        token, _ = self.app_ctx.store.put(b"3mf", "printable.3mf", "model/3mf")
        self.assertEqual(self.client.get(f"/d/{token}").status_code, 200)
        self.assertEqual(self.client.get(f"/d/{token}").status_code, 410)

    def test_expired_link_is_gone(self):
        self.app_ctx.store.once = False
        token, _ = self.app_ctx.store.put(b"3mf", "printable.3mf",
                                          "model/3mf", 1.0)
        # 直接改过期时间（白盒）：真实 sleep 在负载下会偶发不超时
        self.app_ctx.store._items[token].expires_at = time.time() - 1
        response = self.client.get(f"/d/{token}")
        self.assertEqual(response.status_code, 410)

    def test_old_once_test_kept(self):
        response = self.client.get(f"/d/{self.token}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(response.content, b"3mf")
        response = self.client.get(f"/d/{self.token}")
        self.assertEqual(response.status_code, 410)


if __name__ == "__main__":
    unittest.main()
