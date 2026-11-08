"""MCP 服务入口：`Settings.from_env()` → `build_app()` → 运行（stdio / HTTP）。

模块职责（v0.2 分层后）：
- `config.py`        Settings：环境变量只在 from_env() 读一次
- `service/app.py`   build_app：显式装配 FastMCP + 队列 + 交付存储 + 工具/路由
- `service/tools.py` 7 个 MCP 工具（管线层 / 生成层 / 任务查询）
- `service/web.py`   HTTP 路由（/healthz、/d/<token>）
- `pipeline/`        生成（文生/图生/混合）→ 体检 → 修复 → 交付编排
- `lux3d/`           平台客户端 + Asset 上传链；`service/jobs.py` 异步队列
- `core/`            几何内核（纯计算，离线可用）
- `modelscope_agent/` 可选演示层：网页 Agent（组合根按需挂载，核心不感知）

启动：
    python -m printguard_aholo.server              # 默认 stdio
    PRINTGUARD_TRANSPORT=streamable-http python -m printguard_aholo.server
"""
from __future__ import annotations

import logging
import sys

from . import __version__
from .config import Settings
from .pipeline.delivery import cleanup_run_dirs
from .service.app import build_app

logger = logging.getLogger("printguard.server")


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level,
        stream=sys.stderr,  # 绝不能用 stdout：stdio 传输时 stdout 是协议通道
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings.ensure_runs_dir()
    # 启动时清掉上次残留的 run 目录（崩溃/被杀会留半个目录）
    cleanup_run_dirs(settings.runs_dir, settings.run_retention_hours)

    app = build_app(settings)

    # 组合根按需挂载可选演示层（网页 Agent）：核心服务不 import 它
    if settings.llm_token:
        from modelscope_agent import register_routes as register_agent_routes

        register_agent_routes(app)
        logger.info("前端 Agent 已启用（modelscope_agent）：/ 聊天页, "
                    "/api/chat, /api/job")
    else:
        logger.info("未配置 PRINTGUARD_LLM_TOKEN：前端 Agent 未启用"
                    "（MCP 服务 /mcp 不受影响）")

    if settings.transport == "stdio":
        logger.info("PrintGuard_Aholo %s 启动（stdio）", __version__)
        app.mcp.run(transport="stdio")
    elif settings.transport in ("streamable-http", "http"):
        logger.info("PrintGuard_Aholo %s 启动（streamable-http）："
                    "http://%s:%s%s", __version__, settings.host,
                    settings.port, settings.http_path)
        app.mcp.run(transport="streamable-http")
    else:
        raise SystemExit(f"不支持的 PRINTGUARD_TRANSPORT：{settings.transport}"
                         "（可选 stdio / streamable-http）")


if __name__ == "__main__":
    main()
