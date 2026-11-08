"""合成用例自测：不依赖网络与真实模型，验证检测/修复口径未回归。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import trimesh

from .inspect import analyze_mesh
from .meshio import load_mesh
from .repair import repair

def _sphere_with_holes(n_del: int = 40, seed: int = 7) -> trimesh.Trimesh:
    rng = np.random.default_rng(seed)
    s = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
    keep = np.ones(len(s.faces), bool)
    keep[rng.choice(len(s.faces), n_del, replace=False)] = False
    return trimesh.Trimesh(vertices=s.vertices, faces=s.faces[keep], process=False)


def _two_spheres() -> trimesh.Trimesh:
    a = _sphere_with_holes(n_del=30, seed=11)
    b = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
    b.apply_translation([8.0, 0.0, 0.0])
    return trimesh.util.concatenate([a, b])


def _overhang_t() -> trimesh.Trimesh:
    base = trimesh.creation.box(extents=[20, 20, 10])
    base.apply_translation([0, 0, 5])
    plate = trimesh.creation.box(extents=[40, 20, 2])
    plate.apply_translation([0, 0, 11])
    return trimesh.util.concatenate([base, plate])


def _thin_plate() -> trimesh.Trimesh:
    """0.5mm 薄板桌面 + 两侧支撑壁，板下悬空（壁厚 fail 用例）。
    注意：薄板叠在实心底座上不是薄壁（材料连续），必须下方留空气。"""
    wall_a = trimesh.creation.box(extents=[2, 20, 8])
    wall_a.apply_translation([-9, 0, 4])
    wall_b = trimesh.creation.box(extents=[2, 20, 8])
    wall_b.apply_translation([9, 0, 4])
    top = trimesh.creation.box(extents=[20, 20, 0.5])
    top.apply_translation([0, 0, 8.25])
    return trimesh.util.concatenate([wall_a, wall_b, top])


def _healthy_cube() -> trimesh.Trimesh:
    """15mm 实心立方（壁厚 pass 用例）"""
    c = trimesh.creation.box(extents=[15, 15, 15])
    c.apply_translation([0, 0, 7.5])
    return c


def _stacked_boxes() -> trimesh.Trimesh:
    """两层正方体上下叠放，中间 0.3mm 接触面（#4 间隙 fail 用例）"""
    base = trimesh.creation.box(extents=[20, 20, 5])
    base.apply_translation([0, 0, 2.5])
    a = trimesh.creation.box(extents=[9.85, 20, 8])
    a.apply_translation([-5.075, 0, 9])
    b = a.copy()
    b.apply_translation([10.15, 0, 0])
    return trimesh.util.concatenate([base, a, b])


def _roughness(m: trimesh.Trimesh) -> float:
    """>30° 折角占比（selftest 专用回归断言）。

    体素阶梯的指纹就是折角集中在 30-55°：未平滑时 0.17~0.23，Taubin 之后
    应 < 0.05。这个数只用于验证「平滑确实生效」，不作为对外体检项。
    """
    f = np.asarray(m.faces)
    if len(f) < 2:
        return 1.0
    tri = np.asarray(m.vertices, float)[f]
    nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    ln = np.linalg.norm(nrm, axis=1)
    ln[ln == 0] = 1.0
    nrm = nrm / ln[:, None]
    e = np.concatenate([
        np.sort(np.column_stack([f[:, 0], f[:, 1]]), axis=1),
        np.sort(np.column_stack([f[:, 1], f[:, 2]]), axis=1),
        np.sort(np.column_stack([f[:, 2], f[:, 0]]), axis=1)], axis=0)
    fi = np.tile(np.arange(len(f)), 3)
    order = np.lexsort((e[:, 1], e[:, 0]))
    es, fs = e[order], fi[order]
    newrun = np.ones(len(es), bool)
    newrun[1:] = np.any(es[1:] != es[:-1], axis=1)
    run_id = np.cumsum(newrun) - 1
    counts = np.bincount(run_id)
    starts = np.concatenate(([0], np.flatnonzero(newrun)[1:]))
    sel = counts == 2                      # 只有恰好 2 面共边才有二面角
    if not sel.any():
        return 1.0
    i1, i2 = starts[sel], starts[sel] + 1
    n1, n2 = nrm[fs[i1]], nrm[fs[i2]]
    ang = np.degrees(np.arccos(np.clip((n1 * n2).sum(axis=1), -1.0, 1.0)))
    return float((ang > 30).mean())


