"""几何修复：碎片孤岛清理 / 体素重构 / Taubin 去阶梯 / 壁厚增厚 / 自动摆向。

修复原则：
- 尽量不改变外形，只处理「打印会失败」的缺陷；
- 体素重构必然有损（细节磨平到 pitch），必须接 Taubin 平滑去除 45° 阶梯；
- 摆向在平滑之后决定：阶梯伪法线会污染悬垂判断，导致选错姿态。
"""
from __future__ import annotations

import numpy as np
import trimesh

from .inspect import _genuine_thin, analyze_mesh, overhang_stats
from .profiles import MIN_WALL, PrinterProfile, load_profile
from .voxel import _max_voxels, _solid_grid


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
            removed.append(vol)
    if not removed:
        return m, None
    # 描述只给数量与体积区间——逐个列体积会把手办级碎片列表（可上百项）
    # 塞进报告与 Agent 上下文，纯噪声（v0.9.1 修）
    vols = sorted(removed)
    total = sum(vols)
    desc = (f"island_clean(移除 {len(removed)} 个碎片孤岛，"
            f"合计 {total:.1f}mm³，最大 {vols[-1]:.1f}mm³)")
    return trimesh.util.concatenate(keep), desc


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


def smooth_taubin(m: trimesh.Trimesh, iterations: int | None = None,
                  lamb: float = 0.5, nu: float = 0.53) -> trimesh.Trimesh:
    """Taubin λ/μ 平滑（保体积）：消除 marching-cubes 的 45° 阶梯条纹。

    为什么必须做：二值体素 + marching cubes 的等值面**必然带 45° 台阶**——
    台阶角由体素格的立方对称性决定，与 pitch 无关（实测 p95 折角恒为 45.0°，
    pitch 从 0.62 加密到 0.20mm 纹丝不动）。这就是「修复后很多面不平整」的
    根因：加密体素只会让台阶变密变小，不会消失，反而让面数翻 10 倍。

    Taubin 两段式：先 λ 收缩（压高频）、再 μ 膨胀（把体积补回来），μ>λ
    保证整体位置基本不动，只削掉阶梯这种高频噪声。
    实测（100mm 手机支架）：折角 >30° 占比 0.177 → 0.009（10 倍改善），
    p95 折角 45.0° → 14.4°，迭代 20 轮可达 15.2°；体积变化 -0.1%、
    水密性/外框尺寸/壁厚判定均不变。代价极低：13.8 万面 0.2s、27.5 万面
    0.4s，且**不增加峰值内存**（稀疏求解，实测 564MB 前后不变）。

    迭代数：15 轮是性价比拐点（5/10/20/30 轮实测差异 <0.01 且 Taubin 是周期
    滤波、收益随轮数递减——轮数过多反而钝化发丝级细节）；百万面网格降一档。
    """
    if m is None or len(m.faces) < 4:
        return m
    n_faces = len(m.faces)
    if iterations is None:
        iterations = 15 if n_faces <= 800_000 else 10
    if iterations <= 0:
        return m
    try:
        out = trimesh.smoothing.filter_taubin(m.copy(), lamb=lamb, nu=nu,
                                               iterations=iterations)
    except Exception:
        return m
    if out is None or len(out.faces) == 0:
        return m
    try:
        out.process(validate=False)
        trimesh.repair.fix_normals(out)
    except Exception:
        pass
    return out


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
    mw = float(min_wall or MIN_WALL)
    pitch = max(0.12, min(0.2, ext / 400.0))
    while (ext / pitch) ** 3 > _max_voxels() and pitch < 0.6:  # 内存护栏
        pitch *= 1.35
    g = _solid_grid(m, pitch)
    if not g.any():
        return None, None

    def probe(grid):
        gp = np.pad(grid, max_grow + 2)
        d = ndimage.distance_transform_edt(gp, sampling=pitch)
        d = d.astype(np.float32)
        maxf = ndimage.maximum_filter(d, size=3, mode="constant")
        t = 2 * d - pitch
        medial = (d >= maxf - 1e-5) & (d >= pitch - 1e-5) & gp
        mw = float(min_wall or MIN_WALL)
        genuine, _ = _genuine_thin(gp, d, medial, pitch, mw)
        # 5³ 邻域实体占比 ≥0.55：排除孤立板/壳的边缘棱（几何边缘≠薄壁）
        solid_frac = ndimage.uniform_filter(gp.astype(np.float32), size=5, mode="nearest")
        fail = genuine & (t <= mw - pitch) & (solid_frac >= 0.55)
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
        r_need = int(np.ceil((mw + 2 * pitch - t_min) / (2 * pitch)))
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
    """在候选摆放中选（悬垂占比最小，其次贴床面积最大）。
    内存版：复用单个探针实例逐候选重赋顶点（此前每候选整网格复制 + 缓存重建，
    135 万面 × 18 候选实测 +777MB 峰值）；选中后仅做一次最终复制。"""
    cands = _orient_candidates(m)
    if not cands:
        return m
    verts = np.asarray(m.vertices, float)
    probe = m.copy()
    best = None
    for T in cands:
        probe.vertices = verts @ T[:3, :3].T + T[:3, 3]
        probe.apply_translation(-probe.bounds[0])
        _, bad_frac, bed_area = overhang_stats(probe)
        key = (round(bad_frac, 4), -round(bed_area, 1))
        if best is None or key < best[0]:
            best = (key, T)
    out = m.copy()
    out.apply_transform(best[1])
    out.apply_translation(-out.bounds[0])
    return out


def repair(m: trimesh.Trimesh, do_orient: bool = True, pitch: float | None = None,
           fix_wall: bool = True, height_mm: float | None = None,
           smooth: bool = True):
    """检测+修复。height_mm 给定时，摆向后把最大边重缩放到该值
    （尺寸契约锁定在交付姿态——摆向会改变轴对齐包围盒）。

    smooth=True：每次体素重构后接一道 Taubin 平滑，消除 marching-cubes 的
    45° 阶梯（体素重构产物必有台阶，加密 pitch 不能消除、只会让面数暴涨）。
    平滑放在 orient 之前——阶梯产生的 45° 伪法线会污染悬垂判断，导致摆向
    选错姿态。"""
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
        if smooth:
            m = smooth_taubin(m)
            steps.append("smooth_taubin(去阶梯)")
    else:
        steps.append("watertight_ok(skip_rebuild)")
    if fix_wall:
        m2, wall_step = repair_thicken(m)
        if wall_step:
            if m2 is not None:
                m = m2
                if smooth:
                    m = smooth_taubin(m)
                    steps.append("smooth_taubin(去阶梯)")
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
