"""HTTP 路由层（托管形态核心部分）：健康检查与限时下载。

路由：
- `/healthz`     健康检查（含交付存储状态；llm 字段反映演示层是否配置）
- `/d/<token>`   限时下载（内存取件；过期 410）

聊天页与 Agent 回合接口（`/`、`/api/chat`、`/api/job`）属于可选演示层，
由独立包 ``modelscope_agent`` 提供并在组合根按需挂载——本模块不感知它们。

register_routes(ctx) 显式注册；本模块 import 不产生任何副作用。
"""
from __future__ import annotations

import logging
import re

from starlette.responses import JSONResponse, Response

from .. import __version__
from .app import AppContext

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

logger = logging.getLogger("printguard.web")


def register_routes(ctx: AppContext) -> None:
    """把核心 HTTP 路由注册到 ctx.mcp。"""
    mcp = ctx.mcp
    settings = ctx.settings

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request) -> JSONResponse:
        tools = await mcp.list_tools()
        return JSONResponse({
            "status": "ok",
            "service": "PrintGuard_Aholo",
            "version": __version__,
            "transport": settings.transport,
            "uptime_sec": ctx.uptime_sec(),
            "jobs": ctx.mcp_jobs.stats(),
            "tools": [tool.name for tool in tools],
            "runs_dir": str(settings.runs_dir),
            "public_base_url": settings.public_base_url or None,
            "delivery": ctx.store.stats(),
            "llm": {"configured": bool(settings.llm_token),
                    "model": settings.llm_model},
        })

    @mcp.custom_route("/d/{token}", methods=["GET"])
    async def download(request):
        """限时下载：内存取件，过期返回 410，磁盘上不留模型。"""
        token = request.path_params.get("token", "")
        ttl_min = max(1, round(ctx.store.ttl_sec / 60))
        ctx.store.purge_expired()
        if not _TOKEN_RE.match(token):
            return JSONResponse({"error": "非法 token"}, status_code=400)
        item = ctx.store.take(token)
        if item is None:
            return JSONResponse(
                {"error": f"链接已过期（限时交付，默认 {ttl_min} 分钟有效）"},
                status_code=410)
        filename, data, content_type = item
        return Response(
            content=data, media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'})
