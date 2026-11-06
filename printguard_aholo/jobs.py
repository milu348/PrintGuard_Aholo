"""异步任务管理器（骨架版：内存态 + 单工作线程）。

设计意图：
- Lux3D 平台生成类任务并发 = 1 → 单工作线程天然串行，客户端秒回 job_id，
  再由 MCP 工具轮询状态，避免 Agent 工具调用因长任务超时
- 后续「生成 → 体检 → 修复 → 交付」管线复用同一个 worker，保持串行语义
- 状态机：queued → running → succeeded / failed；仅终态任务参与清理
- 任务只存内存：容器重启即丢（客户端重试即可），持久化留到交付层实现
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable

logger = logging.getLogger("printguard.jobs")

TERMINAL = ("succeeded", "failed")


class JobManager:
    """单 worker 异步任务队列。线程安全；`submit` 立即返回 job_id。"""

    def __init__(self, *, retention_sec: float = 3600.0, max_jobs: int = 500):
        self._jobs: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._retention_sec = float(retention_sec)
        self._max_jobs = max(1, int(max_jobs))
        self._worker = threading.Thread(target=self._loop, name="pg-job-worker",
                                        daemon=True)
        self._worker.start()

    # ---------- 对外 ----------

    def submit(self, kind: str, func: Callable[[], Any], *,
               meta: dict | None = None) -> str:
        """入队并立即返回 job_id。func 在 worker 线程串行执行，返回值必须可 JSON 序列化。"""
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        record = {
            "job_id": job_id,
            "kind": kind,
            "status": "queued",
            "created": now,
            "updated": now,
            "meta": meta or {},
            "result": None,
            "error": None,
            "elapsed_sec": None,
        }
        with self._lock:
            self._jobs[job_id] = record
            self._prune_locked(now)
        self._queue.put((job_id, func))
        logger.info("job %s queued: %s", job_id, kind)
        return job_id

    def get(self, job_id: str) -> dict | None:
        """查询任务快照（返回副本，避免外部改动内部状态）。"""
        with self._lock:
            record = self._jobs.get(job_id)
            return dict(record) if record is not None else None

    def wait(self, job_id: str, timeout: float = 30.0) -> dict | None:
        """同步等待至终态（测试/内部使用），超时返回当前快照。"""
        deadline = time.monotonic() + timeout
        while True:
            record = self.get(job_id)
            if record is None or record["status"] in TERMINAL:
                return record
            if time.monotonic() >= deadline:
                return record
            time.sleep(0.02)

    def stats(self) -> dict:
        with self._lock:
            counts: dict[str, int] = {}
            for record in self._jobs.values():
                counts[record["status"]] = counts.get(record["status"], 0) + 1
            return {"total": len(self._jobs), "by_status": counts}

    # ---------- worker ----------

    def _loop(self) -> None:
        while True:
            job_id, func = self._queue.get()
            started = time.monotonic()
            self._update(job_id, status="running")
            try:
                result = func()
                elapsed = round(time.monotonic() - started, 1)
                self._update(job_id, status="succeeded", result=result,
                             elapsed_sec=elapsed)
                logger.info("job %s succeeded in %.1fs", job_id, elapsed)
            except Exception as exc:  # noqa: BLE001 —— 任务边界：任何异常都要落到任务状态
                elapsed = round(time.monotonic() - started, 1)
                logger.exception("job %s failed", job_id)
                self._update(job_id, status="failed",
                             error=f"{type(exc).__name__}: {exc}",
                             elapsed_sec=elapsed)
            finally:
                self._queue.task_done()

    def _update(self, job_id: str, **fields) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is not None:
                record.update(fields, updated=time.time())

    def _prune_locked(self, now: float) -> None:
        """清理：先按保留期删终态任务，再按容量淘汰最旧的终态任务。运行中的永不清理。"""
        for job_id in [jid for jid, rec in self._jobs.items()
                       if rec["status"] in TERMINAL
                       and now - rec["updated"] > self._retention_sec]:
            del self._jobs[job_id]
        if len(self._jobs) <= self._max_jobs:
            return
        for job_id in [jid for jid, rec in self._jobs.items()
                       if rec["status"] in TERMINAL]:
            if len(self._jobs) <= self._max_jobs:
                break
            del self._jobs[job_id]
