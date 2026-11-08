"""Agent 编排：网页聊天 → 大模型 function calling → 同一份工具实现 → 限时交付。

独立包 ``modelscope_agent`` 的编排层。与核心的关系：
- 工具就是 `printguard_aholo.service.tools` 注册进 MCP 的同一批函数，
  经 ``ctx.tool_registry`` 派发（无 import 耦合；MCP 客户端与网页 Agent
  走同一条代码路径）
- 异步工具在回合内阻塞等待到终态（默认上限 `LLM_JOB_TIMEOUT_SEC`=20 分钟）
- **默认必修复**：常规需求一律 printguard_run（自动体检+修复）；只有用户
  明确说不要修复时才用 lux3d_* 纯生成工具（见 SYSTEM_PROMPT 路由规则）
- 图片：前端把用户上传的图片以 data URI 附在用户消息上（`images` 字段）；
  大模型不需要看图片像素——派发工具时若生成类调用没带图片参数，
  自动把本回合的图片注入进去（见 GENERATION_TOOLS / _pending_images）
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .client import LLMClient

if TYPE_CHECKING:  # 仅类型标注：核心不 import 本包，这里反向只做 duck typing
    from printguard_aholo.service.app import AppContext

logger = logging.getLogger("modelscope_agent.agent")

# 生成类工具：本回合有附带图片时自动注入 image/images 参数
GENERATION_TOOLS = {"printguard_run", "lux3d_text_to_3d", "lux3d_image_to_3d"}

SYSTEM_PROMPT = """你是 PrintGuard_Aholo 的助手，把用户的文字/图片描述变成可以 FDM 3D 打印的模型。

工作方式：
1. 尺寸优先：必须拿到「最大边多少毫米」（height_mm）才开始生成。用户没说就反问，绝不替用户猜。
2. **默认必修复**：常规需求一律用 printguard_run(prompt 或 image, height_mm)——
   它会完成 3D 生成、可制造性体检与修复，最终交付 3MF/STL。用户没提修复与否，
   就按「要修复」处理，绝不因为用户没说而跳过修复。
3. **只有用户明确说「不要修复 / 只要原始模型 / 直接给我生成结果」时**，才改用
   lux3d_text_to_3d / lux3d_image_to_3d（只生成不修复），并提醒用户未修复的
   模型直接打印可能失败。拿不准就继续用 printguard_run。
4. 生成模式：只有文字→printguard_run(prompt)；只有图片→printguard_run(image)；
   文字+图片→printguard_run(prompt, image)（文图混合）。多张图→printguard_run(images)。
   用户消息附带的图片会自动作为生成输入，你不需要转述图片内容，也不要编造图片参数。
5. quality：结构件/支架用 fast（快）；手办/细节件用 fine（慢但保真）。
6. 结果呈现：给用户一个下载按钮链接，并简短说明「修了什么、打印建议」。
7. 工具失败时如实说明失败原因（如平台并发排队、尺寸非法），并告诉用户下一步怎么做。

