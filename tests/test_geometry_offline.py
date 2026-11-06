"""几何内核离线测试：检测口径 / 修复闭环 / profile / 网格 I/O。

合成用例来自 selftest（与 CLI `selftest` 同源），不依赖网络与真实模型。
完整 8 用例回归用 `PRINTGUARD_SLOW_TESTS=1` 启用，或直接跑
`python -m printguard_aholo.cli selftest`（约 90 秒）。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from printguard_aholo.inspect import analyze_mesh, watertight_stats
from printguard_aholo.meshio import load_mesh
from printguard_aholo.profiles import (
    BUILTIN_PROFILES,
    PrinterProfile,
    load_profile,
)
from printguard_aholo.repair import repair, repair_island_clean
from printguard_aholo.selftest import (
    _healthy_cube,
    _sphere_with_holes,
    _stacked_boxes,
    _thin_plate,
    _two_spheres,
    run_selftest,
)


class ProfileTests(unittest.TestCase):
    def test_builtin_profiles(self):
        self.assertEqual(load_profile(None).name, "generic-0.4")
        self.assertEqual(load_profile("mini-0.2").min_wall_mm, 0.4)
        self.assertIn("generic-0.4", BUILTIN_PROFILES)

    def test_unknown_profile_raises(self):
        with self.assertRaises(ValueError):
            load_profile("no-such-profile")

    def test_custom_profile_json_overrides_fields(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "profile.json"
            path.write_text(json.dumps({"name": "mini-0.2", "min_wall_mm": 0.6}),
                            encoding="utf-8")
            profile = load_profile(str(path))
        self.assertEqual(profile.name, "mini-0.2")
        self.assertEqual(profile.min_wall_mm, 0.6)
        self.assertEqual(profile.nozzle_mm, 0.2)  # 未覆盖字段回退内置同名片


class MeshIOTests(unittest.TestCase):
    def test_load_mesh_scales_to_height_and_sits_on_bed(self):
        cube = _healthy_cube()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cube.stl"
            cube.export(str(path))
            loaded = load_mesh(str(path), height_mm=30)
        self.assertAlmostEqual(float(max(loaded.extents)), 30.0, places=3)
        self.assertAlmostEqual(float(loaded.bounds[0].min()), 0.0, places=3)

    def test_empty_mesh_raises(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "empty.stl"
            # 合法但 0 三角面的二进制 STL（80 字节头 + uint32 计数 0）
            path.write_bytes(b"\0" * 80 + (0).to_bytes(4, "little"))
            with self.assertRaises(ValueError):
                load_mesh(str(path))


class InspectTests(unittest.TestCase):
    def test_watertight_detects_holes(self):
        stats = watertight_stats(_sphere_with_holes())
        self.assertFalse(stats["watertight"])
        self.assertGreater(stats["boundary_edges"], 0)
        self.assertIsNone(stats["volume_mm3"])

    def test_healthy_cube_passes(self):
        report = analyze_mesh(_healthy_cube())
        self.assertTrue(report["checks"]["watertight"]["watertight"])
        self.assertEqual(report["checks"]["wall_thickness"]["verdict"], "pass")
        self.assertEqual(report["checks"]["overhang"]["bad_frac"], 0.0)
        self.assertIn(report["risk"]["grade"], ("S", "A"))

    def test_stacked_boxes_not_misjudged_as_floating(self):
        # 堆叠贴合面是「桥接」而不是真悬空：修复了开发日志里挂很久的误报
        overhang = analyze_mesh(_stacked_boxes())["checks"]["overhang"]
        self.assertLess(overhang["open_frac"], 0.02)
        self.assertGreaterEqual(overhang["bridged_area_mm2"], 300)

    def test_non_watertight_skips_voxel_checks(self):
        report = analyze_mesh(_sphere_with_holes())
        self.assertEqual(report["checks"]["wall_thickness"]["verdict"], "skipped")
        self.assertIsNone(report["checks"]["overhang"].get("open_frac"))


class RepairTests(unittest.TestCase):
    def test_voxel_rebuild_makes_mesh_watertight_and_smooth(self):
        mesh = _sphere_with_holes()
        fixed, steps, before, after = repair(mesh, do_orient=False, fix_wall=False)
        self.assertTrue(after["checks"]["watertight"]["watertight"])
        self.assertTrue(any(s.startswith("voxel_rebuild") for s in steps))
        self.assertIn("smooth_taubin(去阶梯)", steps)
        # 体素阶梯去除回归：>30° 折角占比必须低于 0.05（未平滑时 0.17~0.23）
        self.assertLess(after["checks"]["surface_roughness"]["frac_gt30"], 0.05)

    def test_thin_plate_is_thickened(self):
        mesh = _thin_plate()
        before = analyze_mesh(mesh)["checks"]["wall_thickness"]
        self.assertEqual(before["verdict"], "fail")
        _, steps, _, after = repair(mesh, do_orient=False)
        self.assertTrue(any(s.startswith("wall_thicken") for s in steps))
        self.assertNotEqual(after["checks"]["wall_thickness"]["verdict"], "fail")

    def test_island_clean_removes_small_fragment(self):
        mesh = _two_spheres()
        strict = PrinterProfile(island_min_mm3=1e9)  # 除主域外全部视为碎片
        cleaned, step = repair_island_clean(mesh, strict)
        self.assertIsNotNone(step)
        self.assertIn("island_clean", step)
        self.assertEqual(len(cleaned.split(only_watertight=False)), 1)

    def test_island_clean_keeps_healthy_mesh(self):
        mesh = _healthy_cube()
        cleaned, step = repair_island_clean(mesh, load_profile(None))
        self.assertIsNone(step)
        self.assertIs(cleaned, mesh)


@unittest.skipUnless(os.environ.get("PRINTGUARD_SLOW_TESTS") == "1",
                     "完整 selftest 约 90s；设 PRINTGUARD_SLOW_TESTS=1 启用")
class FullSelftestTests(unittest.TestCase):
    def test_all_eight_cases_pass(self):
        summary = run_selftest()
        self.assertTrue(summary["ok"], json.dumps(summary, ensure_ascii=False))
        for name in ("sphere_with_holes", "two_spheres", "overhang_t",
                     "thin_plate", "healthy_cube", "stacked_boxes",
                     "glb_roundtrip", "big_facet_reload"):
            self.assertTrue(summary[name]["ok"], name)


if __name__ == "__main__":
    unittest.main()
