"""运行配置：全部来自环境变量（容器 / 魔搭创空间可注入）。

设计约定：
- 代码不硬编码任何密钥；key 由调用方传参或环境变量提供（见 `lux3d.resolve_key`）
- `stdout` 绝不打日志：stdio MCP 传输下 stdout 是协议通道
- 默认端口跟随魔搭约定 7860，但平台注入 `PORT` 时优先用 `PORT`
"""
from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


TRANSPORT = _env("PRINTGUARD_TRANSPORT", "stdio").lower()
HOST = _env(
    "PRINTGUARD_HOST",
    "0.0.0.0" if TRANSPORT != "stdio" else "127.0.0.1")
try:
    PORT = int(_env("PRINTGUARD_PORT") or _env("PORT") or "7860")
except ValueError:
    PORT = 7860
HTTP_PATH = _env("PRINTGUARD_HTTP_PATH", "/mcp") or "/mcp"
RUNS_DIR = Path(_env("PRINTGUARD_RUNS_DIR", "output/runs")).resolve()
PUBLIC_BASE_URL = _env("PRINTGUARD_PUBLIC_BASE_URL").rstrip("/")
LOG_LEVEL = _env("PRINTGUARD_LOG_LEVEL", "INFO").upper()
VERBOSE = _env("PRINTGUARD_VERBOSE") == "1"


def _int_env(name: str, default: int) -> int:
    try:
        return int(_env(name) or str(default))
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


# ---------- 一次性交付（托管形态默认开启） ----------
# 模型不长期留在创空间磁盘上：任务完成后读入内存、删除 run 目录，
# 用户通过 /d/<token> 直接下载；过期（默认 15 分钟）或一次性下载后即销毁。
# auto：托管形态（streamable-http 或配了公网前缀）→ memory；stdio → files
DELIVERY_MODE = _env("PRINTGUARD_DELIVERY", "auto").lower()
if DELIVERY_MODE not in ("auto", "memory", "files"):
    DELIVERY_MODE = "auto"
if DELIVERY_MODE == "auto":
    DELIVERY_MODE = "memory" if (TRANSPORT != "stdio" or PUBLIC_BASE_URL) else "files"

# 模型：5 分钟内有效，到期从内存清除（用户要求）
DELIVERY_TTL_MIN = max(1, _int_env("PRINTGUARD_DELIVERY_TTL_MIN", 5))
# 图片：预览要能重复加载（img 标签+点击+缩放都是独立请求），给长一点
DELIVERY_IMAGE_TTL_MIN = max(1, _int_env("PRINTGUARD_DELIVERY_IMAGE_TTL_MIN", 60))
DELIVERY_MAX_MB = max(1, _int_env("PRINTGUARD_DELIVERY_MAX_MB", 256))
# 默认按时效清除，不按「一次」销毁：浏览器预览会提前吃掉一次性 token（线上实测）
DELIVERY_ONCE = _bool_env("PRINTGUARD_DELIVERY_ONCE", False)
# 启动时清理上次运行残留的 run 目录（托管默认 1 小时，本地默认 7 天）
RUN_RETENTION_HOURS = _int_env(
    "PRINTGUARD_RUN_RETENTION_HOURS", 1 if DELIVERY_MODE == "memory" else 168)


# ---------- 前端 Agent（网页聊天形态） ----------
# 大模型走 ModelScope API-Inference（OpenAI 兼容）。token 由部署方提供。
LLM_BASE_URL = _env("PRINTGUARD_LLM_BASE_URL",
                    "https://api-inference.modelscope.cn/v1").rstrip("/")
