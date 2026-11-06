"""一条龙管线：生成 → 下载 → 体检 → 修复 → 交付。

设计约定：
- **尺寸必填**：AI 生成网格单位不可信，不先把最大边锁到目标毫米数，
  壁厚/悬垂结论没有可比性。
- **生成串行**：Lux3D 生成类任务账号级并发 = 1；本模块阻塞执行，MCP 层用
  `JobManager` 放到单 worker 线程，客户端拿 job_id 轮询。
- **结果 URL 2 小时过期**：`wait()` 成功后立即下载，不做延迟处理。
- **交付图由 Lux3D 提供**：概念图 / 四视图原样落盘，不做本地渲染；
  3D 生成任务本身只返回 GLB，没有模型预览图。
- **只交付模型与图片**：不生成诊断报告（Markdown / JSON），结果只回
  ok / run_id / mode / artifacts / warnings / duration_sec。
- **交付先落本地**：run 目录里放模型/概念图；公网下载链接由交付层
  （`/files` 路由）提供，管线不假设容器外可访问本地路径。
- **失败要响**：生成失败、下载为空、尺寸非法一律抛异常，让上层任务落到
  failed 状态并带原因，不返回半成品。
"""
from __future__ import annotations

import logging
import re
import shutil
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from .config import DELIVERY_MODE
from .lux3d import Lux3DClient
from .meshio import load_mesh
from .repair import repair, repair_voxel, smooth_taubin

logger = logging.getLogger("printguard.pipeline")

QUALITY_PRESETS = {
    # fast：结构件/快速迭代；fine：手办/细节件（生成慢，交付保真）
    "fast": {"version": "G1-Turbo", "face_count": 100_000,
             "pitch": None, "remesh": True},
    "fine": {"version": "G1", "face_count": 300_000,
             "pitch": 0.3, "remesh": False},
}

DEFAULT_OUT_DIR = "output/runs"
MAX_HEIGHT_MM = 10_000.0
# 面数超过此值时降密（quadric 减面会破坏水密性，实测弃用；改体素重闭合）
MAX_DELIVERY_FACES = 1_200_000
REMESH_PITCH = 0.35


class PipelineError(RuntimeError):
    """管线业务失败（生成失败 / 下载为空 / 产物缺失）。"""


# ---------- 内部工具 ----------

def _is_url(value: str) -> bool:
    return str(value).startswith(("http://", "https://"))


def safe_run_id(name: str | None, *, prefix: str = "run") -> str:
    """run 目录名白名单校验（防止 out_dir 被 ../ 穿越）。"""
    if not name:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{prefix}_{stamp}"
    name = str(name).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name) or name in (".", ".."):
        raise ValueError(f"非法 run 名称：{name!r}（仅允许字母/数字/._-，≤64）")
    return name


def _discard_run_dir(out: Path) -> None:
    """一次性交付形态下，失败也不留残缺 run 目录（本地形态保留便于排查）。"""
    if DELIVERY_MODE != "memory" or not out.is_dir():
        return
    shutil.rmtree(out, ignore_errors=True)
    logger.info("任务失败，已清理残缺 run 目录：%s", out)


def _validate_height(height_mm) -> float:
    try:
        height = float(height_mm)
    except (TypeError, ValueError):
        raise ValueError(f"height_mm 必须是数字，得到 {height_mm!r}") from None
    if not 0 < height <= MAX_HEIGHT_MM:
        raise ValueError(f"height_mm 须在 (0, {MAX_HEIGHT_MM:g}]mm，得到 {height}")
    return height


def _make_client(client: Lux3DClient | None, api_key: str | None) -> Lux3DClient:
    return client or Lux3DClient(api_key)


def _prepare_input(client: Lux3DClient, value: str | None) -> str | None:
    """平台签名 URL 不能直接作下游输入（实测），自动 Asset 中转。"""
    if value and "sign=" in value:
        return client.rehost(value)
    return value


