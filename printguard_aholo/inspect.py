"""几何体检：水密性 / 悬垂（含向下支撑探测）/ 壁厚 / 孔 / 孤岛 / 平整度 / 风险评级。

设计口径：
- 对外只给「检测结论 + 可解释指标」，不抛底层异常；
- 非水密网格跳过体素类分析（壳汤上算壁厚没有意义）；
- 长耗时项（壁厚、支撑探测）共用同一体素网格，避免重复体素化。
"""
from __future__ import annotations

import numpy as np
import trimesh

from .profiles import BED_SLAB, MIN_WALL, PrinterProfile, load_profile
from .voxel import _max_voxels, _raw_grid, _solid_grid


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
    pitch = max(0.15, min(0.4, ext / 300.0))
    while (ext / pitch) ** 3 > _max_voxels() and pitch < 0.8:  # 内存护栏
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


_SURROUND_DIRS = np.array([(x, y, z) for x in (-1, 0, 1) for y in (-1, 0, 1)
                           for z in (-1, 0, 1) if (x, y, z) != (0, 0, 0)],
                          dtype=np.float64)
_SURROUND_DIRS /= np.linalg.norm(_SURROUND_DIRS, axis=1, keepdims=True)


def hole_stats(m: trimesh.Trimesh, pitch: float, min_hole_mm: float,
               grid: np.ndarray | None = None) -> dict:
    """孔径检测：内径 < min_hole_mm 的孔/凹腔（FDM 打印会堵塞或丢失特征）。

    体素法：背景体素 EDT（到最近固体距离）≥ 0.5×半径阈值者为「宽空气区」候选
    （0.5× 补偿体素阶梯对窄孔的侵蚀）。连通归组后逐组件判别：
      ① 组件内凸包内的采样点存在（排除模型外部空气——贯穿孔与外部连通）；
      ② 组件内 EDT 最大点的球域固体占比 ≥0.45（真孔被壁面包围）。
    孔径 = 2×(EDT_max + pitch)——补偿体素阶梯侵蚀（实测 Ø0.9→0.80、Ø4.0→3.91）。"""
    from scipy import ndimage as _ndi
    r_min_mm = float(min_hole_mm) / 2.0
    g = _solid_grid(m, pitch) if grid is None else grid
    bg = ~g
    out = {"min_hole_mm": round(min_hole_mm, 2), "holes": 0, "small_holes": 0,
           "verdict": "pass", "details": []}
    if not bg.any():
        return out
    bg_d = _ndi.distance_transform_edt(bg, sampling=pitch)
    wide = bg & (bg_d >= 0.5 * r_min_mm)
    if not wide.any():
        return out
    labels, n = _ndi.label(wide, structure=_ndi.generate_binary_structure(3, 1))
    if n == 0:
        return out
    try:
        hull = m.convex_hull
        bounds0 = np.asarray(m.bounds[0], float)
        hull_contains = hull.contains
    except Exception:
        hull_contains = None
    # 逐连通域只在其包围盒内取体素：原写法 np.argwhere(labels == cid) 对每个域全网格
    # 扫描一遍（O(N·K)，宽空气区动辄上百域 → 数十秒），find_objects 限定到 bbox 后
    # 每个域只处理自身规模，结果与逐域全扫描完全一致。
    slices = _ndi.find_objects(labels)
    details = []
    for cid in range(1, n + 1):
        sl = slices[cid - 1]
        if sl is None:
            continue
        pts = np.argwhere(labels[sl] == cid) + np.array([s.start for s in sl])
        if hull_contains is not None:
            sub = pts[np.linspace(0, len(pts) - 1, min(len(pts), 600)).astype(int)]
            phys = bounds0 + (sub + 0.5) * pitch
            try:
                inside = hull_contains(phys)
            except Exception:
                inside = np.ones(len(phys), bool)
            if not inside.any():
                continue        # 整个组件都在凸包外 = 外部空气
            sub_in = sub[inside]
            d_in = bg_d[sub_in[:, 0], sub_in[:, 1], sub_in[:, 2]]
        else:
            sub_in = pts
            d_in = bg_d[pts[:, 0], pts[:, 1], pts[:, 2]]
        c = sub_in[int(np.argmax(d_in))]
        radius_mm = float(bg_d[c[0], c[1], c[2]])
        d_est = 2 * (radius_mm + pitch)   # 补偿体素阶梯侵蚀（实测标定：Ø0.9/Ø4.0 均吻合）
        # 环绕射线判别（真孔被固体多面包围；外部空气只朝模型方向命中）：
        # 26 个立方邻域方向射线，命中固体且距离 ≤ radius+2*pitch 的方向数 ≥8 → 孔/腔内部
        c_phys = np.asarray(m.bounds[0], float) + (c + 0.5) * pitch
        reach_mm = radius_mm + 2 * pitch
        locs, ray_idx, _ = m.ray.intersects_location(
            np.repeat(c_phys[None, :], len(_SURROUND_DIRS), axis=0), _SURROUND_DIRS)
        if len(locs):
            d_hit = np.linalg.norm(locs - c_phys[None, :], axis=1)
            hit_dirs = np.unique(ray_idx[d_hit <= reach_mm])
        else:
            hit_dirs = []
        if len(hit_dirs) < 8:
            continue
        details.append({"diameter_mm": round(d_est, 2),
                        "center_mm": [round(float(x), 1) for x in c_phys],
                        "surround_dirs": int(len(hit_dirs)), "voxels": int(len(pts))})
    details.sort(key=lambda h: h["diameter_mm"])
    out["holes"] = len(details)
    out["small_holes"] = sum(1 for h in details if h["diameter_mm"] < min_hole_mm)
    out["details"] = details[:5]
    if out["small_holes"]:
        out["verdict"] = "warn"
    return out


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
    holes = {"verdict": "skipped（随体素分析）", "holes": None, "small_holes": None}
    if thickness:
        pitch_h = max(0.12, min(0.2, float(max(m.extents)) / 400.0))
        while (float(max(m.extents)) / pitch_h) ** 3 > _max_voxels() and pitch_h < 0.6:
            pitch_h *= 1.35
        grid = _solid_grid(m, pitch_h)
        wall = wall_thickness(m, min_wall=pf.min_wall_mm, grid=grid)
        sup = _support_split(m, pf.overhang_deg)
        islands = island_stats(m, pf)
        holes = hole_stats(m, pitch_h, pf.min_hole_mm, grid=grid)
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
            "holes": holes,
            "surface_roughness": surface_roughness(m),
        },
        "risk": {"grade": grade, "score": score},
    }


