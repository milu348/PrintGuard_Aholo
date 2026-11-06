"""MCP 工具层：生成层（Lux3D）、管线层（体检+修复+交付）、任务查询。

统一约定：
- 生成 / 修复都是长任务：工具只负责入队，秒回 `job_id`，由
  `printguard_check_job` 轮询（Lux3D 生成并发 = 1，worker 单线程天然串行）
- `api_key` 只作为调用参数下传，绝不写进 job meta（会经 check_job 回显）
- 平台签名 URL（含 `sign=`）不能直接作下游输入，统一 `prepare_image_input` 中转
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from .app import JOBS, mcp
from .config import (DELIVERY_IMAGE_TTL_MIN, DELIVERY_MAX_MB, DELIVERY_MODE,
                     DELIVERY_ONCE, DELIVERY_TTL_MIN, PUBLIC_BASE_URL, RUNS_DIR,
                     VERBOSE)
from .delivery import capture_artifacts
from .lux3d import Lux3DClient
from .pipeline import run_pipeline, run_repair


# ---------- 内部工具 ----------

def make_client(api_key: str | None) -> Lux3DClient:
    """按调用构造客户端（session 按线程隔离，服务内可安全复用函数）。"""
    return Lux3DClient(api_key, verbose=VERBOSE)


def prepare_image_input(client: Lux3DClient, value: str | None) -> str | None:
    """平台签名 URL 不能直接作为下游输入（实测），自动经 Asset 中转。"""
    if value and "sign=" in value:
        return client.rehost(value)
    return value


def prepare_images(client: Lux3DClient,
                   values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    return [prepare_image_input(client, value) for value in values]


def submit(kind: str, run, **meta) -> dict:
    """提交后台任务；meta 里绝不能出现 api_key。"""
    job_id = JOBS.submit(kind, run, meta=meta)
    return {
        "job_id": job_id,
        "status": "queued",
        "kind": kind,
        "hint": "用 printguard_check_job(job_id) 轮询，建议每 5-10 秒一次",
    }


_RUN_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def with_artifact_urls(summary: dict) -> dict:
    """给 run 目录内的产物补下载链接（本地路径保留，便于 stdio 形态直接读）。

    PUBLIC_BASE_URL 未配置时给相对路径 /files/...：HTTP 客户端可自行拼主机；
    stdio 形态下没有 HTTP 服务，调用方应直接用 artifacts 里的本地路径。
    """
    artifacts = (summary or {}).get("artifacts") or {}
    urls: dict[str, str] = {}
    for key, value in artifacts.items():
        if not isinstance(value, str) or not value:
            continue
        try:
            rel = Path(value).resolve().relative_to(RUNS_DIR)
        except (ValueError, OSError):
            continue
        urls[key] = f"{PUBLIC_BASE_URL}/files/{rel.as_posix()}"
    if urls:
        summary["artifact_urls"] = urls
    return summary


def apply_delivery(summary: dict) -> dict:
    """托管形态：交付件搬进一次性存储，删除 run 目录，返回一次性下载链接。

    本地形态（stdio / files）保持 /files 路由，产物留在 run 目录由使用方管理。
    """
    if DELIVERY_MODE != "memory":
        return summary

    artifacts = summary.get("artifacts") or {}
    run_dir = None
    model = artifacts.get("model")
    if isinstance(model, str) and model:
        run_dir = Path(model).resolve().parent
    elif summary.get("run_id"):
        run_dir = RUNS_DIR / str(summary["run_id"])

    downloads, warnings = capture_artifacts(
        artifacts, run_dir,
        ttl_sec=DELIVERY_TTL_MIN * 60,
        image_ttl_sec=DELIVERY_IMAGE_TTL_MIN * 60,
        max_bytes=DELIVERY_MAX_MB * 1024 * 1024,
        once=DELIVERY_ONCE, base_url=PUBLIC_BASE_URL)
    if downloads:
        summary["downloads"] = downloads
        summary.pop("artifact_urls", None)
        # 磁盘文件已删除：只回格式信息，不把失效路径传回调用方
        summary["artifacts"] = {k: v for k, v in artifacts.items()
                                if k == "model_format"}
    summary["warnings"] = list(summary.get("warnings") or []) + warnings
    return summary


# ---------- 工具：生成层（异步） ----------

@mcp.tool()
def lux3d_concept_image(prompt: str = "", reference_image: str | None = None,
                        api_key: str | None = None) -> dict:
    """生成概念设计图（异步任务，返回 job_id）。

    prompt 与 reference_image 至少提供一个，支持纯文字 / 纯图 / 图文组合。
    概念图迭代成本远低于 3D 生成，建议先确认外观再进入建模。
    """
    def run() -> dict:
        client = make_client(api_key)
        task_id = client.concept_image(
            prompt or None,
            reference_image=prepare_image_input(client, reference_image))
        result = client.wait(task_id)
        return {"platform_task_id": task_id, "outputs": result.urls,
                "elapsed_sec": result.elapsed_sec}

    return submit("concept_image", run, prompt=prompt,
                  reference_image=reference_image)


@mcp.tool()
def lux3d_four_view(prompt: str = "", image: str | None = None,
                    api_key: str | None = None) -> dict:
    """生成四视图（正 45° / 对侧 45° / 侧 / 背），异步任务，返回 job_id。

    prompt 与 image 至少提供一个。用于在 3D 生成前锁定外观细节。
    """
    def run() -> dict:
        client = make_client(api_key)
        task_id = client.four_view(
            prompt=prompt or None,
            image=prepare_image_input(client, image))
        result = client.wait(task_id)
        return {"platform_task_id": task_id, "outputs": result.urls,
                "elapsed_sec": result.elapsed_sec}

    return submit("four_view", run, prompt=prompt, image=image)


@mcp.tool()
def lux3d_text_to_3d(prompt: str, version: str = "G1-Turbo",
                     face_count: int = 100_000, style: str | None = None,
                     reference_image: str | None = None,
                     api_key: str | None = None) -> dict:
    """文生 3D（异步任务，返回 job_id）。

    version: G1-Turbo（快，~1-2 分钟）或 G1（细节更好，较慢）。
    face_count: 10000-300000；结构件 100000 足够，手办细节可用 300000。
    style: photorealistic / cartoon / anime / hand_painted / cyberpunk / fantasy / glass。
    reference_image: 可选参考图（公网 URL 或本地路径）。
    """
    def run() -> dict:
        client = make_client(api_key)
        task_id = client.text_to_3d(
            prompt, version=version, face_count=face_count, style=style,
            reference_image=prepare_image_input(client, reference_image))
        result = client.wait(task_id)
        return {"platform_task_id": task_id, "outputs": result.urls,
                "elapsed_sec": result.elapsed_sec}

    return submit("text_to_3d", run, prompt=prompt, version=version,
                  face_count=face_count, style=style)


@mcp.tool()
def lux3d_image_to_3d(image: str | None = None, images: list[str] | None = None,
                      version: str = "G1", face_count: int = 100_000,
                      api_key: str | None = None) -> dict:
    """图生 3D（异步任务，返回 job_id）。

    image（单图）与 images（1-32 张多视角，首张为主参考）必须且只能提供一个。
    单图推荐 version=G1；多视角推荐 version=G1-Turbo。
    """
    def run() -> dict:
        client = make_client(api_key)
        task_id = client.image_to_3d(
            image=prepare_image_input(client, image),
            images=prepare_images(client, images),
            version=version, face_count=face_count)
        result = client.wait(task_id)
        return {"platform_task_id": task_id, "outputs": result.urls,
                "elapsed_sec": result.elapsed_sec}

    return submit("image_to_3d", run, version=version, face_count=face_count,
                  image=image, images=images)


# ---------- 工具：管线层（异步） ----------

@mcp.tool()
def printguard_run(height_mm: float, prompt: str = "",
                   image: str | None = None, images: list[str] | None = None,
                   quality: Literal["fast", "fine"] = "fast",
                   style: str = "photorealistic",
                   version: str | None = None, face_count: int | None = None,
                   fmt: Literal["3mf", "stl"] = "3mf",
                   orient: bool = True, smooth: bool = True,
                   api_key: str | None = None) -> dict:
    """一条龙：生成 3D → 可制造性体检 → 修复 → 交付（异步，返回 job_id）。

    height_mm：目标最大边（毫米，必填）。AI 网格单位不可信，必须锁尺寸。
    prompt / image / images 至少给一个：
      - prompt 文生 3D；prompt+image 文生 3D 带参考图；
      - 只有 image 单图生 3D；images 多视角图生 3D（概念图/四视图结果）。
    quality：fast（G1-Turbo，结构件）或 fine（G1+0.3mm，手办细节）。
    fmt：交付格式 3mf（默认）或 stl。
    交付图：输入的概念图 / 四视图会原样复制进交付目录（不做本地渲染）；
    纯 prompt 模式没有交付图。
    完成后 result 只含 ok / run_id / mode / artifacts / warnings / duration_sec
    与 artifact_urls；不生成诊断报告，也不回体检摘要。
    """
    def run() -> dict:
        client = make_client(api_key)
        summary = run_pipeline(
            prompt or None, height_mm=height_mm, image=image, images=images,
            quality=quality, style=style, version=version,
            face_count=face_count, out_dir=str(RUNS_DIR), fmt=fmt,
            orient=orient, smooth=smooth, client=client)
        return apply_delivery(with_artifact_urls(summary))

    return submit("printguard_run", run, prompt=prompt,
                  height_mm=height_mm, quality=quality, fmt=fmt)


@mcp.tool()
def printguard_repair(source: str, height_mm: float,
                      fmt: Literal["3mf", "stl"] = "3mf",
                      orient: bool = True, smooth: bool = True,
                      pitch: float | None = None,
                      api_key: str | None = None) -> dict:
    """修复已有模型：体检 → 修复 → 交付（异步，返回 job_id）。

    source：公网 URL（推荐，平台签名 URL 也可直接给）或服务端本地路径。
    height_mm：目标最大边（毫米，必填）。
    非水密/非流形时走体素重构（有损，细节磨平到 pitch），薄壁区域会闭环增厚。
    """
    def run() -> dict:
        is_url = str(source).startswith(("http://", "https://"))
        client = make_client(api_key) if is_url else None
        summary = run_repair(
            source, height_mm, out_dir=str(RUNS_DIR), fmt=fmt,
            orient=orient, smooth=smooth, pitch=pitch,
            client=client)
        return apply_delivery(with_artifact_urls(summary))

    return submit("printguard_repair", run,
                  source=source, height_mm=height_mm, fmt=fmt)


# ---------- 工具：任务查询 ----------

@mcp.tool()
def printguard_check_job(job_id: str) -> dict:
    """查询异步任务（生成层与管线层通用）。

    status: queued / running / succeeded / failed。
    succeeded 时 result 为工具结果；管线任务额外含 artifacts / artifact_urls。
    failed 时 error 给出失败原因。建议每 5-10 秒查询一次。
    """
    record = JOBS.get(job_id)
    if record is None:
        return {"error": f"未知 job_id：{job_id}", "job_id": job_id}
    return record


@mcp.tool()
def lux3d_get_task(task_id: int, api_key: str | None = None) -> dict:
    """直接查询 Lux3D 平台任务（同步）。返回原始 taskId / status / outputs。"""
    return make_client(api_key).get_task(task_id)


@mcp.tool()
def lux3d_list_tasks(page: int = 1, page_size: int = 20,
                     status: int | None = None,
                     api_key: str | None = None) -> dict:
    """分页查询平台生成记录（同步）。status 可选 0/1/3/4 过滤。"""
    return make_client(api_key).list_tasks(page=page, page_size=page_size,
                                           status=status)
