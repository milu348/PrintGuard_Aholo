"""前端 Agent：网页聊天 → 大模型 function calling → 进程内 MCP 工具 → 一次性交付。

设计要点：
- 工具就是 `tools.py` 里那 9 个 MCP 工具的同一份实现（网页 Agent 与 MCP 客户端
  走同一条代码路径，不会出现「页面能用、MCP 不能用」的分叉）
- 异步工具在 Agent 回合内阻塞等待到终态（默认上限 12 分钟），
  这样用户一句「做个手机支架」就能直接拿到预览图 + 下载链接
- 交付件由 `delivery.py` 给出一次性链接：模型/图片都不长期留在创空间磁盘
- 尺寸（height_mm）必须由用户给出：模型没确认尺寸时必须反问，绝不替用户猜
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import llm, tools
from .config import (DELIVERY_IMAGE_TTL_MIN, LLM_HISTORY_TURNS,
                     LLM_JOB_TIMEOUT_SEC, LLM_MAX_STEPS, PUBLIC_BASE_URL)
from .delivery import STORE

logger = logging.getLogger("printguard.agent")

SYSTEM_PROMPT = """你是 PrintGuard_Aholo 的助手，帮用户把想法变成可以 FDM 3D 打印的模型。

工作方式：
1. 尺寸优先：必须拿到「最大边多少毫米」（height_mm）才开始生成。用户没说就反问，绝不替用户猜。
2. 概念先行：用户要「好看的/手办/角色/摆件」这类外观优先的需求时，先出概念图（lux3d_concept_image），
   用户满意后再出四视图（lux3d_four_view），最后才进入 3D 生成（printguard_run 传 images）。
   用户明确说「直接建模/结构件/支架」时可以直接生成。
3. 生成由 printguard_run 完成：它会自动做可制造性体检与修复，最终交付 3MF/STL。
4. 结果呈现：一轮对话只给一个最终交付链接，附上「修了什么、打印建议」的简短说明。
5. 工具失败时如实说明失败原因（如平台并发排队、尺寸非法），并告诉用户下一步怎么做。

