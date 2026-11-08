"""一条龙管线：生成 → 下载 → 体检 → 修复 → 交付。

模块地图（依赖方向单向：execute / inputs → 两个入口 → __init__）：
- ``execute``      执行机械：校验 / run 目录 / 修复编排 / 导出 / PipelineError
- ``inputs``       输入归一化：source（本地/URL）、image（URL/本地/base64→Asset URL）
- ``generate``     生成入口：文生 / 图生 / 文图混合 三种模式
- ``repair_run``   修复入口：已有模型（本地路径 / 公网 URL）
- ``delivery``     限时交付：内存取件 + 限时链接，run 目录不长期保留

设计约定：
- **尺寸必填**：AI 生成网格单位不可信，不先把最大边锁到目标毫米数，
  壁厚/悬垂结论没有可比性。
- **默认必修复**：生成后自动体检+修复（产品契约）；唯一不修复路径是
  显式 ``repair=False``（用户明确不要修复）。
- **生成串行**：Lux3D 生成类任务账号级并发 = 1；本包阻塞执行，服务层用
  JobManager 放到单 worker 线程，客户端拿 job_id 轮询。
- **结果 URL 2 小时过期**：wait() 成功后立即下载，不做延迟处理。
- **只交付模型**：不生成诊断报告，结果只回 ok / run_id / mode / artifacts /
  warnings / duration_sec / downloads；体检结论仅内部驱动修复决策。
- **交付先落本地**：管线把模型写进 run 目录，再由 delivery 层转成限时下载
  链接；管线本身不假设容器外可访问本地路径。
- **失败要响**：生成失败、下载为空、尺寸非法一律抛异常，让上层任务落到
  failed 状态并带原因，不返回半成品。
"""
from __future__ import annotations

from .execute import PipelineError
from .generate import run_generate
from .repair_run import run_repair

__all__ = ["PipelineError", "run_generate", "run_repair"]
