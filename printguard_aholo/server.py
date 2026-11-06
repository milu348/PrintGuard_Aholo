"""MCP 服务入口：装配 → 注册 → 运行。

模块职责：
- `config.py`  环境变量与运行参数
- `app.py`     FastMCP 实例 + 单 worker 任务队列
- `tools.py`   9 个 MCP 工具（生成层 / 管线层 / 任务查询）
- `web.py`     HTTP 路由（/healthz、/、/files 下载）
- `pipeline.py`生成 → 体检 → 修复 → 交付编排（结果=模型+图片）
- `delivery.py`一次性交付：内存取件 + 一次性链接，run 目录不长期保留
- `lux3d.py`   平台客户端；`jobs.py` 异步队列

启动：
    python -m printguard_aholo.server              # 默认 stdio
    PRINTGUARD_TRANSPORT=streamable-http python -m printguard_aholo.server
"""
from __future__ import annotations

import logging
import sys

from . import __version__
from . import tools as _tools  # noqa: F401 —— import 即注册 MCP 工具
from . import web as _web      # noqa: F401 —— import 即注册 HTTP 路由
from .app import JOBS, mcp
from .config import (DELIVERY_MODE, HTTP_PATH, HOST, LOG_LEVEL, PORT,
                     PUBLIC_BASE_URL, RUNS_DIR, RUN_RETENTION_HOURS,
                     TRANSPORT, ensure_runs_dir)
from .delivery import cleanup_run_dirs
from .jobs import JobManager  # noqa: F401 —— 供外部按需复用

logger = logging.getLogger("printguard.server")


def main() -> None:
    logging.basicConfig(
        level=LOG_LEVEL,
        stream=sys.stderr,  # 绝不能用 stdout：stdio 传输时 stdout 是协议通道
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ensure_runs_dir()
    if DELIVERY_MODE == "memory":
        # 托管形态：模型转一次性交付，启动时清掉上次残留的 run 目录
        cleanup_run_dirs(RUNS_DIR, RUN_RETENTION_HOURS)
    if TRANSPORT == "stdio":
        logger.info("PrintGuard_Aholo %s 启动（stdio）", __version__)
        mcp.run(transport="stdio")
    elif TRANSPORT in ("streamable-http", "http"):
        logger.info("PrintGuard_Aholo %s 启动（streamable-http）："
                    "http://%s:%s%s", __version__, HOST, PORT, HTTP_PATH)
        mcp.run(transport="streamable-http")
    else:
        raise SystemExit(f"不支持的 PRINTGUARD_TRANSPORT：{TRANSPORT}"
                         "（可选 stdio / streamable-http）")


if __name__ == "__main__":
    main()
