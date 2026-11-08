"""几何内核：纯计算、零服务依赖（不 import config / lux3d / pipeline）。

模块地图（依赖方向单向：profiles → meshio → voxel → inspect → repair → selftest）：
- ``profiles``  打印机 profile 与判定阈值
- ``meshio``    网格加载 / 尺寸归一
- ``voxel``     体素化底层算子与内存护栏
- ``inspect``   几何体检（水密 / 悬垂 / 壁厚——只保留驱动修复决策的三项）
- ``repair``    几何修复（碎片清理 / 重构 / 平滑 / 增厚 / 摆向）
- ``selftest``  合成用例回归

对外统一入口：``analyze_mesh``（体检）、``repair``（修复）、``load_mesh``（加载）、
``run_selftest``（回归）。算法与判定口径自 v0.1.x 起保持不变。
"""
from __future__ import annotations

from .inspect import analyze_mesh, overhang_stats, watertight_stats, wall_thickness
from .meshio import load_mesh
from .profiles import PrinterProfile, load_profile
from .repair import orient, repair, repair_thicken, repair_voxel, smooth_taubin
from .selftest import run_selftest, selftest

__all__ = [
    "analyze_mesh",
    "load_mesh",
    "load_profile",
    "orient",
    "overhang_stats",
    "PrinterProfile",
    "repair",
    "repair_thicken",
    "repair_voxel",
    "run_selftest",
    "selftest",
    "smooth_taubin",
    "wall_thickness",
    "watertight_stats",
]