def surface_roughness(m: trimesh.Trimesh) -> dict:
    """表面平整度（相邻面折角分布，度）：0°=共面，越大越"毛刺/不平整"。

    两种病态各有特征指纹：
    - 体素阶梯：折角集中在 30°~55°（p95 ≈ 45°）；
    - AI 原始壳汤自交：大量 >90° 的折角（p99 常 >100°）。
    报告里给出 mean/p95/frac>30° 三项，前后对比即可看出「修粗糙了没有」。
    非流形边（>2 面共边）无法定义二面角，统计时跳过并如实计数。
    """
    f = np.asarray(m.faces)
    if len(f) < 2:
        return {"paired_edges": 0, "mean_deg": None, "p95_deg": None,
                "frac_gt30": None, "nonmanifold_edges_skipped": 0}
    tri = np.asarray(m.vertices, float)[f]
    nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    ln = np.linalg.norm(nrm, axis=1)
    ln[ln == 0] = 1.0
    nrm = nrm / ln[:, None]
    # 每个面的三条无向边 + 对应面号（edge-row -> face index）
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
    i1, i2 = starts[sel], starts[sel] + 1
    if len(i1) == 0:
        return {"paired_edges": 0, "mean_deg": None, "p95_deg": None,
                "frac_gt30": None,
                "nonmanifold_edges_skipped": int((counts > 2).sum())}
    n1, n2 = nrm[fs[i1]], nrm[fs[i2]]
    ang = np.degrees(np.arccos(np.clip((n1 * n2).sum(axis=1), -1.0, 1.0)))
    return {
        "paired_edges": int(len(ang)),
        "mean_deg": round(float(ang.mean()), 2),
        "p95_deg": round(float(np.percentile(ang, 95)), 2),
        "frac_gt30": round(float((ang > 30).mean()), 4),
        "nonmanifold_edges_skipped": int((counts > 2).sum()),
    }


