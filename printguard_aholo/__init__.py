"""PrintGuard_Aholo —— FDM 打印件的「生成 → 体检 → 修复 → 交付」工具链。

模块地图：
- ``lux3d``     Lux3D 开放平台客户端（生成层）
- ``config``    环境变量与运行参数
- ``jobs``      单 worker 异步任务队列
- ``app``       FastMCP 实例与任务队列装配
- ``tools``     MCP 工具（生成层 / 管线层 / 任务查询）
- ``web``       HTTP 路由（/healthz、/、/files）
- ``server``    入口：装配 + 注册 + 运行
- ``profiles``  打印机 profile 与阈值
- ``meshio``    网格加载/导出
- ``inspect``   几何体检（水密/悬垂/壁厚/孔/孤岛/风险）
- ``repair``    几何修复（重构/平滑/增厚/摆向）
- ``pipeline``  生成 → 体检 → 修复 → 交付编排
- ``selftest``  合成用例回归

惰性导出（PEP 562）：避免包导入即拉起 numpy/trimesh 或 mcp。
"""

__version__ = "0.1.4"

_LAZY = {
    "lux3d": [
        "DEFAULT_BASE_URL", "STYLES", "STATUS_CANCELED", "STATUS_FAILED",
        "STATUS_INITIALIZING", "STATUS_RUNNING", "STATUS_SUCCEEDED",
        "Lux3DClient", "Lux3DError", "TaskResult", "resolve_key",
    ],
    "profiles": ["PrinterProfile", "BUILTIN_PROFILES", "load_profile"],
    "meshio": ["load_mesh"],
    "inspect": ["analyze_mesh", "surface_roughness"],
    "repair": ["repair"],
    "selftest": ["run_selftest"],
    "pipeline": ["run_pipeline", "run_repair", "run_generate", "PipelineError"],
}

__all__ = ["__version__"] + [name for names in _LAZY.values() for name in names]


def __getattr__(name: str):
    for module, names in _LAZY.items():
        if name in names:
            import importlib

            return getattr(importlib.import_module(f".{module}", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
