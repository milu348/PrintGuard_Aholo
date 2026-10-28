#!/usr/bin/env python3
"""PrintGuard_Aholo v0.1 —— 最简版 CLI：analyze / repair / selftest

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
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh

OVERHANG_COS = math.cos(math.radians(45.0))
BED_SLAB = 0.3  # mm：视为贴床的底部厚度
MIN_WALL = 0.8  # mm：最小壁厚阈值（2×0.4 喷嘴）——默认 profile 的值；运行时以 PrinterProfile 为准


# ---------- 打印机 profile（阈值参数化：同一模型对不同打印机结论不同） ----------

@dataclass
class PrinterProfile:
    name: str = "generic-0.4"
    nozzle_mm: float = 0.4
    layer_mm: float = 0.2
    min_wall_mm: float = 0.8      # ≈ 2×喷嘴
    min_hole_mm: float = 0.8      # 孔径下限（预留：孔检测）
    overhang_deg: float = 45.0
    island_min_mm3: float = 60.0  # 低于此体积的孤岛视为碎片（飞件风险）
    bridge_mm: float = 10.0


BUILTIN_PROFILES = {
    "generic-0.4": PrinterProfile(),
    "mini-0.2": PrinterProfile(name="mini-0.2", nozzle_mm=0.2, layer_mm=0.1,
                               min_wall_mm=0.4, min_hole_mm=0.4,
                               island_min_mm3=15.0, overhang_deg=50.0),
}


def load_profile(ref: str | None = None) -> PrinterProfile:
    """按名称取内置 profile，或按路径加载自定义 JSON（缺省字段回退内置默认）。"""
    if not ref:
        return BUILTIN_PROFILES["generic-0.4"]
    if ref in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[ref]
    p = Path(ref)
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        base = BUILTIN_PROFILES.get(data.get("name"), PrinterProfile())
        merged = {**base.__dict__, **{k: v for k, v in data.items() if k in base.__dict__}}
        return PrinterProfile(**merged)
    raise ValueError(f"未知打印机 profile: {ref}（可选 {sorted(BUILTIN_PROFILES)} 或 JSON 文件路径）")


# ---------- 基础 ----------

def load_mesh(path: str, height_mm: float | None = None) -> trimesh.Trimesh:
    m = trimesh.load(path, force="mesh")
    if height_mm:
        m.apply_scale(float(height_mm) / float(max(m.extents)))
    m.apply_translation(-m.bounds[0])
    return m


def overhang_mask(m: trimesh.Trimesh, deg: float = 45.0):
    """悬垂/贴床逐面掩码（几何法）：悬垂=法线朝下超过 deg 度且非贴床薄层。"""
    nz = m.face_normals[:, 2]
    fz = m.triangles[:, :, 2].max(axis=1)
    zmin = float(m.bounds[0][2])
    on_bed = (fz - zmin) <= BED_SLAB
    cos_t = float(np.cos(np.radians(deg)))
    return (nz < -cos_t) & ~on_bed, (nz < -0.9) & on_bed


def overhang_stats(m: trimesh.Trimesh, deg: float = 45.0):
    """几何法悬垂统计（基于 overhang_mask）。orient 沿用（快，候选姿态间可比）；
    对外交付的诚实数字见 _support_split（悬垂面向下支撑检查）。"""
    area = m.area_faces
    down, bed = overhang_mask(m, deg)
    bad_area = float(area[down].sum())
    total = float(area.sum())
    return bad_area, (bad_area / total if total else 0.0), float(area[bed].sum())


BRIDGE_MM = 10.0  # mm：悬垂面向下探测距离，此范围内有材料=桥接/贴材料（无需支撑)


def _surface_voxels(m: trimesh.Trimesh, pitch: float, spacing: float = 0.4) -> np.ndarray:
    """面积加权的表面采样体素化（替代 trimesh 的细分式体素化）。

    为什么不用 trimesh 的 voxelized：它把"边长 > pitch/2"的三角形递归细分到位，
    细分规模 ∝ 面积/pitch² 且**与三角面尺寸强耦合**——稀疏网格上单个横跨模型的
    长三角形就能膨胀出上千万子面（实测 130mm/30 万面件在这一步直接吃掉 >7GB）。
    这里改为两类采样，采样规模均与三角面尺寸无关，内存稳定：

    1. 面内采样：每面 ceil(面积/(spacing·pitch)²) 个点，重心坐标准随机分布；
    2. 棱边采样：每条边 ceil(边长/(spacing·pitch)) 个点——面内样本严格落在
       三角形内部，**网格棱边/顶点附近的体素列两个相邻面都够不着**，会形成
       细贯穿缝（selftest 实测 closing+fill_holes 全部失效），沿边布点封住接缝。

    采样体素壳再由 _solid_grid 的 closing + fill_holes 固化。分块处理避免
    中间数组一次性膨胀。spacing=0.4（每体素 ~6 面内采样点）。
    """
    v = np.asarray(m.vertices, float)
    f = np.asarray(m.faces, np.int64)
    origin = np.asarray(m.bounds[0], float)
    dims = (np.floor((np.asarray(m.bounds[1], float) - origin) / pitch).astype(np.int64) + 1)
    g = np.zeros((int(dims[0]), int(dims[1]), int(dims[2])), bool)
    if len(f) == 0:
        return g
    tri = v[f]

    def _mark(pts: np.ndarray) -> None:
        idx = np.floor((pts - origin) / pitch).astype(np.int64)
        ok = np.all((idx >= 0) & (idx < dims), axis=1)
        idx = idx[ok]
        if len(idx):
            lin = np.unique((idx[:, 0] * dims[1] + idx[:, 1]) * dims[2] + idx[:, 2])
            g[lin // (dims[1] * dims[2]), (lin // dims[2]) % dims[1], lin % dims[2]] = True

    def _sample_chunks(starts: np.ndarray, counts: np.ndarray, point_fn) -> None:
        total = int(counts.sum())
        cum = np.cumsum(counts)
        st = cum - counts
        chunk = 1_000_000
        off = 0
        while off < total:
            hi = min(total, off + chunk)
            # 只展开与 [off, hi) 相交的单元，段内裁剪到 chunk 边界（分块控内存）
            ids = np.flatnonzero((st < hi) & (cum > off))
            lo_i = np.maximum(st[ids], off)
            hi_i = np.minimum(cum[ids], hi)
            cnt = (hi_i - lo_i).astype(np.int64)
            rep = np.repeat(ids, cnt)
            seg = np.repeat(np.cumsum(cnt) - cnt, cnt)   # 单元在本 chunk 内的段起点
            base = np.repeat(lo_i - st[ids], cnt)        # 单元内采样的起始序号
            k = np.arange(int(cnt.sum())) - seg + base
            _mark(point_fn(rep, k))
            off = hi

    # 1) 面内采样
    area = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    n_samp = np.maximum(1, np.ceil(area / (spacing * pitch) ** 2)).astype(np.int64)

    def face_pts(rep, k):
        n = n_samp[rep]
        u = (k + 0.5) / n
        w = np.mod(k * 0.7548776662466927 + 0.5, 1.0)   # 低差异序列，覆盖均匀且确定性
        su = np.sqrt(u)
        return ((1 - su)[:, None] * tri[rep, 0]
                + (su * (1 - w))[:, None] * tri[rep, 1]
                + (su * w)[:, None] * tri[rep, 2])

    _sample_chunks(np.zeros(len(f), np.int64), n_samp, face_pts)

    # 2) 棱边采样（3 条边 × 每边按弧长均匀；相邻面的共享边被重复采样，去重由 _mark 完成）
    A = np.concatenate([tri[:, 0], tri[:, 1], tri[:, 2]])
    B = np.concatenate([tri[:, 1], tri[:, 2], tri[:, 0]])
    e_len = np.linalg.norm(B - A, axis=1)
    n_e = np.maximum(1, np.ceil(e_len / (spacing * pitch)).astype(np.int64))

    def edge_pts(rep, k):
        t = (k + 0.5) / n_e[rep]
        return A[rep] + t[:, None] * (B[rep] - A[rep])

    _sample_chunks(np.zeros(len(A), np.int64), n_e, edge_pts)
    return g


def _raw_grid(m: trimesh.Trimesh, pitch: float) -> np.ndarray:
    """raw 体素网格（无 closing/fill）——支撑探测要的是真实几何，
    不用修复路径的实心化网格（closing 会凭空造出"下方材料"）。"""
    w = m.copy()
    try:
        w.process(validate=True)
    except Exception:
        pass
    if len(w.faces) == 0:
        w = m.copy()  # process 清空网格的版本行为差异，退回原始网格
    try:
        trimesh.repair.fill_holes(w)
    except Exception:
        pass
    if len(w.faces) == 0:
        w = m.copy()
    return _surface_voxels(w, pitch)


def _support_split(m: trimesh.Trimesh, deg: float = 45.0) -> dict | None:
    """#3 悬垂支撑检查：对每个悬垂面（法线朝下超 deg 度、非贴床）沿重力方向在
    raw 体素网格中向下探测 BRIDGE_MM——下方有材料=桥接/贴材料（无需支撑），
    全程无材料=真悬空（需支撑）。修复几何法的堆叠贴合面误报
    （该误报类是开发日志挂了很多轮的已知缺陷）。
    返回 None = 面积为零（无法判断）。"""
    down, _bed = overhang_mask(m, deg)
    area = m.area_faces
    total = float(area.sum())
    if total <= 0:
        return None
    ext = float(max(m.extents))
    pitch = max(0.15, min(0.4, ext / 300.0))
    while (ext / pitch) ** 3 > 1.5e8 and pitch < 0.8:  # 内存护栏
        pitch *= 1.35
    g = _raw_grid(m, pitch)
    out = {"bridge_lookup_mm": BRIDGE_MM, "pitch_mm": round(pitch, 2),
           "method": "悬垂面沿重力方向在 raw 体素网格向下探测（量化 ±1*pitch）"}
    if not g.any() or not down.any():
        out.update({"open_area_mm2": 0.0, "open_frac": 0.0,
                    "bridged_area_mm2": 0.0, "bridged_frac": 0.0})
        return out
    cen = m.triangles_center[down]
    bmin = np.asarray(m.bounds[0], float)
    dims = np.asarray(g.shape)
    idx = np.clip(np.floor((cen - bmin) / pitch).astype(int), 0, dims - 1)
    ix, iy, iz = idx[:, 0], idx[:, 1], idx[:, 2]
    k_max = max(1, int(BRIDGE_MM / pitch))
    supported = np.zeros(int(down.sum()), bool)
    for k in range(1, k_max + 1):
        zkk = iz - k
        ok = zkk >= 0
        if not ok.any():
            break
        supported[ok] |= g[ix[ok], iy[ok], zkk[ok]]
        if supported.all():
            break
    open_a = float(area[down][~supported].sum())
    bridged_a = float(area[down][supported].sum())
    out.update({"open_area_mm2": round(open_a, 1),
                "open_frac": round(open_a / total, 4),
                "bridged_area_mm2": round(bridged_a, 1),
                "bridged_frac": round(bridged_a / total, 4)})
    return out


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


def island_stats(m: trimesh.Trimesh, profile: PrinterProfile) -> dict:
    """孤岛检测：连通体计数 + 小碎片识别（悬浮碎块/细须——切片丢弃或打印中飞件）。
    主域=面积最大的连通体；体积 < island_min 的非主域计为 tiny_islands。"""
    try:
        parts = m.split(only_watertight=False)
    except Exception:
        parts = [m]
    if len(parts) <= 1:
        return {"bodies": 1, "islands": 0, "tiny_islands": 0, "verdict": "pass", "details": []}
    main = max(parts, key=lambda p: p.area)
    islands = []
    for p in parts:
        if p is main:
            continue
        try:
            vol = float(max(p.convex_hull.volume, 0.0))
        except Exception:
            vol = 0.0
        islands.append({"faces": len(p.faces), "area_mm2": round(float(p.area), 1),
                        "volume_mm3": round(vol, 1),
                        "bbox_mm": [round(float(x), 1) for x in p.extents]})
    tiny = [i for i in islands if i["volume_mm3"] < profile.island_min_mm3]
    return {"bodies": len(parts), "islands": len(islands),
            "tiny_islands": len(tiny),
            "main_area_frac": round(float(main.area) / max(float(m.area), 1e-9), 4),
            "details": sorted(islands, key=lambda i: -i["volume_mm3"])[:5],
            "verdict": "warn" if islands else "pass"}


def repair_island_clean(m: trimesh.Trimesh, profile: PrinterProfile):
    """修复步骤：删除体积 < island_min 的碎片孤岛（体积未知者保守保留）。
    返回 (mesh, 描述|None)。"""
    try:
        parts = m.split(only_watertight=False)
    except Exception:
        return m, None
    if len(parts) <= 1:
        return m, None
    main = max(parts, key=lambda p: p.area)
    keep, removed = [], []
    for p in parts:
        if p is main:
            keep.append(p)
            continue
        try:
            vol = float(max(p.convex_hull.volume, 0.0))
        except Exception:
            vol = float("inf")
        if vol >= profile.island_min_mm3:
            keep.append(p)
        else:
            removed.append(round(vol, 1))
    if not removed:
        return m, None
    return trimesh.util.concatenate(keep), f"island_clean(移除 {len(removed)} 个碎片 {removed}mm³)"


def risk_grade(wt: bool, oh_frac: float, nonmanifold: int, degen: int,
               wall: dict | None = None, tiny_islands: int = 0):
    score = 100.0
    score -= min(6.0, 2.0 * max(0, tiny_islands))   # 碎片孤岛：飞件风险（小幅扣分）
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


def analyze_mesh(m: trimesh.Trimesh, thickness: bool | None = None,
                 profile: PrinterProfile | None = None) -> dict:
    pf = profile or load_profile(None)
    wt = watertight_stats(m)
    bad_area, oh_frac, bed_area = overhang_stats(m, pf.overhang_deg)
    if thickness is None:
        # 非水密（壳汤/破面）时体素分析无意义，自动跳过
        thickness = bool(wt["watertight"])
    if thickness:
        wall = wall_thickness(m, min_wall=pf.min_wall_mm)
        sup = _support_split(m, pf.overhang_deg)
        islands = island_stats(m, pf)
    else:
        wall = {"verdict": "skipped", "reason": "非水密，体素壁厚无意义（先修复）"}
        sup = None
        islands = {"bodies": None, "islands": None, "tiny_islands": None,
                   "verdict": "skipped（体素分析跳过，随壁厚）"}
    # 风险评分用"真悬空"占比（有支撑检查时）——桥接/贴材料不需支撑，不该扣分
    eff_frac = sup["open_frac"] if sup and "open_frac" in sup else oh_frac
    grade, score = risk_grade(wt["watertight"], eff_frac,
                              wt["nonmanifold_edges"], wt["degenerate_faces"], wall,
                              tiny_islands=int(islands.get("tiny_islands") or 0))
    overhang = {
        "threshold_deg": pf.overhang_deg,
        "bad_area_mm2": round(bad_area, 1),
        "bad_frac": round(oh_frac, 4),
        "bed_contact_mm2": round(bed_area, 1),
    }
    if sup:
        overhang.update(sup)
    else:
        overhang["support_lookup"] = "skipped（非水密，体素支撑探测无意义）"
    return {
        "bbox_mm": [round(float(x), 2) for x in m.extents],
        "checks": {
            "watertight": wt,
            "overhang": overhang,
            "wall_thickness": wall,
            "islands": islands,
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
    try:
        w.process(validate=True)
    except Exception:
        pass
    if len(w.faces) == 0:
        # 部分 trimesh 版本的 process(validate=True) 会把非流形 AI 网格清空
        # （Notebook 实测导致 wall=skipped），退回未处理网格
        w = m.copy()
    try:
        trimesh.repair.fill_holes(w)  # 先填单三角/四边形洞
    except Exception:
        pass
    if len(w.faces) == 0:
        w = m.copy()
    # 面积加权表面采样体素化：替代 trimesh 细分式体素化，消除长三角形内存炸弹
    g = _surface_voxels(w, pitch)
    pad = 8  # 覆盖最大 closing 半径，防止贴边腐蚀
    g = np.pad(g, pad)
    st1 = ndimage.generate_binary_structure(3, 1)
    hull_vol = max(float(w.convex_hull.volume), 1e-6)
    gc = g
    for it in (2, 4, 6):
        st = ndimage.iterate_structure(st1, it)
        # 薄壳不能直接 binary_closing：贴边 1 体素厚的洞，膨胀相够不到洞外侧
        # 第 2 层，腐蚀相会把洞重新打开（selftest 实测 closing(2/4/6) 全失效）。
        # 三明治原语：膨胀加厚 -> fill_holes 封腔 -> 腐蚀还原外表面，内腔保持实心。
        gd = ndimage.binary_dilation(g, structure=st)
        gc = ndimage.binary_fill_holes(gd)
        gc = ndimage.binary_erosion(gc, structure=st)
        if float(gc.sum()) * pitch**3 >= 0.35 * hull_vol:
            break  # 已实心化
    return gc[pad:-pad, pad:-pad, pad:-pad]


def _pre_watertight(m: trimesh.Trimesh) -> trimesh.Trimesh:
    """使用 pymeshfix 进行水密化预处理：修复自交、填孔、去重复顶点。
    返回处理后的水密网格；若 pymeshfix 不可用或失败，返回原网格。"""
    if not HAS_PYMESHFIX:
        return m
    try:
        mf = pymeshfix.MeshFix(m)
        mf.repair(verbose=False)
        return mf.mesh
    except Exception:
        return m


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


def wall_thickness(m: trimesh.Trimesh, pitch: float | None = None,
                   min_wall: float | None = None) -> dict:
    """最小壁厚估计：体素 EDT -> 中轴点（距离局部极大）-> T = 2D − pitch。
    注意：trimesh 体素索引为 round 量化，表面各外扩约半个体素，薄特征厚度被
    高估约 1 个 pitch，故用 −p 补偿（T 偏保守/低估）。判定：
    fail（T ≤ 0.8−p）/ warn（T < 0.8+2p，亚分辨率带，建议复核）/ pass。
    pitch 上限 0.2：更粗时亚 0.8mm 薄壁会量化漏检。"""
    from scipy import ndimage
    ext = float(max(m.extents))
    mw = float(min_wall or MIN_WALL)
    if pitch is None:
        pitch = max(0.12, min(0.2, ext / 400.0))
    while (ext / pitch) ** 3 > 1.5e8 and pitch < 0.6:  # 内存护栏
        pitch *= 1.35
    g = _solid_grid(m, pitch)
    if not g.any():
        return {"verdict": "skipped", "reason": "体素化后为空", "pitch_mm": round(pitch, 2)}
    gp = np.pad(g, 2)
    origin = np.asarray(m.bounds[0], float)      # 栅格→物理：origin + (gp_idx - 2 + 0.5) * pitch

    def to_phys(coords_gp):
        return origin + (np.asarray(coords_gp, float) - 2 + 0.5) * pitch
    d = ndimage.distance_transform_edt(gp, sampling=pitch)
    d = d.astype(np.float32)[2:-2, 2:-2, 2:-2]
    maxf = ndimage.maximum_filter(d, size=3, mode="constant")
    medial = (d >= maxf - 1e-5) & (d >= pitch - 1e-5) & g
    if not medial.any():
        return {"verdict": "skipped", "reason": "未找到中轴点（网格过细）",
                "pitch_mm": round(pitch, 2)}
    vals = d[medial]
    t_min = float(2 * float(vals.min()) - pitch)
    # 薄弱区聚合：warn+fail 带的中轴体素按连通域归组（报告 top5，诊断图编号对应）
    t_all = 2 * d - pitch
    thin_band = medial & (t_all < mw + 2 * pitch)
    regions, region_coords = [], []
    if thin_band.any():
        labels, n_reg = ndimage.label(thin_band)
        sizes = np.bincount(labels.ravel())
        sizes[0] = 0
        for rid in np.argsort(sizes)[::-1][:5]:
            if sizes[rid] == 0:
                break
            coords = np.argwhere(labels == rid)
            t_reg = 2 * d[coords[:, 0], coords[:, 1], coords[:, 2]] - pitch
            region_coords.append(coords)
            regions.append({"id": len(regions) + 1, "voxels": int(sizes[rid]),
                            "t_min_mm": round(float(t_reg.min()), 2),
                            "center_mm": to_phys(coords.mean(axis=0) + 2).round(1).tolist(),
                            "bbox_mm": [to_phys(coords.min(axis=0) + 2).round(1).tolist(),
                                        to_phys(coords.max(axis=0) + 2).round(1).tolist()]})
    # 薄弱区精测：EDT 是 pitch 量化值（±pitch 误差）。对每个薄弱区采样中轴体素，
    # 沿「指向最近背景体素」的方向双向射线打到原始三角面，T = 去程 + 回程命中距离——
    # 不重体素化、不重网格化，直接从真实几何测出精确壁厚。
    if regions:
        try:
            _, inds = ndimage.distance_transform_edt(gp, sampling=pitch, return_indices=True)
            eps = min(0.02, pitch * 0.25)   # 命中点距原点小于此=原点贴在三角面上（退化）
            for rg, coords in zip(regions, region_coords):
                step = max(1, len(coords) // 40)
                sel = coords[::step][:40]
                # 只采样深部体素（离表面 ≥2 体素）：表面壳/出血体素的射线测量是噪声
                if len(sel):
                    dsel = d[sel[:, 0], sel[:, 1], sel[:, 2]]   # d 已是未 pad 空间，与 coords 一致
                    sel = sel[dsel >= 2 * pitch]
                if not len(sel):
                    continue    # 区域整体过薄（<2 体素）→ 量化值已足够，跳过精测
                sel_gp = sel + 2                          # 未 pad 索引 → gp 索引
                centers = to_phys(sel_gp)
                targets = to_phys(inds[:, sel_gp[:, 0], sel_gp[:, 1], sel_gp[:, 2]].T)
                dirs = targets - centers
                norm = np.linalg.norm(dirs, axis=1, keepdims=True)
                ok = norm[:, 0] > 1e-9
                dirs = dirs[ok] / norm[ok]
                origins = centers[ok] + dirs * (pitch * 0.5)  # 略离开体素中心避免自交
                ts = []
                for o, dvec in zip(origins, dirs):
                    l1, _, _ = m.ray.intersects_location([o], [dvec])
                    l2, _, _ = m.ray.intersects_location([o], [-dvec])
                    # 先剔除退化命中（原点恰在三角面上时 d≈0 的自交假象），再取最近
                    def nearest(hits):
                        if not len(hits):
                            return 0.0
                        dd = np.linalg.norm(np.atleast_2d(hits) - o, axis=1)
                        valid = dd[dd > eps]
                        return float(valid.min()) if len(valid) else 0.0
                    d1, d2 = nearest(l1), nearest(l2)
                    if d1 > 0 and d2 > 0:
                        ts.append(d1 + d2)              # 原点在壁内：两面前后距离相加
                    elif d1 > 0:
                        ts.append(d1)                   # 原点贴面：单侧穿越即壁厚
                    elif d2 > 0:
                        ts.append(d2)
                ts = [t for t in ts if t > 1e-3]
                if ts:
                    rg["t_precise_mm"] = round(float(np.median(ts)), 2)
        except Exception:
            import traceback
            traceback.print_exc()
    verdict = ("fail" if t_min <= mw - pitch
               else "warn" if t_min < mw + 2 * pitch else "pass")
    return {
        "verdict": verdict,
        "min_wall_mm": round(t_min, 2),
        "threshold_mm": mw,
        "pitch_mm": round(pitch, 2),
        "thin_regions": regions,
        "method": "voxel EDT medial, T=2D-p（量化）；薄弱区附射线精测 t_precise_mm（三角面级）",
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


def repair_thicken(m: trimesh.Trimesh, max_grow: int = 4, iters: int = 3,
                   min_wall: float | None = None):
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
        mw = float(min_wall or MIN_WALL)
        fail = medial & (t <= mw - pitch)
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
    pf = load_profile(None)
    m, clean_step = repair_island_clean(m, pf)
    if clean_step:
        steps.append(clean_step)
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
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "WenQuanYi Micro Hei", "DejaVu Sans"]
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
    regions = wall.get("thin_regions") or []
    if regions and wall.get("verdict") in ("fail", "warn"):
        for rg in regions:
            ctr = rg.get("center_mm") or [0, 0, 0]
            ax.scatter([ctr[0]], [ctr[1]], [ctr[2]], c="red", s=90,
                       marker="o", depthshade=False)
            ax.text(ctr[0], ctr[1], ctr[2], str(rg.get("id", "?")), color="white",
                    fontsize=9, ha="center", va="center", depthshade=False)

    wt = checks.get("watertight") or {}
    oh = checks.get("overhang") or {}
    risk = report.get("risk") or {}
    gmin = oh.get("min_wall_mm")
    lines = [
        "PrintGuard_Aholo 诊断报告",
        f"风险: {risk.get('grade', '-')} ({risk.get('score', '-')}/100)",
        "",
        f"水密性: {'通过' if wt.get('watertight') else '未通过'}",
        f"   边界边 {wt.get('boundary_edges', '-')} / 非流形边 {wt.get('nonmanifold_edges', '-')}",
        f"悬垂(>{oh.get('threshold_deg', 45)}°): {oh.get('bad_frac', 0) * 100:.1f}%",
        f"   真悬空(需支撑): {oh.get('open_frac', 0) * 100:.1f}%",
        f"   桥接/贴材料(≤10mm): {oh.get('bridged_frac', 0) * 100:.1f}%",
        f"   问题面积 {oh.get('bad_area_mm2', '-')} mm²",
        f"最小壁厚(≥{wall.get('threshold_mm', 0.8)}mm): {wall.get('min_wall_mm', '-')} mm [{wall.get('verdict', '-')}]",
        "",
        "修复步骤:",
    ]
    lines += [f"  {i + 1}. {s}" for i, s in enumerate(steps or [])]
    lines += ["", "灰=正常  蓝=贴床  红=悬垂  红圈=薄弱区(编号同报告)"]
    ax2 = fig.add_subplot(1, 2, 2)
    ax2.set_axis_off()
    ax2.text(0.02, 1.0, "\n".join(lines), va="top", fontsize=11)
    fig.tight_layout()
    fig.savefig(out_png, bbox_inches="tight")
    plt.close(fig)
    return out_png


def render_model_png(m: trimesh.Trimesh, out_png: str, max_faces: int = 8000) -> str:
    """产品渲染图：最终模型的干净单视角渲染——无诊断着色、无指标面板（交付给用户）。"""
    import os
    import matplotlib
    matplotlib.use("Agg")
    cache = Path(".mplcache")
    cache.mkdir(exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache.resolve()))
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "WenQuanYi Micro Hei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    r = m
    if len(r.faces) > max_faces:
        try:
            r = r.simplify_quadric_decimation(face_count=max_faces)
        except Exception:
            pass
    elev = np.radians(28); azim = np.radians(-50)
    Ldir = np.array([np.cos(elev) * np.cos(azim), np.cos(elev) * np.sin(azim), np.sin(elev)])
    shade = (0.45 + 0.55 * np.clip(r.face_normals @ Ldir, 0.0, 1.0))[:, None]
    base = np.array([0.64, 0.67, 0.72])       # 浅灰蓝：中性产品观感
    colors = np.clip(base * shade, 0, 1)

    fig = plt.figure(figsize=(6, 6), dpi=140)
    fig.patch.set_facecolor("white")
    ax = fig.add_subplot(projection="3d")
    ax.add_collection3d(Poly3DCollection(r.triangles, facecolors=colors, edgecolors="none"))
    b0, b1 = r.bounds
    c = (b0 + b1) / 2
    s = float((b1 - b0).max()) / 2 * 1.05
    ax.set_xlim(c[0] - s, c[0] + s); ax.set_ylim(c[1] - s, c[1] + s); ax.set_zlim(c[2] - s, c[2] + s)
    ax.set_box_aspect((1, 1, 1)); ax.view_init(28, -50); ax.set_axis_off()
    fig.savefig(out_png, bbox_inches="tight", pad_inches=0.05, facecolor="white")
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


def _stacked_boxes() -> trimesh.Trimesh:
    """两层正方体上下叠放，中间 0.3mm 接触面（#4 间隙 fail 用例）"""
    base = trimesh.creation.box(extents=[20, 20, 5])
    base.apply_translation([0, 0, 2.5])
    a = trimesh.creation.box(extents=[9.85, 20, 8])
    a.apply_translation([-5.075, 0, 9])
    b = a.copy()
    b.apply_translation([10.15, 0, 0])
    return trimesh.util.concatenate([base, a, b])


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
        case_ok = (after["checks"]["watertight"]["watertight"]
                   and (rebuilt == rebuild_expected) and vol_ok and ext_ok
                   and wall_ok)
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
        description="PrintGuard_Aholo：FDM 可制造性检测/修复")
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
