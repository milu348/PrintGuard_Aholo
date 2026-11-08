"""打印机 profile 与几何判定阈值。

产品只面向 FDM 0.4 喷嘴 / 0.2 层高这一档：mini-0.2 与自定义 JSON 加载
曾存在，但 `load_profile()` 全仓库只以 None 调用，无第二个消费者，已移除。
"""
from __future__ import annotations

from dataclasses import dataclass

BED_SLAB = 0.3  # mm：视为贴床的底部厚度
MIN_WALL = 0.8  # mm：最小壁厚阈值（2×0.4 喷嘴）


@dataclass(frozen=True)
class PrinterProfile:
    nozzle_mm: float = 0.4
    layer_mm: float = 0.2
    min_wall_mm: float = 0.8      # ≈ 2×喷嘴
    overhang_deg: float = 45.0
    island_min_mm3: float = 60.0  # 低于此体积的孤岛视为碎片（飞件风险）


PROFILE = PrinterProfile()


def load_profile() -> PrinterProfile:
    """取内置 profile（产品只有一档，曾有的多 profile / JSON 加载已移除）。"""
    return PROFILE
