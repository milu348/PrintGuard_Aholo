"""modelscope_agent —— PrintGuard_Aholo 的网页 Agent 演示层（可整体删除）。

把「网页聊天 → 大模型 function calling → PrintGuard_Aholo 工具」这条演示链路
从核心包里独立出来。核心（检查 / 修复 / MCP 服务）不 import 本包；
组合根在 ``PRINTGUARD_LLM_TOKEN`` 已配置时显式挂载：

    from modelscope_agent import register_routes
    register_routes(ctx)   # 挂载 /（聊天页）、/api/chat、/api/job

模块地图：
- ``client``  ModelScope API-Inference 客户端（OpenAI 兼容，参数化可注入）
- ``agent``   回合编排：function calling 循环 / 工具派发 / 图片注入
- ``routes``  HTTP 路由（聊天页 + 回合接口），仅演示层启用时挂载
- ``static/`` 聊天前端（支持图片上传）

核心能力（检查/修复/交付）全部在 printguard_aholo；本包只是它的一个调用方。
"""
from __future__ import annotations

from .agent import (
    GENERATION_TOOLS,
    SYSTEM_PROMPT,
    Reply,
    get_turn,
    respond,
    run_tool,
    submit_turn,
    tool_specs,
)
from .client import ChatResult, LLMClient, LLMError, ToolCall
from .routes import register_routes

__all__ = [
    "GENERATION_TOOLS",
    "LLMClient",
    "LLMError",
    "Reply",
    "SYSTEM_PROMPT",
    "ChatResult",
    "ToolCall",
    "get_turn",
    "register_routes",
    "respond",
    "run_tool",
    "submit_turn",
    "tool_specs",
]
