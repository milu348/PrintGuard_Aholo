"""限时交付：交付件只在内存短暂驻留，到期自动清除，磁盘上不长期保存。

动机（创空间磁盘有限，且模型是用户的生成结果，不应由服务长期持有）：
- 任务成功后把交付件读入内存 → 删除 run 目录 → 返回 `/d/<token>` 链接
- 默认按「时效」管理（模型 5 分钟、可重复下载）：「一次性」模式会被浏览器
  预取 / 下载中断重试提前吃掉 token（线上实测过），到期自动从内存清除
- 需要严格「下载一次即失效」时可显式打开 once
- 内存有总量上限（默认 256MB），超限的件不缓存，只保留 run 目录内的本地路径

本地 CLI 形态不走这条路：产物直接落在 run 目录，用本地路径读取。

相对 v0.1.4 的重构：`DeliveryStore` 不再是模块级单例——实例由 service 层
按 Settings 构造并注入；`apply_delivery`（原 tools.apply_delivery）从
MCP 工具层归位到本模块，交付策略只有这一份实现。
"""
from __future__ import annotations

import logging
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("printguard.delivery")


@dataclass
class _Item:
    token: str
    filename: str
    data: bytes
    content_type: str
    expires_at: float
    hits: int = 0


@dataclass
class DeliveryStore:
    """线程安全的限时交付存储；`take` 取件，过期返回 None。

    once=True 时取件即销毁（严格一次性）；默认 False，只认 TTL——
    因为浏览器预取、用户重复点击、下载中断重试都会产生多次 GET。
    """

    ttl_sec: float = 300.0
    max_bytes: int = 256 << 20
    once: bool = False

    def __post_init__(self):
        self._items: dict[str, _Item] = {}
        self._bytes = 0
        self._lock = threading.Lock()

    # ---------- 对外 ----------

    def put(self, data: bytes, filename: str,
            content_type: str = "application/octet-stream",
            ttl_sec: float | None = None) -> tuple[str, bool]:
        """缓存一份交付件。返回 (token, cached)；超限时 cached=False。"""
        size = len(data)
        with self._lock:
            if size > self.max_bytes:
                return "", False
            while self._bytes + size > self.max_bytes and self._items:
                self._drop_oldest_locked()
            ttl = self.ttl_sec if ttl_sec is None else float(ttl_sec)
            token = secrets.token_urlsafe(16)
            self._items[token] = _Item(
                token=token, filename=filename, data=data,
                content_type=content_type,
                expires_at=time.time() + max(1.0, ttl))
            self._bytes += size
            return token, True

    def take(self, token: str) -> tuple[str, bytes, str] | None:
        """取件；过期返回 None（once 模式下同时移除）。"""
        with self._lock:
            item = self._items.get(token)
            if item is None:
                return None
            if time.time() > item.expires_at:
                self._remove_locked(token)
                return None
            item.hits += 1
            if self.once:
                self._remove_locked(token)
            return item.filename, item.data, item.content_type

    def stats(self) -> dict:
        with self._lock:
            return {"items": len(self._items),
                    "bytes": self._bytes,
                    "ttl_sec": int(self.ttl_sec),
                    "once": self.once}

    def purge_expired(self) -> int:
        now = time.time()
        with self._lock:
            expired = [t for t, item in self._items.items()
                       if now > item.expires_at]
            for token in expired:
                self._remove_locked(token)
            return len(expired)

    # ---------- 内部 ----------

    def _drop_oldest_locked(self) -> None:
        oldest = min(self._items.values(), key=lambda item: item.expires_at)
        self._remove_locked(oldest.token)

    def _remove_locked(self, token: str) -> None:
        item = self._items.pop(token, None)
        if item is not None:
            self._bytes -= len(item.data)


def content_type_for(path: Path) -> str:
    """交付件 MIME：让浏览器按文件下载处理。"""
    suffix = Path(path).suffix.lower()
    if suffix == ".stl":
        return "model/stl"
    if suffix == ".3mf":
        return "model/3mf"
    return "application/octet-stream"


