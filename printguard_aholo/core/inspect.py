"""几何体检：水密性 / 悬垂（含向下支撑探测）/ 壁厚。

只保留**驱动修复决策**的三项（水密→是否重构；悬垂→摆向；壁厚→是否增厚）。
孔洞、孤岛、平整度、风险评级曾是报告字段，服务与 CLI 都不消费，已删除
（平整度回归挪到 selftest，作为 Taubin 平滑的断言）。

设计口径：
- 对外只给「检测结论 + 可解释指标」，不抛底层异常；
- 非水密网格跳过体素类分析（壳汤上算壁厚没有意义）；
- 壁厚与支撑探测共用同一体素网格，避免重复体素化。
"""
from __future__ import annotations

import logging

import numpy as np
import trimesh

from .profiles import BED_SLAB, MIN_WALL, PrinterProfile, load_profile
from .voxel import fit_pitch, raw_grid, solid_grid

logger = logging.getLogger("printguard.core.inspect")


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
    pitch = fit_pitch(ext, lo=0.15, hi=0.4)
    g = raw_grid(m, pitch)
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


def analyze_mesh(m: trimesh.Trimesh, thickness: bool | None = None,
                 profile: PrinterProfile | None = None) -> dict:
    pf = profile or load_profile()
    wt = watertight_stats(m)
    bad_area, oh_frac, bed_area = overhang_stats(m, pf.overhang_deg)
    if thickness is None:
        # 非水密（壳汤/破面）时体素分析无意义，自动跳过
        thickness = bool(wt["watertight"])
    if thickness:
        ext_h = float(max(m.extents))
        pitch_h = fit_pitch(ext_h, lo=0.12, hi=0.2)
        grid = solid_grid(m, pitch_h)
        wall = wall_thickness(m, min_wall=pf.min_wall_mm, grid=grid)
        sup = _support_split(m, pf.overhang_deg)
    else:
        wall = {"verdict": "skipped", "reason": "非水密，体素壁厚无意义（先修复）"}
        sup = None
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
        },
    }


def _wall_grid_state(m, pitch, grid):
    """体素 EDT + 中轴 + 独立薄壁掩码。返回 None 表示无法判定。"""
    from scipy import ndimage
    g = solid_grid(m, pitch) if grid is None else grid
    if not g.any():
        return None
    gp = np.pad(g, 2)
    origin = np.asarray(m.bounds[0], float)   # 栅格→物理：origin + (idx - 2 + 0.5) * pitch
    d_pad = np.asarray(ndimage.distance_transform_edt(gp, sampling=pitch),
                       dtype=np.float32)
    d = d_pad[2:-2, 2:-2, 2:-2]               # 未 pad 空间（与 g 同构）
    maxf = ndimage.maximum_filter(d, size=3, mode="constant")
    medial = (d >= maxf - 1e-5) & (d >= pitch - 1e-5) & g
    if not medial.any():
        return None
    genuine, _ = genuine_thin(gp, d_pad, medial, pitch, MIN_WALL)
    thin = medial & genuine
    t_min_raw = float(2 * float(d[medial].min()) - pitch)   # 含凹角的原始值
    return {"gp": gp, "d_pad": d_pad, "d": d, "medial": medial, "thin": thin,
            "origin": origin, "t_min_raw": t_min_raw}


def _thin_regions(state, pitch, mw, to_phys, top_k=5):
    """薄弱区聚合：warn+fail 带的中轴体素按连通域归组（取 top_k）。"""
    from scipy import ndimage
    d = state["d"]
    thin_band = state["thin"] & (2 * d - pitch < mw + 2 * pitch)
    regions, region_coords = [], []
    if not thin_band.any():
        return regions, region_coords, None
    labels, _ = ndimage.label(thin_band)
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0
    for rid in np.argsort(sizes)[::-1][:top_k]:
        if sizes[rid] == 0:
            break
        coords = np.argwhere(labels == rid)
        t_reg = 2 * d[coords[:, 0], coords[:, 1], coords[:, 2]] - pitch
        region_coords.append(coords)          # 未 pad 空间（与 d 同构）
        regions.append({"id": len(regions) + 1, "voxels": int(sizes[rid]),
                        "t_min_mm": round(float(t_reg.min()), 2),
                        "center_mm": to_phys(coords.mean(axis=0) + 2).round(1).tolist(),
                        "bbox_mm": [to_phys(coords.min(axis=0) + 2).round(1).tolist(),
                                    to_phys(coords.max(axis=0) + 2).round(1).tolist()]})
    t_min = float(2 * float(d[thin_band].min()) - pitch)
    return regions, region_coords, t_min


