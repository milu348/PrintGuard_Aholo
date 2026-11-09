"""MCP 工具层：注册 7 个工具，返回工具注册表（agent 派发的唯一来源）。

工具面（v0.2）：

| 工具 | 类型 | 说明 |
|---|---|---|
| printguard_run | 异步 | 一条龙：生成（文生/图生/文图混合）→ 体检 → 修复 → 交付 |
| printguard_repair | 异步 | 修复已有模型（公网 URL 或服务端本地路径） |
| printguard_check_job | 同步 | 查询任意异步任务的 job_id |
| lux3d_text_to_3d | 异步 | 文生 3D（可选参考图 = 文图混合 / 风格） |
| lux3d_image_to_3d | 异步 | 图生 3D（单图或 1-32 张多视角） |
| lux3d_get_task | 同步 | 直查 Lux3D 平台任务 |
| lux3d_list_tasks | 同步 | 分页查询平台生成记录 |

统一约定：
- 生成 / 修复都是长任务：工具只负责入队，秒回 `job_id`，由
  `printguard_check_job` 轮询（Lux3D 生成并发 = 1，worker 单线程天然串行）
- `api_key` 只作为调用参数下传，绝不写进 job meta（会经 check_job 回显）
- `source` 传公网 URL 由客户端下载（带 SSRF 校验），本地路径直接读
- image/images 接受公网 URL、服务端本地路径或 data URI（base64）；
  非公网 URL 由 pipeline.inputs 经 Asset 上传转干净 URL
- 体检只在管线内部驱动修复决策：结果不回诊断报告，只回模型与链接
"""
from __future__ import annotations

from typing import Callable, Literal

from ..pipeline import run_generate, run_repair
from ..pipeline.delivery import apply_delivery
from .app import AppContext

# 异步工具（返回 job_id，agent 回合内需要阻塞等待）
ASYNC_TOOLS = {"printguard_run", "printguard_repair",
               "lux3d_text_to_3d", "lux3d_image_to_3d"}


