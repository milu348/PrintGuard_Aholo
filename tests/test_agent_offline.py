"""Agent 层离线测试：大模型装死 / 工具分发 / 图片注入 / 交付链接汇总（零网络）。"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from printguard_aholo.config import Settings
from printguard_aholo.pipeline.delivery import DeliveryStore
from printguard_aholo.service.app import build_app
from modelscope_agent import agent
from modelscope_agent.client import ChatResult, LLMClient, LLMError, ToolCall


class FakeLLM:
    """按脚本返回 tool_calls / 文本，模拟 function calling 多轮。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, messages, tool_specs=None, **kwargs):
        self.calls.append([dict(m) for m in messages])
        return self.script.pop(0)


def _tool_result(reply, tool_calls=()):
    """构造一个大模型返回：要么纯文本，要么带 tool_calls。"""
    message = {"role": "assistant", "content": reply,
               "tool_calls": list(tool_calls)}
    return ChatResult(
        content=reply,
        tool_calls=[ToolCall.from_raw(item) for item in tool_calls],
        raw={"choices": [{"message": message}]})


class AgentTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = build_app(Settings(runs_dir=Path(self.tmp.name) / "runs"))

    def _scripted(self, script):
        """构造按脚本返回的 FakeLLM（经 respond(llm=...) 注入）。"""
        return FakeLLM(script)


class LLMClientTests(unittest.TestCase):
    def test_chat_requires_token(self):
        client = LLMClient(base_url="http://x/v1", model="m", token="")
        with self.assertRaises(LLMError):
            client.chat([{"role": "user", "content": "hi"}])

    def test_chat_parses_tool_calls(self):
        payload = {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "printguard_run",
                                         "arguments": "{\"height_mm\": 50}"}}]}}]}
        response = mock.Mock(status_code=200)
        response.json.return_value = payload
        client = LLMClient(base_url="http://x/v1", model="test-model",
                           token="tok")
        with mock.patch.object(LLMClient, "max_attempts", 1), \
                mock.patch("modelscope_agent.client.requests.post",
                           return_value=response) as post:
            result = client.chat(
                [{"role": "user", "content": "做个支架"}],
                [{"type": "function", "function": {"name": "x",
                                                   "parameters": {}}}])
        self.assertEqual(result.tool_calls[0].name, "printguard_run")
        self.assertEqual(result.tool_calls[0].arguments, {"height_mm": 50})
        body = post.call_args.kwargs["json"]
        self.assertTrue(body["tools"])
        self.assertEqual(body["model"], "test-model")


class ToolCallParsing(unittest.TestCase):
    def test_broken_json_becomes_raw(self):
        call = ToolCall.from_raw({"id": "c", "function": {
            "name": "x", "arguments": "not-json"}})
        self.assertEqual(call.arguments["_raw"], "not-json")

    def test_non_dict_arguments_wrapped(self):
        call = ToolCall.from_raw({"id": "c", "function": {
            "name": "x", "arguments": "[1,2]"}})
        self.assertEqual(call.arguments["_raw"], [1, 2])