LLM_MODEL = _env("PRINTGUARD_LLM_MODEL", "Qwen/Qwen3.5-27B")
LLM_TOKEN = _env("PRINTGUARD_LLM_TOKEN") or _env("MODELSCOPE_API_TOKEN")
LLM_TIMEOUT_SEC = _int_env("PRINTGUARD_LLM_TIMEOUT_SEC", 120)
# 免费额度经常 429：按次数退避重试，别让一次限流毁掉整轮对话
LLM_MAX_ATTEMPTS = max(1, _int_env("PRINTGUARD_LLM_MAX_ATTEMPTS", 4))
LLM_RETRY_BACKOFF_SEC = _int_env("PRINTGUARD_LLM_RETRY_BACKOFF_SEC", 8)
LLM_MAX_STEPS = max(1, _int_env("PRINTGUARD_LLM_MAX_STEPS", 8))
# 单个 job 在 Agent 回合内的最长等待（G1-Turbo 通常 1-3 分钟；fine 档更慢）
LLM_JOB_TIMEOUT_SEC = _int_env("PRINTGUARD_LLM_JOB_TIMEOUT_SEC", 1200)
# 会话历史最多携带的轮数（超长历史会撑爆上下文）
LLM_HISTORY_TURNS = max(2, _int_env("PRINTGUARD_LLM_HISTORY_TURNS", 12))

# 供 MCP 客户端读取的能力说明（写清能做什么、不能做什么，避免模型编造）
INSTRUCTIONS = """\
PrintGuard_Aholo：FDM 打印件的「生成 → 体检 → 修复 → 交付」MCP 服务。

推荐工作流：
1. 一步到位：printguard_run(prompt, height_mm, quality=...)
   —— 生成 3D、检测可制造性、修复、导出 3MF/STL，立即返回 job_id。
2. 已有模型：printguard_repair(source, height_mm)
   —— source 为公网 URL 或服务端本地路径；只做体检+修复+交付。
3. 轮询：printguard_check_job(job_id)，建议每 5-10 秒一次。
   status=succeeded 后，result 只回 ok / run_id / mode / artifacts / warnings /
   duration_sec；若服务配置了公网前缀，artifact_urls 是可直接下载的链接。
   不生成诊断报告，也不回体检/修复摘要。
4. 概念先行：先 lux3d_concept_image 或 lux3d_four_view 产出设计图，
   再把图片 URL 传给 printguard_run 的 image / images 参数。
   服务端会把概念图 / 四视图原样复制进交付目录（不进行本地渲染），
   并自动做 Asset 中转（平台签名 URL 不能直接作输入——实测结论）。
5. 只做生成（不修复）：lux3d_text_to_3d / lux3d_image_to_3d，同样返回 job_id。

交付图说明：3D 生成任务的开放 API 只返回 GLB，没有模型预览图；交付图只来自
概念图 / 四视图（原样保存）。不要声称服务端会渲染模型。

尺寸：height_mm 必填，表示模型最大边缩放到该毫米数。AI 生成网格单位不可信，
不锁尺寸就无法判断壁厚/悬垂；未确认尺寸时先问用户。

任务串行：Lux3D 生成并发 = 1，服务端单 worker 排队执行，不要并发提交多个生成任务。

API Key：优先用每次调用的 api_key 参数；部署方可配置 AHOLO_API_KEY 环境变量。
"""


def ensure_runs_dir() -> Path:
    """确保交付目录存在（服务启动时调用；worker 侧也会按需创建）。"""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return RUNS_DIR


def startup_summary() -> dict:
    """健康检查 / 启动日志用的配置摘要（绝不包含密钥内容）。"""
    return {
        "transport": TRANSPORT,
        "host": HOST,
        "port": PORT,
        "mcp_path": HTTP_PATH,
        "runs_dir": str(RUNS_DIR),
        "public_base_url": PUBLIC_BASE_URL or None,
        "delivery_mode": DELIVERY_MODE,
        "delivery_ttl_min": DELIVERY_TTL_MIN,
        "llm_configured": bool(LLM_TOKEN),
        "llm_model": LLM_MODEL,
        "api_key_configured": bool(os.environ.get("AHOLO_API_KEY")),
    }
