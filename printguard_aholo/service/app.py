"""MCP 应用装配：`build_app(Settings)` 显式构造一切，import 零副作用。

相对 v0.1.4 的重构：
- FastMCP 实例、两个任务队列、限时交付存储都不再是模块级单例——
  由 `build_app()` 在调用时创建并装进 `AppContext`；测试 / 嵌入式调用
  直接 `build_app(Settings(...))` 即可得到一个完整可用的应用。
- 工具与路由注册也在 `build_app` 内显式触发，不再依赖「谁 import 了谁」。

核心包只含检查 / 修复 / MCP 服务。网页 Agent 演示层在独立包
``modelscope_agent``，由组合根（server.main）按需挂载——本包不感知它。

两个单 worker 队列（各自串行，互不阻塞）：
- MCP_JOBS：MCP 工具提交的生成/修复任务（平台并发=1，天然串行）
- AGENT_JOBS：网页 Agent 回合（演示层启用时使用）；它内部的工具调用仍
  提交到 MCP_JOBS，所以必须是另一个队列，否则会把自己堵死
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from mcp.server.fastmcp import FastMCP

from ..config import Settings
from ..pipeline.delivery import DeliveryStore
from .jobs import JobManager

# 供 MCP 客户端读取的能力说明（写清能做什么、不能做什么，避免模型编造）
INSTRUCTIONS = """\
PrintGuard_Aholo：FDM 打印件的「生成 → 体检 → 修复 → 交付」MCP 服务。

推荐工作流：
1. 一步到位：printguard_run(prompt 或 image/images, height_mm, quality=...)
   —— 生成 3D（文生 / 图生 / 文图混合）、检测可制造性、修复、导出 3MF/STL，
   立即返回 job_id。**默认自动体检+修复**（repair 参数默认 True）；只有用户
   明确表示不修复 / 只要原始模型时才传 repair=False，或改用纯生成工具
   lux3d_text_to_3d / lux3d_image_to_3d（不体检不修复）。
2. 已有模型：printguard_repair(source, height_mm)
   —— source 为公网 URL 或服务端本地路径；体检+修复+交付。
3. 轮询：printguard_check_job(job_id)，建议每 5-10 秒一次。
   status=succeeded 后，result 只含 ok / run_id / mode / artifacts /
   warnings / duration_sec 与 downloads；downloads 里的链接是限时下载 URL
   （默认 5 分钟）。不生成诊断报告，也不回体检/修复摘要。
4. 只生成不修复：lux3d_text_to_3d（文生 / 文图混合）、lux3d_image_to_3d
   （图生，单图或 1-32 张多视角），同样返回 job_id。
   仅限用户明确不要修复时使用；常规需求请用 printguard_run。

范围说明：只做「文字/图片 → 3D 模型 → 体检修复 → 交付」。
概念图 / 四视图 / 材质迁移 / 骨骼动画等平台能力不在工具列表里，不要声称能用。
生成任务的开放 API 只返回 GLB，交付物只有模型文件。

尺寸：height_mm 必填，表示模型最大边缩放到该毫米数。AI 生成网格单位不可信，
不锁尺寸就无法判断壁厚/悬垂；未确认尺寸时先问用户。

图片输入：image/images 接受公网 URL、服务端本地路径或 data URI（base64）；
本地与 base64 输入会自动经 Asset 上传转成平台可用 URL。

任务串行：Lux3D 生成并发 = 1，服务端单 worker 排队执行，不要并发提交多个生成任务。

API Key：优先用每次调用的 api_key 参数；部署方可配置 AHOLO_API_KEY 环境变量。
"""


@dataclass
class AppContext:
    """一个服务实例的全部运行时状态。测试可以整体构造 / 替换。"""

    settings: Settings
    mcp: FastMCP
    mcp_jobs: JobManager
    agent_jobs: JobManager
    store: DeliveryStore
    started: float = field(default_factory=time.time)
    # 工具名 → 原始函数（register_tools 填充；agent 派发用，免字符串反射）
    tool_registry: dict[str, Callable[..., dict]] = field(default_factory=dict)
    # 异步工具名集合：这些工具只入队返回 job_id（agent 回合内要阻塞等待）
    async_tools: set[str] = field(default_factory=set)

    def uptime_sec(self) -> float:
        return round(time.time() - self.started, 1)


def build_app(settings: Settings) -> AppContext:
    """装配一个完整应用：FastMCP + 队列 + 交付存储 + 工具/路由注册。"""
    mcp = FastMCP(
        "PrintGuard_Aholo",
        instructions=INSTRUCTIONS,
        host=settings.host,
        port=settings.port,
        streamable_http_path=settings.http_path,
        log_level=settings.log_level,
    )
    ctx = AppContext(
        settings=settings,
        mcp=mcp,
        mcp_jobs=JobManager(),
        agent_jobs=JobManager(retention_sec=3600.0, max_jobs=200,
                              timeout_sec=1800.0),
        store=DeliveryStore(ttl_sec=settings.delivery_ttl_min * 60,
                            max_bytes=settings.delivery_max_bytes,
                            once=settings.delivery_once),
    )

    from .tools import register_tools
    from .web import register_routes

    register_tools(ctx)    # 注册 7 个 MCP 工具 + 填充 tool_registry
    register_routes(ctx)   # 注册 /healthz、/d/<token>
    return ctx


__all__ = ["AppContext", "build_app", "INSTRUCTIONS"]
