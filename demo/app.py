"""PrintGuard_Aholo 网页 Demo —— LLM 编排 + MCP 调用方。

职责边界（架构约定）：
- 本应用 **不运行** PrintGuard 服务本体，仅作为 MCP 客户端调用
  魔搭 MCP 部署服务上的 hosted 实例；
- LLM 编排使用魔搭 API-Inference（OpenAI 兼容），工具 schema 直接取自 MCP list_tools；
- printguard_start_job 为异步任务：此处包装为「提交 + 自动轮询 + 汇总」，
  对 LLM 呈现为一个同步工具，轮询进度以事件流推给前端。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ.get("PRINTGUARD_MCP_URL",
                         "https://mcp.api-inference.modelscope.net/0907605ef46a4d/mcp")
LLM_BASE = os.environ.get("DEMO_LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")
LLM_MODEL = os.environ.get("DEMO_LLM_MODEL", "z-ai/glm-5.3-flash")
LLM_TOKEN = os.environ.get("LLM_API_TOKEN", "")
MAX_TURNS = 8
JOB_POLL_INTERVAL = 5.0
JOB_POLL_CAP = 240          # 240 × 5s = 20 分钟上限

SESSIONS: dict[str, dict] = {}

_SYSTEM_PROMPT = (
    "你是 PrintGuard_Aholo 的产品助手：帮用户把 AI 生成的 3D 模型变成能上 FDM 打印机的东西。\n"
    "可用工具（MCP）：printguard_start_job（提交生成/修复任务）、printguard_check_job（查任务）、"
    "printguard_analyze_printability（体检已有模型，水密/悬垂/壁厚）。\n"
    "行为准则：\n"
    "1. 用户给了模型链接且想知道能不能打 → 调 printguard_analyze_printability；\n"
    "2. 用户要修复已有模型 → 调 printguard_start_job(file_or_url=...)；\n"
    "3. 工具返回 JSON 时，用通俗中文解读关键结论（风险等级、水密性、悬垂、壁厚），不要堆原始 JSON；\n"
    "4. 生成全新模型的功能在当前 Demo 环境暂未开放（需要 AHOLO key），如用户想生成，"
    "请说明并建议改用检测/修复，或引导到 labs.aholo3d.cn 获取 key 后在本地 Agent 使用；\n"
    "5. 回答简洁，重点前置。"
)


# ---------- MCP 调用 ----------

async def _mcp_list_tools() -> list[dict]:
    async with streamablehttp_client(MCP_URL) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return [{"type": "function",
                     "function": {"name": t.name,
                                  "description": t.description or "",
                                  "parameters": t.inputSchema or {"type": "object", "properties": {}}}}
                    for t in (await s.list_tools()).tools]


async def _mcp_call(name: str, args: dict) -> dict:
    async with streamablehttp_client(MCP_URL) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(name, args)
            text = res.content[0].text if res.content else "{}"
            try:
                return {"ok": not res.isError, "data": json.loads(text)}
            except json.JSONDecodeError:
                return {"ok": not res.isError, "data": {"text": text}}


async def _mcp_call_with_poll(name: str, args: dict, emit) -> dict:
    """printguard_start_job 包装：提交后自动轮询直至完成，进度以事件流推送。"""
    sub = await _mcp_call(name, args)
    if not sub.get("ok"):
        return sub
    job_id = (sub.get("data") or {}).get("job_id")
    if not job_id:
        return sub
    emit("job", {"stage": "submitted", "job_id": job_id})
    waited = 0.0
    last = None
    while waited < JOB_POLL_INTERVAL * JOB_POLL_CAP:
        await asyncio.sleep(JOB_POLL_INTERVAL)
        waited += JOB_POLL_INTERVAL
        st = await _mcp_call("printguard_check_job", {"job_id": job_id})
        data = st.get("data") or {}
        status = data.get("status")
        if status != last:
            emit("job", {"stage": status or "unknown", "job_id": job_id,
                         "elapsed_s": round(waited)})
            last = status
        if status in ("done", "failed"):
            return st
    return {"ok": False, "data": {"text": f"任务超时未完成（>{int(waited)}s），job_id={job_id}，"
                                           "可稍后用 printguard_check_job 查询"}}


# ---------- LLM ----------

async def _llm(messages: list[dict], tools: list[dict]) -> dict:
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{LLM_BASE}/chat/completions",
                              headers={"Authorization": f"Bearer {LLM_TOKEN}"},
                              json={"model": LLM_MODEL, "messages": messages,
                                    "tools": tools or None, "temperature": 0.3})
        r.raise_for_status()
        return r.json()


# ---------- Agent 循环 ----------

async def agent_loop(sid: str, user_msg: str) -> None:
    sess = SESSIONS[sid]

    def emit(kind, payload):
        sess["events"].append({"type": kind, "payload": payload, "t": round(time.time() - sess["t0"], 1)})

    try:
        tools = await _mcp_list_tools()
        emit("info", {"text": f"已连接 MCP 服务（{len(tools)} 个工具）"})
        messages = [{"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg}]
        for _ in range(MAX_TURNS):
            resp = await _llm(messages, tools)
            msg = resp["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if calls:
                messages.append({"role": "assistant",
                                 "content": msg.get("content") or "",
                                 "tool_calls": calls})
                for tc in calls:
                    fn = tc["function"]
                    name, args = fn["name"], json.loads(fn.get("arguments") or "{}")
                    emit("tool_call", {"name": name, "args": args})
                    if name == "printguard_start_job":
                        result = await _mcp_call_with_poll(name, args, emit)
                    else:
                        result = await _mcp_call(name, args)
                    emit("tool_result", {"name": name, "ok": result.get("ok"),
                                         "data": result.get("data")})
                    messages.append({"role": "tool", "tool_call_id": tc["id"],
                                     "content": json.dumps(result.get("data"), ensure_ascii=False)})
            else:
                emit("final", {"text": msg.get("content") or ""})
                return
        emit("final", {"text": "（达到单轮工具调用上限，请换个更简单的问法）"})
    except Exception as e:
        emit("final", {"text": f"Demo 后端出错：{type(e).__name__}: {e}"})


# ---------- HTTP ----------

async def index(request: Request):
    return HTMLResponse((Path(__file__).parent / "index.html").read_text(encoding="utf-8"))


async def post_chat(request: Request):
    body = await request.json()
    msg = (body.get("message") or "").strip()
    if not msg or len(msg) > 2000:
        return JSONResponse({"error": "消息为空或超长"}, status_code=400)
    if not LLM_TOKEN:
        return JSONResponse({"error": "后端未配置 LLM_API_TOKEN"}, status_code=500)
    if sum(1 for s in SESSIONS.values() if s["status"] == "running") >= 3:
        return JSONResponse({"error": "演示并发已达上限，请稍后再试"}, status_code=429)
    sid = uuid.uuid4().hex[:12]
    SESSIONS[sid] = {"events": [], "status": "running", "t0": time.time()}
    asyncio.get_event_loop().create_task(agent_loop(sid, msg))
    return JSONResponse({"sid": sid})


async def get_chat(request: Request):
    sid = request.path_params["sid"]
    sess = SESSIONS.get(sid)
    if not sess:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    events = sess["events"]
    if events and events[-1]["type"] == "final":
        sess["status"] = "done"
    return JSONResponse({"status": sess["status"], "events": events})


async def health(request: Request):
    return JSONResponse({"ok": True, "mcp_url": MCP_URL, "llm": LLM_MODEL})


app = Starlette(routes=[
    Route("/", index, methods=["GET"]),
    Route("/api/chat", post_chat, methods=["POST"]),
    Route("/api/chat/{sid}", get_chat, methods=["GET"]),
    Route("/api/health", health, methods=["GET"]),
])
