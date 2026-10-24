"""MCP 协议级回归：stdio 启动 mcp_server -> initialize/list_tools/call_tool
验证四项 Agent 样式对齐：instructions 下发、Literal 枚举 schema、错误统一 raise、工具可调用。"""
import asyncio
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = str(Path(__file__).resolve().parent)  # 仓库根（本脚本所在目录），任何机器 clone 后可直接跑


async def main() -> None:
    params = StdioServerParameters(
        command=ROOT + r"\.venv\Scripts\printguard-mcp.exe",
        args=[],
        cwd=ROOT,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            print("instructions:", (init.instructions or "")[:60].replace("\n", " "), "...")
            tools = {t.name: t for t in (await s.list_tools()).tools}
            print("tools:", sorted(tools))
            schema = tools["start_printguard_job"].inputSchema
            q_enum = schema["properties"]["quality"].get("enum")
            s_enum = schema["properties"]["style"].get("enum")
            print("quality enum:", q_enum)
            print("style enum size:", len(s_enum or []))
            err = await s.call_tool("check_printguard_job", {"job_id": "nonexistent"})
            print("unknown job_id isError:", err.isError, "| msg:", err.content[0].text[:60])
            res = await s.call_tool(
                "analyze_printability",
                {"file_or_url": "output/healthy_cube.stl",
                 "height_mm": 80})
            text = res.content[0].text
            print("analyze result (前 200 字):")
            print(text[:200])
            ok = (bool(init.instructions) and q_enum == ["fast", "fine"]
                  and len(s_enum or []) == 7 and err.isError and "risk" in text)
            print("PROTOCOL", "PASS" if ok else "FAIL")


asyncio.run(main())