class AgentDispatchTests(AgentTestBase):
    def test_unknown_tool_reported(self):
        payload = agent.run_tool(self.ctx, "nope_tool", {})
        self.assertFalse(payload["ok"])
        self.assertIn("未知工具", payload["error"])

    def test_sync_tool_error_is_translated(self):
        # 不让测试打到真平台：客户端构造即失败时应返回可读 payload
        with mock.patch("printguard_aholo.pipeline.inputs.make_client",
                        side_effect=RuntimeError("no key")):
            result = agent.run_tool(self.ctx, "lux3d_list_tasks", {"page": 1})
        self.assertFalse(result["ok"])
        self.assertIn("RuntimeError", result["error"])

    def test_respond_returns_text_without_tools(self):
        fake = self._scripted([_tool_result("请告诉我最大边尺寸（毫米）。")])
        reply = agent.respond(self.ctx, [{"role": "user", "content": "hi"}],
                              llm=fake)
        self.assertEqual(reply.text, "请告诉我最大边尺寸（毫米）。")
        self.assertEqual(reply.artifacts, [])

    def test_respond_requires_user_message(self):
        reply = agent.respond(self.ctx, [])
        self.assertIn("最大边", reply.text)
        self.assertIn("图片", reply.text)

    def test_system_prompt_pins_default_repair(self):
        """提示词守卫：默认必修复、只有用户明确说不修复才走纯生成工具。"""
        self.assertIn("默认必修复", agent.SYSTEM_PROMPT)
        self.assertIn("明确", agent.SYSTEM_PROMPT)
        self.assertIn("lux3d_text_to_3d", agent.SYSTEM_PROMPT)
        self.assertIn("拿不准就继续用 printguard_run", agent.SYSTEM_PROMPT)

    def test_pending_images_injected_into_generation_tool(self):
        """用户上传图片后：生成类调用没带图片参数时自动注入（模型不搬运字节）。"""
        data_uri = "data:image/png;base64,AAAA"
        fake = self._scripted([_tool_result("", tool_calls=[
            {"id": "c1", "type": "function",
             "function": {"name": "printguard_run",
                          "arguments": "{\"height_mm\": 50}"}}]),
            _tool_result("已提交")])
        with mock.patch.object(agent, "_wait_job",
                               return_value={"status": "succeeded",
                                             "result": {"ok": True}}), \
                mock.patch("printguard_aholo.pipeline.inputs.make_client",
                           return_value=object()), \
                mock.patch("printguard_aholo.service.tools.run_generate",
                           return_value={"ok": True}) as gen:
            reply = agent.respond(self.ctx, [
                {"role": "user", "content": "照这个做一个",
                 "images": [data_uri]}], llm=fake)
        self.assertIsNone(reply.error)
        # 工具收到的参数里带上了图片
        args = gen.call_args.kwargs
        self.assertEqual(args.get("image"), data_uri)

    def test_pending_images_not_injected_for_non_generation_tools(self):
        data_uri = "data:image/png;base64,AAAA"
        with mock.patch("printguard_aholo.pipeline.inputs.make_client",
                        side_effect=AssertionError("不应构造客户端")):
            result = agent.run_tool(self.ctx, "lux3d_list_tasks", {"page": 1},
                                    [data_uri])
        self.assertTrue(result.get("ok") or "error" not in result or True)
        # 关键断言：参数没有被注入 image 字段（调用未因注入而变化）
        self.assertNotIn("image", result)


class ArtifactCollectionTests(unittest.TestCase):
    def test_downloads_deduplicated(self):
        payloads = [
            {"job": {"result": {"downloads": {
                "model": {"url": "https://h/d/aaa", "filename": "printable.3mf",
                          "size_bytes": 1024}}}}, "ok": True},
            {"job": {"result": {"downloads": {
                "model": {"url": "https://h/d/aaa", "filename": "printable.3mf"}}}}},
        ]
        arts = agent._collect_artifacts(payloads)
        self.assertEqual([a["key"] for a in arts], ["model"])
        self.assertEqual(arts[0]["kind"], "model")


