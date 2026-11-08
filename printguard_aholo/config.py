"""运行配置：`Settings` 数据类 + `Settings.from_env()`。

重构要点（相对 v0.1.4）：
- 不再在 import 时固化模块级常量——环境变量只在 `from_env()` 被显式调用时读取一次，
  之后以 `Settings` 实例在服务内传递。测试可以直接构造 `Settings(...)` 注入，
  不再需要 reload 模块。
- 环境变量名全部保持不变（容器 / 魔搭创空间的部署配置零迁移）。

设计约定：
- 代码不硬编码任何密钥；key 由调用方传参或环境变量提供（见 `lux3d.client.resolve_key`）
- `stdout` 绝不打日志：stdio MCP 传输下 stdout 是协议通道
- 默认端口跟随魔搭约定 7860，但平台注入 `PORT` 时优先用 `PORT`
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


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


@dataclass
class Settings:
    """一次服务运行的全部配置。所有字段都有安全默认值，可按需覆盖。"""

    # ---------- 传输与监听（stdio / streamable-http） ----------
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 7860
    http_path: str = "/mcp"

    # ---------- 存储与交付 ----------
    runs_dir: Path = field(default_factory=lambda: Path("output/runs").resolve())
    public_base_url: str = ""
    # 限时交付：模型不长期留在创空间磁盘上——任务成功后读入内存、删除 run 目录，
    # 用户经 /d/<token> 下载，到期（默认 5 分钟）自动从内存清除。
    # 本地 CLI 形态不走这条路：产物直接落在 run 目录，用本地路径。
    delivery_ttl_min: int = 5
    delivery_max_mb: int = 256
    # 默认按时效清除，不按「一次」销毁：浏览器预取会提前吃掉 once token（线上实测）
    delivery_once: bool = False
    # 启动时清理上次运行残留的 run 目录（防崩溃残留越积越多）
    run_retention_hours: int = 168

    # ---------- 前端 Agent（网页聊天形态） ----------
    llm_base_url: str = "https://api-inference.modelscope.cn/v1"
    llm_model: str = "Qwen/Qwen3.5-27B"
    llm_token: str = ""
    llm_timeout_sec: int = 120
    # 免费额度经常 429：按次数退避重试，别让一次限流毁掉整轮对话
    llm_max_attempts: int = 4
    llm_retry_backoff_sec: int = 8
    llm_max_steps: int = 8
    # 单个 job 在 Agent 回合内的最长等待（G1-Turbo 通常 1-3 分钟；fine 档更慢）
    llm_job_timeout_sec: int = 1200
    # 会话历史最多携带的轮数（超长历史会撑爆上下文）
    llm_history_turns: int = 12

    # ---------- 行为开关 ----------
    log_level: str = "INFO"
    verbose: bool = False
    # 允许下载内网 / 保留地址（仅本地调试；托管形态必须保持 False 防 SSRF）
    allow_private_urls: bool = False

    # ---------- 派生便捷属性 ----------

    @property
    def delivery_ttl_sec(self) -> float:
        return self.delivery_ttl_min * 60

    @property
    def delivery_max_bytes(self) -> int:
        return self.delivery_max_mb * 1024 * 1024

    def ensure_runs_dir(self) -> Path:
        """确保交付目录存在（服务启动时调用；worker 侧也会按需创建）。"""
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        return self.runs_dir

    # ---------- 构造 ----------

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量读取全部配置（环境变量名与 v0.1.4 完全一致）。"""
        transport = _env("PRINTGUARD_TRANSPORT", "stdio").lower()
        try:
            port = int(_env("PRINTGUARD_PORT") or _env("PORT") or "7860")
        except ValueError:
            port = 7860
        return cls(
            transport=transport,
            host=_env(
                "PRINTGUARD_HOST",
                "0.0.0.0" if transport != "stdio" else "127.0.0.1"),
            port=port,
            http_path=_env("PRINTGUARD_HTTP_PATH", "/mcp") or "/mcp",
            runs_dir=Path(_env("PRINTGUARD_RUNS_DIR", "output/runs")).resolve(),
            public_base_url=_env("PRINTGUARD_PUBLIC_BASE_URL").rstrip("/"),
            delivery_ttl_min=max(1, _int_env("PRINTGUARD_DELIVERY_TTL_MIN", 5)),
            delivery_max_mb=max(1, _int_env("PRINTGUARD_DELIVERY_MAX_MB", 256)),
            delivery_once=_bool_env("PRINTGUARD_DELIVERY_ONCE", False),
            run_retention_hours=_int_env("PRINTGUARD_RUN_RETENTION_HOURS", 168),
            llm_base_url=_env(
                "PRINTGUARD_LLM_BASE_URL",
                "https://api-inference.modelscope.cn/v1").rstrip("/"),
            llm_model=_env("PRINTGUARD_LLM_MODEL", "Qwen/Qwen3.5-27B"),
            llm_token=_env("PRINTGUARD_LLM_TOKEN") or _env("MODELSCOPE_API_TOKEN"),
            llm_timeout_sec=_int_env("PRINTGUARD_LLM_TIMEOUT_SEC", 120),
            llm_max_attempts=max(1, _int_env("PRINTGUARD_LLM_MAX_ATTEMPTS", 4)),
            llm_retry_backoff_sec=_int_env("PRINTGUARD_LLM_RETRY_BACKOFF_SEC", 8),
            llm_max_steps=max(1, _int_env("PRINTGUARD_LLM_MAX_STEPS", 8)),
            llm_job_timeout_sec=_int_env("PRINTGUARD_LLM_JOB_TIMEOUT_SEC", 1200),
            llm_history_turns=max(2, _int_env("PRINTGUARD_LLM_HISTORY_TURNS", 12)),
            log_level=_env("PRINTGUARD_LOG_LEVEL", "INFO").upper(),
            verbose=_env("PRINTGUARD_VERBOSE") == "1",
            allow_private_urls=_env("PRINTGUARD_ALLOW_PRIVATE_URLS") == "1",
        )
