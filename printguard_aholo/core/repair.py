"""几何修复：碎片孤岛清理 / 体素重构 / Taubin 去阶梯 / 壁厚增厚 / 自动摆向。

修复原则：
- 尽量不改变外形，只处理「打印会失败」的缺陷；
- 体素重构必然有损（细节磨平到 pitch），必须接 Taubin 平滑去除 45° 阶梯；
- 摆向在平滑之后决定：阶梯伪法线会污染悬垂判断，导致选错姿态。
"""
from __future__ import annotations

import numpy as np
import trimesh

from .inspect import (analyze_mesh, genuine_thin, overhang_stats,
                      wall_thickness, watertight_stats)
from .profiles import MIN_WALL, PrinterProfile, load_profile
from .voxel import fit_pitch, solid_grid

# 比例原则（v0.1.1）：fail 体积占模型体积比低于该值时跳过增厚——
# 为千分之几的薄弱点重建整个表面（marching cubes + 降密）得不偿失，
# 薄弱点仅报告不改形。0.5% ≈ 一个 50mm 玩偶上允许 ~17.5mm³ 的局部薄区。
THICKEN_FAIL_VOL_RATIO = 0.005

# 重建保真度门槛（v0.1.1）：模型细节尺度（中位边长）< 3×可达重建 pitch 时
# 跳过体素重建——九尾妖狐案例：中位边长 0.25mm ≈ 可达 pitch（0.15~0.23mm），
# 重建后脸部/细腿/裙褶全部熔毁。此时诚实跳过重建、原样交付并报告风险，
# 而不是输出毁容模型。
REBUILD_DETAIL_RATIO = 3.0

# 微小孤岛的「贴近主体」距离（顶点近似）：距大部件 ≤3mm 的微小壳是
# 细节特征（毛发/装饰/浮丝），保留；远离主体的才是飞件风险，删除。
# 九尾妖狐案例教训：按个体体积判据会把整个发型当碎片删掉（秃头渲染）。
ISLAND_NEAR_GAP_MM = 3.0


def repair_island_clean(m: trimesh.Trimesh, profile: PrinterProfile):
    """修复步骤：删除「远离主体的」体积 < island_min 的碎片孤岛；
    贴近大部件的微小壳是细节特征（毛发/装饰），保留。体积未知者保守保留。
    返回 (mesh, 描述|None)。

    v0.1.1 内存安全重写：不再 m.split() 物化所有连通体——G1 带纹理输出
    常见数千个碎壳（发丝/细部独立壳），split 会把每个碎壳物化成完整
    Trimesh 对象（含材质引用），实测 5096 连通体直接 OOM（阿狸案例）。
    改为：连通体标记（face_adjacency，便宜）→ 逐块凸包体积判定 →
    贴近主体距离校验 → update_faces 删除，内存 O(面数)。

    判据两问：① 它小吗（凸包 < island_min）？② 它远离主体吗（顶点距离
    > ISLAND_NEAR_GAP_MM）？两者皆是才是飞件垃圾——贴近主体的微小壳
    是设计细节（发型/装饰），删了就是「秃头渲染」事故。"""
    if len(m.faces) == 0:
        return m, None
    try:
        labels = trimesh.graph.connected_component_labels(
            m.face_adjacency, node_count=len(m.faces))
    except Exception:
        return m, None
    n_comp = int(labels.max()) + 1
    if n_comp <= 1:
        return m, None

    face_areas = np.asarray(m.area_faces, float)
    areas = np.bincount(labels, weights=face_areas, minlength=n_comp)
    main = int(areas.argmax())          # 最大面积的主连通体无条件保留
    order = np.argsort(labels, kind="stable")
    starts = np.searchsorted(labels[order], np.arange(n_comp), side="left")
    ends = np.searchsorted(labels[order], np.arange(n_comp), side="right")

    from scipy.spatial import ConvexHull, cKDTree
    keep = np.zeros(len(m.faces), bool)
    small_cids = []          # (cid, comp_faces, hull_vol) 微小孤岛候选
    removed_vols = []
    vertices = np.asarray(m.vertices, float)
    faces = np.asarray(m.faces)
    for cid in range(n_comp):
        comp_faces = order[starts[cid]:ends[cid]]
        if cid == main:
            keep[comp_faces] = True
            continue
        try:
            verts = np.unique(faces[comp_faces].ravel())
            vol = float(ConvexHull(vertices[verts]).volume)
        except Exception:
            vol = float("inf")   # 凸包不可测（退化/共面碎壳）：保守保留
        if vol >= profile.island_min_mm3:
            keep[comp_faces] = True
        else:
            small_cids.append((cid, comp_faces, min(vol, 1e9)))

    # 第二遍（v0.1.1）：贴近大部件的微小孤岛是细节特征（毛发/装饰/浮丝），
    # 保留；远离主体的才是飞件风险，删除。阿狸案例：按个体体积判据会把
    # 整个发型当碎片删掉（秃头渲染）。
    if small_cids:
        large_verts = np.unique(faces[keep].ravel())
        tree = cKDTree(vertices[large_verts])
        for cid, comp_faces, vol in small_cids:
            comp_verts = np.unique(faces[comp_faces].ravel())
            dist, _ = tree.query(vertices[comp_verts], k=1)
            if float(dist.min()) <= ISLAND_NEAR_GAP_MM:
                keep[comp_faces] = True
            else:
                removed_vols.append(vol)

    if not removed_vols:
        return m, None
    # 描述只给数量与体积区间——逐个列体积会把手办级碎片列表（可上千项）
    # 塞进报告与 Agent 上下文，纯噪声（v0.9.1 修）
    m2 = m.copy()
    m2.update_faces(keep)
    m2.remove_unreferenced_vertices()
    vols = sorted(removed_vols)
    desc = (f"island_clean(移除 {len(vols)} 个碎片孤岛，"
            f"合计 {sum(vols):.1f}mm³，最大 {vols[-1]:.1f}mm³)")
    return m2, desc


