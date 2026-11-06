"""管线离线测试：端到端交付 / 生成路由 / 参数校验（不发真实网络请求）。"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from printguard_aholo import pipeline
from printguard_aholo.lux3d import TaskResult
from printguard_aholo.pipeline import run_generate, run_pipeline, run_repair
from printguard_aholo.selftest import _healthy_cube


class FakeClient:
    """只实现管线用到的客户端方法，记录调用参数。"""

    def __init__(self, raw_path: Path):
        self.raw_path = Path(raw_path)
        self.calls = []
        self.rehosted = []

    def text_to_3d(self, prompt, **kwargs):
        self.calls.append(("text_to_3d", prompt, kwargs))
        return 1

    def image_to_3d(self, **kwargs):
        self.calls.append(("image_to_3d", kwargs))
        return 2

    def rehost(self, url):
        self.rehosted.append(url)
        return "https://asset.example/rehosted.glb"

    def download(self, url, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x89PNG\r\n\x1a\n")
        return str(dest)

    def wait(self, task_id, on_progress=None):
        if on_progress:
            on_progress(task_id, 1, 0.1)
        return TaskResult(task_id=task_id, status=3,
                          outputs=["https://platform.example/raw.glb"],
                          elapsed_sec=0.1)

    def download_outputs(self, result, dest_dir, *, extensions=None, prefix=None):
        dest = Path(dest_dir) / f"{prefix or 'raw'}.glb"
        shutil.copy(self.raw_path, dest)
        return [str(dest)]


class PipelineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.raw = self.dir / "raw.glb"
        _healthy_cube().export(str(self.raw))


class RepairPipelineTests(PipelineTestBase):
    def test_local_repair_end_to_end(self):
        summary = run_repair(str(self.raw), 15, name="repair_e2e",
                             out_dir=str(self.dir / "runs"), fmt="3mf")
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["mode"], "repair")
        self.assertEqual(summary["run_id"], "repair_e2e")
        artifacts = summary["artifacts"]
        self.assertEqual(sorted(artifacts), ["model", "model_format"])
        self.assertTrue(Path(artifacts["model"]).is_file())
        self.assertTrue(artifacts["model"].endswith(".3mf"))
        self.assertGreater(Path(artifacts["model"]).stat().st_size, 0)
        # 交付物只有模型：没有渲染件、没有诊断报告、没有体检摘要
        run_dir = self.dir / "runs" / "repair_e2e"
        self.assertEqual(
            sorted(path.name for path in run_dir.iterdir()),
            ["printable.3mf"])
        for key in ("report_md", "report_json", "risk", "watertight",
                    "repair_steps", "render", "sheet", "gif"):
            self.assertNotIn(key, summary)

    def test_result_has_no_diagnostic_summary(self):
        summary = run_repair(str(self.raw), 15, name="repair_report",
                             out_dir=str(self.dir / "runs"), fmt="stl")
        self.assertEqual(
            sorted(summary),
            ["artifacts", "duration_sec", "mode", "ok", "run_id", "warnings"])
        self.assertFalse((self.dir / "runs" / "repair_report" / "report.md").exists())
        self.assertFalse((self.dir / "runs" / "repair_report" / "report.json").exists())

    def test_run_pipeline_source_dispatches_to_repair(self):
        summary = run_pipeline(height_mm=15, source=str(self.raw),
                               name="via_source", out_dir=str(self.dir / "runs"),
                               fmt="stl")
        self.assertEqual(summary["mode"], "repair")

    def test_unsafe_run_name_rejected(self):
        with self.assertRaises(ValueError):
            run_repair(str(self.raw), 15, name="../escape",
                       out_dir=str(self.dir / "runs"))

    def test_invalid_height_rejected(self):
        with self.assertRaises(ValueError):
            run_repair(str(self.raw), 0, out_dir=str(self.dir / "runs"))
        with self.assertRaises(ValueError):
            run_repair(str(self.raw), -5, out_dir=str(self.dir / "runs"))

    def test_missing_source_rejected(self):
        with self.assertRaises(pipeline.PipelineError):
            run_repair(str(self.dir / "nope.stl"), 15,
                       out_dir=str(self.dir / "runs"))


class GenerateRoutingTests(PipelineTestBase):
    def _run(self, **kwargs):
        client = FakeClient(self.raw)
        with mock.patch.object(pipeline, "_process",
                               return_value={"ok": True, "mode": "stub"}) as proc:
            summary = run_generate(height_mm=15, client=client, name="route",
                                   out_dir=str(self.dir / "runs"), **kwargs)
        return client, summary, proc

    def test_text_to_3d_with_reference_image(self):
        client, summary, proc = self._run(
            prompt="cube", image="https://example.com/ref.png")
        name, prompt, kwargs = client.calls[0]
        self.assertEqual(name, "text_to_3d")
        self.assertEqual(prompt, "cube")
        self.assertEqual(kwargs["reference_image"], "https://example.com/ref.png")
        self.assertEqual(proc.call_args.kwargs["mode"], "text_to_3d")
        extras = proc.call_args.kwargs["extra_artifacts"]
        self.assertIn("reference_image", extras)
        self.assertTrue(Path(extras["reference_image"]).is_file())

    def test_image_only_uses_image_to_3d(self):
        client, summary, proc = self._run(image="https://example.com/a.png")
        name, kwargs = client.calls[0]
        self.assertEqual(name, "image_to_3d")
        self.assertEqual(kwargs["image"], "https://example.com/a.png")
        self.assertNotIn("images", kwargs)
        self.assertEqual(proc.call_args.kwargs["mode"], "image_to_3d")
        extras = proc.call_args.kwargs["extra_artifacts"]
        self.assertIn("concept_image", extras)

    def test_local_concept_image_copied_to_run_dir(self):
        concept = self.dir / "concept.png"
        concept.write_bytes(b"\x89PNG\r\n\x1a\n")
        _, _, proc = self._run(image=str(concept))
        extras = proc.call_args.kwargs["extra_artifacts"]
        copied = Path(extras["concept_image"])
        self.assertTrue(copied.is_file())
        self.assertEqual(copied.name, "concept_image.png")
        self.assertNotEqual(copied, concept)  # 复制进 run 目录，不是引用原路径

    def test_multi_view_rehosts_signed_urls(self):
        client, _, proc = self._run(
            images=["https://platform.example/a.png?sign=abc",
                    "https://platform.example/b.png?sign=def"])
        self.assertEqual(len(client.rehosted), 2)
        name, kwargs = client.calls[0]
        self.assertEqual(name, "image_to_3d")
        self.assertEqual(kwargs["images"],
                         ["https://asset.example/rehosted.glb"] * 2)
        extras = proc.call_args.kwargs["extra_artifacts"]
        self.assertEqual(sorted(extras), ["four_view_01", "four_view_02"])

    def test_image_and_images_conflict(self):
        with self.assertRaises(ValueError):
            self._run(image="https://example.com/a.png",
                      images=["https://example.com/b.png"])

    def test_empty_input_rejected(self):
        with self.assertRaises(ValueError):
            self._run()

    def test_bad_quality_rejected(self):
        with self.assertRaises(ValueError):
            self._run(prompt="cube", quality="ultra")


if __name__ == "__main__":
    unittest.main()
