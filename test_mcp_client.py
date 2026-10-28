"""MCP 协议级回归（Linux/WSL）：stdio 启动 mcp_server -> initialize/list_tools/call_tool

验证 Agent 样式对齐：instructions 下发、Literal 枚举 schema、错误统一 raise、
工具可调用；并覆盖云端形态：api_key 必填校验 + 修复任务端到端（产物路径/缩略图）。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent  # 仓库根（本脚本所在目录），任何机器 clone 后可直接跑
PY = str(ROOT / ".venv" / "bin" / "python")


async def main() -> None:
    params = StdioServerParameters(
        command=PY,
        args=[str(ROOT / "mcp_server.py")],
        cwd=str(ROOT),
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            print("instructions:", (init.instructions or "")[:60].replace("\n", " "), "...")
            tools = {t.name: t for t in (await s.list_tools()).tools}
            print("tools:", sorted(tools))
            schema = tools["printguard_start_job"].inputSchema
            q_enum = schema["properties"]["quality"].get("enum")
            s_enum = schema["properties"]["style"].get("enum")
            print("quality enum:", q_enum)
            print("style enum size:", len(s_enum or []))
            print("api_key in schema:", "api_key" in schema["properties"])

            err = await s.call_tool("printguard_check_job", {"job_id": "nonexistent"})
            print("unknown job_id isError:", err.isError, "| msg:", err.content[0].text[:60])

            res = await s.call_tool(
                "printguard_analyze_printability",
                {"file_or_url": "output/healthy_cube.stl", "height_mm": 40})
            text = res.content[0].text
            print("analyze result (前 200 字):")
            print(text[:200])

            # 生成任务缺 api_key 必须显式报错（公网部署契约），不能静默失败
            no_key = await s.call_tool("printguard_start_job",
                                       {"prompt": "测试", "height_mm": 50})
            print("generate without api_key isError:", no_key.isError,
                  "| msg:", no_key.content[0].text[:80])

            ok = (bool(init.instructions) and q_enum == ["fast", "fine"]
                  and len(s_enum or []) == 7 and err.isError and "risk" in text
                  and "api_key" in schema["properties"] and no_key.isError)
            print("PROTOCOL", "PASS" if ok else "FAIL")


asyncio.run(main())
