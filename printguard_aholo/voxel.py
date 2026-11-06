"""体素化底层算子（检测与修复共用）。

- `_surface_voxels`：面积加权表面采样，替代 trimesh 细分式体素化（长三角形内存炸弹）
- `_raw_grid`：真实几何网格（悬垂支撑探测用，不做实心化）
- `_solid_grid`：实心化网格（closing + fill_holes，壁厚/孔检测用）

内存护栏：`PRINTGUARD_MAX_VOXELS` 可调体素上限，超限自动放粗 pitch。
"""
from __future__ import annotations

import os

import numpy as np
import trimesh

DEFAULT_MAX_VOXELS = 150_000_000  # 体素网格上限（≈7GB 量级，见 README 大件实测）


def _max_voxels() -> float:
    """体素网格上限（内存护栏）。低内存环境可用环境变量 PRINTGUARD_MAX_VOXELS
    收紧，pitch 会自动放粗、内存随之下探——同一份代码即可在 8GB 桌面与
    ~2GB 免费沙箱上运行（精度换内存）。默认 1.5e8（DEFAULT_MAX_VOXELS）。"""
    try:
        return float(os.environ.get("PRINTGUARD_MAX_VOXELS", DEFAULT_MAX_VOXELS))
    except (TypeError, ValueError):
        return float(DEFAULT_MAX_VOXELS)


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
            # 不去重：布尔网格重复写入 True 与去重后等价，而 np.unique 的排序去重
            # 是采样体素化的最大热点（大规模采样下每次 O(n log n)）。
            g[idx[:, 0], idx[:, 1], idx[:, 2]] = True

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
