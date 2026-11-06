"""打印机 profile 与几何判定阈值。

阈值必须参数化：同一模型在不同喷嘴/层高下结论不同（0.4 喷嘴的 0.8mm
壁厚对 0.2 喷嘴就是偏厚）。几何模块统一从这里取阈值，不在算法里写死。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

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
