#!/usr/bin/env python3
"""PrintGuard 一条龙：生成 → 检测 → 修复 → 交付 3MF + 诊断图 + GIF

双入口：
  CLI：  python pipeline.py "prompt" --height-mm 130 --quality fine
  MCP：  mcp_server.py 的 start_printguard_job（复用本模块函数）

尺寸必填（尺寸策略：本地确定性缩放；未给尺寸应先询问——CLI 层用 required 强制）。
生成串行（Lux3D 并发=1）；结果 URL 2 小时过期，下载后立刻处理。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import printguard as pg
from lux3d_client import create_task, download_glbs, load_key, poll_task

QUALITY_PRESETS = {
    # fast：结构件/快速迭代（全程 ~2min）；fine：手办/细节件（生成 ~5min+本地 ~6min，文件大但保真）
    "fast": {"version": "G1-Turbo", "face_count": 100_000, "pitch": None, "remesh": True},
    "fine": {"version": "G1", "face_count": 300_000, "pitch": 0.3, "remesh": False},
}


def deliver_outputs(fixed, after, steps, out) -> dict:
    """交付件：printable.3mf（打印基准）+ diagnostic_sheet.png（报告+渲染合一）+ turntable.gif（准 3D）
    + sheet_preview.jpg（诊断图缩略，供 MCP 内联给 agent 看）。路径一律绝对值
    （uvx 分发时宿主 cwd 不确定，绝对路径是无服务器形态的兜底）。单项失败不阻塞其余交付。"""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    d = {}
    f3 = out / "printable.3mf"
    fixed.export(str(f3))
    d["3mf"] = str(f3.resolve())
    try:
        sh = out / "diagnostic_sheet.png"
        pg.render_diagnostic_sheet(fixed, after, steps, sh)
        d["sheet"] = str(sh.resolve())
        try:  # 内联用缩略图（JPEG ~百KB 级，控制 agent 上下文体积；全尺寸 PNG 留在路径）
            from PIL import Image as PILImage
            th = out / "sheet_preview.jpg"
            with PILImage.open(sh) as im:
                im.thumbnail((512, 512))
                im.convert("RGB").save(th, "JPEG", quality=85)
            d["sheet_preview"] = str(th.resolve())
        except Exception:
            pass  # 缩略图失败不影响交付，agent 退化为读路径
    except Exception as e:
        d["sheet"] = f"sheet skipped: {e}"[:120]
    try:
        gf = out / "turntable.gif"
        pg.render_turntable_gif(fixed, gf)
        d["gif"] = str(gf.resolve())
    except Exception as e:
        d["gif"] = f"gif skipped: {e}"[:120]
    return d


def run_repair(file_path: str, height_mm: float, name: str | None = None,
               out_dir: str = "output/runs", no_orient: bool = False) -> dict:
    """修复已有 mesh（CLI/MCP 复用）：加载 → 检测 → 修复 → 交付。返回 summary dict。"""
    name = name or time.strftime("repair_%H%M%S")
    out = Path(out_dir) / name
    out.mkdir(parents=True, exist_ok=True)
    m = pg.load_mesh(file_path, height_mm)
    fixed, steps, before, after = pg.repair(m, do_orient=not no_orient,
                                            height_mm=height_mm)
    if len(fixed.faces) > 1_200_000:
        try:  # 网格过密：体素重闭合降密（quadric 减面会破坏水密性，实测弃用）
            fixed, p2 = pg.repair_voxel(fixed, pitch=0.35)
            steps.append(f"remesh({p2}mm)")
        except Exception:
            pass  # 失败时按原网格交付（大但正确）
    deliver = deliver_outputs(fixed, after, steps, out)
    return {
        "file": str(Path(file_path).resolve()), "height_mm": height_mm,
        "risk": f"{before['risk']['grade']}->{after['risk']['grade']}",
        "watertight": (f"{before['checks']['watertight']['watertight']}"
                       f"->{after['checks']['watertight']['watertight']}"),
        "overhang_frac": (f"{before['checks']['overhang']['bad_frac']}"
                          f"->{after['checks']['overhang']['bad_frac']}"),
        "repair_steps": steps,
        **deliver,
    }


def run_pipeline(prompt: str, height_mm: float, quality: str = "fast",
                 style: str = "photorealistic", image: str | None = None,
                 version: str | None = None, face_count: int | None = None,
                 from_file: str | None = None, name: str | None = None,
                 out_dir: str = "output/runs", no_orient: bool = False) -> dict:
    """一条龙核心（CLI 与 MCP 复用）。返回 summary dict（失败时含 failed 标记）。
    image：可选参考图（本地路径或 URL），与 prompt 同时使用（文生 3D 的 img 字段）。"""
    q = QUALITY_PRESETS[quality]
    version = version or q["version"]
    face_count = face_count or q["face_count"]

    name = name or time.strftime("run_%H%M%S")
    out = Path(out_dir) / name
    out.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    tid = None
    gen_sec = 0
    if from_file:
        raw_glb = Path(from_file)
        print(f"[skip-gen] from-file: {raw_glb}", flush=True)
    else:
        key = load_key()
        tid = create_task(key, prompt, version=version,
                          face_count=face_count, style=style, image=image)
        print(f"[gen] task={tid} version={version} mode={quality} ...", flush=True)
        data = poll_task(key, tid)
        if data["status"] != 3:
            return {"taskid": tid, "status": data["status"], "failed": True}
        gen_sec = data["elapsed_sec"]
        glbs = download_glbs(data.get("outputs", []), "raw", out_dir=out)
        if not glbs:
            return {"failed": True, "error": "no glb downloaded"}
        raw_glb = glbs[0]

    m = pg.load_mesh(str(raw_glb), height_mm)
    fixed, steps, before, after = pg.repair(m, do_orient=not no_orient,
                                            pitch=q["pitch"], height_mm=height_mm)
    if q["remesh"] and len(fixed.faces) > 1_200_000:
        try:  # 网格过密：体素重闭合降密（quadric 减面会破坏水密性，实测弃用）
            fixed, p2 = pg.repair_voxel(fixed, pitch=0.35)
            steps.append(f"remesh({p2}mm)")
        except Exception:
            pass  # 失败时按原网格交付（大但正确）

    deliver = deliver_outputs(fixed, after, steps, out)

    report = {
        "prompt": prompt, "style": style, "version": version,
        "quality": quality, "taskid": tid, "gen_elapsed_sec": gen_sec,
        "height_mm": height_mm, "raw_glb": str(raw_glb), "image": image,
        "before": before, "repair_steps": steps, "after": after,
        **deliver,
    }
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    summary = {
        "prompt": prompt,
        "quality": quality,
        "taskid": tid,
        "gen_sec": gen_sec,
        "total_sec": round(time.time() - t0),
        "risk": f"{before['risk']['grade']}->{after['risk']['grade']}",
        "watertight": (f"{before['checks']['watertight']['watertight']}"
                       f"->{after['checks']['watertight']['watertight']}"),
        "overhang_frac": (f"{before['checks']['overhang']['bad_frac']}"
                          f"->{after['checks']['overhang']['bad_frac']}"),
        **deliver,
    }
    return summary


def main(argv=None) -> int:
    try:  # 控制台可能是 GBK，报告含中文时避免打印崩溃
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="pipeline",
                                 description="PrintGuard 一条龙：生成→检测→修复→交付")
    ap.add_argument("prompt", help="要生成的模型描述")
    ap.add_argument("--height-mm", type=float, required=True,
                    help="目标尺寸：模型最大边缩放到该 mm（必填）")
    ap.add_argument("--quality", default="fast", choices=["fast", "fine"],
                    help="fast=G1-Turbo+100k+自适应pitch(~2min)；fine=G1+300k+0.3mm(手办/细节件,~10min)")
    ap.add_argument("--style", default="photorealistic",
                    choices=["photorealistic", "cartoon", "anime", "hand_painted",
                             "cyberpunk", "fantasy", "glass"])
    ap.add_argument("--version", default=None, choices=["G1", "G1-Turbo"],
                    help="覆盖 quality 预设的模型版本")
    ap.add_argument("--face-count", type=int, default=None,
                    help="覆盖 quality 预设的面数")
    ap.add_argument("--from-file", default=None,
                    help="跳过生成，直接用本地 GLB（测试/资产池复用）")
    ap.add_argument("--image", default=None,
                    help="可选参考图（本地路径或 URL），与 prompt 同时使用")
    ap.add_argument("--name", default=None, help="输出子目录名（默认按时间）")
    ap.add_argument("--out-dir", default="output/runs")
    ap.add_argument("--no-orient", action="store_true", help="跳过自动摆向")
    args = ap.parse_args(argv)

    summary = run_pipeline(prompt=args.prompt, height_mm=args.height_mm,
                           quality=args.quality, style=args.style,
                           image=args.image,
                           version=args.version, face_count=args.face_count,
                           from_file=args.from_file, name=args.name,
                           out_dir=args.out_dir, no_orient=args.no_orient)
    print(json.dumps(summary, ensure_ascii=False, indent=1), flush=True)
    return 0 if summary.get("3mf") else 1


if __name__ == "__main__":
    sys.exit(main())
