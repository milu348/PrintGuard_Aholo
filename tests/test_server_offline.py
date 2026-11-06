"""MCP 服务离线单测：工具注册 / 任务 / 产物下载（不发真实 API 请求）。

分层之后的装配：
- `app.mcp`  FastMCP 实例（server 导入 tools/web 时完成注册）
- `tools`    9 个 MCP 工具与 job 提交
- `web`      /healthz、/、/files 下载
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from printguard_aholo import server, tools, web
from printguard_aholo.pipeline import PipelineError

EXPECTED_TOOLS = {
    "lux3d_concept_image",
    "lux3d_four_view",
    "lux3d_text_to_3d",
    "lux3d_image_to_3d",
    "lux3d_get_task",
    "lux3d_list_tasks",
    "printguard_run",
    "printguard_repair",
    "printguard_check_job",
}


class ServerTests(unittest.TestCase):
    def test_tools_registered(self):
        names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
        self.assertTrue(EXPECTED_TOOLS <= names,
                        f"缺少工具：{EXPECTED_TOOLS - names}")

    def test_healthz(self):
        response = asyncio.run(web.healthz(None))
        body = json.loads(response.body)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["service"], "PrintGuard_Aholo")
        self.assertIn("printguard_check_job", body["tools"])
        self.assertIn("total", body["jobs"])
        self.assertIn("runs_dir", body)

    def test_unknown_job(self):
        result = tools.printguard_check_job("nope")
        self.assertIn("error", result)

    def test_invalid_input_fails_async(self):
        # 无 prompt 也无参考图：任务应在 worker 内快速失败（不触网）
        job = tools.lux3d_concept_image("")
        record = tools.JOBS.wait(job["job_id"], timeout=5)
        self.assertEqual(record["status"], "failed")
        self.assertIn("至少提供一个", record["error"])

    def test_job_meta_never_contains_api_key(self):
        job = tools.lux3d_concept_image("", api_key="secret-key-123")
        record = tools.JOBS.wait(job["job_id"], timeout=5)
        self.assertNotIn("secret-key-123", json.dumps(record, ensure_ascii=False))


class PipelineToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name)

    def test_printguard_run_success(self):
        def fake_pipeline(*args, **kwargs):
            self.assertEqual(kwargs["height_mm"], 50.0)
            self.assertEqual(kwargs["out_dir"], str(self.runs))
            return {"ok": True, "run_id": "r1", "mode": "text_to_3d",
                    "artifacts": {"model": str(self.runs / "r1" / "printable.3mf")}}

        with mock.patch.object(tools, "RUNS_DIR", self.runs), \
                mock.patch.object(tools, "make_client", return_value=object()), \
                mock.patch.object(tools, "run_pipeline", side_effect=fake_pipeline):
            job = tools.printguard_run(height_mm=50, prompt="cube",
                                       api_key="secret-key")
            record = tools.JOBS.wait(job["job_id"], timeout=5)

        self.assertEqual(record["status"], "succeeded")
        self.assertTrue(record["result"]["ok"])
        self.assertEqual(record["result"]["artifact_urls"]["model"],
                         "/files/r1/printable.3mf")
        self.assertNotIn("secret-key", json.dumps(record, ensure_ascii=False))

    def test_printguard_run_failure_marks_job_failed(self):
        with mock.patch.object(tools, "make_client", return_value=object()), \
                mock.patch.object(tools, "run_pipeline",
                                  side_effect=PipelineError("生成失败")):
            job = tools.printguard_run(height_mm=50, prompt="cube")
            record = tools.JOBS.wait(job["job_id"], timeout=5)

        self.assertEqual(record["status"], "failed")
        self.assertIn("PipelineError", record["error"])
        self.assertIn("生成失败", record["error"])

    def test_repair_local_file_does_not_require_key(self):
        captured = {}

        def fake_repair(source, height_mm, **kwargs):
            captured.update(source=source, height_mm=height_mm, **kwargs)
            return {"ok": True, "artifacts": {}}

        with mock.patch.object(tools, "run_repair", side_effect=fake_repair):
            job = tools.printguard_repair("/tmp/model.stl", 60)
            record = tools.JOBS.wait(job["job_id"], timeout=5)

        self.assertEqual(record["status"], "succeeded")
        self.assertIsNone(captured["client"])  # 本地文件不需要 API Key
        self.assertEqual(captured["height_mm"], 60)

    def test_repair_url_uses_client(self):
        captured = {}
        sentinel = object()

        def fake_repair(source, height_mm, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "artifacts": {}}

        with mock.patch.object(tools, "make_client", return_value=sentinel), \
                mock.patch.object(tools, "run_repair", side_effect=fake_repair):
            job = tools.printguard_repair("https://example.com/a.glb", 60)
            tools.JOBS.wait(job["job_id"], timeout=5)

        self.assertIs(captured["client"], sentinel)


class ArtifactUrlTests(unittest.TestCase):
    def test_within_runs_dir_gets_url(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td)
            with mock.patch.object(tools, "RUNS_DIR", runs), \
                    mock.patch.object(tools, "PUBLIC_BASE_URL",
                                     "https://x.ms.show"):
                summary = tools.with_artifact_urls({
                    "artifacts": {"model": str(runs / "r1" / "printable.3mf"),
                                  "note": "plain-text"}})
        self.assertEqual(summary["artifact_urls"]["model"],
                         "https://x.ms.show/files/r1/printable.3mf")
        self.assertNotIn("note", summary["artifact_urls"])  # 非路径值不转换

    def test_outside_runs_dir_not_exposed(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            with mock.patch.object(tools, "RUNS_DIR", runs):
                summary = tools.with_artifact_urls(
                    {"artifacts": {"model": "/etc/passwd"}})
        self.assertNotIn("artifact_urls", summary)


class FileRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name)
        self.run_dir = self.runs / "r1"
        self.run_dir.mkdir()
        self.file = self.run_dir / "printable.3mf"
        self.file.write_bytes(b"3mf")

    def _call(self, run_id, filename):
        request = SimpleNamespace(path_params={"run_id": run_id,
                                               "filename": filename})
        with mock.patch.object(web, "RUNS_DIR", self.runs):
            return asyncio.run(web.files(request))

    def test_serves_file_inside_run_dir(self):
        response = self._call("r1", "printable.3mf")
        self.assertEqual(Path(response.path), self.file.resolve())

    def test_rejects_parent_directory(self):
        response = self._call("..", "secret")
        self.assertEqual(response.status_code, 400)
        response = self._call("r1", "../secret")
        self.assertEqual(response.status_code, 400)

    def test_missing_file_returns_404(self):
        response = self._call("r1", "nope.3mf")
        self.assertEqual(response.status_code, 404)
        response = self._call("nope", "printable.3mf")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