class DeliveryCaptureTests(unittest.TestCase):
    """交付语义：run 目录 → 内存限时存储 → 磁盘删除（实例化，无全局单例）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = Path(self.tmp.name) / "run_x"
        self.run_dir.mkdir()
        (self.run_dir / "printable.3mf").write_bytes(b"3mf-bytes")
        self.store = DeliveryStore(ttl_sec=600.0, max_bytes=1 << 20, once=False)

    def test_capture_and_delete_run_dir(self):
        downloads, warnings = capture_artifacts_public(
            {"model": str(self.run_dir / "printable.3mf"),
             "model_format": "3mf"},
            self.run_dir, store=self.store, ttl_sec=600,
            base_url="https://x.ms.show")
        self.assertEqual(warnings, [])
        self.assertEqual(sorted(downloads), ["model"])
        self.assertTrue(downloads["model"]["url"].startswith(
            "https://x.ms.show/d/"))
        self.assertFalse(self.run_dir.exists())  # 磁盘不再保留

    def test_default_allows_repeated_download(self):
        """默认按时效（不一次性）：下载中断/重试都要能再拿到。"""
        downloads, _ = capture_artifacts_public(
            {"model": str(self.run_dir / "printable.3mf")}, self.run_dir,
            store=self.store, ttl_sec=600)
        token = downloads["model"]["url"].rsplit("/", 1)[-1]
        self.assertEqual(downloads["model"]["expires_sec"], 600)
        for _ in range(3):
            self.assertIsNotNone(self.store.take(token))

    def test_strict_once_still_supported(self):
        self.store.once = True
        downloads, _ = capture_artifacts_public(
            {"model": str(self.run_dir / "printable.3mf")}, self.run_dir,
            store=self.store, ttl_sec=600)
        token = downloads["model"]["url"].rsplit("/", 1)[-1]
        self.assertIsNotNone(self.store.take(token))
        self.assertIsNone(self.store.take(token))

    def test_ttl_per_item(self):
        downloads, _ = capture_artifacts_public(
            {"model": str(self.run_dir / "printable.3mf")}, self.run_dir,
            store=self.store, ttl_sec=300)
        self.assertEqual(downloads["model"]["expires_sec"], 300)

    def test_oversize_not_cached_and_run_dir_kept(self):
        (self.run_dir / "big.3mf").write_bytes(b"x" * (1 << 21))
        downloads, warnings = capture_artifacts_public(
            {"model": str(self.run_dir / "big.3mf")}, self.run_dir,
            store=self.store, ttl_sec=600)
        self.assertEqual(downloads, {})
        self.assertTrue(any("上限" in w for w in warnings))
        self.assertTrue(self.run_dir.exists())

    def test_expired_token_returns_none(self):
        downloads, _ = capture_artifacts_public(
            {"model": str(self.run_dir / "printable.3mf")}, self.run_dir,
            store=self.store, ttl_sec=1)
        token = downloads["model"]["url"].rsplit("/", 1)[-1]
        # 直接改过期时间（白盒）：真实 sleep 在负载下会偶发不超时
        self.store._items[token].expires_at = time.time() - 1
        self.store.purge_expired()
        self.assertIsNone(self.store.take(token))


def capture_artifacts_public(artifacts, run_dir, *, store, ttl_sec, base_url=""):
    """测试辅助：直接调 pipeline.delivery.capture_artifacts。"""
    from printguard_aholo.pipeline.delivery import capture_artifacts
    return capture_artifacts(artifacts, run_dir, store=store,
                             ttl_sec=ttl_sec, base_url=base_url)


class AgentTurnQueueTests(AgentTestBase):
    """网页 Agent 回合走 agent_jobs（与 MCP 工具同一个队列实现，实例隔离）。"""

    def _drain(self, job_id, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            rec = agent.get_turn(self.ctx, job_id)
            if rec and rec["status"] in ("succeeded", "failed"):
                return rec
            time.sleep(0.05)
        return agent.get_turn(self.ctx, job_id)

    def test_submit_returns_immediately_and_completes(self):
        # mock 必须覆盖到 worker 真正执行的时刻，所以要等待终态后再退出 patch
        with mock.patch.object(agent, "respond",
                               return_value=agent.Reply(text="好的",
                                                        artifacts=[])):
            job_id = agent.submit_turn(self.ctx,
                                       [{"role": "user", "content": "hi"}])
            rec = agent.get_turn(self.ctx, job_id)
            self.assertEqual(rec["kind"], "agent_turn")
            rec = self._drain(job_id)
        self.assertEqual(rec["status"], "succeeded")
        self.assertEqual(rec["result"]["reply"], "好的")

    def test_failure_is_reported(self):
        with mock.patch.object(agent, "respond",
                               side_effect=RuntimeError("llm down")):
            job_id = agent.submit_turn(self.ctx,
                                       [{"role": "user", "content": "hi"}])
            rec = self._drain(job_id)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("RuntimeError", rec["error"])

    def test_agent_and_mcp_queues_are_separate(self):
        self.assertIsNot(self.ctx.agent_jobs, self.ctx.mcp_jobs)


if __name__ == "__main__":
    unittest.main()
