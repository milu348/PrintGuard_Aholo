#!/usr/bin/env python3
"""Aholo 开放平台 API 客户端：生成任务/轮询/下载、Asset 上传，
以及概念图能力（schema 直译自官方 OpenAPI，已实测）。
常规一条龙走 pipeline.py；概念图可独立导入调用（与生成同串行规则）。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

BASE = "https://api.aholo3d.cn"
KEY_FILE = "secrets.local.json"
OUT_DIR = Path("output/smoke")
POLL_SEC = 15
TIMEOUT_SEC = 20 * 60


def load_key(explicit: str | None = None) -> str:
    """取 API Key，优先级：显式入参（公网部署由用户填写）> 环境变量 > 本地 secrets.local.json。

    公网部署（魔搭等托管平台）里不能把 key 写进镜像/环境变量，必须由调用方
    通过工具参数 api_key 传入；本地开发仍可用环境变量或 secrets.local.json。"""
    import os
    for cand in (explicit, os.environ.get("AHOLO_API_KEY", "")):
        s = (cand or "").strip()
        if s:
            return s
    p = Path(KEY_FILE)
    if p.exists():
        raw = p.read_text(encoding="utf-8").strip()
        s = json.loads(raw) if raw.startswith('"') else raw
        if ":" in s:
            s = s.split(":", 1)[1].strip()
        if s:
            return s
    raise SystemExit(
        "缺少 AHOLO API Key：公网调用请在工具参数 api_key 传入，"
        "本地可设置环境变量 AHOLO_API_KEY 或填 secrets.local.json")


def create_task(key: str, prompt: str, version: str = "G1-Turbo",
                face_count: int = 100000, style: str | None = None,
                output_format: list | None = None,
                image: str | None = None) -> int:
    body = {
        "prompt": prompt,
        "version": version,
        "faceCount": face_count,
        "outputFormat": output_format or ["glb"],
        "enablePbr": False,       # 白模（仅 G1-Turbo 生效）
        "aiPredictSize": False,   # 尺寸策略：本地确定性缩放
    }
    if style:
        body["style"] = style
    if image:  # 可选参考图（图+文同时输入）：URL 直传，本地文件先走 Asset 上传
        body["img"] = image if image.startswith(("http://", "https://")) else upload_mesh(image, key=key)
    for attempt in range(3):
        r = requests.post(f"{BASE}/lux3d/v1/generate/text-to-3d/task/create",
                          headers={"Authorization": key}, json=body, timeout=30)
        d = r.json()
        if d.get("c") == "0":
            return int(d["d"])
        code = str(d.get("c", ""))
        print(f"[create] attempt{attempt} c={code} m={d.get('m')}", flush=True)
        if "CONCURRENCY" in code:
            time.sleep(30)
            continue
        if r.status_code >= 500:
            time.sleep(10)
            continue
        raise SystemExit(f"create failed: {d}")
    raise SystemExit("create: 重试耗尽")


def poll_task(key: str, taskid: int) -> dict:
    t0 = time.time()
    last = None
    while time.time() - t0 < TIMEOUT_SEC:
        r = requests.get(f"{BASE}/lux3d/v1/generate/task/get",
                         headers={"Authorization": key},
                         params={"taskid": taskid}, timeout=30)
        d = r.json()
        if d.get("c") != "0":
            print(f"[poll] c={d.get('c')} m={d.get('m')}", flush=True)
            time.sleep(POLL_SEC)
            continue
        data = d["d"]
        st = data["status"]
        if st != last:
            print(f"[poll] task={taskid} status={st} elapsed={time.time()-t0:.0f}s", flush=True)
            last = st
        if st in (3, 4, 6):
            data["elapsed_sec"] = round(time.time() - t0)
            return data
        time.sleep(POLL_SEC)
    raise SystemExit(f"task {taskid} 轮询超时")


def download_glbs(outputs: list, name: str,
                  out_dir: Path | None = None) -> list[Path]:
    out_dir = Path(out_dir) if out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for o in outputs:
        url = o.get("content") or ""
        if not url.startswith("http"):
            continue  # 跳过 NOT_REQUESTED 等
        ext = url.split("?")[0].rsplit(".", 1)[-1].lower() or "glb"
        fn = out_dir / f"{name}_{len(paths)}.{ext}"  # 按真实扩展名命名（G1 会同时给 ZIP+GLB）
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(fn, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        paths.append(fn)
        print(f"[download] {fn.name} {fn.stat().st_size/1e6:.1f}MB", flush=True)
    return paths


# ---------- Asset 上传（本地文件 -> 公网 URL；参考图上传用） ----------

def _client_config(key: str | None = None):
    """SDK v1.9+：create_*_client(config)，config 显式带 key 与 region。"""
    from manycore.aholo_sdk_core import AholoClientConfig
    return AholoClientConfig(api_key=load_key(key), region="cn")


def upload_mesh(path, key: str | None = None) -> str:
    """本地文件 -> 公网 URL（官方 Asset SDK，内置分块上传）。"""
    from manycore.aholo_sdk_asset import create_asset_client
    asset = create_asset_client(_client_config(key))
    res = asset.upload_file(str(path))
    return res.url


# ---------- 群核能力扩展（概念图）——官方 OpenAPI schema 直译，已实测（v0.3.1），未接入交付链 ----------

def _wait_and_download(key: str, tid: int, dest_dir, want_ext: set) -> list:
    """Lux3D 任务通用：轮询至终态 + 按扩展名下载。"""
    data = poll_task(key, tid)
    if data["status"] != 3:
        raise RuntimeError(f"task {tid} 未成功: status={data['status']}")
    out_dir = Path(dest_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for o in data.get("outputs", []):
        u = (o or {}).get("content") or ""
        if not u.startswith("http"):
            continue
        ext = u.split("?")[0].rsplit(".", 1)[-1].lower()
        if ext not in want_ext:
            continue
        dest = out_dir / f"{tid}_{len(paths)}.{ext}"
        with requests.get(u, stream=True, timeout=180) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        paths.append(dest)
    return paths


def concept_image(key: str, prompt: str, image: str | None = None,
                  dest_dir="output/concept") -> Path:
    """multimodal-to-image：概念图（纯文案或图文）。返回图片路径。"""
    body = {"prompt": prompt}
    if image:
        body["img"] = image if image.startswith(("http://", "https://")) else upload_mesh(image, key=key)
    r = requests.post(f"{BASE}/lux3d/v1/generate/multimodal-to-image/task/create",
                      headers={"Authorization": key}, json=body, timeout=30)
    d = r.json()
    if d.get("c") != "0":
        raise RuntimeError(f"concept create failed: {d}")
    paths = _wait_and_download(key, int(d["d"]), dest_dir, {"png", "jpg", "jpeg", "webp"})
    if not paths:
        raise RuntimeError("concept: no image url in outputs")
    return paths[0]
