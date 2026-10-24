#!/usr/bin/env python3
"""PrintGuard v0.1 —— 最简版 CLI：analyze / repair / selftest

检测（P0）：水密性、悬垂（45°，贴床面豁免）、最小壁厚（体素 EDT 中轴法）
修复：体素重构（封小洞 + 实心化 + marching cubes）+ 自动摆向（悬垂最小化）
交付：STL + JSON 报告（before/after 指标）

已知局限：
- 悬垂指标为几何法（未做向下射线支撑检查），堆叠贴合面会误报为悬垂；
- 修复体素重构是有损近似（分辨率 = pitch），细节会被磨平；
- 壁厚为体素量化估计（T=2D−p，round 量化补偿），fail/warn/pass 三档；
- 切片分析器（小孔/桥接/孤岛）为后续范围；渲染交付在 pipeline（诊断图 + GIF）。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import trimesh

OVERHANG_COS = math.cos(math.radians(45.0))
BED_SLAB = 0.3  # mm：视为贴床的底部厚度
MIN_WALL = 0.8  # mm：最小壁厚阈值（2×0.4 喷嘴）


# ---------- 基础 ----------

def load_mesh(path: str, height_mm: float | None = None) -> trimesh.Trimesh:
    m = trimesh.load(path, force="mesh")
    if height_mm:
        m.apply_scale(float(height_mm) / float(max(m.extents)))
    m.apply_translation(-m.bounds[0])
    return m


def overhang_mask(m: trimesh.Trimesh):
    """悬垂/贴床逐面掩码（几何法）：悬垂=法线朝下超 45° 且非贴床薄层。"""
    nz = m.face_normals[:, 2]
    fz = m.triangles[:, :, 2].max(axis=1)
    zmin = float(m.bounds[0][2])
    on_bed = (fz - zmin) <= BED_SLAB
    return (nz < -OVERHANG_COS) & ~on_bed, (nz < -0.9) & on_bed


def overhang_stats(m: trimesh.Trimesh):
    """几何法悬垂统计（基于 overhang_mask）。"""
    area = m.area_faces
    down, bed = overhang_mask(m)
    bad_area = float(area[down].sum())
    total = float(area.sum())
    return bad_area, (bad_area / total if total else 0.0), float(area[bed].sum())


def watertight_stats(m: trimesh.Trimesh) -> dict:
    if len(m.faces) == 0:
        return dict(watertight=False, winding=False, boundary_edges=0,
                    nonmanifold_edges=0, degenerate_faces=0, volume_mm3=None)
    u, c = np.unique(m.edges_sorted, axis=0, return_counts=True)
    return dict(
        watertight=bool(m.is_watertight),
        winding=bool(m.is_winding_consistent),
        boundary_edges=int((c == 1).sum()),
        nonmanifold_edges=int((c > 2).sum()),
        degenerate_faces=int((m.area_faces <= 1e-12).sum()),
        volume_mm3=round(float(m.volume), 1) if m.is_watertight else None,
    )


def risk_grade(wt: bool, oh_frac: float, nonmanifold: int, degen: int,
               wall: dict | None = None):
    score = 100.0
    if not wt:
        score -= 35
    if nonmanifold:
        score -= 10
    if degen:
        score -= 5
    score -= min(30.0, oh_frac * 200.0)
    v = (wall or {}).get("verdict")
    if v == "fail":
        score -= 20
    elif v == "warn":
        score -= 5
    score = max(0.0, score)
    g = "S" if score >= 90 else "A" if score >= 75 else "B" if score >= 60 else "C" if score >= 40 else "D"
    return g, round(score, 1)


def analyze_mesh(m: trimesh.Trimesh, thickness: bool | None = None) -> dict:
    wt = watertight_stats(m)
    bad_area, oh_frac, bed_area = overhang_stats(m)
    if thickness is None:
        # 非水密（壳汤/破面）时体素壁厚无意义，自动跳过
        thickness = bool(wt["watertight"])
    if thickness:
        wall = wall_thickness(m)
    else:
        wall = {"verdict": "skipped", "reason": "非水密，体素壁厚无意义（先修复）"}
    grade, score = risk_grade(wt["watertight"], oh_frac,
                              wt["nonmanifold_edges"], wt["degenerate_faces"], wall)
    return {
        "bbox_mm": [round(float(x), 2) for x in m.extents],
        "checks": {
            "watertight": wt,
            "overhang": {
                "threshold_deg": 45,
                "bad_area_mm2": round(bad_area, 1),
                "bad_frac": round(oh_frac, 4),
                "bed_contact_mm2": round(bed_area, 1),
            },
            "wall_thickness": wall,
        },
        "risk": {"grade": grade, "score": score},
    }


# ---------- 修复 ----------

def _solid_grid(m: trimesh.Trimesh, pitch: float) -> np.ndarray:
    """体素实心化：拓扑填洞 -> closing（自适应半径）-> fill_holes。
    检验标准：实心体积 >= 35% 凸包体积，否则加大 closing 半径重试，
    防止"洞没封住 -> 实心化失败 -> 只产出薄壳"的静默失败。"""
    from scipy import ndimage
    w = m.copy()
    w.process(validate=True)
    try:
        trimesh.repair.fill_holes(w)  # 先填单三角/四边形洞
    except Exception:
        pass
    # 大面片网格（如低多边形盒子）细分到细 pitch 需 >10 轮，默认 max_iter 会 "max_iter exceeded"
    g = w.voxelized(pitch, max_iter=64).matrix.astype(bool)
    pad = 8  # 覆盖最大 closing 半径，防止贴边腐蚀
    g = np.pad(g, pad)
    st1 = ndimage.generate_binary_structure(3, 1)
    hull_vol = max(float(w.convex_hull.volume), 1e-6)
    gc = g
    for it in (2, 4, 6):
        st = ndimage.iterate_structure(st1, it)
        gc = ndimage.binary_closing(g, structure=st)
        gc = ndimage.binary_fill_holes(gc)
        if float(gc.sum()) * pitch**3 >= 0.35 * hull_vol:
            break  # 已实心化
    return gc[pad:-pad, pad:-pad, pad:-pad]


def repair_voxel(m: trimesh.Trimesh, pitch: float | None = None):
    """体素重构修复：输出恒为水密实体；有损（细节磨平到 pitch 量级）。"""
    if pitch is None:
        pitch = min(2.0, max(0.4, float(m.extents.max()) / 160.0))
    g = _solid_grid(m, pitch)
    r = trimesh.voxel.ops.matrix_to_marching_cubes(g, pitch=pitch)
    r.process(validate=True)
    trimesh.repair.fix_normals(r)
    r.apply_translation(-r.bounds[0])
    return r, pitch


def wall_thickness(m: trimesh.Trimesh, pitch: float | None = None) -> dict:
    """最小壁厚估计：体素 EDT -> 中轴点（距离局部极大）-> T = 2D − pitch。
    注意：trimesh 体素索引为 round 量化，表面各外扩约半个体素，薄特征厚度被
    高估约 1 个 pitch，故用 −p 补偿（T 偏保守/低估）。判定：
    fail（T ≤ 0.8−p）/ warn（T < 0.8+2p，亚分辨率带，建议复核）/ pass。
    pitch 上限 0.2：更粗时亚 0.8mm 薄壁会量化漏检。"""
    from scipy import ndimage
    ext = float(max(m.extents))
    if pitch is None:
        pitch = max(0.12, min(0.2, ext / 400.0))
    while (ext / pitch) ** 3 > 1.5e8 and pitch < 0.6:  # 内存护栏
        pitch *= 1.35
    g = _solid_grid(m, pitch)
    if not g.any():
        return {"verdict": "skipped", "reason": "体素化后为空", "pitch_mm": round(pitch, 2)}
    gp = np.pad(g, 2)
    d = ndimage.distance_transform_edt(gp, sampling=pitch)
    d = d.astype(np.float32)[2:-2, 2:-2, 2:-2]
    maxf = ndimage.maximum_filter(d, size=3, mode="constant")
    medial = (d >= maxf - 1e-5) & (d >= pitch - 1e-5) & g
    if not medial.any():
        return {"verdict": "skipped", "reason": "未找到中轴点（网格过细）",
                "pitch_mm": round(pitch, 2)}
    vals = d[medial]
    t_min = float(2 * float(vals.min()) - pitch)
    thin = medial & (d <= vals.min() + 1e-5)
    spots = (np.argwhere(thin)[:3] * pitch).round(1).tolist()
    verdict = ("fail" if t_min <= MIN_WALL - pitch
               else "warn" if t_min < MIN_WALL + 2 * pitch else "pass")
    return {
        "verdict": verdict,
        "min_wall_mm": round(t_min, 2),
        "threshold_mm": MIN_WALL,
        "pitch_mm": round(pitch, 2),
        "thin_spots_mm": spots,
        "method": "voxel EDT medial, T=2D-p（round 量化补偿，量化 ±2*pitch）",
    }


def _manhattan_ball(n: int) -> np.ndarray | None:
    """曼哈顿距离 ≤ n 的 3x3x(2n+1) 球形膨胀结构（n=0 返回 None 表示不膨胀）。"""
    if n <= 0:
        return None
    c = n
    idx = np.arange(2 * n + 1)
    dist = (np.abs(idx - c)[:, None, None] + np.abs(idx - c)[None, :, None]
            + np.abs(idx - c)[None, None, :])
    return dist <= n


def repair_thicken(m: trimesh.Trimesh, max_grow: int = 4, iters: int = 3):
    """壁厚修复 v2（体素域闭环收敛，尽力而为）：探测 fail 带中轴 -> 膨胀回贴 -> 再探测，
    最多 iters 轮；目标 T ≥ 0.8 + 2*pitch（安全裕量，补偿 marching-cubes 往返量化
    ——v1 一次性开环膨胀在细 pitch 下会残留 fail 边缘特征，复检仍 fail，实测教训）。
    总膨胀 > max_grow 判"不可修，建议重新设计"。
    代价：薄壁区域轮廓外扩约 total_r*pitch（厚壁区域不动）。
    返回 (mesh|None, step 描述|None)；mesh=None 且 step 给出时为无可修项/不可修。"""
    from scipy import ndimage
    ext = float(max(m.extents))
    pitch = max(0.12, min(0.2, ext / 400.0))
    while (ext / pitch) ** 3 > 1.5e8 and pitch < 0.6:  # 内存护栏
        pitch *= 1.35
    g = _solid_grid(m, pitch)
    if not g.any():
        return None, None

    def probe(grid):
        gp = np.pad(grid, max_grow + 2)
        d = ndimage.distance_transform_edt(gp, sampling=pitch).astype(np.float32)
        maxf = ndimage.maximum_filter(d, size=3, mode="constant")
        t = 2 * d - pitch
        medial = (d >= maxf - 1e-5) & (d >= pitch - 1e-5) & gp
        fail = medial & (t <= MIN_WALL - pitch)
        return gp, d, t, medial, fail

    gp, d, t, medial, fail = probe(g)
    if not fail.any():
        return None, None
    t0 = float(2 * float(d[fail].min()) - pitch)
    n_fail0 = int(fail.sum())
    total_r = 0
    residual = None
    for i in range(iters):
        t_min = float(2 * float(d[fail].min()) - pitch)
        r_need = int(np.ceil((MIN_WALL + 2 * pitch - t_min) / (2 * pitch)))
        if total_r + r_need > max_grow:
            residual = f"预算>{max_grow}voxel"
            break
        ball = _manhattan_ball(r_need)
        dil = ndimage.binary_dilation(gp, structure=ball) if ball is not None else gp
        zone = ndimage.binary_dilation(fail, structure=_manhattan_ball(r_need + 2))
        g = (gp | (dil & zone))[max_grow + 2:-(max_grow + 2),
                                max_grow + 2:-(max_grow + 2),
                                max_grow + 2:-(max_grow + 2)]
        total_r += r_need
        gp, d, t, medial, fail = probe(g)
        if not fail.any():
            residual = None
            break
        residual = f"残余fail中轴{int(fail.sum())}体素"
    if total_r == 0:
        return None, (f"wall_unrepairable(min~{t0:.2f}mm, 建议重新设计)")
    r2 = trimesh.voxel.ops.matrix_to_marching_cubes(g, pitch=pitch)
    r2.process(validate=True)
    trimesh.repair.fix_normals(r2)
    r2.apply_translation(-r2.bounds[0])
    tail = ("" if residual is None else f"({residual}, 发丝级细节建议重设计或接受)")
    return r2, (f"wall_thicken(+{total_r}voxel={total_r * pitch:.2f}mm/side, "
                f"fail中轴 {n_fail0}->{int(fail.sum())} 体素{tail})")


def _orient_candidates(m: trimesh.Trimesh, k: int = 12):
    cands = []
    # 6 个轴对齐摆放（绕 Z 的旋转不改变悬垂，无需 24 个全枚举）
    for ax in range(3):
        for sign in (1.0, -1.0):
            v = np.zeros(3)
            v[ax] = sign
            cands.append(trimesh.geometry.align_vectors(v, [0.0, 0.0, -1.0]))
    # 凸包大面积面朝下（稳定姿态）
    hull = m.convex_hull
    if len(hull.faces):
        order = np.argsort(hull.area_faces)[::-1][:k]
        for i in order:
            n = hull.face_normals[i]
            if abs(float(n[2])) > 0.999:
                continue
            cands.append(trimesh.geometry.align_vectors(n, [0.0, 0.0, -1.0]))
    return cands


def orient(m: trimesh.Trimesh) -> trimesh.Trimesh:
    """在候选摆放中选（悬垂占比最小，其次贴床面积最大）。"""
    best = None
    for T in _orient_candidates(m):
        t = m.copy()
        t.apply_transform(T)
        t.apply_translation(-t.bounds[0])
        _, bad_frac, bed_area = overhang_stats(t)
        key = (round(bad_frac, 4), -round(bed_area, 1))
        if best is None or key < best[0]:
            best = (key, t)
    return best[1]


def repair(m: trimesh.Trimesh, do_orient: bool = True, pitch: float | None = None,
           fix_wall: bool = True, height_mm: float | None = None):
    """检测+修复。height_mm 给定时，摆向后把最大边重缩放到该值
    （尺寸契约锁定在交付姿态——摆向会改变轴对齐包围盒）。"""
    steps = []
    before = analyze_mesh(m)
    wt = before["checks"]["watertight"]
    if (not wt["watertight"]) or wt["nonmanifold_edges"] > 0:
        m, p = repair_voxel(m, pitch)
        steps.append(f"voxel_rebuild(pitch={p:.2f}mm)")
    else:
        steps.append("watertight_ok(skip_rebuild)")
    if fix_wall:
        m2, wall_step = repair_thicken(m)
        if wall_step:
            if m2 is not None:
                m = m2
            steps.append(wall_step)
    if do_orient:
        m = orient(m)
        steps.append("orient(overhang_min)")
    if height_mm:
        s = float(height_mm) / float(max(m.extents))
        if abs(s - 1.0) > 1e-3:
            m.apply_scale(s)
            steps.append(f"scale_to({height_mm:g}mm)")
    after = analyze_mesh(m)
    return m, steps, before, after


# ---------- 渲染 ----------

def render_diagnostic_sheet(m: trimesh.Trimesh, report: dict, steps, out_png: str,
                            max_faces: int = 8000) -> str:
    """诊断报告单图：左=缺陷着色 3D，右=指标面板（替代 HTML 报告的整合交付）。"""
    import os
    import matplotlib
    matplotlib.use("Agg")
    cache = Path(".mplcache")
    cache.mkdir(exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache.resolve()))
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False

    r = m
    if len(r.faces) > max_faces:
        try:
            r = r.simplify_quadric_decimation(face_count=max_faces)
        except Exception:
            pass
    down, bed = overhang_mask(r)
    # Lambert 头灯着色：按面法线与相机方向的夹角调亮度（否则统一灰色无立体感）
    elev = np.radians(22); azim = np.radians(-55)
    Ldir = np.array([np.cos(elev) * np.cos(azim), np.cos(elev) * np.sin(azim), np.sin(elev)])
    shade = (0.45 + 0.55 * np.clip(r.face_normals @ Ldir, 0.0, 1.0))[:, None]
    colors = np.full((len(r.faces), 3), 0.72)
    colors[bed] = (0.29, 0.50, 0.71)
    colors[down] = (0.85, 0.20, 0.20)
    colors = np.clip(colors * shade, 0, 1)

    fig = plt.figure(figsize=(10, 5.6), dpi=140)
    ax = fig.add_subplot(1, 2, 1, projection="3d")
    ax.add_collection3d(Poly3DCollection(r.triangles, facecolors=colors, edgecolors="none"))
    b0, b1 = r.bounds
    c = (b0 + b1) / 2
    s = float((b1 - b0).max()) / 2 * 1.05
    ax.set_xlim(c[0] - s, c[0] + s); ax.set_ylim(c[1] - s, c[1] + s); ax.set_zlim(c[2] - s, c[2] + s)
    ax.set_box_aspect((1, 1, 1)); ax.view_init(elev=22, azim=-55); ax.set_axis_off()
    checks = report.get("checks") or {}
    wall = checks.get("wall_thickness") or {}
    spots = wall.get("thin_spots_mm") or []
    if spots and wall.get("verdict") in ("fail", "warn"):
        sp = np.asarray(spots, float)
        ax.scatter(sp[:, 0], sp[:, 1], sp[:, 2], c="red", s=70, marker="D", depthshade=False)

    wt = checks.get("watertight") or {}
    oh = checks.get("overhang") or {}
    risk = report.get("risk") or {}
    lines = [
        "PrintGuard 诊断报告",
        f"风险: {risk.get('grade', '-')} ({risk.get('score', '-')}/100)",
        "",
        f"水密性: {'通过' if wt.get('watertight') else '未通过'}",
        f"   边界边 {wt.get('boundary_edges', '-')} / 非流形边 {wt.get('nonmanifold_edges', '-')}",
        f"悬垂(>45°): {oh.get('bad_frac', 0) * 100:.1f}%",
        f"   问题面积 {oh.get('bad_area_mm2', '-')} mm²",
        f"最小壁厚(>=0.8mm): {wall.get('min_wall_mm', '-')} mm [{wall.get('verdict', '-')}]",
        "",
        "修复步骤:",
    ]
    lines += [f"  {i + 1}. {s}" for i, s in enumerate(steps or [])]
    lines += ["", "灰=正常  蓝=贴床  红=悬垂  ◆=最薄壁"]
    ax2 = fig.add_subplot(1, 2, 2)
    ax2.set_axis_off()
    ax2.text(0.02, 1.0, "\n".join(lines), va="top", fontsize=11)
    fig.tight_layout()
    fig.savefig(out_png, bbox_inches="tight")
    plt.close(fig)
    return out_png


def render_turntable_gif(m: trimesh.Trimesh, out_gif: str, frames: int = 16,
                         max_faces: int = 6000) -> str:
    """旋转一周 GIF：agent 对话内的"准 3D"预览（聊天里内嵌，无需浏览器）。"""
    import os
    import matplotlib
    matplotlib.use("Agg")
    cache = Path(".mplcache")
    cache.mkdir(exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache.resolve()))
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    from PIL import Image

    r = m
    if len(r.faces) > max_faces:
        try:
            r = r.simplify_quadric_decimation(face_count=max_faces)
        except Exception:
            pass
    cols = np.full((len(r.faces), 3), 0.72)
    frames_img = []
    for i in range(frames):
        azim = i * 360 / frames
        elev = np.radians(20)
        Ldir = np.array([np.cos(elev) * np.cos(np.radians(azim)),
                         np.cos(elev) * np.sin(np.radians(azim)), np.sin(elev)])
        shade = (0.45 + 0.55 * np.clip(r.face_normals @ Ldir, 0.0, 1.0))[:, None]
        fcols = np.clip(cols * shade, 0, 1)
        fig = plt.figure(figsize=(5, 5), dpi=110)
        ax = fig.add_subplot(projection="3d")
        ax.add_collection3d(Poly3DCollection(r.triangles, facecolors=fcols, edgecolors="none"))
        b0, b1 = r.bounds
        c = (b0 + b1) / 2
        s = float((b1 - b0).max()) / 2 * 1.05
        ax.set_xlim(c[0] - s, c[0] + s); ax.set_ylim(c[1] - s, c[1] + s); ax.set_zlim(c[2] - s, c[2] + s)
        ax.set_box_aspect((1, 1, 1)); ax.view_init(elev=20, azim=azim); ax.set_axis_off()
        fig.tight_layout(pad=0)
        fig.canvas.draw()
        frames_img.append(np.asarray(fig.canvas.buffer_rgba())[..., :3].copy())
        plt.close(fig)
    ims = [Image.fromarray(f) for f in frames_img]
    ims[0].save(out_gif, save_all=True, append_images=ims[1:], duration=250, loop=0)
    return out_gif


# ---------- selftest ----------

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


def selftest(outdir: str = "output") -> int:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(0)

    cases = {
        # name: (mesh, rebuild_expected, volume_range, wall_expected)
        "sphere_with_holes": (_sphere_with_holes(), True, (2700, 5700), None),
        "two_spheres": (_two_spheres(), True, (4500, 8200), None),
        "overhang_t": (_overhang_t(), False, (5300, 5900), None),
        "thin_plate": (_thin_plate(), False, (750, 1050), "warn"),
        "healthy_cube": (_healthy_cube(), False, (3040, 3710), "pass"),
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
        case_ok = (after["checks"]["watertight"]["watertight"]
                   and (rebuilt == rebuild_expected) and vol_ok and ext_ok
                   and wall_ok)
        all_ok &= case_ok
        summary[name] = {
            "ok": case_ok,
            "steps": steps,
            "risk": f"{before['risk']['grade']}->{after['risk']['grade']}",
            "watertight": f"{before['checks']['watertight']['watertight']}->{after['checks']['watertight']['watertight']}",
            "overhang_frac": f"{before['checks']['overhang']['bad_frac']}->{after['checks']['overhang']['bad_frac']}",
            "volume_mm3_after": after["checks"]["watertight"]["volume_mm3"],
            "volume_ok": vol_ok,
            "extents_ok": bool(ext_ok),
            "wall": (f"{wall_verdict}(exp={wall_exp})"
                     if wall_exp else wall_verdict),
            "wall_before": wall_before,
            "min_wall_mm": after["checks"]["wall_thickness"].get("min_wall_mm"),
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

    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("SELFTEST", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


# ---------- CLI ----------

def main(argv=None) -> int:
    try:  # 控制台可能是 GBK，报告含中文/特殊符号时避免打印崩溃
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        prog="printguard",
        description="PrintGuard：FDM 可制造性检测/修复")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a1 = sub.add_parser("analyze", help="检测 mesh，输出 JSON 报告")
    a1.add_argument("file")
    a1.add_argument("--height-mm", type=float, default=None,
                    help="先把模型最大边缩放到指定 mm（GLB 单位不可信时必用）")
    a1.add_argument("--json", default=None, help="报告写出到该文件")

    a2 = sub.add_parser("repair", help="检测 + 修复 + 导出 STL")
    a2.add_argument("file")
    a2.add_argument("-o", "--out", required=True, help="输出 STL 路径")
    a2.add_argument("--height-mm", type=float, default=None)
    a2.add_argument("--pitch", type=float, default=None, help="体素分辨率 mm（默认自适应）")
    a2.add_argument("--no-orient", action="store_true", help="跳过自动摆向")
    a2.add_argument("--json", default=None, help="报告写出到该文件")

    sub.add_parser("selftest", help="内置合成用例自测")
    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()

    if args.cmd == "analyze":
        m = load_mesh(args.file, args.height_mm)
        rep = analyze_mesh(m)
    else:
        m = load_mesh(args.file, args.height_mm)
        m, steps, before, after = repair(m, do_orient=not args.no_orient,
                                         pitch=args.pitch)
        m.export(args.out)
        rep = {"before": before, "repair_steps": steps, "after": after,
               "output": args.out}

    text = json.dumps(rep, ensure_ascii=False, indent=1)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
