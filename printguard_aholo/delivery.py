"""限时交付：交付件只在内存短暂驻留，到期自动清除，磁盘上不长期保存。

动机（创空间磁盘有限，且模型是用户的生成结果，不应由服务长期持有）：
- 任务成功后把交付件读入内存 → 删除 run 目录 → 返回 `/d/<token>` 链接
- 默认按「时效」而非「一次性」管理：浏览器加载预览图本身就会消耗一次请求，
  一次性 token 会被预览抢用掉（线上实测过），所以模型默认 5 分钟、图片默认
  60 分钟内可重复下载，到期自动从内存清除
- 需要严格「下载一次即失效」时可显式打开 once（PRINTGUARD_DELIVERY_ONCE=1）
- 内存有总量上限（默认 256MB），超限的件不缓存，只保留 run 目录内的本地路径

文件模式（stdio / 本地 CLI）：保持原 `/files/<run_id>/<filename>` 行为，
产物留在 run 目录，由使用方自行管理。
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

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


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
    因为浏览器预览、用户重复点击、下载中断重试都会产生多次 GET。
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


# 模块级单例：服务内所有任务共享这一个限时存储
STORE = DeliveryStore()


def is_image(filename: str | Path) -> bool:
    return Path(filename).suffix.lower() in IMAGE_SUFFIXES


def content_type_for(path: Path) -> str:
    """交付件 MIME：3MF/STL 让浏览器当下载处理，图片可直接预览。"""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image/jpeg" if suffix == ".jpg" else f"image/{suffix.lstrip('.')}"
    if suffix == ".stl":
        return "model/stl"
    if suffix == ".3mf":
        return "model/3mf"
    return "application/octet-stream"


def capture_artifacts(artifacts: dict, run_dir: Path, *,
                      ttl_sec: float, max_bytes: int, once: bool = False,
                      base_url: str = "", image_ttl_sec: float | None = None,
                      ) -> tuple[dict, list[str]]:
    """把 run 目录内的交付件搬进限时存储，并删掉整个 run 目录。

    ttl_sec：模型等非图片件的时效；image_ttl_sec：图片时效（预览要能重复加载）。
    返回 (downloads, warnings)。缓存超限或不读的文件不删，仍可用本地路径。
    """
    downloads: dict[str, dict] = {}
    warnings: list[str] = []
    prefix = (base_url or "").rstrip("/")
    STORE.ttl_sec = float(ttl_sec)
    STORE.max_bytes = int(max_bytes)
    STORE.once = bool(once)

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
        ttl = float(ttl_sec)
        if image_ttl_sec is not None and is_image(path.name):
            ttl = float(image_ttl_sec)
        token, cached = STORE.put(data, path.name, content_type_for(path), ttl)
        if not cached:
            warnings.append(
                f"交付件 {key} 超过交付内存上限（{max_bytes >> 20}MB），"
                "未缓存，仅保留 run 目录内文件")
            continue
        downloads[key] = {
            "url": f"{prefix}/d/{token}",
            "filename": path.name,
            "size_bytes": len(data),
            "expires_sec": int(ttl),
        }
        logger.info("delivery %s → %s（%d B，有效期 %d min%s）",
                    key, token[:6], len(data), round(ttl / 60),
                    "，一次性" if once else "")

    # 内存里已有副本：磁盘上的 run 目录整体删除（不留生成结果）
    if downloads:
        try:
            if run_dir.is_dir():
                shutil.rmtree(run_dir)
                logger.info("run 目录已删除（交付件转入限时存储）：%s", run_dir)
        except OSError as exc:
            warnings.append(f"run 目录删除失败：{exc}")
    return downloads, warnings


def cleanup_run_dirs(runs_dir: Path, retention_hours: float) -> int:
    """启动时清理过期 run 目录（托管形态防止上次残留越积越多）。"""
    if retention_hours <= 0 or not runs_dir.is_dir():
        return 0
    deadline = time.time() - retention_hours * 3600
    removed = 0
    for entry in runs_dir.iterdir():
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
