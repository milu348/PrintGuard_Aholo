"""生成入口：文生 / 图生 / 文图混合 三种模式 → 体检 → 修复 → 交付。

输入路由（prompt / image / images 任意组合，规则与官方 API 一一对应）：

| 输入 | 平台端点 | mode |
|---|---|---|
| 只有 prompt | text-to-3d | `text_to_3d` |
| prompt + 单张图 | text-to-3d（img=参考图，文图混合） | `mixed_to_3d` |
| prompt + 多张图 | 不支持（平台限制）→ ValueError | — |
| 只有 1 张图 | img-to-3d（img=） | `image_to_3d` |
| 多张图（1-32） | img-to-3d（imgs=，首张为主参考） | `image_to_3d` |

quality 预设同时约束生成参数（版本 / 面数）与修复 pitch。
"""
from __future__ import annotations

import logging
import time

from ..core.meshio import load_mesh
from .execute import (DEFAULT_OUT_DIR, PipelineError, process_mesh, run_scope,
                      validate_height)
from .inputs import make_client, normalize_generation_images

logger = logging.getLogger("printguard.pipeline")

QUALITY_PRESETS = {
    # fast：结构件/快速迭代；fine：手办/细节件（生成慢，交付保真）
    "fast": {"version": "G1-Turbo", "face_count": 100_000,
             "pitch": None, "remesh": True},
    "fine": {"version": "G1", "face_count": 300_000,
             "pitch": 0.3, "remesh": False},
}


def run_generate(prompt: str | None = None, *, height_mm,
                 image: str | None = None,
                 images: list[str] | None = None,
                 quality: str = "fast", style: str = "photorealistic",
                 version: str | None = None, face_count: int | None = None,
                 name: str | None = None,
                 out_dir: str = DEFAULT_OUT_DIR,
                 fmt: str = "3mf", orient: bool = True, smooth: bool = True,
                 repair: bool = True,
                 client=None, api_key: str | None = None,
                 verbose: bool = False) -> dict:
    """生成并交付：生成（三种模式）→ 下载 GLB → 体检 → 修复 → 3MF/STL。

    repair 默认 True（产品契约：生成后自动体检+修复）；仅当用户明确
    表示不修复 / 只要原始模型时才允许传 False——此时跳过整条修复链，
    只做格式导出，warnings 里会带提示。
    """
    started = time.time()
    height = validate_height(height_mm)
    if quality not in QUALITY_PRESETS:
        raise ValueError(f"quality 须为 {sorted(QUALITY_PRESETS)} 之一，得到 {quality!r}")
    preset = QUALITY_PRESETS[quality]
    version = version or preset["version"]
    face_count = face_count or preset["face_count"]

    prompt = (str(prompt).strip() if prompt else "") or None
    if not prompt and not image and not images:
        raise ValueError("必须提供 prompt（文字描述）或 image/images（图片输入）")

    # 参数全部合法才建 run 目录；失败自动清理半成品
    with run_scope(name, prefix="run", out_dir=out_dir) as (run_id, out):
        client = make_client(client, api_key, verbose=verbose)
        urls = normalize_generation_images(client, image=image, images=images)

        if prompt and urls:
            if len(urls) > 1:
                raise ValueError(
                    "文图混合生成只支持单张参考图；多图请去掉文字描述，"
                    "或只传图片（img-to-3d 多视角）")
            task_id = client.text_to_3d(
                prompt, version=version, face_count=face_count,
                style=style, img=urls[0])
            mode = "mixed_to_3d"
        elif prompt:
            task_id = client.text_to_3d(
                prompt, version=version, face_count=face_count, style=style)
            mode = "text_to_3d"
        elif len(urls) == 1:
            task_id = client.img_to_3d(
                version=version, face_count=face_count, image=urls[0])
            mode = "image_to_3d"
        else:
            task_id = client.img_to_3d(
                version=version, face_count=face_count, images=urls)
            mode = "image_to_3d"

        logger.info("generate %s task=%s version=%s quality=%s mode=%s",
                    run_id, task_id, version, quality, mode)
        result = client.wait(
            task_id, on_progress=lambda tid, status, elapsed: logger.info(
                "task=%s status=%s elapsed=%.0fs", tid, status, elapsed))
        saved = client.download_outputs(result, out, extensions={"glb", "zip"},
                                        prefix=f"raw_{task_id}")
        if not saved:
            raise PipelineError(f"任务 {task_id} 成功但没有可下载的模型产物："
                                f"{result.outputs}")
        # GLB 优先：zip 可能是多文件包（贴图/多格式），直接交给 trimesh 会解析失败
        raw = next((path for path in saved if path.lower().endswith(".glb")),
                   saved[0])
        logger.info("task=%s 产物已下载：%s", task_id, raw)

        mesh = load_mesh(raw, height)
        return process_mesh(mesh, run_id=run_id, mode=mode, out=out,
                            height_mm=height, pitch=preset["pitch"],
                            smooth=smooth, orient=orient,
                            remesh=preset["remesh"], fmt=fmt,
                            do_repair=repair, started=started)