工具返回的 JSON 里：
- downloads：限时下载链接（模型 5 分钟内有效），模型文件都在这里
- warnings：非致命提示，需要时转述给用户
绝对不要编造下载链接，只使用工具返回的 url。回复用中文，简洁，别复述整个 JSON。"""


@dataclass
class Reply:
    """Agent 一轮的产出。"""

    text: str
    artifacts: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    error: str | None = None


def tool_specs(ctx: "AppContext") -> list[dict]:
    """MCP 注册表 → OpenAI function calling specs。

    唯一来源是 MCP 注册表：改工具签名只改 tools.py 一处，specs 自动跟上。
    """
    registered = asyncio.run(ctx.mcp.list_tools())
    return [{"type": "function",
             "function": {"name": t.name,
                          "description": t.description or "",
                          "parameters": t.inputSchema or {"type": "object",
                                                          "properties": {}}}}
            for t in registered]


def _wait_job(ctx: "AppContext", job_id: str, timeout: float) -> dict:
    """把异步任务阻塞到终态（Agent 回合内），返回最终 job 记录。"""
    deadline = time.monotonic() + timeout
    while True:
        record = ctx.mcp_jobs.get(job_id)
        if record is None:
            return {"status": "failed", "error": f"未知 job_id：{job_id}"}
        if record["status"] in ("succeeded", "failed"):
            return record
        if time.monotonic() >= deadline:
            return {"status": record["status"], "job_id": job_id,
                    "error": f"等待超时（{timeout:.0f}s），任务仍在 {record['status']}，"
                             "请稍后用 printguard_check_job 再查"}
        time.sleep(1.5)


def run_tool(ctx: "AppContext", name: str, arguments: dict,
             pending_images: list[str] | None = None) -> dict:
    """执行一个工具：异步工具内部等 job 到终态，同步工具直接返回。

    pending_images：本回合用户附带的图片（data URI）。生成类工具调用没带
    图片参数时自动注入——大模型只负责决定「做什么」，不搬运图片字节。
    """
    func = ctx.tool_registry.get(name)
    if func is None:
        return {"ok": False, "error": f"未知工具：{name}"}
    if pending_images and name in GENERATION_TOOLS:
        if not arguments.get("image") and not arguments.get("images"):
            arguments = dict(arguments)
            if len(pending_images) == 1:
                arguments["image"] = pending_images[0]
            else:
                arguments["images"] = list(pending_images)
    try:
        if name in ctx.async_tools:
            submitted = func(**arguments)
            job_id = submitted.get("job_id")
            if not job_id:
                return {"ok": False, "error": f"工具未返回 job_id：{submitted}"}
            record = _wait_job(ctx, job_id,
                               float(ctx.settings.llm_job_timeout_sec))
            return {"job": _compact(record)}
        return _compact(func(**arguments))
    except TypeError as exc:  # 参数名/必填项不匹配
        return {"ok": False, "error": f"参数错误：{exc}"}
    except Exception as exc:  # noqa: BLE001 —— 工具异常要翻译成 LLM 能读的文本
        logger.exception("agent tool %s failed", name)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _compact(payload) -> dict | list | str:
    """压缩工具结果给 LLM：保留链接，丢掉体积类冗余字段。"""
    if isinstance(payload, dict):
        record = dict(payload)
        for key in ("created", "updated", "raw", "meta"):
            record.pop(key, None)
        result = record.get("result")
        if isinstance(result, dict):
            inner = dict(result)
            artifacts = inner.get("artifacts")
            if isinstance(artifacts, dict):
                inner["artifacts"] = {k: (v if k == "model_format" else "<已转入限时下载>")
                                      for k, v in artifacts.items()}
            record["result"] = inner
        return record
    return payload


def _collect_artifacts(tool_payloads: list[dict]) -> list[dict]:
    """从工具结果里提取交付件（目前只有模型），给前端渲染用。"""
    seen: dict[str, dict] = {}

    def _nodes(node, depth=0):
        """工具结果可能嵌在 job / result 里，往下挖几层。"""
        if not isinstance(node, dict) or depth > 3:
            return
        yield node
        for key in ("job", "result"):
            child = node.get(key)
            if isinstance(child, dict):
                yield from _nodes(child, depth + 1)

    for payload in tool_payloads:
        for source in _nodes(payload):
            for key, entry in (source.get("downloads") or {}).items():
                if not isinstance(entry, dict) or not entry.get("url"):
                    continue
                token = str(entry["url"])
                if token in seen:
                    continue
                filename = str(entry.get("filename") or "")
                seen[token] = {
                    "key": key,
                    "url": entry["url"],
                    "filename": filename,
                    "size_bytes": entry.get("size_bytes"),
                    # 目前只有模型一种交付件；留扩展位而不是写死字符串
                    "kind": "image" if filename.lower().endswith(
                        (".png", ".jpg", ".jpeg", ".webp", ".gif")) else "model",
                }
    return [seen[token] for token in sorted(seen, key=lambda t: seen[t]["key"])]


def _pending_images_from(history: list[dict]) -> list[str]:
    """取本回合（最后一条用户消息）附带的图片 data URI 列表。"""
    for item in reversed(history):
        if item.get("role") == "user":
            images = item.get("images") or []
            return [str(x) for x in images
                    if isinstance(x, str) and x.startswith("data:")]
    return []


def respond(ctx: "AppContext", history: list[dict], *,
            llm: LLMClient | None = None) -> Reply:
    """跑一轮 Agent。history 为 [{role, content, images?}]（前端全量传入）。

    llm 可注入（测试用 FakeLLM）；缺省按 Settings 构造真实客户端。
    """
    settings = ctx.settings
    llm = llm or LLMClient.from_settings(settings)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    trimmed = history[-settings.llm_history_turns * 2:]
    messages.extend({"role": item.get("role", "user"),
                     "content": str(item.get("content") or "")}
                    for item in trimmed)

    if not any(item["role"] == "user" for item in messages):
        return Reply(text="请告诉我你想做什么，以及模型的最大边尺寸（毫米）。"
                          "也可以直接上传图片来图生 3D。")

    pending_images = _pending_images_from(history)
    if pending_images:
        messages.append({
            "role": "system",
            "content": f"用户本回合附带了 {len(pending_images)} 张图片，调用生成类"
                       "工具时无需传图片参数（系统自动注入）；若用户未说明尺寸，先反问。",
        })

    tool_payloads: list[dict] = []
    steps: list[dict] = []

    for _ in range(settings.llm_max_steps):
        try:
            result = llm.chat(messages, tool_specs(ctx))
        except Exception as exc:  # LLMError 及网络层异常统一兜底
            artifacts = _collect_artifacts(tool_payloads)
            if artifacts:
                return Reply(
                    text=f"大模型这一轮限流/出错了（{exc}）。不过上一轮已经拿到交付件，"
                         "先用下面的链接下载；稍等片刻可以继续对话。",
                    artifacts=artifacts, steps=steps, error=str(exc))
            return Reply(text=f"大模型调用失败：{exc}", steps=steps, error=str(exc))
        if not result.tool_calls:
            return Reply(text=result.content or "（模型没有返回内容）",
                         artifacts=_collect_artifacts(tool_payloads), steps=steps)

        assistant_message = ((result.raw.get("choices") or [{}])[0].get("message") or {})
        messages.append(assistant_message)
        for call in result.tool_calls:
            payload = run_tool(ctx, call.name, call.arguments, pending_images)
            tool_payloads.append(payload)
            steps.append({"tool": call.name, "arguments": _safe_args(call.arguments),
                          "ok": bool(payload.get("ok", True))})
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(payload, ensure_ascii=False)[:4000]})

    return Reply(text="这一轮思考步数用完了，还没给出结论。请把需求说得更具体一点，"
                      "或者直接指定尺寸后重试。",
                 artifacts=_collect_artifacts(tool_payloads), steps=steps)


def _safe_args(arguments: dict) -> dict:
    """steps 里回显参数时折叠超长值（data URI 动辄几 MB，纯噪声）。"""
    clean = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str) and len(value) > 80:
            clean[key] = f"<{len(value)} chars>"
        elif isinstance(value, list) and any(
                isinstance(v, str) and len(v) > 80 for v in value):
            clean[key] = f"<{len(value)} items>"
        else:
            clean[key] = value
    return clean


# ---------- 回合任务（网页 Agent 的异步化） ----------
# /api/chat 只负责入队：生成+修复可能 3-6 分钟，同步等待会被网关 504。
# 队列本体是 ctx.agent_jobs（printguard_aholo 的 JobManager），
# 这里只补「提交/查询」两个薄封装，避免第二套队列实现。


def _run_turn(ctx: "AppContext", history: list[dict]) -> dict:
    """跑一个回合并转成可 JSON 序列化的结果（队列返回值契约）。"""
    reply = respond(ctx, list(history))
    return {"reply": reply.text, "artifacts": reply.artifacts,
            "steps": reply.steps, "error": reply.error}


def submit_turn(ctx: "AppContext", history: list[dict]) -> str:
    """提交一个 Agent 回合，返回 job_id（agent_jobs 单 worker 串行执行）。"""
    job_id = ctx.agent_jobs.submit(
        "agent_turn", lambda: _run_turn(ctx, list(history)),
        meta={"turns": len(history)})
    logger.info("agent turn %s queued（%d 轮历史）", job_id, len(history))
    return job_id


def get_turn(ctx: "AppContext", job_id: str) -> dict | None:
    """查询回合状态：queued / running / succeeded / failed。"""
    return ctx.agent_jobs.get(job_id)
