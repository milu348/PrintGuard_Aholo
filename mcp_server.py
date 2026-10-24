#!/usr/bin/env python3
"""PrintGuard MCP Server —— stdio 挂载（宿主本机运行，产物以本地路径交付）。

宿主配置（Claude Desktop / Cursor / DSH 皆可）：
  {"mcpServers": {"printguard": {
      "command": "<项目根>\\.venv\\Scripts\\python.exe",
      "args": ["<项目根>\\mcp_server.py"],
      "cwd": "<项目根>"}}}

异步任务模式（生成/修复耗时长，MCP 调用立即返回 job_id，宿主轮询）：
  start_printguard_job(...) -> job_id
  check_printguard_job(job_id) -> status: queued|running|done|error
                                + 产物绝对路径 + 内联诊断缩略图（ImageContent）
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from typing import Literal

from mcp.server.fastmcp import FastMCP, Image

import printguard as pg
from pipeline import run_pipeline, run_repair

Quality = Literal["fast", "fine"]
Style = Literal["photorealistic", "cartoon", "anime", "hand_painted",
                "cyberpunk", "fantasy", "glass"]

# 随服务器下发的使用须知（MCP initialize 时宿主即可读到，不依赖 skill 文档）
_INSTRUCTIONS = """\
PrintGuard：把 Lux3D 生成的 3D 模型修成可 FDM 打印的 3MF 并交付诊断报告。
使用规则：
1. height_mm（目标尺寸 mm）必须先向终端用户确认后传入，绝不猜测；
2. 生成类任务（只给 prompt）全平台并发为 1：必须等 check_printguard_job 返回 done/error 再提交下一个；
3. 交付以 printable.3mf 为打印基准；diagnostic_sheet.png 是缺陷着色诊断图（任务完成时随结果内联返回）；
4. risk 为 C/D 时先向用户逐项告知问题再交付；薄壁修不动时如实说"建议重新设计"，不假装能修。"""

mcp = FastMCP("printguard", instructions=_INSTRUCTIONS)

# 简易内存任务表（单实例足够）
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _fetch_file(file_or_url: str, dest_dir: Path) -> Path:
    """入参兼容：公网 URL（自动下载到 dest_dir）或本机路径。"""
    import requests
    if file_or_url.startswith(("http://", "https://")):
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / ("input_" + (file_or_url.split("?")[0].rsplit("/", 1)[-1] or "input.glb"))
        with requests.get(file_or_url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        return dest
    p = Path(file_or_url)
    if not p.exists():
        raise FileNotFoundError(f"找不到文件：{file_or_url}")
    return p


def _run_job(job_id: str, kind: str, prompt: str | None, file_or_url: str | None,
             height_mm: float, quality: str, style: str,
             image: str | None = None) -> None:
    try:
        with _JOBS_LOCK:
            _JOBS[job_id]["status"] = "running"
        if kind == "generate":
            summary = run_pipeline(prompt=prompt, height_mm=height_mm, quality=quality,
                                   style=style, image=image,
                                   name=job_id, out_dir="output/jobs")
            if summary.get("failed"):
                raise RuntimeError(f"生成失败 task={summary.get('taskid')} "
                                   f"status={summary.get('status')} {summary.get('error', '')}")
        else:  # repair
            src = _fetch_file(file_or_url, Path("output/jobs") / job_id)
            summary = run_repair(str(src), height_mm, name=job_id, out_dir="output/jobs")
        files = {k: summary.get(k) for k in ("3mf", "sheet", "sheet_preview", "gif")}
        meta = {k: summary.get(k) for k in ("risk", "watertight", "overhang_frac",
                                            "repair_steps")}
        with _JOBS_LOCK:
            _JOBS[job_id].update({
                "status": "done",
                "result": {"meta": meta, "files": files},
                "finished_at": time.strftime("%H:%M:%S"),
            })
    except Exception as e:
        with _JOBS_LOCK:
            _JOBS[job_id].update({"status": "error", "error": str(e)[:300]})


@mcp.tool()
def start_printguard_job(prompt: str | None = None, height_mm: float | None = None,
                         quality: Quality = "fast",
                         style: Style = "photorealistic",
                         file_or_url: str | None = None,
                         image: str | None = None):
    """提交一个 PrintGuard 任务（立即返回 job_id，不阻塞）。用 check_printguard_job 轮询结果。

    两种任务：只给 prompt = 生成新模型（Lux3D → 检测 → 修复 → 交付）；
    给 file_or_url（mesh 文件）+ prompt 可留空说明 = 修复已有模型。

    Args:
        prompt: 模型描述（生成任务必填；描述外形比报 IP 名更有效）
        height_mm: 目标尺寸 mm——缺失时不要猜，先向终端用户询问后再调用
        quality: fast=G1-Turbo 结构件（全程 ~2min）；fine=G1+300k 手办/细节件（~10min，交付以 3MF 为准）
        style: 生成风格
        file_or_url: 已有 mesh 文件（本机路径或公网 URL）；提供时任务为"修复"而非"生成"
        image: 可选参考图（本机路径或公网 URL），与 prompt 同时使用——图文结合生成
    """
    # height_mm 故意不在 schema 层 required：required 会逼宿主 LLM 编一个数，
    # 违反"绝不猜尺寸"；显式报错把宿主顶回去问用户才是正确契约。
    if height_mm is None:
        raise ValueError("height_mm（目标尺寸 mm）必须由终端用户提供，绝不猜测——先询问用户")
    if not file_or_url and not prompt:
        raise ValueError("生成任务需要 prompt；修复任务需要 file_or_url")
    job_id = uuid.uuid4().hex[:12]
    kind = "repair" if file_or_url else "generate"
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "queued", "kind": kind,
                         "created_at": time.strftime("%H:%M:%S")}
    threading.Thread(target=_run_job, daemon=True,
                     args=(job_id, kind, prompt, file_or_url, height_mm,
                           quality, style, image)).start()
    return json.dumps({"job_id": job_id, "status": "queued",
                       "hint": "用 check_printguard_job(job_id) 轮询；生成 fine 档约 10min"},
                      ensure_ascii=False)


@mcp.tool()
def check_printguard_job(job_id: str):
    """查询 PrintGuard 任务状态：queued/running/done/error；done 时返回产物本地绝对路径，
    并内联附上诊断缩略图 sheet_preview.jpg（ImageContent，宿主可直接渲染；全尺寸图见路径）。"""
    thumb = None
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            raise ValueError(f"未知 job_id: {job_id}")
        text = json.dumps({"job_id": job_id, **job}, ensure_ascii=False)
        if job.get("status") == "done":
            thumb = ((job.get("result") or {}).get("files") or {}).get("sheet_preview")
    if thumb and Path(thumb).is_file():
        return [text, Image(path=thumb)]
    return text


@mcp.tool()
def analyze_printability(file_or_url: str, height_mm: float):
    """秒级检测 mesh 可打印性（同步返回）：水密性、悬垂(>45°)、最小壁厚(>=0.8mm)、风险评分 S-D。

    Args:
        file_or_url: mesh 文件（本机路径或公网 URL）
        height_mm: 真实目标尺寸 mm——必须由终端用户提供，绝不猜测
    """
    src = _fetch_file(file_or_url, Path("output/jobs/_tmp"))
    m = pg.load_mesh(str(src), height_mm)
    rep = pg.analyze_mesh(m)
    rep["file"] = file_or_url
    return json.dumps(rep, ensure_ascii=False)


def main() -> None:
    mcp.run()  # stdio：由宿主（Claude Desktop / Cursor / DSH / uvx 入口）拉起


if __name__ == "__main__":
    main()
