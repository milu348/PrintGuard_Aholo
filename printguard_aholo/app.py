"""MCP 应用装配：只创建 `mcp` 实例与任务队列，不注册任何工具/路由。

拆分动机：
- `app` 不 import `tools` / `web`，避免循环依赖
- `tools.py`（MCP 工具）、`web.py`（HTTP 路由）都从这里取 `mcp` 实例
- `server.py` 只做入口：import app + tools + web 触发注册，然后跑 transport
"""
from __future__ import annotations

import time

from mcp.server.fastmcp import FastMCP

from . import __version__
from .config import HOST, HTTP_PATH, INSTRUCTIONS, LOG_LEVEL, PORT
from .jobs import JobManager

mcp = FastMCP(
    "PrintGuard_Aholo",
    instructions=INSTRUCTIONS,
    host=HOST,
    port=PORT,
    streamable_http_path=HTTP_PATH,
    log_level=LOG_LEVEL,
)

# 单 worker：Lux3D 生成类任务账号级并发 = 1，排队比并发失败更符合用户预期
JOBS = JobManager()

_STARTED = time.time()


def started_ts() -> float:
    return _STARTED


__all__ = ["mcp", "JOBS", "__version__", "started_ts"]
