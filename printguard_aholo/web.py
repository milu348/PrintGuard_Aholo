"""HTTP 路由层（托管形态）：健康检查 / 服务信息 / 交付件下载。

路由：
- `/`            前端聊天页（Agent 形态，静态 HTML）
- `/api/chat`    前端 Agent 回合：大模型 function calling → MCP 工具 → 一次性交付
- `/healthz`     健康检查（含 LLM / 一次交付存储状态）
- `/d/<token>`   一次性下载（内存取件；过期/已用 410）
- `/files/...`   本地/stdio 形态的产物下载

安全约束：
- `/files` 只允许 runs 目录下一层 `<run_id>/<filename>`，拒绝 `..` 与绝对路径
- 路径全部 resolve 后校验父目录等于目标 run 目录（挡符号链接穿越）
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse,
                                 Response)

from . import __version__
from .app import JOBS, mcp, started_ts
from .config import (HTTP_PATH, LLM_MODEL, LLM_TOKEN, PUBLIC_BASE_URL,
                     RUNS_DIR, TRANSPORT)
from .delivery import STORE

STATIC_DIR = Path(__file__).resolve().parent / "static"

_RUN_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

logger = logging.getLogger("printguard.web")


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request) -> JSONResponse:
    tools = await mcp.list_tools()
    return JSONResponse({
        "status": "ok",
        "service": "PrintGuard_Aholo",
        "version": __version__,
        "transport": TRANSPORT,
        "uptime_sec": round(time.time() - started_ts()),
        "jobs": JOBS.stats(),
        "tools": [tool.name for tool in tools],
        "runs_dir": str(RUNS_DIR),
        "public_base_url": PUBLIC_BASE_URL or None,
        "delivery": STORE.stats(),
        "llm": {"configured": bool(LLM_TOKEN), "model": LLM_MODEL},
    })


@mcp.custom_route("/", methods=["GET"])
async def root(request):
    """前端聊天页（Agent 形态）；页面缺失时退回服务信息 JSON。"""
    page = STATIC_DIR / "index.html"
    if page.is_file():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return JSONResponse({
        "service": "PrintGuard_Aholo",
        "version": __version__,
        "mcp_endpoint": HTTP_PATH,
        "health": "/healthz",
        "files": "/files/<run_id>/<filename>",
        "hint": "前端页面未找到；MCP 客户端请连接 /mcp（streamable-http）"
                if TRANSPORT != "stdio" else "stdio 形态不提供 HTTP 服务",
    })


@mcp.custom_route("/api/chat", methods=["POST"])
async def api_chat(request):
    """提交一个 Agent 回合（立即返回 job_id，不阻塞网关）。

    概念图→四视图→建模可能 5-8 分钟，同步等待会被网关 504 掉；
    前端拿到 job_id 后轮询 /api/job/<job_id>。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
    history = body.get("history") or []
    if not isinstance(history, list):
        return JSONResponse({"error": "history 必须是消息数组"}, status_code=400)

    from . import agent  # 延迟导入：未配置 LLM 时不拉起额外依赖

    job_id = await run_in_threadpool(agent.RUNNER.submit, history[-40:])
    return JSONResponse({"job_id": job_id, "status": "queued",
                         "hint": "轮询 /api/job/<job_id> 查看进度"})


@mcp.custom_route("/api/job/{job_id}", methods=["GET"])
async def api_job(request):
    """查询 Agent 回合状态：queued / running / succeeded / failed。"""
    from . import agent

    job_id = request.path_params.get("job_id", "")
    record = agent.RUNNER.get(job_id)
    if record is None:
        return JSONResponse({"error": f"未知 job_id：{job_id}",
                             "job_id": job_id}, status_code=404)
    agent.STORE.purge_expired()
    return JSONResponse(record)


@mcp.custom_route("/files/{run_id}/{filename}", methods=["GET"])
async def files(request):
    """交付件下载：只允许 runs 目录内一层 run_id/filename，拒绝路径穿越。"""
    run_id = request.path_params.get("run_id", "")
    filename = request.path_params.get("filename", "")
    if (not _RUN_FILE_RE.match(run_id)
            or not _RUN_FILE_RE.match(filename)
            or run_id in (".", "..") or filename in (".", "..")):
        return JSONResponse({"error": "非法路径"}, status_code=400)
    run_dir = (RUNS_DIR / run_id).resolve()
    path = (run_dir / filename).resolve()
    if path.parent != run_dir or not path.is_file():
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    return FileResponse(path)


@mcp.custom_route("/d/{token}", methods=["GET"])
async def download(request):
    """一次性交付下载：内存取件，过期/已用返回 410，磁盘上不留模型。"""
    token = request.path_params.get("token", "")
    STORE.purge_expired()
    if not _TOKEN_RE.match(token):
        return JSONResponse({"error": "非法 token"}, status_code=400)
    item = STORE.take(token)
    if item is None:
        return JSONResponse(
            {"error": "链接已过期或已被使用（一次性交付，默认 15 分钟有效）"},
            status_code=410)
    filename, data, content_type = item
    return Response(
        content=data, media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