def make_download(store: DeliveryStore, key: str, data: bytes, filename: str, *,
                  content_type: str = "application/octet-stream",
                  ttl_sec: float, base_url: str = "") -> dict:
    """把一份字节数据放进限时存储，返回给前端的 downloads 条目。

    超限时返回空 dict（调用方记 warning）。
    """
    token, cached = store.put(data, filename, content_type, ttl_sec)
    if not cached:
        return {}
    return {"url": f"{base_url.rstrip('/')}/d/{token}",
            "filename": filename, "size_bytes": len(data),
            "expires_sec": int(ttl_sec)}


def capture_artifacts(artifacts: dict, run_dir: Path | None, *, store: DeliveryStore,
                      ttl_sec: float, base_url: str = "") -> tuple[dict, list[str]]:
    """把 run 目录内的交付件搬进限时存储，并删掉整个 run 目录。

    返回 (downloads, warnings)。缓存超限或不读的文件不删，仍可用本地路径。
    """
    downloads: dict[str, dict] = {}
    warnings: list[str] = []
    prefix = (base_url or "").rstrip("/")

    for key, value in sorted((artifacts or {}).items()):
        if key == "model_format" or not isinstance(value, str) or not value:
            continue
        path = Path(value)
        try:
            if not path.is_file():
                continue
            data = path.read_bytes()
        except OSError as exc:
            warnings.append(f"交付件 {key} 读取失败：{exc}")
            continue
        entry = make_download(store, key, data, path.name,
                              content_type=content_type_for(path),
                              ttl_sec=float(ttl_sec), base_url=prefix)
        if not entry:
            warnings.append(
                f"交付件 {key} 超过交付内存上限（{store.max_bytes >> 20}MB），"
                "未缓存，仅保留 run 目录内文件")
            continue
        downloads[key] = entry
        logger.info("delivery %s → %s（%d B，有效期 %d min，%s）",
                    key, entry["url"].rsplit("/", 1)[-1], len(data),
                    round(ttl_sec / 60),
                    "一次性" if store.once else "可重复下载")

    # 内存里已有副本：磁盘上的 run 目录整体删除（不留生成结果）
    if downloads and run_dir is not None:
        try:
            if Path(run_dir).is_dir():
                shutil.rmtree(run_dir)
                logger.info("run 目录已删除（交付件转入限时存储）：%s", run_dir)
        except OSError as exc:
            warnings.append(f"run 目录删除失败：{exc}")
    return downloads, warnings


def apply_delivery(summary: dict, *, store: DeliveryStore, ttl_sec: float,
                   base_url: str = "", runs_dir: str | Path = "output/runs") -> dict:
    """任务成功后的交付收口：run 目录产物 → 限时下载链接。

    托管形态（MCP / 网页）：模型成功后磁盘即删（创空间不留生成结果）；
    超限或不读时不删 run 目录，退化为本地路径交付。
    本地 CLI 不经过这里，产物直接落在 run 目录。
    """
    artifacts = summary.get("artifacts") or {}
    run_dir = None
    model = artifacts.get("model")
    if isinstance(model, str) and model:
        run_dir = Path(model).resolve().parent
    elif summary.get("run_id"):
        run_dir = Path(runs_dir) / str(summary["run_id"])

    downloads, warnings = capture_artifacts(
        artifacts, run_dir, store=store, ttl_sec=ttl_sec, base_url=base_url)
    if downloads:
        summary["downloads"] = downloads
        # 磁盘文件已删除：只回格式信息，不把失效路径传回调用方
        summary["artifacts"] = {k: v for k, v in artifacts.items()
                                if k == "model_format"}
    summary["warnings"] = list(summary.get("warnings") or []) + warnings
    return summary


def cleanup_run_dirs(runs_dir: Path, retention_hours: float) -> int:
    """启动时清理过期 run 目录（托管形态防止上次残留越积越多）。"""
    if retention_hours <= 0 or not Path(runs_dir).is_dir():
        return 0
    deadline = time.time() - retention_hours * 3600
    removed = 0
    for entry in Path(runs_dir).iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime < deadline:
                shutil.rmtree(entry)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("启动清理：删除 %d 个超过 %.1f 小时的 run 目录", removed,
                    retention_hours)
    return removed
