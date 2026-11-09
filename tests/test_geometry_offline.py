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

import trimesh

from printguard_aholo.core.inspect import analyze_mesh, watertight_stats
from printguard_aholo.core.meshio import load_mesh
from printguard_aholo.core.profiles import PROFILE, PrinterProfile, load_profile
from printguard_aholo.core.repair import repair, repair_island_clean
from printguard_aholo.core.selftest import (
    _healthy_cube,
    _roughness,
    _sphere_with_holes,
    _stacked_boxes,
    _thin_plate,
    _two_spheres,
    run_selftest,
)


class ProfileTests(unittest.TestCase):
    def test_single_builtin_profile(self):
        self.assertIs(load_profile(), PROFILE)
        self.assertIsInstance(PROFILE, PrinterProfile)
        self.assertEqual(PROFILE.min_wall_mm, 0.8)
        self.assertEqual(PROFILE.overhang_deg, 45.0)

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
        # 风险评级/孔洞/孤岛/平整度已从体检移除（报告字段，无消费者）
        for gone in ("risk", "holes", "islands", "surface_roughness"):
            self.assertNotIn(gone, report)
            self.assertNotIn(gone, report["checks"])

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
        self.assertLess(_roughness(fixed), 0.05)

    def test_thin_plate_is_thickened(self):
        mesh = _thin_plate()
        before = analyze_mesh(mesh)["checks"]["wall_thickness"]
        self.assertEqual(before["verdict"], "fail")
        _, steps, _, after = repair(mesh, do_orient=False)
        self.assertTrue(any(s.startswith("wall_thicken") for s in steps))
        self.assertNotEqual(after["checks"]["wall_thickness"]["verdict"], "fail")

    def test_proportionality_skips_tiny_thin_features(self):
        """比例原则：健康实体上一根 0.5mm 细针（fail 占比 <0.5%）
        只报告不改形——不为千分之几的薄弱点重建整个表面（奶龙教训）。"""
        from printguard_aholo.core.selftest import _thin_spike

        mesh = _thin_spike()
        n_faces, n_verts = len(mesh.faces), len(mesh.vertices)
        m2, steps, _, _ = repair(mesh, do_orient=False)
        self.assertTrue(any(s.startswith("wall_thicken_skipped") for s in steps),
                        f"应触发比例原则跳过，实际 steps={steps}")
        self.assertFalse(any(s.startswith("wall_thicken(") for s in steps))
        # 几何未被改动：面数/顶点数一致（没有体素重建，也没有增厚）
        self.assertEqual(len(m2.faces), n_faces)
        self.assertEqual(len(m2.vertices), n_verts)

    def test_fidelity_gate_skips_dense_soup_rebuild(self):
        """重建保真度门槛：细节尺度 ≈ 可达 pitch 的非水密模型，
        原样交付而不是毁容式重建（九尾妖狐教训）。"""
        from printguard_aholo.core.selftest import _dense_soup

        mesh = _dense_soup()
        n_faces = len(mesh.faces)
        m2, steps, _, _ = repair(mesh, do_orient=False)
        self.assertTrue(any(s.startswith("rebuild_skipped") for s in steps),
                        f"应触发保真度门槛，实际 steps={steps}")
        self.assertFalse(any(s.startswith("voxel_rebuild") for s in steps))
        self.assertFalse(any(s.startswith("wall_thicken(") for s in steps))
        # 原样交付：几何未被改动
        self.assertEqual(len(m2.faces), n_faces)

    def test_force_rebuild_overrides_fidelity_gate(self):
        """force_rebuild=True 显式越过保真度门槛（调用方自担后果）。"""
        from printguard_aholo.core.selftest import _dense_soup

        mesh = _dense_soup()
        m2, steps, _, _ = repair(mesh, do_orient=False, force_rebuild=True)
        self.assertTrue(any(s.startswith("voxel_rebuild") for s in steps))

    def test_island_clean_removes_small_fragment(self):
        """远离主体的微小孤岛（hull < island_min 且距离 > 3mm）= 飞件风险，删除。"""
        main = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
        debris = trimesh.creation.icosphere(subdivisions=2, radius=1.5)
        debris.apply_translation([30, 0, 0])   # 表面间隙 ~18.5mm，远离主体
        mesh = trimesh.util.concatenate([main, debris])
        cleaned, step = repair_island_clean(mesh, load_profile())
        self.assertIsNotNone(step)
        self.assertIn("island_clean", step)
        self.assertEqual(len(cleaned.split(only_watertight=False)), 1)

    def test_island_clean_keeps_nearby_micro_feature(self):
        """贴近主体的微小孤岛 = 细节特征（毛发/装饰），保留（阿狸秃头教训）。"""
        main = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
        tuft = trimesh.creation.icosphere(subdivisions=2, radius=1.5)
        tuft.apply_translation([11, 0, 0])     # 与主体表面相接（间隙 ~0）
        mesh = trimesh.util.concatenate([main, tuft])
        cleaned, step = repair_island_clean(mesh, load_profile())
        self.assertIsNone(step)                 # 贴近 → 不删
        self.assertEqual(len(cleaned.faces), len(mesh.faces))

    def test_island_clean_keeps_healthy_mesh(self):
        mesh = _healthy_cube()
        cleaned, step = repair_island_clean(mesh, load_profile())
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