def run_selftest(outdir: str | Path | None = None) -> dict:
    """跑全部合成用例，返回结构化摘要（不打印、不退出）。

    outdir 为空时写到系统临时目录，避免在服务进程工作目录落文件。
    """
    if outdir is None:
        outdir = Path(tempfile.gettempdir()) / "printguard-selftest"
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)

    cases = {
        # name: (mesh, rebuild_expected, volume_range, wall_expected)
        "sphere_with_holes": (_sphere_with_holes(), True, (2700, 5700), None),
        "two_spheres": (_two_spheres(), True, (4500, 8200), None),
        "overhang_t": (_overhang_t(), False, (5300, 5900), None),
        "thin_plate": (_thin_plate(), False, (750, 1050), "warn"),
        "healthy_cube": (_healthy_cube(), False, (3040, 3710), "pass"),
        "stacked_boxes": (_stacked_boxes(), False, (4950, 5350), "pass"),
    }
    summary = {}
    all_ok = True
    for name, (mesh, rebuild_expected, vol_range, wall_exp) in cases.items():
        ext_before = sorted(float(x) for x in mesh.extents)
        m, steps, before, after = repair(mesh, do_orient=True)
        m.export(str(out / f"{name}.stl"))
        rebuilt = any(s.startswith("voxel_rebuild") for s in steps)
        vol_ok = vol_range[0] <= after["checks"]["watertight"]["volume_mm3"] <= vol_range[1]
        # 体素重构是有损的：只对走重构的用例校验外框尺寸（容忍 pitch 量级偏差）
        if rebuilt:
            ext_ok = np.allclose(sorted(float(x) for x in m.extents), ext_before,
                                 atol=max(ext_before) * 0.15)
        else:
            ext_ok = True
        wall_before = before["checks"]["wall_thickness"]["verdict"]
        wall_verdict = after["checks"]["wall_thickness"]["verdict"]
        wall_ok = (wall_exp is None) or (wall_verdict == wall_exp)
        if name == "thin_plate":  # 额外验证：检测到 fail 且被增厚修复
            wall_ok = (wall_ok and wall_before == "fail"
                       and any(s.startswith("wall_thicken") for s in steps))
        # 表面平整度回归：体素重构/增厚的产物必须已去阶梯
        # （>30° 折角占比阈值 0.05——未平滑时体素阶梯普遍在 0.17~0.23）
        voxel_touched = any(s.startswith("voxel_rebuild") or s.startswith("wall_thicken")
                            for s in steps)
        # 体素阶梯去除回归：走过体素流程的产物，>30° 折角占比必须 < 0.05
        rough_after = _roughness(m) if voxel_touched else None
        rough_ok = True if rough_after is None else rough_after < 0.05
        case_ok = (after["checks"]["watertight"]["watertight"]
                   and (rebuilt == rebuild_expected) and vol_ok and ext_ok
                   and wall_ok and rough_ok)
        if name == "stacked_boxes":
            # 验证：堆叠贴合面不再被误判为悬垂
            gap = before["checks"]["overhang"]
            ok = (gap.get("open_frac", 1.0) < 0.02
                  and gap.get("bridged_area_mm2", 0) >= 300)
            case_ok = case_ok and ok
        all_ok &= case_ok
        summary[name] = {
            "ok": case_ok,
            "steps": steps,
            "watertight": f"{before['checks']['watertight']['watertight']}->{after['checks']['watertight']['watertight']}",
            "overhang_frac": f"{before['checks']['overhang']['bad_frac']}->{after['checks']['overhang']['bad_frac']}",
            "volume_mm3_after": after["checks"]["watertight"]["volume_mm3"],
            "volume_ok": vol_ok,
            "extents_ok": bool(ext_ok),
            "wall": (f"{wall_verdict}(exp={wall_exp})"
                     if wall_exp else wall_verdict),
            "wall_before": wall_before,
            "min_wall_mm": after["checks"]["wall_thickness"].get("min_wall_mm"),
            "rough_gt30": (None if rough_after is None else round(rough_after, 4)),
            "rough_ok": bool(rough_ok),
        }
        (out / f"{name}.report.json").write_text(
            json.dumps({"before": before, "repair_steps": steps, "after": after},
                       ensure_ascii=False, indent=1),
            encoding="utf-8")

    # GLB 往返：场景加载 + 非水密检测
    cases["sphere_with_holes"][0].export(str(out / "roundtrip.glb"))
    rt = load_mesh(str(out / "roundtrip.glb"))
    glb_wt = analyze_mesh(rt)["checks"]["watertight"]["watertight"]
    all_ok &= (glb_wt is False)
    summary["glb_roundtrip"] = {"ok": glb_wt is False, "detected_watertight": glb_wt}

    # 大面片 STL 重载分析（MCP analyze 路径回归：voxelized 细分需足够 max_iter）
    big = load_mesh(str(out / "healthy_cube.stl"), 80)
    wall = analyze_mesh(big)["checks"]["wall_thickness"]
    big_ok = wall.get("verdict") == "pass"
    all_ok &= big_ok
    summary["big_facet_reload"] = {"ok": big_ok, "wall": wall.get("verdict"),
                                   "min_wall_mm": wall.get("min_wall_mm")}

    summary["ok"] = bool(all_ok)
    return summary


def selftest(outdir: str | Path | None = None) -> int:
    """CLI 兼容入口：打印摘要并返回退出码。"""
    summary = run_selftest(outdir)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("SELFTEST", "PASS" if summary["ok"] else "FAIL")
    return 0 if summary["ok"] else 1