def register_tools(ctx: AppContext) -> dict[str, Callable[..., dict]]:
    """把 8 个工具注册到 ctx.mcp，并把原始函数登记进 ctx.tool_registry。"""
    settings = ctx.settings

    def submit(kind: str, run, **meta) -> dict:
        """提交后台任务；meta 里绝不能出现 api_key。"""
        job_id = ctx.mcp_jobs.submit(kind, run, meta=meta)
        return {
            "job_id": job_id,
            "status": "queued",
            "kind": kind,
            "hint": "用 printguard_check_job(job_id) 轮询，建议每 5-10 秒一次",
        }

    def deliver(summary: dict) -> dict:
        """任务成功后的交付收口：run 目录产物 → 限时下载链接（见 pipeline.delivery）。"""
        return apply_delivery(
            summary, store=ctx.store, ttl_sec=settings.delivery_ttl_sec,
            base_url=settings.public_base_url, runs_dir=settings.runs_dir)

    # ---------- 工具：管线层（异步） ----------

    def printguard_run(height_mm: float, prompt: str = "",
                       image: str | None = None,
                       images: list[str] | None = None,
                       quality: Literal["fast", "fine"] = "fast",
                       style: str = "photorealistic",
                       version: str | None = None,
                       face_count: int | None = None,
                       fmt: Literal["3mf", "stl"] = "3mf",
                       orient: bool = True, smooth: bool = True,
                       repair: bool = True,
                       api_key: str | None = None) -> dict:
        """一条龙：生成 3D → 可制造性体检 → 修复 → 交付（异步，返回 job_id）。

        height_mm：目标最大边（毫米，必填）。AI 网格单位不可信，必须锁尺寸。
        prompt：文字描述（文生 / 文图混合）。
        image：单张图片（URL / 服务端本地路径 / data URI）。prompt+image = 文图混合；
        只有图片 = 图生。images：1-32 张多视角图（与 image 互斥性由管线自动归并）。
        quality：fast（G1-Turbo，结构件）或 fine（G1+0.3mm，手办细节）。
        fmt：交付格式 3mf（默认）或 stl。
        repair：**默认 True，生成后自动体检+修复再交付**；只有用户明确表示
        「不修复 / 只要原始模型」时才允许传 False（跳过整条修复链，
        warnings 里会提示未修复，直接打印可能失败）。
        完成后 result 只含 ok / run_id / mode / artifacts / warnings /
        duration_sec 与 downloads（限时下载链接）。
        """
        from ..pipeline.inputs import make_client

        def run() -> dict:
            client = make_client(None, api_key, verbose=settings.verbose)
            summary = run_generate(
                prompt or None, height_mm=height_mm,
                image=image or None, images=images,
                quality=quality, style=style, version=version,
                face_count=face_count, out_dir=str(settings.runs_dir), fmt=fmt,
                orient=orient, smooth=smooth, repair=repair, client=client)
            return deliver(summary)

        return submit("printguard_run", run, prompt=prompt or None,
                      has_image=bool(image or images),
                      height_mm=height_mm, quality=quality, fmt=fmt,
                      repair=repair)

    def printguard_repair(source: str, height_mm: float,
                          fmt: Literal["3mf", "stl"] = "3mf",
                          orient: bool = True, smooth: bool = True,
                          pitch: float | None = None,
                          force_rebuild: bool = False,
                          api_key: str | None = None) -> dict:
        """修复已有模型：体检 → 修复 → 交付（异步，返回 job_id）。

        source：公网 URL（推荐，平台签名 URL 也可直接给）或服务端本地路径。
        height_mm：目标最大边（毫米，必填）。
        非水密/非流形时走体素重构（有损，细节磨平到 pitch），薄壁区域会闭环增厚。
        保真度门槛：模型细节密度接近可达重建分辨率时自动跳过重建、原样交付
        （warnings 会说明）；确要强制重建时传 force_rebuild=true（自担细节
        损失风险）。
        """
        from ..pipeline.inputs import is_url, make_client

        def run() -> dict:
            client = (make_client(None, api_key, verbose=settings.verbose)
                      if is_url(source) else None)
            summary = run_repair(
                source, height_mm, out_dir=str(settings.runs_dir), fmt=fmt,
                orient=orient, smooth=smooth, pitch=pitch,
                force_rebuild=force_rebuild, client=client)
            return deliver(summary)

        return submit("printguard_repair", run,
                      source=source, height_mm=height_mm, fmt=fmt,
                      force_rebuild=force_rebuild)

    # ---------- 工具：生成层（异步） ----------

    def lux3d_text_to_3d(prompt: str, version: str = "G1-Turbo",
                         face_count: int = 100_000,
                         style: str | None = None,
                         image: str | None = None,
                         api_key: str | None = None) -> dict:
        """文生 3D / 文图混合（异步任务，返回 job_id）。

        只生成，**不体检不修复**——这是「用户明确不要修复」时才用的工具；
        常规需求请用 printguard_run（默认自动体检+修复后交付）。
        version: G1-Turbo（快，~1-2 分钟）或 G1（细节更好，较慢）。
        face_count: 10000-300000；结构件 100000 足够，手办细节可用 300000。
        style: photorealistic / cartoon / anime / hand_painted / cyberpunk / fantasy / glass。
        image: 可选参考图 URL / 本地路径 / data URI——给了就是文图混合生成
        （prompt 描述要在参考图基础上生成或修改什么）。
        """
        from ..pipeline.inputs import make_client, normalize_image_url

        def run() -> dict:
            client = make_client(None, api_key, verbose=settings.verbose)
            img_url = normalize_image_url(client, image) if image else None
            task_id = client.text_to_3d(
                prompt, version=version, face_count=face_count, style=style,
                img=img_url)
            result = client.wait(task_id)
            return {"platform_task_id": task_id, "outputs": result.urls,
                    "elapsed_sec": result.elapsed_sec}

        return submit("text_to_3d", run, prompt=prompt, version=version,
                      face_count=face_count, style=style, has_image=bool(image))

    def lux3d_image_to_3d(image: str | None = None,
                          images: list[str] | None = None,
                          version: str = "G1",
                          face_count: int = 100_000,
                          api_key: str | None = None) -> dict:
        """图生 3D（异步任务，返回 job_id）。

        只生成，**不体检不修复**——这是「用户明确不要修复」时才用的工具；
        常规需求请用 printguard_run（默认自动体检+修复后交付）。
        image：单张图片（URL / 本地路径 / data URI），G1 与 G1-Turbo 均支持。
        images：1-32 张多视角图片，保留顺序、第一张为主参考（必须显式传 version）。
        image 与 images 只能提供一个。注意本工具没有文字参数；
        带文字描述的需求请用 lux3d_text_to_3d(image=...) 或 printguard_run。
        """
        from ..pipeline.inputs import make_client, normalize_generation_images

        def run() -> dict:
            client = make_client(None, api_key, verbose=settings.verbose)
            urls = normalize_generation_images(client, image=image,
                                               images=images)
            if not urls:
                raise ValueError("必须提供 image（单图）或 images（多视角）")
            if len(urls) == 1:
                task_id = client.img_to_3d(version=version,
                                           face_count=face_count, image=urls[0])
            else:
                task_id = client.img_to_3d(version=version,
                                           face_count=face_count, images=urls)
            result = client.wait(task_id)
            return {"platform_task_id": task_id, "outputs": result.urls,
                    "elapsed_sec": result.elapsed_sec}

        return submit("image_to_3d", run, version=version,
                      face_count=face_count, image_count=1 if image else len(images or []))

    # ---------- 工具：任务查询（同步） ----------

    def printguard_check_job(job_id: str) -> dict:
        """查询异步任务（生成层与管线层通用）。

        status: queued / running / succeeded / failed。
        succeeded 时 result 为工具结果；管线任务另含 downloads（限时下载链接）。
        failed 时 error 给出失败原因。建议每 5-10 秒查询一次。
        """
        record = ctx.mcp_jobs.get(job_id)
        if record is None:
            return {"error": f"未知 job_id：{job_id}", "job_id": job_id}
        return record

    def lux3d_get_task(task_id: int, api_key: str | None = None) -> dict:
        """直接查询 Lux3D 平台任务（同步）。返回原始 taskId / status / outputs。"""
        from ..pipeline.inputs import make_client
        return make_client(None, api_key,
                           verbose=settings.verbose).get_task(task_id)

    def lux3d_list_tasks(page: int = 1, page_size: int = 20,
                         status: int | None = None,
                         api_key: str | None = None) -> dict:
        """分页查询平台生成记录（同步）。status 可选 0/1/3/4 过滤。"""
        from ..pipeline.inputs import make_client
        return make_client(None, api_key, verbose=settings.verbose).list_tasks(
            page=page, page_size=page_size, status=status)

    # ---------- 注册到 MCP + 登记注册表 ----------

    tools = {
        "printguard_run": printguard_run,
        "printguard_repair": printguard_repair,
        "lux3d_text_to_3d": lux3d_text_to_3d,
        "lux3d_image_to_3d": lux3d_image_to_3d,
        "printguard_check_job": printguard_check_job,
        "lux3d_get_task": lux3d_get_task,
        "lux3d_list_tasks": lux3d_list_tasks,
    }
    for func in tools.values():
        ctx.mcp.tool()(func)   # FastMCP 以 __name__ 作为工具名
    ctx.tool_registry.update(tools)
    ctx.async_tools = set(ASYNC_TOOLS)
    return tools


__all__ = ["register_tools", "ASYNC_TOOLS"]
