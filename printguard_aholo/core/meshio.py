"""网格 I/O：加载（可选高度归一）与导出。"""
from __future__ import annotations

import trimesh


def load_mesh(path: str, height_mm: float | None = None) -> trimesh.Trimesh:
    """加载网格；height_mm 给定时把最大边缩放到该值。

    GLB/GLTF 的单位不可信（可能来自任意建模软件），打印前必须用
    height_mm 锁定尺寸，否则壁厚/悬垂结论全部不可比。
    """
    m = trimesh.load(path, force="mesh")
    if len(m.faces) == 0:
        raise ValueError(f"网格为空或无法解析：{path}")
    if height_mm:
        m.apply_scale(float(height_mm) / float(max(m.extents)))
    m.apply_translation(-m.bounds[0])
    return m


