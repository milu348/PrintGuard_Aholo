"""Agent 层离线测试：大模型装死 / 工具分发 / 交付链接汇总（零网络、零平台调用）。"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from printguard_aholo import agent, delivery, tools
from printguard_aholo.llm import LLMError, ToolCall, chat


class FakeLLM:
    """按脚本返回 tool_calls / 文本，模拟 function calling 多轮。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, messages, tool_specs=None):
        self.calls.append([dict(m) for m in messages])
        return self.script.pop(0)


def _tool_result(reply, tool_calls=()):
    """构造一个大模型返回：要么纯文本，要么带 tool_calls。"""
    message = {"role": "assistant", "content": reply,
               "tool_calls": list(tool_calls)}
    return agent.llm.ChatResult(
        content=reply,
        tool_calls=[ToolCall.from_raw(item) for item in tool_calls],
        raw={"choices": [{"message": message}]})


class LLMClientTests(unittest.TestCase):
    def test_chat_requires_token(self):
        with mock.patch.object(agent.llm, "LLM_TOKEN", ""):
            with self.assertRaises(LLMError):
                chat([{"role": "user", "content": "hi"}])

    def test_chat_parses_tool_calls(self):
        payload = {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "printguard_run",
                                         "arguments": "{\"height_mm\": 50}"}}]}}]}
        response = mock.Mock(status_code=200)
        response.json.return_value = payload
        with mock.patch.object(agent.llm.requests, "post",
                               return_value=response) as post, \
                mock.patch.object(agent.llm, "LLM_TOKEN", "tok"):
            result = chat([{"role": "user", "content": "做个支架"}],
                          [{"type": "function", "function": {"name": "x",
                                                             "parameters": {}}}])
        self.assertEqual(result.tool_calls[0].name, "printguard_run")
        self.assertEqual(result.tool_calls[0].arguments, {"height_mm": 50})
        body = post.call_args.kwargs["json"]
        self.assertTrue(body["tools"])
        self.assertEqual(body["model"], agent.llm.LLM_MODEL)


class ToolCallParsing(unittest.TestCase):
    def test_broken_json_becomes_raw(self):
        call = ToolCall.from_raw({"id": "c", "function": {
            "name": "x", "arguments": "not-json"}})
        self.assertEqual(call.arguments["_raw"], "not-json")

    def test_non_dict_arguments_wrapped(self):
        call = ToolCall.from_raw({"id": "c", "function": {
            "name": "x", "arguments": "[1,2]"}})
        self.assertEqual(call.arguments["_raw"], [1, 2])


class AgentDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_unknown_tool_reported(self):
        payload = agent.run_tool("nope_tool", {})
        self.assertFalse(payload["ok"])
        self.assertIn("未知工具", payload["error"])

    def test_sync_tool_error_is_translated(self):
        # 不让测试打到真平台：客户端构造即失败时应返回可读 payload
        with mock.patch.object(tools, "make_client",
                               side_effect=RuntimeError("no key")):
            result = agent.run_tool("lux3d_list_tasks", {"page": 1})
        self.assertFalse(result["ok"])
        self.assertIn("RuntimeError", result["error"])

    def test_respond_returns_text_without_tools(self):
        fake = FakeLLM([_tool_result("请告诉我最大边尺寸（毫米）。")])
        with mock.patch.object(agent.llm, "chat", fake):
            reply = agent.respond([{"role": "user", "content": "hi"}])
        self.assertEqual(reply.text, "请告诉我最大边尺寸（毫米）。")
        self.assertEqual(reply.artifacts, [])

    def test_respond_requires_user_message(self):
        reply = agent.respond([])
        self.assertIn("最大边", reply.text)


class ArtifactCollectionTests(unittest.TestCase):
    def test_downloads_deduplicated_and_typed(self):
        payloads = [
            {"job": {"result": {"downloads": {
                "model": {"url": "https://h/d/aaa", "filename": "printable.3mf",
                          "size_bytes": 1024},
                "concept_image": {"url": "https://h/d/bbb", "filename": "concept.png"}}}},
             "ok": True},
            {"job": {"result": {"downloads": {
                "model": {"url": "https://h/d/aaa", "filename": "printable.3mf"}}}}},
        ]
        arts = agent._collect_artifacts(payloads)
        self.assertEqual([a["key"] for a in arts], ["concept_image", "model"])
        kinds = {a["filename"]: a["kind"] for a in arts}
        self.assertEqual(kinds["concept.png"], "image")
        self.assertEqual(kinds["printable.3mf"], "model")

    def test_legacy_artifact_urls_still_collected(self):
        arts = agent._collect_artifacts(
            [{"artifact_urls": {"model": "https://h/files/r1/printable.3mf"}}])
        self.assertEqual(arts[0]["kind"], "model")


class DeliveryCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = Path(self.tmp.name) / "run_x"
        self.run_dir.mkdir()
        (self.run_dir / "printable.3mf").write_bytes(b"3mf-bytes")
        (self.run_dir / "concept.png").write_bytes(b"png-bytes")
        delivery.STORE.ttl_sec = 900.0
        delivery.STORE.max_bytes = 1 << 20
        delivery.STORE.once = True

    def test_capture_and_delete_run_dir(self):
        downloads, warnings = delivery.capture_artifacts(
            {"model": str(self.run_dir / "printable.3mf"),
             "model_format": "3mf",
             "concept_image": str(self.run_dir / "concept.png")},
            self.run_dir, ttl_sec=600, max_bytes=1 << 20, once=True,
            base_url="https://x.ms.show")
        self.assertEqual(warnings, [])
        self.assertEqual(sorted(downloads), ["concept_image", "model"])
        self.assertTrue(downloads["model"]["url"].endswith(
            downloads["model"]["url"].rsplit("/", 1)[-1]))
        self.assertFalse(self.run_dir.exists())  # 磁盘不再保留

    def test_default_allows_repeated_download(self):
        """默认按时效（不一次性）：浏览器预览 + 用户点击都要能拿到。"""
        downloads, _ = delivery.capture_artifacts(
            {"model": str(self.run_dir / "printable.3mf")}, self.run_dir,
            ttl_sec=600, max_bytes=1 << 20)
        token = downloads["model"]["url"].rsplit("/", 1)[-1]
        self.assertEqual(downloads["model"]["expires_sec"], 600)
        for _ in range(3):
            self.assertIsNotNone(delivery.STORE.take(token))

    def test_strict_once_still_supported(self):
        downloads, _ = delivery.capture_artifacts(
            {"model": str(self.run_dir / "printable.3mf")}, self.run_dir,
            ttl_sec=600, max_bytes=1 << 20, once=True)
        token = downloads["model"]["url"].rsplit("/", 1)[-1]
        self.assertIsNotNone(delivery.STORE.take(token))
        self.assertIsNone(delivery.STORE.take(token))

    def test_model_and_image_have_different_ttl(self):
        downloads, _ = delivery.capture_artifacts(
            {"model": str(self.run_dir / "printable.3mf"),
             "concept_image": str(self.run_dir / "concept.png")}, self.run_dir,
            ttl_sec=300, image_ttl_sec=3600, max_bytes=1 << 20)
        self.assertEqual(downloads["model"]["expires_sec"], 300)
        self.assertEqual(downloads["concept_image"]["expires_sec"], 3600)

    def test_oversize_not_cached_and_run_dir_kept(self):
        (self.run_dir / "big.3mf").write_bytes(b"x" * (1 << 21))
        delivery.STORE.max_bytes = 1 << 20
        downloads, warnings = delivery.capture_artifacts(
            {"model": str(self.run_dir / "big.3mf")}, self.run_dir,
            ttl_sec=600, max_bytes=1 << 20, once=True)
        self.assertEqual(downloads, {})
        self.assertTrue(any("上限" in w for w in warnings))
        self.assertTrue(self.run_dir.exists())

    def test_expired_token_returns_none(self):
        downloads, _ = delivery.capture_artifacts(
            {"model": str(self.run_dir / "printable.3mf")}, self.run_dir,
            ttl_sec=1, max_bytes=1 << 20, once=True)
        token = downloads["model"]["url"].rsplit("/", 1)[-1]
        delivery.STORE.purge_expired()
        time.sleep(1.05)
        self.assertIsNone(delivery.STORE.take(token))


