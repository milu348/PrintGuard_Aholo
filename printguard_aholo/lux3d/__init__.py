"""Aholo 开放平台 API（Lux3D 生成 + Asset 上传）。

模块地图：
- ``client``  Lux3D 客户端：任务创建（文生 / 图生 / 文图混合）、轮询、查询、下载
- ``asset``   Asset 上传链：本地 / base64 图片 → 干净公网 URL（OUS COS 直传协议）

对外统一入口（由本包 re-export，调用方无需感知内部拆分）：
``Lux3DClient``、``Lux3DError``、``TaskResult``、``resolve_key``、``STYLES``。
"""
from __future__ import annotations

from .client import (
    DEFAULT_BASE_URL,
    STYLES,
    VERSIONS,
    Lux3DClient,
    Lux3DError,
    TaskResult,
    resolve_key,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "Lux3DClient",
    "Lux3DError",
    "STYLES",
    "TaskResult",
    "VERSIONS",
    "resolve_key",
]