def _download_source(source: str, out: Path,
                     client: Lux3DClient | None) -> str:
    """本地路径直接返回；URL 走客户端下载（带 SSRF 防护）。"""
    if not _is_url(source):
        local = Path(source)
        if not local.is_file():
            raise PipelineError(f"源文件不存在：{source}")
        return str(local)
    if client is None:
        raise PipelineError(
            "URL 输入需要 API Key（下载经客户端做 SSRF 校验）；"
            "也可以先下载到本地再传 path")
    suffix = Path(urllib.parse.urlparse(source).path).suffix or ".glb"
    dest = out / f"source{suffix}"
    return client.download(source, dest)


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def _image_suffix(value: str) -> str:
    if _is_url(value):
        suffix = Path(urllib.parse.urlparse(value).path).suffix.lower()
    else:
        suffix = Path(value).suffix.lower()
    return suffix if suffix in _IMAGE_EXTS else ".png"


def _save_visual_inputs(client: Lux3DClient, values, out: Path, *,
                        stem: str) -> tuple[dict, list[str]]:
    """把 Lux3D 产出的概念图 / 四视图原样落进 run 目录。

    这些图是交付图（不是本地渲染），下载失败只记 warning，不阻塞模型交付。
    单图命名 stem.png，多图命名 stem_01.png / stem_02.png ...
    """
    values = [value for value in (values or []) if value]
    artifacts: dict[str, str] = {}
    warnings: list[str] = []
    multi = len(values) > 1
    for index, value in enumerate(values, 1):
        suffix = _image_suffix(value)
        name = (f"{stem}_{index:02d}{suffix}" if multi
                else f"{stem}{suffix}")
        dest = out / name
        try:
            if _is_url(value):
                client.download(value, dest)
            else:
                source = Path(value)
                if not source.is_file():
                    raise FileNotFoundError(f"文件不存在：{value}")
                shutil.copy2(source, dest)
            if not dest.is_file() or dest.stat().st_size == 0:
                raise ValueError("文件为空")
        except Exception as exc:
            warnings.append(f"交付图 {name} 保存失败：{exc}")
            continue
        key = f"{stem}_{index:02d}" if multi else stem
        artifacts[key] = str(dest.resolve())
    return artifacts, warnings


def _remesh_if_needed(mesh, steps: list[str], *, smooth: bool):
    """面数过多时体素重闭合降密（降密失败按原网格交付：大但正确）。"""
    if len(mesh.faces) <= MAX_DELIVERY_FACES:
        return mesh
    try:
        mesh, pitch = repair_voxel(mesh, pitch=REMESH_PITCH)
    except Exception as exc:  # 降密是优化项：失败按原网格交付（大但正确）
        logger.warning("remesh 失败，按原网格交付：%s", exc)
        return mesh
    steps.append(f"remesh(pitch={pitch:.2f}mm)")
    if smooth:
        mesh = smooth_taubin(mesh)
        steps.append("smooth_taubin(去阶梯)")
    return mesh


def _deliver(mesh, out: Path, *, fmt: str) -> tuple[dict, list[str]]:
    """导出交付模型。返回 (artifacts, warnings)。

    交付图（概念图 / 四视图）由 Lux3D 提供，在生成阶段已落盘；
    诊断报告不生成，服务结果只回模型与图片。
    """
    artifacts: dict[str, str | None] = {}
    warnings: list[str] = []

    fmt = "stl" if str(fmt).lower() == "stl" else "3mf"
    model_path = out / f"printable.{fmt}"
    try:
        mesh.export(str(model_path))
    except Exception as exc:
        if fmt == "3mf":
            warnings.append(f"3MF 导出失败，回退 STL：{exc}")
            fmt = "stl"
            model_path = out / "printable.stl"
            try:
                mesh.export(str(model_path))
            except Exception as fallback_exc:
                raise PipelineError(
                    f"模型导出失败（3MF 与 STL 均失败）：{fallback_exc}") from fallback_exc
        else:
            raise PipelineError(f"模型导出失败：{exc}") from exc
    if not model_path.is_file() or model_path.stat().st_size == 0:
        raise PipelineError(f"模型导出为空：{model_path}")
    artifacts["model"] = str(model_path.resolve())
    artifacts["model_format"] = fmt

    return artifacts, warnings


