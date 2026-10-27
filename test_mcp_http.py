"""云端形态回归（streamable-http）：MCP over HTTP + 交付件签名下载链接。

覆盖：
  1. /healthz 存活探针；
  2. streamable-http 端点 list_tools / call_tool（analyze_printability）；
  3. 修复任务端到端：提交 -> 轮询 -> 结果含签名下载链接 -> 链接可下载；
  4. 链接防篡改：签名/过期校验（403/410）。

用法：.venv/bin/python test_mcp_http.py [--port 8765]
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parent
PY = str(ROOT / ".venv" / "bin" / "python")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    port = args.port
    base = f"http://127.0.0.1:{port}"

    env = dict(os.environ)
    env.update({
        "PRINTGUARD_TRANSPORT": "streamable-http",
        "PRINTGUARD_HOST": "127.0.0.1",
        "PRINTGUARD_PORT": str(port),
        "PRINTGUARD_JOB_DIR": "output/jobs_http_test",
        "PRINTGUARD_JOB_DB": "output/jobs_http_test/jobs.db",
        "PRINTGUARD_FILE_SECRET": "test-secret-not-for-production",
        "PRINTGUARD_PUBLIC_BASE_URL": base,
        "PRINTGUARD_ADMIN_TOKEN": "test-admin-token",
    })
    proc = subprocess.Popen([PY, str(ROOT / "mcp_server.py")], cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # 等待服务起来
        for _ in range(60):
            try:
                if requests.get(f"{base}/healthz", timeout=2).ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("服务未启动")

        health = requests.get(f"{base}/healthz", timeout=5).json()
        print("healthz:", health)

        # 友好路由：根路径落地页（创空间 iframe 用）+ /invoke 平台探测
        land = requests.get(f"{base}/", timeout=5)
        inv = requests.post(f"{base}/invoke", timeout=5)
        landing_ok = (land.status_code == 200
                      and "text/html" in land.headers.get("content-type", "")
                      and "PrintGuard" in land.text)
        invoke_ok = (inv.status_code == 200 and inv.json().get("service") == "printguard")
        print(f"landing: {land.status_code} html={landing_ok}  invoke: {inv.status_code} ok={invoke_ok}")

        # 远程测试通道门禁：无令牌必须 404（不泄露端点存在），有令牌返回脚本清单
        # （header 与查询参数两条路径都验证——云端网关只放得行查询参数）
        admin = os.environ.get("PRINTGUARD_ADMIN_TOKEN") or env["PRINTGUARD_ADMIN_TOKEN"]
        dbg_no = requests.get(f"{base}/debug/run", timeout=5)
        dbg_ok = dbg_no.status_code == 404
        dbg_hdr = requests.get(f"{base}/debug/run", timeout=5,
                               headers={"Authorization": f"Bearer {admin}"})
        dbg_qry = requests.get(f"{base}/debug/run?admin_token={admin}", timeout=5)
        dbg_ok = dbg_ok and dbg_hdr.status_code == 200 and dbg_qry.status_code == 200 \
            and "selftest" in dbg_qry.text
        print(f"debug gate: no_token={dbg_no.status_code} header={dbg_hdr.status_code}"
              f" query={dbg_qry.status_code}")
        health_admin = requests.get(f"{base}/healthz", timeout=5).json().get("admin_configured")
        print("admin_configured(容器内 env 注入):", health_admin)

        async with streamablehttp_client(f"{base}/mcp") as (read, write, _):
            async with ClientSession(read, write) as s:
                await s.initialize()
                tools = sorted(t.name for t in (await s.list_tools()).tools)
                print("tools:", tools)

                res = await s.call_tool("analyze_printability",
                                        {"file_or_url": "output/healthy_cube.stl",
                                         "height_mm": 40})
                rep = json.loads(res.content[0].text)
                print("analyze risk:", rep["risk"])

                # 修复任务（不需要 API Key）
                sub = await s.call_tool("start_printguard_job",
                                        {"file_or_url": "output/healthy_cube.stl",
                                         "height_mm": 40})
                job_id = json.loads(sub.content[0].text)["job_id"]
                print("job:", job_id)
                payload = None
                for _ in range(120):
                    await asyncio.sleep(2)
                    st = await s.call_tool("check_printguard_job", {"job_id": job_id})
                    text = st.content[0].text
                    payload = json.loads(text)
                    if payload.get("status") in ("done", "error"):
                        break
                print("job status:", payload.get("status"))
                if payload.get("status") != "done":
                    print("error:", payload.get("error"))
                    raise SystemExit("修复任务失败")

                files = payload["result"]["files"]
                print("files:", json.dumps(files, ensure_ascii=False)[:400])
                url = files["3mf"]["url"]
                dl = requests.get(url, timeout=60)
                print("download 3mf:", dl.status_code, len(dl.content), "bytes")
                local = Path(files["3mf"]["path"])
                same = local.exists() and local.stat().st_size == len(dl.content)

                # 防篡改：改签名 / 改过期时间
                bad = requests.get(url.replace("sig=", "sig=x"), timeout=10)
                tampered = requests.get(url[:-3] + "999", timeout=10)

                ok = (tools == ["analyze_printability", "check_printguard_job",
                                "start_printguard_job"]
                      and dl.status_code == 200 and same
                      and bad.status_code == 403
                      and tampered.status_code in (403, 410)
                      and landing_ok and invoke_ok and dbg_ok)
                print("HTTP", "PASS" if ok else "FAIL",
                      f"(download={dl.status_code} same={same} bad_sig={bad.status_code} "
                      f"tampered={tampered.status_code})")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


asyncio.run(main())
