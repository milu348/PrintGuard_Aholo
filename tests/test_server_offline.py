"""MCP 服务离线单测：build_app 装配 / 工具注册 / 任务 / 交付（零网络）。

v0.2 装配方式：`build_app(Settings(...))` 得到完整应用——工具、队列、
交付存储都是显式构造的实例，测试可以随意注入与替换。
"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from printguard_aholo.config import Settings
from printguard_aholo.pipeline import PipelineError
from printguard_aholo.service.app import build_app
from printguard_aholo.service.tools import ASYNC_TOOLS


def _drain(manager, job_id, timeout=5.0):
    """自旋等终态（JobManager 只保证 submit/get/stats）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = manager.get(job_id)
        if record and record["status"] in ("succeeded", "failed"):
            return record
        time.sleep(0.02)
    return manager.get(job_id)


EXPECTED_TOOLS = {
    "lux3d_text_to_3d",
    "lux3d_image_to_3d",
    "lux3d_get_task",
    "lux3d_list_tasks",
    "printguard_run",
    "printguard_repair",
    "printguard_check_job",
}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(
            runs_dir=Path(self.tmp.name) / "runs",
            delivery_ttl_min=5, public_base_url="")
        self.app = build_app(self.settings)

    def test_tools_registered(self):
        names = {tool.name for tool in asyncio.run(self.app.mcp.list_tools())}
        self.assertTrue(EXPECTED_TOOLS <= names,
                        f"缺少工具：{EXPECTED_TOOLS - names}")

    def test_registry_and_async_set_consistent(self):
        """agent 派发依赖 tool_registry；异步集合必须与注册表键一致。"""
        self.assertEqual(set(self.app.tool_registry), EXPECTED_TOOLS)
        self.assertTrue(ASYNC_TOOLS <= set(self.app.tool_registry))
        for name in ASYNC_TOOLS:
            self.assertIn(name, self.app.async_tools)

    def test_job_meta_never_contains_api_key(self):
        tool = self.app.tool_registry["printguard_run"]
        with mock.patch("printguard_aholo.pipeline.inputs.make_client",
                        return_value=object()), \
                mock.patch("printguard_aholo.service.tools.run_generate",
                           side_effect=RuntimeError("boom")):
            job = tool(height_mm=50, prompt="cube", api_key="secret-key-123")
            record = _drain(self.app.mcp_jobs, job["job_id"], timeout=5)
        self.assertEqual(record["status"], "failed")
        self.assertIn("RuntimeError", record["error"])
        # key 只作为调用参数下传，绝不进 meta / result（会经 check_job 回显）
        self.assertNotIn("secret-key-123", json.dumps(record["meta"]))
        self.assertNotIn("secret-key-123", json.dumps(record["result"] or {}))

    def test_unknown_job(self):
        result = self.app.tool_registry["printguard_check_job"]("nope")
        self.assertIn("error", result)


class PipelineToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name)
        self.app = build_app(Settings(runs_dir=self.runs))
        self.tools = self.app.tool_registry

    def test_printguard_run_success(self):
        run_dir = self.runs / "run_r1"
        run_dir.mkdir()
        (run_dir / "printable.3mf").write_bytes(b"3mf")

        def fake_generate(prompt=None, **kwargs):
            self.assertEqual(kwargs["height_mm"], 50.0)
            self.assertEqual(kwargs["out_dir"], str(self.runs))
            return {"ok": True, "run_id": "run_r1", "mode": "text_to_3d",
                    "artifacts": {"model": str(run_dir / "printable.3mf"),
                                  "model_format": "3mf"}}

        with mock.patch("printguard_aholo.pipeline.inputs.make_client",
                        return_value=object()), \
                mock.patch("printguard_aholo.service.tools.run_generate",
                           side_effect=fake_generate):
            job = self.tools["printguard_run"](height_mm=50, prompt="cube",
                                               api_key="secret-key")
            record = _drain(self.app.mcp_jobs, job["job_id"], timeout=5)

        result = record["result"]
        self.assertEqual(record["status"], "succeeded")
        self.assertTrue(result["ok"])
        # 限时交付：downloads + run 目录已删 + 本地路径不残留
        self.assertIn("downloads", result)
        self.assertTrue(result["downloads"]["model"]["url"].startswith("/d/"))
        self.assertFalse(run_dir.exists())
        self.assertNotIn("secret-key", json.dumps(record, ensure_ascii=False))

    def test_printguard_run_failure_marks_job_failed(self):
        with mock.patch("printguard_aholo.pipeline.inputs.make_client",
                        return_value=object()), \
                mock.patch("printguard_aholo.service.tools.run_generate",
                           side_effect=PipelineError("生成失败")):
            job = self.tools["printguard_run"](height_mm=50, prompt="cube")
            record = _drain(self.app.mcp_jobs, job["job_id"], timeout=5)

        self.assertEqual(record["status"], "failed")
        self.assertIn("PipelineError", record["error"])
        self.assertIn("生成失败", record["error"])

    def test_repair_defaults_true_and_passthrough(self):
        """默认 repair=True（自动修复）；显式 False 才跳过。"""
        captured = {}

        def fake_generate(prompt=None, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "artifacts": {}}

        with mock.patch("printguard_aholo.pipeline.inputs.make_client",
                        return_value=object()), \
                mock.patch("printguard_aholo.service.tools.run_generate",
                           side_effect=fake_generate):
            job = self.tools["printguard_run"](height_mm=50, prompt="cube")
            _drain(self.app.mcp_jobs, job["job_id"], timeout=5)
            self.assertTrue(captured["repair"])

            job = self.tools["printguard_run"](height_mm=50, prompt="cube",
                                               repair=False)
            _drain(self.app.mcp_jobs, job["job_id"], timeout=5)
        self.assertFalse(captured["repair"])
        # meta 不含 key，但 repair 记录可查（调用审计用）
        meta = self.app.mcp_jobs.get(job["job_id"])["meta"]
        self.assertFalse(meta["repair"])

    def test_repair_local_file_does_not_require_key(self):
        captured = {}

        def fake_repair(source, height_mm, **kwargs):
            captured.update(source=source, height_mm=height_mm, **kwargs)
            return {"ok": True, "artifacts": {"model_format": "3mf"}}

        with mock.patch("printguard_aholo.service.tools.run_repair",
                        side_effect=fake_repair):
            job = self.tools["printguard_repair"]("/tmp/model.stl", 60)
            record = _drain(self.app.mcp_jobs, job["job_id"], timeout=5)

        self.assertEqual(record["status"], "succeeded")
        self.assertIsNone(captured["client"])  # 本地文件不需要 API Key
        self.assertEqual(captured["height_mm"], 60)

    def test_repair_url_uses_client(self):
        captured = {}
        sentinel = object()

        def fake_repair(source, height_mm, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "artifacts": {}}

        # URL 输入的客户端来自 pipeline.inputs.make_client（本地路径不需要客户端）
        with mock.patch("printguard_aholo.pipeline.inputs.make_client",
                        return_value=sentinel), \
                mock.patch("printguard_aholo.service.tools.run_repair",
                           side_effect=fake_repair):
            job = self.tools["printguard_repair"]("https://example.com/a.glb", 60)
            _drain(self.app.mcp_jobs, job["job_id"], timeout=5)

        self.assertIs(captured["client"], sentinel)


if __name__ == "__main__":
    unittest.main()