class AgentRunnerTests(unittest.TestCase):
    """Agent 回合队列：提交秒回、终态可查、失败可读（504 的根治方案）。"""

    def test_submit_returns_immediately_and_completes(self):
        runner = agent.AgentRunner(timeout_sec=30)

        def fake_respond(history):
            return agent.Reply(text="好的", artifacts=[], steps=[])

        with mock.patch.object(agent, "respond", fake_respond):
            job_id = runner.submit([{"role": "user", "content": "hi"}])
            record = runner.get(job_id)
            # 提交即回：初始状态允许已被 worker 秒执行完
            self.assertIn(record["status"],
                          ("queued", "running", "succeeded"))
            for _ in range(100):
                record = runner.get(job_id)
                if record["status"] in ("succeeded", "failed"):
                    break
                time.sleep(0.05)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["result"]["reply"], "好的")

    def test_failure_is_reported(self):
        runner = agent.AgentRunner(timeout_sec=30)

        def boom(history):
            raise RuntimeError("llm down")

        with mock.patch.object(agent, "respond", boom):
            job_id = runner.submit([{"role": "user", "content": "hi"}])
            for _ in range(100):
                record = runner.get(job_id)
                if record["status"] in ("succeeded", "failed"):
                    break
                time.sleep(0.05)
        self.assertEqual(record["status"], "failed")
        self.assertIn("RuntimeError", record["error"])


class ImageDeliveryTests(unittest.TestCase):
    """概念图 / 四视图：平台 URL → 一次性交付（线上真实故障点，必须覆盖）。"""

    class _StubClient:
        def download_bytes(self, url):
            return b"jpeg-bytes", "hash.jpg"

    def setUp(self):
        delivery.STORE.ttl_sec = 900.0
        delivery.STORE.max_bytes = 1 << 20
        delivery.STORE.once = True

    def test_concept_image_url_becomes_download(self):
        with mock.patch.object(agent, "PUBLIC_BASE_URL", "https://x.ms.show"), \
                mock.patch.object(tools, "make_client",
                                  return_value=self._StubClient()):
            payload = agent._deliver_image_outputs(
                {"platform_task_id": 1, "outputs": ["https://cos/abc.jpg"]},
                "lux3d_concept_image")
        entry = payload["downloads"]["concept_image"]
        self.assertEqual(entry["filename"], "concept_image.jpg")
        self.assertTrue(entry["url"].startswith("https://x.ms.show/d/"))
        self.assertEqual(entry["size_bytes"], len(b"jpeg-bytes"))
        arts = agent._collect_artifacts([payload])
        self.assertEqual(arts[0]["kind"], "image")
        self.assertEqual(arts[0]["url"], entry["url"])

    def test_four_view_urls_are_numbered(self):
        with mock.patch.object(agent, "PUBLIC_BASE_URL", ""), \
                mock.patch.object(tools, "make_client",
                                  return_value=self._StubClient()):
            payload = agent._deliver_image_outputs(
                {"outputs": ["https://cos/a.jpg", "https://cos/b.jpg"]},
                "lux3d_four_view")
        self.assertEqual(sorted(payload["downloads"]),
                         ["four_view_01", "four_view_02"])
        self.assertTrue(payload["downloads"]["four_view_01"]["url"].startswith("/d/"))

    def test_non_image_tool_untouched(self):
        payload = {"outputs": ["https://cos/mesh.glb"]}
        out = agent._deliver_image_outputs(payload, "printguard_run")
        self.assertNotIn("downloads", out)

    def test_download_failure_only_warns(self):
        class Boom:
            def download_bytes(self, url):
                raise OSError("boom")

        with mock.patch.object(tools, "make_client", return_value=Boom()):
            payload = agent._deliver_image_outputs(
                {"outputs": ["https://cos/a.jpg"]}, "lux3d_concept_image")
        self.assertTrue(any("中转失败" in w for w in payload["warnings"]))
        self.assertNotIn("downloads", payload)


class ToolsDeliveryModeTests(unittest.TestCase):
    def test_apply_delivery_files_mode_is_noop(self):
        summary = {"ok": True, "artifacts": {"model": "/tmp/x/printable.3mf"}}
        with mock.patch.object(tools, "DELIVERY_MODE", "files"):
            out = tools.apply_delivery(summary)
        self.assertIs(out, summary)
        self.assertNotIn("downloads", out)


if __name__ == "__main__":
    unittest.main()
