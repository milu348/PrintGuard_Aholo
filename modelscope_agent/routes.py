"""演示层的 HTTP 路由：聊天页 / Agent 回合接口。

由组合根（server.main 或测试）在 ``PRINTGUARD_LLM_TOKEN`` 已配置时显式挂载：
    from modelscope_agent import register_routes
    register_routes(ctx)

printguard_aholo 核心不注册也不感知这些路由——不挂载时 /、/api/chat、
/api/job 直接 404，核心 MCP 服务（/mcp、/healthz、/d/<token>）不受影响。
"""
from __future__ import annotations

from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse

from printguard_aholo import __version__
from printguard_aholo.service.app import AppContext

from . import agent

STATIC_DIR = Path(__file__).resolve().parent / "static"


def register_routes(ctx: AppContext) -> None:
    """把聊天页与 Agent 回合接口注册到 ctx.mcp（仅演示层启用时调用）。"""
    mcp = ctx.mcp

    @mcp.custom_route("/", methods=["GET"])
    async def root(request):
        """前端聊天页；页面缺失时退回服务信息 JSON。"""
        page = STATIC_DIR / "index.html"
        if page.is_file():
            return HTMLResponse(page.read_text(encoding="utf-8"))
        return JSONResponse({
            "service": "PrintGuard_Aholo",
            "version": __version__,
            "mcp_endpoint": ctx.settings.http_path,
            "health": "/healthz",
            "hint": "前端页面未找到；MCP 客户端请连接 /mcp（streamable-http）",
        })

    @mcp.custom_route("/api/chat", methods=["POST"])
    async def api_chat(request):
        """提交一个 Agent 回合（立即返回 job_id，不阻塞网关）。

        生成+修复可能 3-6 分钟，同步等待会被网关 504 掉；
        前端拿到 job_id 后轮询 /api/job/<job_id>。
        请求体 {"history": [{role, content, images?}]}，images 为 data URI 列表。
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
        history = body.get("history") or []
        if not isinstance(history, list):
            return JSONResponse({"error": "history 必须是消息数组"}, status_code=400)

        job_id = await run_in_threadpool(agent.submit_turn, ctx, history[-40:])
        return JSONResponse({"job_id": job_id, "status": "queued",
                             "hint": "轮询 /api/job/<job_id> 查看进度"})

    @mcp.custom_route("/api/job/{job_id}", methods=["GET"])
    async def api_job(request):
        """查询 Agent 回合状态：queued / running / succeeded / failed。"""
        job_id = request.path_params.get("job_id", "")
        ctx.store.purge_expired()
        record = agent.get_turn(ctx, job_id)
        if record is None:
            return JSONResponse({"error": f"未知 job_id：{job_id}",
                                 "job_id": job_id}, status_code=404)
        return JSONResponse(record)