def _process(mesh, *, run_id: str, mode: str, out: Path, height_mm: float,
             pitch: float | None, smooth: bool, orient: bool, remesh: bool,
             fmt: str, started: float,
             extra_artifacts: dict | None = None,
             extra_warnings: list[str] | None = None) -> dict:
    """体检 → 修复 →（降密）→ 导出，返回最小交付结果。

    height_mm 传给 repair：自动摆向会改变轴对齐包围盒，必须在最终姿态上
    重新把最大边锁回目标毫米数（尺寸契约）。
    """
    fixed, steps, _before, _after = repair(
        mesh, do_orient=orient, pitch=pitch, height_mm=height_mm, smooth=smooth)

    if remesh:
        fixed = _remesh_if_needed(fixed, steps, smooth=smooth)

    artifacts, warnings = _deliver(fixed, out, fmt=fmt)
    artifacts.update(extra_artifacts or {})
    warnings.extend(extra_warnings or [])
    return {
        "ok": True,
        "run_id": run_id,
        "mode": mode,
        "artifacts": artifacts,
        "warnings": warnings,
        "duration_sec": round(time.time() - started, 1),
    }


# ---------- 对外入口 ----------

def run_repair(source: str, height_mm, *, name: str | None = None,
               out_dir: str = DEFAULT_OUT_DIR, fmt: str = "3mf",
               orient: bool = True, smooth: bool = True,
               pitch: float | None = None,
               client: Lux3DClient | None = None,
               api_key: str | None = None) -> dict:
    """修复已有网格：本地路径或公网 URL → 检测 → 修复 → 交付。"""
    started = time.time()
    height = _validate_height(height_mm)
    run_id = safe_run_id(name, prefix="repair")
    out = Path(out_dir) / run_id
    out.mkdir(parents=True, exist_ok=True)

    try:
        if _is_url(source):
            client = _make_client(client, api_key)
        local = _download_source(source, out, client)
        mesh = load_mesh(local, height)
        logger.info("repair %s（%d 面，目标 %.1fmm）", run_id, len(mesh.faces), height)
        return _process(mesh, run_id=run_id, mode="repair", out=out,
                        height_mm=height, pitch=pitch, smooth=smooth, orient=orient,
                        remesh=True, fmt=fmt, started=started)
    except Exception:
        _discard_run_dir(out)
        raise


