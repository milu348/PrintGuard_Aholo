"""修复入口：已有模型（本地路径 / 公网 URL）→ 体检 → 修复 → 交付。

URL 输入经客户端下载（带 SSRF 防护），因此需要 API Key；
本地路径完全离线、无需任何 Key。
"""
from __future__ import annotations

import logging
import time

from ..core.meshio import load_mesh
from .execute import DEFAULT_OUT_DIR, process_mesh, run_scope, validate_height
from .inputs import is_url, make_client, resolve_source

logger = logging.getLogger("printguard.pipeline")


def run_repair(source: str, height_mm, *, name: str | None = None,
               out_dir: str = DEFAULT_OUT_DIR, fmt: str = "3mf",
               orient: bool = True, smooth: bool = True,
               pitch: float | None = None,
               client=None, api_key: str | None = None,
               verbose: bool = False) -> dict:
    """修复已有网格：本地路径或公网 URL → 检测 → 修复 → 交付。"""
    started = time.time()
    height = validate_height(height_mm)
    with run_scope(name, prefix="repair", out_dir=out_dir) as (run_id, out):
        if is_url(source):
            client = make_client(client, api_key, verbose=verbose)
        local = resolve_source(source, out, client)
        mesh = load_mesh(local, height)
        logger.info("repair %s（%d 面，目标 %.1fmm）", run_id, len(mesh.faces),
                    height)
        return process_mesh(mesh, run_id=run_id, mode="repair", out=out,
                            height_mm=height, pitch=pitch, smooth=smooth,
                            orient=orient, remesh=True, fmt=fmt,
                            started=started)
