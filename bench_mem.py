#!/usr/bin/env python3
"""内存/耗时基准：python bench_mem.py <mesh文件> [height_mm] [--repair]

阶段化打印峰值 RSS 与耗时，用于定位大模型的内存热点。
建议在魔搭 Notebook（免费 8核32G CPU 实例）上跑大件，本机 8GB 会 OOM：

    git clone https://www.modelscope.cn/studios/miluzzz/PrintGuard.git
    cd PrintGuard && python -m venv .venv && .venv/bin/pip install -e .
    .venv/bin/python bench_mem.py ahri.glb 130
"""
from __future__ import annotations

import resource
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0] or ".")
import printguard as pg  # noqa: E402


def peak_mb() -> float:
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return v / 1024 if sys.platform != "darwin" else v / 1048576


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    height = float(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else None
    do_repair = "--repair" in sys.argv

    t0 = time.monotonic()
    m = pg.load_mesh(path, height)
    print(f"[load]    {time.monotonic()-t0:7.1f}s  faces={len(m.faces):>9,}  "
          f"extents={m.extents.round(1).tolist()}  peak={peak_mb():6.0f}MB", flush=True)

    t0 = time.monotonic()
    rep = pg.analyze_mesh(m)
    ck = rep["checks"]
    print(f"[analyze] {time.monotonic()-t0:7.1f}s  risk={rep['risk']['grade']}"
          f"({rep['risk']['score']:.0f})  wall={ck['wall_thickness'].get('verdict')}"
          f"/{ck['wall_thickness'].get('min_wall_mm')}mm  "
          f"overhang_bad={ck['overhang'].get('bad_frac')}  peak={peak_mb():6.0f}MB", flush=True)

    # 非水密（AI 生成件常态）时 analyze 按设计跳过壁厚——重活在 repair 的体素重建里
    if do_repair or not ck["watertight"]["watertight"]:
        t0 = time.monotonic()
        m2, steps, before, after = pg.repair(m, do_orient=False)
        ck2 = after["checks"]
        print(f"[repair]  {time.monotonic()-t0:7.1f}s  steps={steps}  peak={peak_mb():6.0f}MB",
              flush=True)
        print(f"[after]   watertight={ck2['watertight']['watertight']}  "
              f"wall={ck2['wall_thickness'].get('verdict')}"
              f"/{ck2['wall_thickness'].get('min_wall_mm')}mm  "
              f"risk={after['risk']['grade']}({after['risk']['score']:.0f})  "
              f"peak={peak_mb():6.0f}MB", flush=True)
    print(f"[done] 最终峰值 RSS = {peak_mb():.0f}MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