def _refine_regions_by_rays(m, regions, region_coords, state, pitch, samples=40):
    """薄弱区射线精测：沿「指向最近背景」的方向双向打到三角面，取命中距离和。

    EDT 是 pitch 量化值（±pitch 误差）；这一步不重体素化、不重网格化，
    直接从真实几何测精确壁厚。任何异常都只静默跳过（精测是增强，不是判定）。
    """
    d, d_pad, gp, origin = state["d"], state["d_pad"], state["gp"], state["origin"]

    def to_phys(coords_gp):
        return origin + (np.asarray(coords_gp, float) - 2 + 0.5) * pitch

    eps = min(0.02, pitch * 0.25)   # 命中点距原点小于此 = 原点贴在三角面上（退化）
    try:
        for rg, coords in zip(regions, region_coords):
            step = max(1, len(coords) // samples)
            sel = coords[::step][:samples]
            if len(sel):
                dsel = d[sel[:, 0], sel[:, 1], sel[:, 2]]
                sel = sel[dsel >= 2 * pitch]   # 只测深部体素：表面壳的射线是噪声
            if not len(sel):
                continue                      # 区域整体过薄（<2 体素）：量化值已够
            sel_gp = sel + 2
            centers = to_phys(sel_gp)
            sel_dv = d_pad[sel_gp[:, 0], sel_gp[:, 1], sel_gp[:, 2]] / pitch
            targets = to_phys(nearest_bg_targets(gp, sel_gp, sel_dv))
            dirs = targets - centers
            norm = np.linalg.norm(dirs, axis=1, keepdims=True)
            ok = norm[:, 0] > 1e-9
            dirs = dirs[ok] / norm[ok]
            origins = centers[ok] + dirs * (pitch * 0.5)   # 略离体素中心避免自交
            ts = []
            for o, dvec in zip(origins, dirs):
                l1, _, _ = m.ray.intersects_location([o], [dvec])
                l2, _, _ = m.ray.intersects_location([o], [-dvec])

                def nearest(hits):
                    if not len(hits):
                        return 0.0
                    dd = np.linalg.norm(np.atleast_2d(hits) - o, axis=1)
                    valid = dd[dd > eps]     # 剔除自交假象
                    return float(valid.min()) if len(valid) else 0.0

                d1, d2 = nearest(l1), nearest(l2)
                if d1 > 0 and d2 > 0:
                    ts.append(d1 + d2)       # 原点在壁内：两面前后距离相加
                elif d1 > 0:
                    ts.append(d1)            # 原点贴面：单侧穿越即壁厚
                elif d2 > 0:
                    ts.append(d2)
            ts = [t for t in ts if t > 1e-3]
            if ts:
                rg["t_precise_mm"] = round(float(np.median(ts)), 2)
    except Exception:
        logger.exception("薄弱区射线精测失败（增强项，静默跳过不影响判定）")


def wall_thickness(m: trimesh.Trimesh, pitch: float | None = None,
                   min_wall: float | None = None,
                   grid: np.ndarray | None = None) -> dict:
    """最小壁厚估计：体素 EDT → 中轴 → T = 2D − pitch；薄弱区附射线精测。

    注意：trimesh 体素索引为 round 量化，表面各外扩约半个体素，薄特征厚度被
    高估约 1 个 pitch，故用 −p 补偿（T 偏保守/低估）。判定：
    fail（T ≤ 0.8−p）/ warn（T < 0.8+2p，亚分辨率带，建议复核）/ pass。
    pitch 上限 0.2：更粗时亚 0.8mm 薄壁会量化漏检。

    编排只做四步：建网格状态 → 聚薄弱区 → 精测 → 判定；三个职责各自成函数。
    """
    ext = float(max(m.extents))
    mw = float(min_wall or MIN_WALL)
    if pitch is None:
        pitch = fit_pitch(ext, lo=0.12, hi=0.2)

    state = _wall_grid_state(m, pitch, grid)
    if state is None:
        return {"verdict": "skipped",
                "reason": "体素化后为空或未找到中轴点", "pitch_mm": round(pitch, 2)}

    to_phys = lambda c: state["origin"] + (np.asarray(c, float) - 2 + 0.5) * pitch
    regions, region_coords, t_min = _thin_regions(state, pitch, mw, to_phys)
    if t_min is None:                      # 无独立薄壁：凹角/被包裹结构非打印障碍
        t_min = state["t_min_raw"]

    if regions:
        _refine_regions_by_rays(m, regions, region_coords, state, pitch)

    verdict = ("fail" if t_min <= mw - pitch
               else "warn" if t_min < mw + 2 * pitch else "pass")
    return {
        "verdict": verdict,
        "min_wall_mm": round(t_min, 2),
        "min_wall_raw_mm": round(state["t_min_raw"], 2),   # 含凹角（信息参考）
        "threshold_mm": mw,
        "pitch_mm": round(pitch, 2),
        "thin_regions": regions,
        "method": "voxel EDT medial, T=2D-p（量化）；薄弱区附射线精测 t_precise_mm（三角面级）",
    }


def nearest_bg_targets(gp, coords, dv):
    """候选体素的最近背景体素坐标（局部窗口精确搜索）。

    代替全网格 EDT 的 return_indices——特征变换在数百万体素上的内部峰值 ~2GB，
    实测把 60mm 生成任务的子进程挤爆（0.8.6 魔搭托管）。最近背景体素必在
    dv+1 体素内（EDT 距离即到最近背景的距离），窗口半径 = ceil(max(dv))+2 即精确；
    且只有薄壁候选（dv 小）会进到这里，窗口很小。按距离从近到远扫描，
    首个背景体素即最近（与 EDT argmin 等价，平局取点可不同但距离相同）。"""
    r = int(np.ceil(dv.max())) + 2 if len(dv) else 1
    ax = np.arange(-r, r + 1)
    ox, oy, oz = np.meshgrid(ax, ax, ax, indexing="ij")
    off = np.stack([ox.ravel(), oy.ravel(), oz.ravel()], axis=1)
    off = off[np.argsort(np.linalg.norm(off, axis=1))]          # 近→远
    targets = np.empty_like(coords)
    remaining = np.arange(len(coords))
    dims = np.asarray(gp.shape)
    cur = 0
    while len(remaining) and cur < len(off):
        o = off[cur]
        cand = coords[remaining] + o
        inb = np.all((cand >= 0) & (cand < dims), axis=1)
        is_bg = np.zeros(len(remaining), bool)
        vi = np.flatnonzero(inb)
        is_bg[vi] = ~gp[cand[vi, 0], cand[vi, 1], cand[vi, 2]]
        done = remaining[is_bg]
        targets[done] = coords[done] + o
        remaining = remaining[~is_bg]
        cur += 1
    if len(remaining):      # 理论不可达（dv 内必有背景）；兜底取自身
        targets[remaining] = coords[remaining]
    return targets


def genuine_thin(gp, d_mm, medial, pitch, mw):
    """薄弱带判定（pad 空间）：中轴体素中 t < mw+2*pitch 者，再按「背衬判别」
    剔除非独立薄壁伪影——沿最近表面法向的两侧，穿越面之外 3 体素内仍有材料，
    说明该处是材料连续的凹角/贴面结构（FDM 可正常打印），而非独立薄壁。
    返回 (genuine 布尔掩码[pad 空间], 真薄弱体素坐标)。
    最近背景方向只对薄壁候选（数量少、dv 小）做局部窗口搜索，避免全网格
    EDT return_indices 的 ~2GB 特征变换峰值。"""
    coords = np.argwhere(medial)
    genuine = np.zeros(medial.shape, bool)   # 全尺寸掩码（与 medial 同构）
    if not len(coords):
        return genuine, coords
    dvals = d_mm[coords[:, 0], coords[:, 1], coords[:, 2]]
    dv = dvals / pitch
    centers = coords + 0.5
    t = 2 * dvals - pitch
    thin = t < mw + 2 * pitch
    idxs = np.flatnonzero(thin)
    dims = np.asarray(gp.shape)
    backed = np.zeros(0, bool)
    if len(idxs):
        c = coords[idxs]; dvx = dv[idxs]
        targets = nearest_bg_targets(gp, c, dvx) + 0.5
        dirs = targets - centers[idxs]
        norm = np.linalg.norm(dirs, axis=1, keepdims=True)
        dirs_u = np.zeros_like(dirs)
        ok = norm[:, 0] > 1e-9
        dirs_u[ok] = dirs[ok] / norm[ok]
        du = dirs_u
        solid_near = np.ones(len(idxs), bool)
        solid_far = np.ones(len(idxs), bool)
        for k in (1, 2, 3):
            pn = np.round(c + 0.5 + du * ((dvx + k)[:, None])).astype(int)          # 近侧面之外
            pf = np.round(c + 0.5 - du * ((np.maximum(dvx - 1.0, 0.0) + k)[:, None])).astype(int)  # 远侧面之外
            for pts, acc in ((pn, solid_near), (pf, solid_far)):
                inb = np.all((pts >= 0) & (pts < dims), axis=1)
                vi = np.flatnonzero(inb)
                solid = np.zeros(len(pts), bool)
                solid[vi] = gp[pts[vi, 0], pts[vi, 1], pts[vi, 2]]
                acc &= solid        # 连续性要求：k=1..3 全为材料才构成背衬
        backed = solid_near & solid_far
        gsel = idxs[~backed]
        genuine[coords[gsel, 0], coords[gsel, 1], coords[gsel, 2]] = True
    return genuine, coords[idxs[~backed]]
