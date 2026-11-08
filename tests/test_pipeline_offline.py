"""管线离线测试：端到端交付 / 三种生成路由 / 参数校验（零网络）。

生成路由（官方 API 对应关系）：
- 只有 prompt            → text_to_3d
- prompt + 单图           → text_to_3d(img=…)（文图混合）
- prompt + 多图           → ValueError（平台不支持文 + 多图）
- 单图 / 多图（无 prompt）→ img_to_3d(image=… / images=…)
- 什么都没有              → ValueError
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from printguard_aholo.core.selftest import _healthy_cube
from printguard_aholo.lux3d import TaskResult
from printguard_aholo.pipeline import PipelineError, run_generate, run_repair
from printguard_aholo.pipeline import generate as generate_mod


class FakeClient:
    """只实现管线用到的客户端方法，记录调用参数。"""

    def __init__(self, raw_path: Path):
        self.raw_path = Path(raw_path)
        self.calls = []

    def text_to_3d(self, prompt, **kwargs):
        self.calls.append(("text_to_3d", prompt, kwargs))
        return 1

    def img_to_3d(self, *, version, face_count, image=None, images=None):
        self.calls.append(("img_to_3d", None,
                           {"version": version, "face_count": face_count,
                            "image": image, "images": images}))
        return 2

    def upload_image(self, data, filename="image.png"):
        self.calls.append(("upload_image", filename, len(data)))
        return f"https://asset.example/{filename}"

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
        self.assertFalse(
            (self.dir / "runs" / "repair_report" / "report.md").exists())
        self.assertFalse(
            (self.dir / "runs" / "repair_report" / "report.json").exists())

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
        with self.assertRaises(PipelineError):
            run_repair(str(self.dir / "nope.stl"), 15,
                       out_dir=str(self.dir / "runs"))


class GenerationRoutingTests(PipelineTestBase):
    """三种生成模式的输入路由（FakeClient 记录平台调用）。"""

    def setUp(self):
        super().setUp()
        self.client = FakeClient(self.raw)
        # 生成路由测试只关心平台调用，网格处理直接短路
        patcher = mock.patch.object(generate_mod, "load_mesh",
                                    return_value="MESH"), \
            mock.patch.object(generate_mod, "process_mesh",
                              side_effect=lambda mesh, **kw: {
                                  "ok": True, "mode": kw["mode"]})
        for p in patcher:
            p.start()
            self.addCleanup(p.stop)

    def test_prompt_only_routes_to_text_to_3d(self):
        summary = run_generate("cube", height_mm=15, client=self.client,
                               name="gen", out_dir=str(self.dir / "runs"))
        name, prompt, kwargs = self.client.calls[0]
        self.assertEqual(name, "text_to_3d")
        self.assertEqual(prompt, "cube")
        self.assertNotIn("img", kwargs)
        self.assertEqual(summary["mode"], "text_to_3d")

    def test_prompt_plus_one_image_is_mixed(self):
        summary = run_generate("cube", height_mm=15,
                               image="https://cdn.example/ref.jpg",
                               client=self.client, name="gen",
                               out_dir=str(self.dir / "runs"))
        name, prompt, kwargs = self.client.calls[0]
        self.assertEqual(name, "text_to_3d")
        self.assertEqual(kwargs["img"], "https://cdn.example/ref.jpg")
        self.assertEqual(summary["mode"], "mixed_to_3d")

    def test_prompt_plus_multi_image_rejected(self):
        with self.assertRaises(ValueError):
            run_generate("cube", height_mm=15,
                         images=["https://a/1.jpg", "https://a/2.jpg"],
                         client=self.client, out_dir=str(self.dir / "runs"))

    def test_single_image_routes_to_img_to_3d(self):
        summary = run_generate(height_mm=15,
                               image="https://cdn.example/ref.jpg",
                               client=self.client, name="gen",
                               out_dir=str(self.dir / "runs"))
        name, _, kwargs = self.client.calls[0]
        self.assertEqual(name, "img_to_3d")
        self.assertEqual(kwargs["image"], "https://cdn.example/ref.jpg")
        self.assertIsNone(kwargs["images"])
        self.assertEqual(summary["mode"], "image_to_3d")

    def test_multi_image_routes_to_imgs(self):
        run_generate(height_mm=15,
                     images=["https://a/1.jpg", "https://a/2.jpg",
                             "https://a/3.jpg"],
                     client=self.client, out_dir=str(self.dir / "runs"))
        name, _, kwargs = self.client.calls[0]
        self.assertEqual(name, "img_to_3d")
        self.assertEqual(kwargs["images"],
                         ["https://a/1.jpg", "https://a/2.jpg",
                          "https://a/3.jpg"])

    def test_data_uri_uploads_to_asset(self):
        run_generate("cube", height_mm=15,
                     image="data:image/png;base64,AAAA",
                     client=self.client, out_dir=str(self.dir / "runs"))
        uploads = [c for c in self.client.calls if c[0] == "upload_image"]
        self.assertEqual(len(uploads), 1)
        name, prompt, kwargs = self.client.calls[-1]
        self.assertEqual(name, "text_to_3d")
        self.assertEqual(kwargs["img"], "https://asset.example/input.png")

    def test_no_input_rejected(self):
        with self.assertRaises(ValueError):
            run_generate(height_mm=15, client=self.client,
                         out_dir=str(self.dir / "runs"))

    def test_bad_quality_rejected(self):
        with self.assertRaises(ValueError):
            run_generate("cube", height_mm=15, quality="ultra",
                         client=self.client, out_dir=str(self.dir / "runs"))


class AutoRepairContractTests(PipelineTestBase):
    """产品契约：生成后**默认自动体检+修复**；唯一不修复路径是显式 repair=False。"""

    def test_default_repairs_end_to_end(self):
        """默认参数（不传 repair）走完整修复链并交付。"""
        summary = run_generate("cube", height_mm=15, client=FakeClient(self.raw),
                               name="gen_default", out_dir=str(self.dir / "runs"))
        self.assertTrue(summary["ok"])
        self.assertTrue(Path(summary["artifacts"]["model"]).is_file())
        # 默认路径不带「跳过修复」提示
        self.assertFalse(any("跳过修复" in w for w in summary["warnings"]))

    def test_repair_false_skips_chain_with_warning(self):
        """显式 repair=False：跳过整条修复链，warnings 带未修复提示。"""
        summary = run_generate("cube", height_mm=15, repair=False,
                               client=FakeClient(self.raw),
                               name="gen_raw", out_dir=str(self.dir / "runs"))
        self.assertTrue(summary["ok"])
        self.assertTrue(Path(summary["artifacts"]["model"]).is_file())
        self.assertTrue(any("跳过修复" in w for w in summary["warnings"]))

    def test_repair_false_still_scales_to_height(self):
        """跳过修复不等于跳过尺寸契约：模型仍按 height_mm 缩放。"""
        run_generate("cube", height_mm=40, repair=False,
                     client=FakeClient(self.raw),
                     name="gen_raw40", out_dir=str(self.dir / "runs"),
                     fmt="stl")
        import trimesh
        mesh = trimesh.load(str(self.dir / "runs" / "gen_raw40" / "printable.stl"))
        self.assertAlmostEqual(float(max(mesh.extents)), 40.0, places=1)


if __name__ == "__main__":
    unittest.main()