def repair_voxel(m: trimesh.Trimesh, pitch: float | None = None):
    """体素重构修复：输出恒为水密实体；有损（细节磨平到 pitch 量级）。

    pitch 下限 0.15mm（v0.1.1 前是 0.4——对 50mm 手办是 1/125 分辨率，
    脸/手指/细腿全部熔毁，实测教训）。预算会自动放粗 pitch 控内存：
    内存紧张的容器上宁可诚实报告「分辨率不足」，也不输出毁容模型。"""
    if pitch is None:
        pitch = fit_pitch(float(m.extents.max()), lo=0.15, hi=2.0)
    g = solid_grid(m, pitch)
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
    pitch = fit_pitch(ext, lo=0.12, hi=0.2)
    g = solid_grid(m, pitch)
    if not g.any():
        return None, None

    def probe(grid):
        """一次探测。内存要点：EDT 直接落 float32（scipy 默认 float64，
        29M 体素上单独就是 240MB），t/medial 不返回（调用方下一轮只需要
        gp/d/fail），让中间数组尽早可回收。"""
        gp = np.pad(grid, max_grow + 2)
        d = np.asarray(ndimage.distance_transform_edt(gp, sampling=pitch),
                       dtype=np.float32)
        maxf = ndimage.maximum_filter(d, size=3, mode="constant")
        medial = (d >= maxf - 1e-5) & (d >= pitch - 1e-5) & gp
        del maxf
        mw = float(min_wall or MIN_WALL)
        genuine, _ = genuine_thin(gp, d, medial, pitch, mw)
        # 5³ 邻域实体占比 ≥0.55：排除孤立板/壳的边缘棱（几何边缘≠薄壁）
        solid_frac = ndimage.uniform_filter(gp.astype(np.float32), size=5,
                                            mode="nearest")
        # fail = genuine & (2d - pitch <= mw - pitch) & solid_frac>=0.55
        #       ⟺ genuine & (2d <= mw)             & solid_frac>=0.55
        fail = genuine & (2 * d <= mw) & (solid_frac >= 0.55)
        del medial, solid_frac
        return gp, d, fail

    gp, d, fail = probe(g)
    if not fail.any():
        return None, None

    # 比例原则：fail 体积占比过小时不值得全表面重建（v0.1.1 教训——
    # 奶龙案例：0.001% 的薄弱点触发重建 + 降密，交付表面出现波纹劣化）
    fail_vol = float(fail.sum()) * pitch**3
    model_vol = abs(float(m.volume))
    if model_vol > 0 and fail_vol < THICKEN_FAIL_VOL_RATIO * model_vol:
        return None, (f"wall_thicken_skipped(比例原则: fail {fail_vol:.2f}mm³ 占"
                      f"{fail_vol / model_vol * 100:.3f}% < "
                      f"{THICKEN_FAIL_VOL_RATIO * 100:g}%，薄弱点仅报告不改形)")

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
        gp, d, fail = probe(g)
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
           smooth: bool = True, analyze: bool = True,
           force_rebuild: bool = False):
    """检测+修复。height_mm 给定时，摆向后把最大边重缩放到该值
    （尺寸契约锁定在交付姿态——摆向会改变轴对齐包围盒）。

    smooth=True：每次体素重构后接一道 Taubin 平滑，消除 marching-cubes 的
    45° 阶梯（体素重构产物必有台阶，加密 pitch 不能消除、只会让面数暴涨）。
    平滑放在 orient 之前——阶梯产生的 45° 伪法线会污染悬垂判断，导致摆向
    选错姿态。"""
    steps = []
    mesh_original = m
    pf = load_profile()
    m, clean_step = repair_island_clean(m, pf)
    if clean_step:
        steps.append(clean_step)
    # 是否重构只看水密性——不需要完整 analyze（那要建 1.25 亿体素的大网格）
    wt = watertight_stats(m)
    needs_rebuild = (not wt["watertight"]) or wt["nonmanifold_edges"] > 0
    rebuild_blocked = False
    if needs_rebuild:
        r_pitch = pitch if pitch is not None else fit_pitch(
            float(m.extents.max()), lo=0.15, hi=2.0)
        # 重建保真度门槛：模型细节尺度（中位边长）逼近可达重建分辨率时，
        # 体素重构必然抹平细节——诚实跳过重建、原样交付并报告，而不是
        # 输出毁容模型（九尾妖狐案例实测教训）。force_rebuild=True 可显式
        # 越过门槛（调用方自担后果）。
        detail = float(np.median(m.edges_unique_length))
        if (not force_rebuild
                and detail < REBUILD_DETAIL_RATIO * r_pitch):
            steps.append(
                f"rebuild_skipped(保真度门槛: 中位边长{detail:.2f}mm < "
                f"{REBUILD_DETAIL_RATIO:g}×可达pitch{r_pitch:.2f}mm，"
                "细节密集非水密模型原样交付)")
            rebuild_blocked = True
    if needs_rebuild and not rebuild_blocked:
        m, p = repair_voxel(m, r_pitch)
        steps.append(f"voxel_rebuild(pitch={p:.2f}mm)")
        if smooth:
            m = smooth_taubin(m)
            steps.append("smooth_taubin(去阶梯)")
    elif not needs_rebuild:
        steps.append("watertight_ok(skip_rebuild)")
    if fix_wall and not rebuild_blocked:
        m2, wall_step = repair_thicken(m)
        if wall_step:
            if m2 is not None:
                m = m2
                if smooth:
                    m = smooth_taubin(m)
                    steps.append("smooth_taubin(去阶梯)")
            steps.append(wall_step)
    # 二次清岛（v0.1.1）：体素重构/增厚的 marching cubes 会产生浮点噪声碎屑
    # （2 面翻转小片等），它们让整体 is_watertight=False，违背「输出恒为水密
    # 实体」的承诺。复用 island_clean（凸包体积口径）清掉，主实体不受影响。
    if any(s.startswith(("voxel_rebuild", "wall_thicken(")) for s in steps):
        m, clean2 = repair_island_clean(m, pf)
        if clean2:
            steps.append(clean2)
    if do_orient:
        m = orient(m)
        steps.append("orient(overhang_min)")
    if height_mm:
        s = float(height_mm) / float(max(m.extents))
        if abs(s - 1.0) > 1e-3:
            m.apply_scale(s)
            steps.append(f"scale_to({height_mm:g}mm)")
    # analyze=False：不算体检（管线/服务只交付模型，before/after 全被丢弃，
    # 而一次 analyze 在 10 万面 mesh 上要 ~74s + 数 GB）。CLI --json 与 selftest
    # 用默认 True，前后对比与判定口径都不变。
    before = analyze_mesh(mesh_original) if analyze else None
    after = analyze_mesh(m) if analyze else None
    return m, steps, before, after