def run_generate(prompt: str | None = None, *, height_mm,
                 image: str | None = None,
                 images: list[str] | None = None, quality: str = "fast",
                 style: str = "photorealistic",
                 version: str | None = None, face_count: int | None = None,
                 name: str | None = None, out_dir: str = DEFAULT_OUT_DIR,
                 fmt: str = "3mf", orient: bool = True, smooth: bool = True,
                 client: Lux3DClient | None = None,
                 api_key: str | None = None) -> dict:
    """生成并交付：文生 3D / 单图生 3D / 多视角图生 3D。

    - `images`（概念图/四视图结果）优先走图生 3D；
    - 只有 `image` 且无 prompt → 单图生 3D；
    - prompt + image → 文生 3D 并带参考图。

    输入图会原样复制到 run 目录作为交付图（不做本地渲染）：
    images → four_view_XX，单图生 3D → concept_image，参考图 → reference_image。
    """
    started = time.time()
    height = _validate_height(height_mm)
    if quality not in QUALITY_PRESETS:
        raise ValueError(f"quality 须为 {sorted(QUALITY_PRESETS)} 之一，得到 {quality!r}")
    preset = QUALITY_PRESETS[quality]
    version = version or preset["version"]
    face_count = face_count or preset["face_count"]

    if not prompt and not image and not images:
        raise ValueError("prompt / image / images 至少提供一个")
    if image and images:
        raise ValueError("image（单图）与 images（多视角）只能提供一个")

    run_id = safe_run_id(name, prefix="run")
    out = Path(out_dir) / run_id
    out.mkdir(parents=True, exist_ok=True)
    client = _make_client(client, api_key)

    # 交付图先落盘：概念图 / 四视图来自 Lux3D，原样保存，不做本地渲染
    # （一次性交付形态下，本函数任何环节失败都不留残缺 run 目录）
    if images:
        visual_artifacts, visual_warnings = _save_visual_inputs(
            client, images, out, stem="four_view")
    elif image and not prompt:
        visual_artifacts, visual_warnings = _save_visual_inputs(
            client, [image], out, stem="concept_image")
    elif image:
        visual_artifacts, visual_warnings = _save_visual_inputs(
            client, [image], out, stem="reference_image")
    else:
        visual_artifacts, visual_warnings = {}, []

    try:
        if images:
            inputs = [_prepare_input(client, url) for url in images]
            task_id = client.image_to_3d(images=inputs, version=version,
                                         face_count=face_count)
            mode = "image_to_3d"
        elif image and not prompt:
            task_id = client.image_to_3d(image=_prepare_input(client, image),
                                         version=version, face_count=face_count)
            mode = "image_to_3d"
        else:
            task_id = client.text_to_3d(
                prompt, version=version, face_count=face_count, style=style,
                reference_image=_prepare_input(client, image) if image else None)
            mode = "text_to_3d"

        logger.info("generate %s task=%s version=%s quality=%s",
                    run_id, task_id, version, quality)
        result = client.wait(
            task_id, on_progress=lambda tid, status, elapsed: logger.info(
                "task=%s status=%s elapsed=%.0fs", tid, status, elapsed))
        saved = client.download_outputs(result, out, extensions={"glb", "zip"},
                                        prefix=f"raw_{task_id}")
        if not saved:
            raise PipelineError(f"任务 {task_id} 成功但没有可下载的模型产物："
                                f"{result.outputs}")
        # GLB 优先：zip 可能是多文件包（贴图/多格式），直接交给 trimesh 会解析失败
        raw = next((path for path in saved if path.lower().endswith(".glb")), saved[0])
        logger.info("task=%s 产物已下载：%s", task_id, raw)

        mesh = load_mesh(raw, height)
        return _process(mesh, run_id=run_id, mode=mode, out=out,
                        height_mm=height, pitch=preset["pitch"], smooth=smooth,
                        orient=orient, remesh=preset["remesh"], fmt=fmt,
                        started=started,
                        extra_artifacts=visual_artifacts,
                        extra_warnings=visual_warnings)
    except Exception:
        _discard_run_dir(out)
        raise


def run_pipeline(prompt: str | None = None, *, height_mm,
                 source: str | None = None,
                 image: str | None = None, images: list[str] | None = None,
                 quality: str = "fast", style: str = "photorealistic",
                 version: str | None = None, face_count: int | None = None,
                 name: str | None = None, out_dir: str = DEFAULT_OUT_DIR,
                 fmt: str = "3mf", orient: bool = True, smooth: bool = True,
                 client: Lux3DClient | None = None,
                 api_key: str | None = None) -> dict:
    """统一入口：给了 source 走修复，否则走生成。"""
    if source:
        return run_repair(source, height_mm, name=name, out_dir=out_dir,
                          fmt=fmt, orient=orient, smooth=smooth,
                          client=client, api_key=api_key)
    return run_generate(
        prompt, height_mm=height_mm, image=image, images=images, quality=quality,
        style=style, version=version, face_count=face_count, name=name,
        out_dir=out_dir, fmt=fmt, orient=orient, smooth=smooth,
        client=client, api_key=api_key)