工具返回的 JSON 里：
- downloads：一次性下载链接（默认 15 分钟有效、下载一次后失效），模型和图片都在这里
- warnings：非致命提示，需要时转述给用户
绝对不要编造下载链接，只使用工具返回的 url。回复用中文，简洁，别复述整个 JSON。"""

# OpenAI function calling 工具描述（与 MCP 工具参数一一对应）
TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "printguard_run",
            "description": "一条龙：生成 3D → 可制造性体检 → 修复 → 交付 3MF/STL。",
            "parameters": {
                "type": "object",
                "properties": {
                    "height_mm": {"type": "number",
                                  "description": "目标最大边毫米数，必填，需用户确认"},
                    "prompt": {"type": "string", "description": "文生 3D 的描述"},
                    "image": {"type": "string", "description": "单图（URL）：单图生 3D"},
                    "images": {"type": "array", "items": {"type": "string"},
                               "description": "多视角图 URL 列表（概念图/四视图结果）"},
                    "quality": {"type": "string", "enum": ["fast", "fine"],
                                "description": "fast=结构件快；fine=手办细节（慢）"},
                    "style": {"type": "string",
                              "description": "photorealistic/cartoon/anime/hand_painted/..."},
                    "fmt": {"type": "string", "enum": ["3mf", "stl"]},
                    "orient": {"type": "boolean"},
                    "smooth": {"type": "boolean"},
                },
                "required": ["height_mm"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "printguard_repair",
            "description": "修复已有模型（公网 URL 或服务端路径）：体检 → 修复 → 交付。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "模型 URL 或服务端本地路径"},
                    "height_mm": {"type": "number", "description": "目标最大边毫米数，必填"},
                    "fmt": {"type": "string", "enum": ["3mf", "stl"]},
                    "orient": {"type": "boolean"},
                    "smooth": {"type": "boolean"},
                    "pitch": {"type": "number", "description": "体素分辨率 mm"},
                },
                "required": ["source", "height_mm"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lux3d_concept_image",
            "description": "生成概念设计图（成本远低于 3D，先锁外观）。返回图片 URL。",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "reference_image": {"type": "string", "description": "参考图 URL"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lux3d_four_view",
            "description": "生成四视图（正45°/对侧45°/侧/背），给 3D 生成提供多视角输入。",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "image": {"type": "string", "description": "概念图 URL（推荐）"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lux3d_text_to_3d",
            "description": "只生成不修复：文生 3D，返回模型 URL。",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "version": {"type": "string", "enum": ["G1", "G1-Turbo"]},
                    "face_count": {"type": "integer"},
                    "style": {"type": "string"},
                    "reference_image": {"type": "string"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lux3d_image_to_3d",
            "description": "只生成不修复：图生 3D（单图或多视角），返回模型 URL。",
            "parameters": {
                "type": "object",
                "properties": {
                    "image": {"type": "string"},
                    "images": {"type": "array", "items": {"type": "string"}},
                    "version": {"type": "string", "enum": ["G1", "G1-Turbo"]},
                    "face_count": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "printguard_check_job",
            "description": "查询异步任务状态（生成层与管线层通用）。",
            "parameters": {
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lux3d_get_task",
            "description": "直接查询 Lux3D 平台任务（同步）。",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lux3d_list_tasks",
            "description": "分页查询平台生成记录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer"},
                    "page_size": {"type": "integer"},
                    "status": {"type": "integer"},
                },
            },
        },
    },
]

_ASYNC_TOOLS = {"lux3d_concept_image", "lux3d_four_view", "lux3d_text_to_3d",
                "lux3d_image_to_3d", "printguard_run", "printguard_repair"}

# 只出图的工具：平台 URL 受 2 小时签名限制，转成一次性交付链接更稳
_IMAGE_TOOLS = {"lux3d_concept_image": "concept_image",
                "lux3d_four_view": "four_view"}


@dataclass
class Reply:
    """Agent 一轮的产出。"""

    text: str
    artifacts: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    error: str | None = None


def _wait_job(job_id: str, timeout: float) -> dict:
    """把异步任务阻塞到终态（Agent 回合内），返回最终 job 记录。"""
    deadline = time.monotonic() + timeout
    while True:
        record = tools.JOBS.get(job_id)
        if record is None:
            return {"status": "failed", "error": f"未知 job_id：{job_id}"}
        if record["status"] in ("succeeded", "failed"):
            return record
        if time.monotonic() >= deadline:
            return {"status": record["status"], "job_id": job_id,
                    "error": f"等待超时（{timeout:.0f}s），任务仍在 {record['status']}，"
                             "请稍后用 printguard_check_job 再查"}
        time.sleep(1.5)


def run_tool(name: str, arguments: dict) -> dict:
    """执行一个工具：异步工具内部等 job 到终态，同步工具直接返回。"""
    func = getattr(tools, name, None)
    if func is None or not callable(func):
        return {"ok": False, "error": f"未知工具：{name}"}
    try:
        if name in _ASYNC_TOOLS:
            submitted = func(**arguments)
            job_id = submitted.get("job_id")
            if not job_id:
                return {"ok": False, "error": f"工具未返回 job_id：{submitted}"}
            record = _wait_job(job_id, float(LLM_JOB_TIMEOUT_SEC))
            result = (record.get("result") or {})
            _deliver_image_outputs(result, name)
            return {"job": _compact(record)}
        return _compact(func(**arguments))
    except TypeError as exc:  # 参数名/必填项不匹配
        return {"ok": False, "error": f"参数错误：{exc}"}
    except Exception as exc:  # noqa: BLE001 —— 工具异常要翻译成 LLM 能读的文本
        logger.exception("agent tool %s failed", name)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _deliver_image_outputs(payload: dict, tool_name: str) -> dict:
    """概念图 / 四视图：下载平台 URL → 一次性交付，附回 payload 的 downloads。

    平台结果 URL 带签名、2 小时过期；转成自己的链接后，用户在对话里点开即可，
    也不依赖平台域名可达性。失败只记 warning，不影响主流程。
    """
    stem = _IMAGE_TOOLS.get(tool_name)
    if not stem or not isinstance(payload, dict):
        return payload
    urls = [u for u in (payload.get("outputs") or []) if isinstance(u, str)]
    if not urls:
        return payload
    try:
        client = tools.make_client(None)
    except Exception as exc:  # noqa: BLE001 —— 没配 key 就退回原始 URL
        payload.setdefault("warnings", []).append(f"图片中转跳过：{exc}")
        return payload

    downloads = dict(payload.get("downloads") or {})
    for index, url in enumerate(urls, 1):
        key = f"{stem}_{index:02d}" if len(urls) > 1 else stem
        try:
            data, name = client.download_bytes(url)
        except Exception as exc:  # noqa: BLE001
            payload.setdefault("warnings", []).append(f"图片 {key} 中转失败：{exc}")
            continue
        suffix = Path(name).suffix or ".jpg"
        pretty = (f"{stem}{suffix}" if len(urls) == 1
                  else f"{stem}_{index:02d}{suffix}")
        token, cached = STORE.put(data, pretty, "image/jpeg",
                                    DELIVERY_IMAGE_TTL_MIN * 60)
        if not cached:
            payload.setdefault("warnings", []).append(f"图片 {key} 超过交付内存上限")
            continue
        downloads[key] = {
            "url": f"{PUBLIC_BASE_URL}/d/{token}" if PUBLIC_BASE_URL else f"/d/{token}",
            "filename": pretty, "size_bytes": len(data),
            "expires_sec": DELIVERY_IMAGE_TTL_MIN * 60,
        }
    if downloads:
        payload["downloads"] = downloads
    return payload


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
                inner["artifacts"] = {k: (v if k == "model_format" else "<已转入一次性下载>")
                                     for k, v in artifacts.items()}
            record["result"] = inner
        return record
    return payload


def _collect_artifacts(tool_payloads: list[dict]) -> list[dict]:
    """从工具结果里提取交付件（模型 / 图片），给前端渲染用。"""
    seen: dict[str, dict] = {}

    def _nodes(node, depth=0):
        """工具结果可能嵌在 job / result 里，往下挖几层。"""
        if not isinstance(node, dict) or depth > 3:
            return
        yield node
        for key in ("job", "result", "record"):
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
                    "kind": "image" if filename.lower().endswith(
                        (".png", ".jpg", ".jpeg", ".webp", ".gif")) else "model",
                }
            # 兼容本地 / stdio 形态的 /files 链接
            for key, url in (source.get("artifact_urls") or {}).items():
                if isinstance(url, str) and url not in seen:
                    filename = url.rsplit("/", 1)[-1]
                    seen[url] = {
                        "key": key, "url": url, "filename": filename,
                        "size_bytes": None,
                        "kind": "image" if filename.lower().endswith(
                            (".png", ".jpg", ".jpeg", ".webp")) else "model",
                    }
    return [seen[token] for token in sorted(seen, key=lambda t: seen[t]["key"])]


def respond(history: list[dict]) -> Reply:
    """跑一轮 Agent。history 为 [{role: user|assistant, content}]（前端全量传入）。"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    trimmed = history[-LLM_HISTORY_TURNS * 2:]
    messages.extend({"role": item.get("role", "user"),
                     "content": str(item.get("content") or "")}
                    for item in trimmed)

    if not any(item["role"] == "user" for item in messages):
        return Reply(text="请告诉我你想做什么，以及模型的最大边尺寸（毫米）。")

    tool_payloads: list[dict] = []
    steps: list[dict] = []

    for _ in range(LLM_MAX_STEPS):
        try:
            result = llm.chat(messages, TOOL_SPECS)
        except llm.LLMError as exc:
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
            payload = run_tool(call.name, call.arguments)
            tool_payloads.append(payload)
            steps.append({"tool": call.name, "arguments": call.arguments,
                         "ok": bool(payload.get("ok", True))})
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(payload, ensure_ascii=False)[:4000]})

    return Reply(text="这一轮思考步数用完了，还没给出结论。请把需求说得更具体一点，"
                      "或者直接指定尺寸后重试。",
                 artifacts=_collect_artifacts(tool_payloads), steps=steps)


