#!/usr/bin/env python3
"""PrintGuard_Aholo MCP Server —— Linux 优先；stdio（本地桌面宿主）/ streamable-http（云端托管）。

本地（Claude Desktop / Cursor / DSH）：
  {"mcpServers": {"printguard": {"command": "<repo>/.venv/bin/python",
                                 "args": ["<repo>/mcp_server.py"], "cwd": "<repo>"}}}

云端（魔搭社区等托管平台）：
  PRINTGUARD_TRANSPORT=streamable-http PRINTGUARD_HOST=0.0.0.0 PRINTGUARD_PORT=7860 \
      .venv/bin/python mcp_server.py
  MCP 端点：   {base}/mcp
  产物下载：   {base}/files/{job_id}/{文件名}?exp=..&sig=..（HMAC 签名限时链接）

API Key：公网部署由用户在工具参数 api_key 传入（不落盘、不写日志）；
本地开发可用环境变量 AHOLO_API_KEY 或 secrets.local.json。

任务模式：异步 job（生成/修复耗时长，提交即返回 job_id，宿主轮询）。
任务表落 SQLite（容器重启后仍可查历史任务）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import resource
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse

from mcp.server.fastmcp import FastMCP, Image
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response

import printguard as pg
from pipeline import run_pipeline, run_repair

Quality = Literal["fast", "fine"]
Style = Literal["photorealistic", "cartoon", "anime", "hand_painted",
                "cyberpunk", "fantasy", "glass"]

# ---------- 配置（全部可用环境变量覆盖，云端部署只改 env） ----------

JOB_DIR = Path(os.environ.get("PRINTGUARD_JOB_DIR", "output/jobs")).resolve()
DB_PATH = Path(os.environ.get("PRINTGUARD_JOB_DB", str(JOB_DIR / "jobs.db"))).resolve()
TRANSPORT = os.environ.get("PRINTGUARD_TRANSPORT", "stdio").strip().lower()
HOST = os.environ.get("PRINTGUARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("PRINTGUARD_PORT") or os.environ.get("PORT") or 8000)  # 魔搭创空间=7860
FILE_TTL = int(os.environ.get("PRINTGUARD_FILE_TTL", "3600"))          # 下载链接有效期（秒）
MAX_UPLOAD_MB = int(os.environ.get("PRINTGUARD_MAX_UPLOAD_MB", "64"))  # 远端输入文件上限
PUBLIC_BASE = os.environ.get("PRINTGUARD_PUBLIC_BASE_URL", "").rstrip("/")
STATELESS = os.environ.get("PRINTGUARD_STATELESS", "1" if TRANSPORT != "stdio" else "0")
# 文件下载链接签名密钥：多副本部署必须显式设置同一个值，否则重启后旧链接失效
_FILE_SECRET = os.environ.get("PRINTGUARD_FILE_SECRET") or secrets.token_hex(32)
ALLOW_PRIVATE_URL = os.environ.get("PRINTGUARD_ALLOW_PRIVATE_URLS", "0") == "1"

# 随服务器下发的使用须知（MCP initialize 时宿主即可读到，不依赖 skill 文档）
_INSTRUCTIONS = """\
PrintGuard_Aholo：把 Lux3D 生成的 3D 模型修成可 FDM 打印的 3MF 并交付诊断报告。
使用规则：
1. height_mm（目标尺寸 mm）必须先向终端用户确认后传入，绝不猜测；
2. 生成类任务同一 AHOLO 账号全平台并发为 1：必须等 printguard_check_job 返回 done/error 再提交下一个；
3. AHOLO API Key 由用户在 api_key 参数提供（不要替用户编造或猜测 key）；本地部署可走环境变量；
4. 交付 = printable.3mf（打印基准）+ turntable.gif（渲染预览）；诊断图（缺陷着色）仅供你查看讲解修复，不作为文件交付；
5. risk 为 C/D 时先向用户逐项告知问题再交付；薄壁修不动时如实说"建议重新设计"，不假装能修。"""

mcp = FastMCP("PrintGuard_Aholo", instructions=_INSTRUCTIONS,
              host=HOST, port=PORT, stateless_http=STATELESS == "1")


# ---------- 任务表（SQLite：容器重启后仍可查历史任务） ----------

class JobStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS jobs ("
            " job_id TEXT PRIMARY KEY, status TEXT NOT NULL, kind TEXT NOT NULL,"
            " created_at TEXT, updated_at TEXT, result TEXT, error TEXT)")
        self._conn.commit()

    def create(self, job_id: str, kind: str) -> None:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO jobs (job_id,status,kind,created_at,updated_at)"
                " VALUES (?,?,?,?,?)", (job_id, "queued", kind, now, now))
            self._conn.commit()

    def update(self, job_id: str, *, status: str | None = None,
               result: dict | None = None, error: str | None = None) -> None:
        sets, vals = ["updated_at=?"], [time.strftime("%Y-%m-%d %H:%M:%S")]
        if status is not None:
            sets.append("status=?"); vals.append(status)
        if result is not None:
            sets.append("result=?"); vals.append(json.dumps(result, ensure_ascii=False))
        if error is not None:
            sets.append("error=?"); vals.append(error[:300])
        vals.append(job_id)
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {','.join(sets)} WHERE job_id=?", vals)
            self._conn.commit()

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT job_id,status,kind,created_at,updated_at,result,error"
                " FROM jobs WHERE job_id=?", (job_id,))
            row = cur.fetchone()
        if not row:
            return None
        job = {"job_id": row[0], "status": row[1], "kind": row[2],
               "created_at": row[3], "updated_at": row[4]}
        if row[5]:
            try:
                job["result"] = json.loads(row[5])
            except Exception:
                job["result"] = {}
        if row[6]:
            job["error"] = row[6]
        return job


_STORE = JobStore(DB_PATH)


# ---------- 同一 AHOLO 账号的生成串行（平台侧并发上限=1） ----------

_KEY_LOCKS: dict[str, threading.Lock] = {}
_KEY_LOCKS_GUARD = threading.Lock()


def _key_lock(key_id: str) -> threading.Lock:
    with _KEY_LOCKS_GUARD:
        lk = _KEY_LOCKS.get(key_id)
        if lk is None:
            lk = threading.Lock()
            _KEY_LOCKS[key_id] = lk
        return lk


def _key_id(key: str) -> str:
    """只用 key 的哈希做锁标识，避免明文 key 留在内存字典里。"""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ---------- 输入获取（远端 URL 下载；含 SSRF 与体积防护） ----------

_ALLOWED_EXT = {".3mf", ".stl", ".glb", ".gltf", ".obj", ".ply", ".off", ".gif",
                ".png", ".jpg", ".jpeg", ".webp", ".json"}


def _assert_public_url(url: str) -> None:
    """拒绝内网/回环地址（SSRF 防护）；本地 stdio 模式可放开。"""
    if ALLOW_PRIVATE_URL or TRANSPORT == "stdio":
        return
    import ipaddress
    import socket
    host = urlparse(url).hostname
    if not host:
        raise ValueError("URL 缺少主机名")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise ValueError(f"域名解析失败：{host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"拒绝访问内网地址：{host}")


def _fetch_file(file_or_url: str, dest_dir: Path) -> Path:
    """入参兼容：公网 URL（自动下载到 dest_dir）或本机路径。"""
    import requests
    if file_or_url.startswith(("http://", "https://")):
        _assert_public_url(file_or_url)
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = (file_or_url.split("?")[0].rsplit("/", 1)[-1] or "input.glb")
        name = Path(name).name or "input.glb"
        dest = dest_dir / ("input_" + name)
        limit = MAX_UPLOAD_MB * 1024 * 1024
        total = 0
        with requests.get(file_or_url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    total += len(chunk)
                    if total > limit:
                        f.close()
                        dest.unlink(missing_ok=True)
                        raise ValueError(f"输入文件超过 {MAX_UPLOAD_MB}MB 上限")
                    f.write(chunk)
        return dest
    p = Path(file_or_url)
    if not p.exists():
        raise FileNotFoundError(f"找不到文件：{file_or_url}")
    return p


# ---------- 交付件与签名下载链接 ----------

def _sign(job_id: str, filename: str, exp: int) -> str:
    msg = f"{job_id}|{filename}|{exp}".encode()
    return hmac.new(_FILE_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:32]


def _base_url() -> str:
    if PUBLIC_BASE:
        return PUBLIC_BASE
    host = "localhost" if HOST in ("0.0.0.0", "::") else HOST
    return f"http://{host}:{PORT}"


def _signed_url(job_id: str, filename: str) -> str:
    exp = int(time.time()) + FILE_TTL
    sig = _sign(job_id, filename, exp)
    return f"{_base_url()}/files/{job_id}/{quote(filename)}?exp={exp}&sig={sig}"


def _files_payload(files: dict, job_id: str) -> dict:
    """stdio 模式给绝对路径；http 模式给签名下载链接（远程用户拿不到本地路径）。"""
    out = {}
    for name, val in (files or {}).items():
        if not isinstance(val, str) or not val.startswith("/"):
            out[name] = val          # "sheet skipped: ..." 之类的说明文本
            continue
        p = Path(val)
        item: dict = {"path": val}
        try:
            item["bytes"] = p.stat().st_size
        except OSError:
            pass
        if TRANSPORT != "stdio":
            item["url"] = _signed_url(job_id, p.name)
        out[name] = item
    return out


# ---------- HTTP 路由（云端托管时随 MCP 一起暴露） ----------

# 创空间页面 iframe 会直接 GET /（带 studio_token 等参数），没有根路由会渲染成
# "Not Found"。这里返回一个自包含的落地页（无外部资源），并实时拉取 /healthz 状态。
_LANDING_HTML = """<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PrintGuard_Aholo — FDM 打印件体检</title>
<style>
 body{font-family:system-ui,"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0f1115;color:#e6e8ee}
 .card{max-width:660px;margin:24px;padding:36px 40px;border:1px solid #2a2f3a;border-radius:16px;background:#161a22}
 h1{margin:0 0 10px;font-size:24px}
 p{line-height:1.7;color:#aeb6c4;margin:8px 0}
 code{background:#222836;border-radius:6px;padding:2px 8px;font-size:13px;color:#8fd0ff;word-break:break-all}
 .ok{color:#4ade80}.bad{color:#f87171}
 ul{color:#aeb6c4;line-height:2.0;padding-left:22px}
</style></head>
<body><div class="card">
<h1>🛡️ PrintGuard_Aholo <span id="st" class="bad">检查中…</span></h1>
<p>FDM 打印件的生成 → 可制造性校核 → 修复 → 交付（MCP 服务）。</p>
<p>本服务通过 <b>MCP 协议</b>调用，没有网页交互界面。在支持 MCP 的客户端（OpenCode / Claude / Cursor 等）里这样接入：</p>
<ul>
 <li>传输方式：<code>streamable-http</code></li>
 <li>服务地址：<code id="mcpurl">/mcp</code></li>
 <li>工具：<code>printguard_start_job</code>（文生 3D + 体检）、<code>get_printguard_status</code>（查进度 / 取签名下载链接）、<code>printguard_analyze_printability</code>（体检已有 mesh）</li>
 <li>生成功能需自带 AHOLO API Key（调用时传入 <code>api_key</code> 参数，平台不托管）</li>
</ul>
<p style="color:#6b7280">健康检查 <code>/healthz</code> · MCP 端点 <code>/mcp</code> · 交付文件为 HMAC 签名限时链接</p>
</div>
<script>
var s=document.getElementById('st');
fetch('/healthz').then(function(r){return r.json()}).then(function(j){
 s.textContent=j.ok?'● 运行中':'● 异常';s.className=j.ok?'ok':'bad'})
 .catch(function(){s.textContent='● 异常'});
document.getElementById('mcpurl').textContent=location.origin+'/mcp';
</script>
</body></html>"""


@mcp.custom_route("/", methods=["GET"])
async def landing(request: Request):
    return Response(_LANDING_HTML, media_type="text/html")


# 平台/函数计算会对独立服务 POST /invoke 做探测，返回服务说明而非 404
@mcp.custom_route("/invoke", methods=["GET", "POST"])
async def invoke_probe(request: Request):
    return JSONResponse({"service": "printguard", "transport": TRANSPORT,
                         "mcp_endpoint": "/mcp", "health": "/healthz",
                         "hint": "MCP 协议服务：请使用 MCP 客户端连接 /mcp (streamable-http)"})


# ---------- 远程测试通道（管理员，云端跑测试用） ----------
# 目的：创空间的 2vCPU/8GB 就是生产环境，回归/基准直接在云上跑最真实，
# 也不占用开发者本机（本机 8GB 跑大模型基准会 OOM）。
# 门禁：PRINTGUARD_ADMIN_TOKEN 未设置时整个通道不可见（404）；
# 子进程带 RLIMIT_AS 内存护栏，测试失控不会拖垮宿主服务；同时只允许一个测试。

_APP_DIR = Path(__file__).resolve().parent
_DEBUG_LOCK = threading.Lock()
_DEBUG_SCRIPTS = {
    # name: (脚本文件, 附加参数, 说明)
    "selftest": ("printguard.py", ["selftest"],
                 "内核合成用例回归（6 用例 + GLB 往返 + 大面片重载）"),
    "http_regression": ("test_mcp_http.py", [],
                        "MCP over HTTP + 签名下载 + 防篡改回归（容器内自起 8765 端口）"),
}
_DEBUG_WRAPPER = (
    "import resource, runpy, sys\n"
    "lim = int(sys.argv[1]) * 1024 * 1024\n"
    "resource.setrlimit(resource.RLIMIT_AS, (lim, lim))\n"
    "sys.argv = sys.argv[2:]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
)


def _admin_ok(request: Request) -> bool:
    tok = os.environ.get("PRINTGUARD_ADMIN_TOKEN", "")
    if not tok:
        return False
    # 查询参数优先：云端网关独占 Authorization 头（转发的是魔搭令牌），
    # 管理令牌在云端只能走 ?admin_token=；header 分支用于本地无网关场景。
    supplied = request.query_params.get("admin_token", "")
    if not supplied:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    return bool(supplied) and hmac.compare_digest(supplied, tok)


def _debug_run(script: str) -> JSONResponse:
    if script not in _DEBUG_SCRIPTS:
        return JSONResponse({"ok": False, "error": f"未知脚本，可选：{sorted(_DEBUG_SCRIPTS)}"},
                            status_code=400)
    if not _DEBUG_LOCK.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "另一个测试正在运行，请稍后重试"},
                            status_code=409)
    try:
        cap_mb = int(os.environ.get("PRINTGUARD_DEBUG_RLIMIT_MB", "5800"))
        timeout = int(os.environ.get("PRINTGUARD_DEBUG_TIMEOUT", "1800"))
        cmd = [os.path.abspath(sys.executable), "-c", _DEBUG_WRAPPER,
               str(cap_mb), str(_APP_DIR / _DEBUG_SCRIPTS[script][0]),
               *_DEBUG_SCRIPTS[script][1]]
        t0 = time.monotonic()
        try:
            proc = subprocess.run(cmd, cwd=str(_APP_DIR), capture_output=True,
                                  text=True, timeout=timeout)
            exit_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as e:
            exit_code, stdout, stderr = -9, e.stdout or "", f"超时（>{timeout}s）"
        elapsed = round(time.monotonic() - t0, 1)
        peak_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        tail = (stdout or "")[-6000:]
        if stderr:
            tail += "\n--stderr--\n" + (stderr or "")[-2000:]
        return JSONResponse({
            "ok": exit_code == 0, "script": script, "exit": exit_code,
            "elapsed_s": elapsed, "peak_rss_mb": round(peak_kb / 1024),
            "rlimit_mb": cap_mb, "output": tail,
        })
    finally:
        _DEBUG_LOCK.release()


@mcp.custom_route("/debug/run", methods=["GET", "POST"])
async def debug_run(request: Request):
    if not _admin_ok(request):
        return PlainTextResponse("not found", status_code=404)
    if request.method == "GET":
        return JSONResponse({"scripts": {k: v[2] for k, v in _DEBUG_SCRIPTS.items()},
                             "usage": "POST /debug/run {\"script\": \"selftest\"}，Bearer <PRINTGUARD_ADMIN_TOKEN>"})
    try:
        body = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    return _debug_run(str(body.get("script", "")))


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request):
    return JSONResponse({"ok": True, "transport": TRANSPORT,
                         "job_dir": str(JOB_DIR), "db": str(DB_PATH),
                         "admin_configured": bool(os.environ.get("PRINTGUARD_ADMIN_TOKEN"))})


@mcp.custom_route("/files/{job_id}/{filename}", methods=["GET"])
async def download_file(request: Request):
    job_id = request.path_params["job_id"]
    filename = request.path_params["filename"]
    exp_raw, sig = request.query_params.get("exp"), request.query_params.get("sig")
    try:
        exp = int(exp_raw or "")
    except ValueError:
        return PlainTextResponse("invalid link", status_code=403)
    if not sig or not hmac.compare_digest(_sign(job_id, filename, exp), sig):
        return PlainTextResponse("invalid signature", status_code=403)
    if exp < time.time():
        return PlainTextResponse("link expired", status_code=410)
    if Path(filename).suffix.lower() not in _ALLOWED_EXT:
        return PlainTextResponse("not downloadable", status_code=404)
    base = (JOB_DIR / job_id).resolve()
    path = (base / filename).resolve()
    if not str(path).startswith(str(base) + os.sep) or not path.is_file():
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(path, filename=path.name)


# ---------- 任务执行 ----------

def _run_job(job_id: str, kind: str, prompt: str | None, file_or_url: str | None,
             height_mm: float, quality: str, style: str,
             image: str | None = None, api_key: str | None = None) -> None:
    try:
        _STORE.update(job_id, status="running")
        if kind == "generate":
            key = api_key or ""
            with _key_lock(_key_id(key)):   # 同一账号串行，避免撞平台并发上限
                summary = run_pipeline(prompt=prompt, height_mm=height_mm,
                                       quality=quality, style=style, image=image,
                                       name=job_id, out_dir=str(JOB_DIR), api_key=key)
            if summary.get("failed"):
                raise RuntimeError(f"生成失败 task={summary.get('taskid')} "
                                   f"status={summary.get('status')} {summary.get('error', '')}")
        else:  # repair
            src = _fetch_file(file_or_url, JOB_DIR / job_id)
            summary = run_repair(str(src), height_mm, name=job_id, out_dir=str(JOB_DIR))
        files = {k: summary.get(k) for k in ("3mf", "gif", "sheet", "sheet_preview")}
        meta = {k: summary.get(k) for k in ("risk", "watertight", "overhang_frac",
                                            "open_overhang_frac", "bridged_frac",
                                            "repair_steps")}
        _STORE.update(job_id, status="done",
                      result={"meta": meta, "files": files})
    except Exception as e:
        _STORE.update(job_id, status="error", error=str(e))


# ---------- MCP 工具 ----------

@mcp.tool()
def printguard_start_job(prompt: str | None = None, height_mm: float | None = None,
                         quality: Quality = "fast",
                         style: Style = "photorealistic",
                         file_or_url: str | None = None,
                         image: str | None = None,
                         api_key: str | None = None):
    """提交一个 PrintGuard_Aholo 任务（立即返回 job_id，不阻塞）。用 printguard_check_job 轮询结果。

    两种任务：只给 prompt = 生成新模型（Lux3D → 检测 → 修复 → 交付）；
    给 file_or_url（mesh 文件）+ prompt 可留空说明 = 修复已有模型。

    Args:
        prompt: 模型描述（生成任务必填；描述外形比报 IP 名更有效）
        height_mm: 目标尺寸 mm——缺失时不要猜，先向终端用户询问后再调用
        quality: fast=G1-Turbo 结构件（全程 ~2min）；fine=G1+300k 手办/细节件（~10min，交付以 3MF 为准）
        style: 生成风格
        file_or_url: 已有 mesh 文件（公网部署传公网 URL；本地可用本机路径）；提供时任务为"修复"而非"生成"
        image: 可选参考图（本机路径或公网 URL），与 prompt 同时使用——图文结合生成
        api_key: 用户的 AHOLO API Key（公网部署必填，本地可留空走环境变量）
    """
    # height_mm 故意不在 schema 层 required：required 会逼宿主 LLM 编一个数，
    # 违反"绝不猜尺寸"；显式报错把宿主顶回去问用户才是正确契约。
    if height_mm is None:
        raise ValueError("height_mm（目标尺寸 mm）必须由终端用户提供，绝不猜测——先询问用户")
    if not file_or_url and not prompt:
        raise ValueError("生成任务需要 prompt；修复任务需要 file_or_url")
    key = (api_key or os.environ.get("AHOLO_API_KEY", "")).strip()
    kind = "repair" if file_or_url else "generate"
    if kind == "generate" and not key:
        raise ValueError("生成任务需要 AHOLO API Key：请让用户填写后放入 api_key 参数")
    job_id = uuid.uuid4().hex[:12]
    _STORE.create(job_id, kind)
    threading.Thread(target=_run_job, daemon=True,
                     args=(job_id, kind, prompt, file_or_url, height_mm,
                           quality, style, image, key or None)).start()
    return json.dumps({"job_id": job_id, "status": "queued",
                       "hint": "用 printguard_check_job(job_id) 轮询；生成 fine 档约 10min"},
                      ensure_ascii=False)


@mcp.tool()
def printguard_check_job(job_id: str):
    """查询 PrintGuard_Aholo 任务状态：queued/running/done/error；done 时返回交付件
    （printable.3mf + turntable.gif）——本地部署给绝对路径，云端部署给签名下载链接，
    并内联附上诊断缩略图（ImageContent——缺陷着色图，供你查看讲解修复，非交付文件）。"""
    job = _STORE.get(job_id)
    if not job:
        raise ValueError(f"未知 job_id: {job_id}")
    result = job.get("result") or {}
    payload = dict(job)
    if result:
        payload["result"] = {"meta": result.get("meta") or {},
                              "files": _files_payload(result.get("files") or {}, job_id)}
    text = json.dumps(payload, ensure_ascii=False)
    thumb = ((result.get("files") or {}).get("sheet_preview"))
    if isinstance(thumb, str) and thumb.startswith("/") and Path(thumb).is_file():
        return [text, Image(path=thumb)]
    return text


@mcp.tool()
def printguard_analyze_printability(file_or_url: str, height_mm: float):
    """秒级检测 mesh 可打印性（同步返回）：水密性、悬垂(>45°)、最小壁厚(>=0.8mm)、风险评分 S-D。

    Args:
        file_or_url: mesh 文件（公网部署传公网 URL；本地可用本机路径）
        height_mm: 真实目标尺寸 mm——必须由终端用户提供，绝不猜测
    """
    src = _fetch_file(file_or_url, JOB_DIR / "_tmp")
    m = pg.load_mesh(str(src), height_mm)
    rep = pg.analyze_mesh(m)
    rep["file"] = file_or_url
    return json.dumps(rep, ensure_ascii=False)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(prog="printguard-mcp",
                                 description="PrintGuard_Aholo MCP Server（stdio / streamable-http）")
    ap.add_argument("--transport", choices=["stdio", "streamable-http"], default=None,
                    help=f"传输方式（默认读环境变量 PRINTGUARD_TRANSPORT，当前 {TRANSPORT}）")
    ap.add_argument("--host", default=None, help=f"监听地址（默认 {HOST}）")
    ap.add_argument("--port", type=int, default=None, help=f"监听端口（默认 {PORT}）")
    args = ap.parse_args()
    transport = args.transport or TRANSPORT
    if args.host:
        mcp.settings.host = args.host
    if args.port:
        mcp.settings.port = args.port
    mcp.run(transport=transport)   # stdio: 本地桌面宿主；streamable-http: 云端托管


if __name__ == "__main__":
    main()
