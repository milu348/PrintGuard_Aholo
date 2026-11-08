"""可制造性 CLI：run（一条龙）/ analyze / repair / selftest，不依赖 MCP 服务。

与 v0.1.4 的差异：`run` 支持图片输入（--image / --images，文生 / 图生 /
文图混合三合一，与 MCP 工具 printguard_run 同一条管线）。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .core.inspect import analyze_mesh
from .core.meshio import load_mesh
from .core.repair import repair
from .core.selftest import selftest
from .lux3d import STYLES


def _build_run_parser(sub) -> None:
    run = sub.add_parser("run", help="一条龙：生成 → 体检 → 修复 → 交付")
    run.add_argument("prompt", nargs="?", default=None,
                     help="模型描述（文生 / 文图混合；纯图片生成可省略）")
    run.add_argument("--height-mm", type=float, required=True,
                     help="目标最大边（mm）；生成网格单位不可信，必填")
    run.add_argument("--from-file", "--source", dest="source", default=None,
                     help="跳过生成，直接修复本地文件或公网 URL")
    run.add_argument("--image", default=None,
                     help="参考图（URL / 本地路径）：配 prompt=文图混合，"
                          "单独使用=图生 3D")
    run.add_argument("--images", nargs="+", default=None,
                     help="1-32 张多视角图片（URL / 本地路径），与 --image 二选一")
    run.add_argument("--quality", default="fast",
                     choices=("fast", "fine"),
                     help="fast=G1-Turbo+100k；fine=G1+300k+0.3mm（手办/细节件）")
    run.add_argument("--style", default="photorealistic", choices=STYLES)
    run.add_argument("--version", default=None, choices=("G1", "G1-Turbo"),
                     help="覆盖 quality 预设的模型版本")
    run.add_argument("--face-count", type=int, default=None,
                     help="覆盖 quality 预设的面数（10000-300000）")
    run.add_argument("--name", default=None, help="run 目录名（默认按时间）")
    run.add_argument("--out-dir", default="output/runs")
    run.add_argument("--fmt", default="3mf", choices=("3mf", "stl"))
    run.add_argument("--no-orient", action="store_true", help="跳过自动摆向")
    run.add_argument("--no-smooth", action="store_true",
                     help="跳过去阶梯平滑（保留 45° 阶梯面）")
    run.add_argument("--no-repair", action="store_true",
                     help="跳过整条修复链（默认必修复；仅用户明确不要修复时使用）")
    run.add_argument("--api-key", default=None,
                     help="Lux3D API Key（缺省读 AHOLO_API_KEY 环境变量）")


def main(argv=None) -> int:
    try:  # 控制台可能是 GBK，报告含中文/特殊符号时避免打印崩溃
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    ap = argparse.ArgumentParser(
        prog="printguard",
        description="PrintGuard_Aholo：FDM 打印件生成→体检→修复→交付")
    sub = ap.add_subparsers(dest="cmd", required=True)

    _build_run_parser(sub)

    a1 = sub.add_parser("analyze", help="检测 mesh，输出 JSON 报告")
    a1.add_argument("file")
    a1.add_argument("--height-mm", type=float, default=None,
                    help="先把模型最大边缩放到指定 mm（GLB 单位不可信时必用）")
    a1.add_argument("--json", default=None, help="报告写出到该文件")

    a2 = sub.add_parser("repair", help="检测 + 修复 + 导出 STL（几何层，不含交付）")
    a2.add_argument("file")
    a2.add_argument("-o", "--out", required=True, help="输出 STL 路径")
    a2.add_argument("--height-mm", type=float, default=None)
    a2.add_argument("--pitch", type=float, default=None, help="体素分辨率 mm（默认自适应）")
    a2.add_argument("--no-orient", action="store_true", help="跳过自动摆向")
    a2.add_argument("--no-smooth", action="store_true",
                    help="跳过去阶梯平滑（保留体素重构的原始 45° 阶梯面）")
    a2.add_argument("--json", default=None, help="报告写出到该文件")

    sub.add_parser("selftest", help="内置合成用例自测")
    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()

    if args.cmd == "run":
        from .lux3d import Lux3DError
        from .pipeline import PipelineError, run_generate, run_repair

        try:
            if args.source:
                summary = run_repair(
                    args.source, args.height_mm, name=args.name,
                    out_dir=args.out_dir, fmt=args.fmt,
                    orient=not args.no_orient, smooth=not args.no_smooth,
                    api_key=args.api_key)
            else:
                summary = run_generate(
                    args.prompt, height_mm=args.height_mm,
                    image=args.image, images=args.images,
                    quality=args.quality, style=args.style,
                    version=args.version, face_count=args.face_count,
                    name=args.name, out_dir=args.out_dir, fmt=args.fmt,
                    orient=not args.no_orient, smooth=not args.no_smooth,
                    repair=not args.no_repair, api_key=args.api_key)
        except (ValueError, PipelineError, Lux3DError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)},
                             ensure_ascii=False, indent=1), file=sys.stderr)
            return 2
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 0 if summary.get("ok") else 1

    if args.cmd == "analyze":
        m = load_mesh(args.file, args.height_mm)
        rep = analyze_mesh(m)
    else:
        m = load_mesh(args.file, args.height_mm)
        m, steps, before, after = repair(m, do_orient=not args.no_orient,
                                         pitch=args.pitch,
                                         smooth=not args.no_smooth)
        m.export(args.out)
        rep = {"before": before, "repair_steps": steps, "after": after,
               "output": args.out}

    text = json.dumps(rep, ensure_ascii=False, indent=1)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