# ---------- 回合任务（网页 Agent 的异步化） ----------
# /api/chat 曾经同步等整轮（概念图→四视图→建模可能 5-8 分钟），创空间网关会 504。
# 现在提交即回 job_id，由前端轮询 /api/job/<job_id>，每个 HTTP 请求都在毫秒级。
# 用独立 worker 而不是复用 tools.JOBS：回合内部的工具调用也要提交到 JOBS，
# 复用同一条队列会把自己堵死。

TERMINAL = ("succeeded", "failed")


class AgentRunner:
    """单 worker 的 Agent 回合队列（线程安全，内存态）。"""

    def __init__(self, *, retention_sec: float = 3600.0, max_jobs: int = 200,
                 timeout_sec: float = 1800.0):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._queue: "queue.Queue[tuple[str, list[dict]]]" = queue.Queue()
        self._retention_sec = float(retention_sec)
        self._max_jobs = max(1, int(max_jobs))
        self._timeout_sec = float(timeout_sec)
        self._worker = threading.Thread(target=self._loop, name="pg-agent-worker",
                                        daemon=True)
        self._worker.start()

    def submit(self, history: list[dict]) -> str:
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        record = {"job_id": job_id, "status": "queued", "created": now,
                  "updated": now, "meta": {"turns": len(history)},
                  "result": None, "error": None}
        with self._lock:
            self._jobs[job_id] = record
            self._prune_locked(now)
        self._queue.put((job_id, list(history)))
        logger.info("agent job %s queued（%d 轮历史）", job_id, len(history))
        return job_id

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            record = self._jobs.get(job_id)
            return dict(record) if record is not None else None

    def stats(self) -> dict:
        with self._lock:
            counts: dict[str, int] = {}
            for record in self._jobs.values():
                counts[record["status"]] = counts.get(record["status"], 0) + 1
            return {"total": len(self._jobs), "by_status": counts}

    # ---------- worker ----------

    def _loop(self) -> None:
        while True:
            job_id, history = self._queue.get()
            self._update(job_id, status="running")
            deadline = time.monotonic() + self._timeout_sec
            try:
                reply = respond(history)
                self._update(job_id, status="succeeded",
                             result={"reply": reply.text,
                                     "artifacts": reply.artifacts,
                                     "steps": reply.steps},
                             error=reply.error)
                logger.info("agent job %s succeeded", job_id)
            except Exception as exc:  # noqa: BLE001 —— 任务边界：异常落到状态
                logger.exception("agent job %s failed", job_id)
                self._update(job_id, status="failed",
                             error=f"{type(exc).__name__}: {exc}")
            finally:
                self._queue.task_done()

    def _update(self, job_id: str, **fields) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is not None:
                record.update(fields, updated=time.time())

    def _prune_locked(self, now: float) -> None:
        for job_id in [jid for jid, rec in self._jobs.items()
                       if rec["status"] in TERMINAL
                       and now - rec["updated"] > self._retention_sec]:
            del self._jobs[job_id]
        if len(self._jobs) <= self._max_jobs:
            return
        for job_id in [jid for jid, rec in self._jobs.items()
                       if rec["status"] in TERMINAL]:
            if len(self._jobs) <= self._max_jobs:
                break
            del self._jobs[job_id]


# 模块级单例：服务内共享一个回合队列（串行，天然匹配平台生成并发 = 1）
RUNNER = AgentRunner()