def wall_thickness(m: trimesh.Trimesh, pitch: float | None = None,
                   min_wall: float | None = None,
                   grid: np.ndarray | None = None) -> dict:
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
    while (ext / pitch) ** 3 > _max_voxels() and pitch < 0.6:  # 内存护栏
        pitch *= 1.35
    g = _solid_grid(m, pitch) if grid is None else grid
    if not g.any():
        return {"verdict": "skipped", "reason": "体素化后为空", "pitch_mm": round(pitch, 2)}
    gp = np.pad(g, 2)
    origin = np.asarray(m.bounds[0], float)      # 栅格→物理：origin + (gp_idx - 2 + 0.5) * pitch

    def to_phys(coords_gp):
        return origin + (np.asarray(coords_gp, float) - 2 + 0.5) * pitch
    d_pad = ndimage.distance_transform_edt(gp, sampling=pitch)
    d = d_pad.astype(np.float32)[2:-2, 2:-2, 2:-2]   # 未 pad 空间（与 g 同构）
    maxf = ndimage.maximum_filter(d, size=3, mode="constant")
    medial = (d >= maxf - 1e-5) & (d >= pitch - 1e-5) & g
    if not medial.any():
        return {"verdict": "skipped", "reason": "未找到中轴点（网格过细）",
                "pitch_mm": round(pitch, 2)}
    genuine, coords_g = _genuine_thin(gp, d_pad, medial, pitch, mw)
    thin = medial & genuine
    vals_all = d[medial]
    t_min_raw = float(2 * float(vals_all.min()) - pitch)   # 原始最小壁厚（含凹角，进报告）
    # 薄弱区聚合：warn+fail 带的中轴体素按连通域归组（报告取 top5）
    t_all = 2 * d - pitch
    thin_band = thin & (t_all < mw + 2 * pitch)
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
            region_coords.append(coords)          # 未 pad 空间（与 d/t_all 一致）
            regions.append({"id": len(regions) + 1, "voxels": int(sizes[rid]),
                            "t_min_mm": round(float(t_reg.min()), 2),
                            "center_mm": to_phys(coords.mean(axis=0) + 2).round(1).tolist(),
                            "bbox_mm": [to_phys(coords.min(axis=0) + 2).round(1).tolist(),
                                        to_phys(coords.max(axis=0) + 2).round(1).tolist()]})
    if thin_band.any():
        t_min = float(2 * float(d[thin_band].min()) - pitch)
        verdict = "fail" if t_min <= mw - pitch else "warn"
    else:
        t_min = t_min_raw
        verdict = "pass"     # 无独立薄壁：凹角/被材料包裹结构均非打印障碍
    # 薄弱区精测：EDT 是 pitch 量化值（±pitch 误差）。对每个薄弱区采样中轴体素，
    # 沿「指向最近背景体素」的方向双向射线打到原始三角面，T = 去程 + 回程命中距离——
    # 不重体素化、不重网格化，直接从真实几何测出精确壁厚。
    if regions:
        try:
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
                sel_dv = d_pad[sel_gp[:, 0], sel_gp[:, 1], sel_gp[:, 2]] / pitch
                targets = to_phys(_nearest_bg_targets(gp, sel_gp, sel_dv))
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
        "min_wall_raw_mm": round(t_min_raw, 2),   # 含凹角的原始值（信息参考）
        "threshold_mm": mw,
        "pitch_mm": round(pitch, 2),
        "thin_regions": regions,
        "method": "voxel EDT medial, T=2D-p（量化）；薄弱区附射线精测 t_precise_mm（三角面级）",
    }


def _nearest_bg_targets(gp, coords, dv):
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


def _genuine_thin(gp, d_mm, medial, pitch, mw):
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
        targets = _nearest_bg_targets(gp, c, dvx) + 0.5
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
