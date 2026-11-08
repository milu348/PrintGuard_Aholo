"""一次 run 的执行机械：参数校验 / run 目录 / 修复编排 / 导出。

合并自 v0.2 初版的 runs / export / process 三个小模块——它们只被
generate / repair_run 两个入口消费，合为一个「执行机械」模块后，
pipeline 层的读法变成直觉的「3 个入口 + 2 个支撑」。

内容：
- PipelineError        业务失败统一类型（参数非法用内置 ValueError）
- safe_run_id          run 目录名白名单（防目录穿越）
- validate_height      height_mm 校验（产品契约：尺寸必填）
- run_scope            入口共用的目录生命周期：建 → 执行 → 失败自动清理
- process_mesh         体检→修复→(降密)→导出 编排（do_repair=False = 用户显式跳过）
- export_model         3MF→STL 回退导出
- remesh_if_needed     面数超限体素降密
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ..core.repair import repair, repair_voxel, smooth_taubin

logger = logging.getLogger("printguard.pipeline")

DEFAULT_OUT_DIR = "output/runs"
MAX_HEIGHT_MM = 10_000.0
# 面数超过此值时降密（quadric 减面会破坏水密性，实测弃用；改体素重闭合）
MAX_DELIVERY_FACES = 1_200_000
REMESH_PITCH = 0.35

_RUN_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


class PipelineError(RuntimeError):
    """管线业务失败（生成失败 / 下载为空 / 产物缺失）。

    参数非法用内置 ValueError（不建 run 目录、无可重试性）；本类型表示
    「输入合法但执行失败」，上层任务落到 failed 并带原因，客户端可重试。
    """


# ---------- 参数校验 ----------

def safe_run_id(name: str | None, *, prefix: str = "run") -> str:
    """run 目录名白名单校验（防止 out_dir 被 ../ 穿越）。"""
    if not name:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{prefix}_{stamp}"
    name = str(name).strip()
    if not _RUN_ID_RE.fullmatch(name) or name in (".", ".."):
        raise ValueError(f"非法 run 名称：{name!r}（仅允许字母/数字/._-，≤64）")
    return name


def validate_height(height_mm) -> float:
    """height_mm 校验：数字、落在 (0, MAX_HEIGHT_MM]。AI 网格单位不可信，必填。"""
    try:
        height = float(height_mm)
    except (TypeError, ValueError):
        raise ValueError(f"height_mm 必须是数字，得到 {height_mm!r}") from None
    if not 0 < height <= MAX_HEIGHT_MM:
        raise ValueError(f"height_mm 须在 (0, {MAX_HEIGHT_MM:g}]mm，得到 {height}")
    return height


# ---------- run 目录生命周期 ----------

def prepare_run_dir(out_dir: str | Path, run_id: str) -> Path:
    """创建 run 目录（已存在时复用，幂等）。"""
    out = Path(out_dir) / run_id
    out.mkdir(parents=True, exist_ok=True)
    return out


def discard_run_dir(out: Path) -> None:
    """失败也不留残缺 run 目录（含只下载了一半的原始 GLB）。"""
    if not out.is_dir():
        return
    shutil.rmtree(out, ignore_errors=True)
    logger.info("任务失败，已清理残缺 run 目录：%s", out)


@contextmanager
def run_scope(name: str | None, *, prefix: str, out_dir: str | Path):
    """入口共用的 run 目录生命周期：校验后建目录 → 执行 → 失败自动清理。

    用法（generate / repair_run 两个入口的样板收敛到这里）：
        with run_scope(name, prefix="run", out_dir=out_dir) as (run_id, out):
            ...  # 异常自动丢弃半成品目录并原样抛出
    """
    run_id = safe_run_id(name, prefix=prefix)
    out = prepare_run_dir(out_dir, run_id)
    try:
        yield run_id, out
    except Exception:
        discard_run_dir(out)
        raise


# ---------- 修复编排与导出 ----------

def process_mesh(mesh, *, run_id: str, mode: str, out: Path, height_mm: float,
                 pitch: float | None, smooth: bool, orient: bool, remesh: bool,
                 fmt: str, started: float,
                 do_repair: bool = True) -> dict:
    """体检 → 修复 →（降密）→ 导出，返回最小交付结果。

    do_repair=False 时跳过整条修复链（含摆向/平滑/降密），仅做格式导出
    ——这是「用户明确不修复」的唯一入口，默认必修复（产品契约）。
    height_mm 传给 repair：自动摆向会改变轴对齐包围盒，必须在最终姿态上
    重新把最大边锁回目标毫米数（尺寸契约）。
    """
    warnings: list[str] = []
    if do_repair:
        try:
            fixed, steps, _before, _after = repair(
                mesh, do_orient=orient, pitch=pitch, height_mm=height_mm,
                smooth=smooth, analyze=False)
        except MemoryError:
            # 托管容器内存吃紧：放粗 pitch 再试一次（精度换内存，别让任务直接失败）
            logger.warning("run %s 修复内存不足，放粗 pitch 重试", run_id)
            coarse = min(2.0, max(0.4, (pitch or 0.5) * 2.0))
            fixed, steps, _before, _after = repair(
                mesh, do_orient=orient, pitch=coarse, height_mm=height_mm,
                smooth=smooth, analyze=False)

        if remesh:
            fixed = remesh_if_needed(fixed, steps, smooth=smooth)
    else:
        fixed = mesh
        warnings.append("已按用户要求跳过修复：模型未做可制造性体检与修复，"
                        "直接打印可能失败")
        logger.info("run %s 按用户要求跳过修复链", run_id)

    artifacts, export_warnings = export_model(fixed, out, fmt=fmt)
    warnings.extend(export_warnings)
    return {
        "ok": True,
        "run_id": run_id,
        "mode": mode,
        "artifacts": artifacts,
        "warnings": warnings,
        "duration_sec": round(time.time() - started, 1),
    }


def remesh_if_needed(mesh, steps: list[str], *, smooth: bool):
    """面数过多时体素重闭合降密（降密失败按原网格交付）。

    quadric 减面会破坏水密性（实测弃用），改体素重闭合；降密失败按
    原网格交付：大但正确。
    """
    if len(mesh.faces) <= MAX_DELIVERY_FACES:
        return mesh
    try:
        mesh, pitch = repair_voxel(mesh, pitch=REMESH_PITCH)
    except Exception as exc:  # 降密是优化项：失败按原网格交付
        logger.warning("remesh 失败，按原网格交付：%s", exc)
        return mesh
    steps.append(f"remesh(pitch={pitch:.2f}mm)")
    if smooth:
        mesh = smooth_taubin(mesh)
        steps.append("smooth_taubin(去阶梯)")
    return mesh


def export_model(mesh, out_dir: Path, *, fmt: str) -> tuple[dict, list[str]]:
    """导出交付模型到 out_dir。返回 (artifacts, warnings)。

    诊断报告不生成：服务结果只回模型与链接（体检结论仅内部驱动修复决策）。
    """
    artifacts: dict[str, str | None] = {}
    warnings: list[str] = []

    fmt = "stl" if str(fmt).lower() == "stl" else "3mf"
    model_path = out_dir / f"printable.{fmt}"
    try:
        mesh.export(str(model_path))
    except Exception as exc:
        if fmt == "3mf":
            warnings.append(f"3MF 导出失败，回退 STL：{exc}")
            fmt = "stl"
            model_path = out_dir / "printable.stl"
            try:
                mesh.export(str(model_path))
            except Exception as fallback_exc:
                raise PipelineError(
                    f"模型导出失败（3MF 与 STL 均失败）：{fallback_exc}"
                ) from fallback_exc
        else:
            raise PipelineError(f"模型导出失败：{exc}") from exc
    if not model_path.is_file() or model_path.stat().st_size == 0:
        raise PipelineError(f"模型导出为空：{model_path}")
    artifacts["model"] = str(model_path.resolve())
    artifacts["model_format"] = fmt

    return artifacts, warnings
