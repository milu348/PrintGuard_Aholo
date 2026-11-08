"""异步任务管理器：内存态 + 单工作线程，服务内唯一的队列实现。

设计意图：
- Lux3D 平台生成类任务并发 = 1 → 单 worker 天然串行；客户端秒回 job_id，
  再轮询状态，避免长任务把 MCP 调用 / HTTP 请求拖死
- MCP 工具与网页 Agent 回合各持一个实例（`app.MCP_JOBS` / `app.AGENT_JOBS`），
  两条流水线互不阻塞，但各自串行
- 状态机：queued → running → succeeded / failed；仅终态任务参与清理
- `timeout_sec`：单任务执行上限，超时判 failed（不让一个卡死任务占住 worker）
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

    def __init__(self, *, retention_sec: float = 3600.0, max_jobs: int = 500,
                 timeout_sec: float = 0.0):
        self._jobs: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._retention_sec = float(retention_sec)
        self._max_jobs = max(1, int(max_jobs))
        # <=0 表示不设执行超时（MCP 工具自己管平台侧等待）
        self._timeout_sec = float(timeout_sec)
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
        """查询任务快照（返回副本，避免外部改动内部状态）。

        等待终态由调用方自旋完成（服务端轮询 printguard_check_job、
        测试里各自 drain），队列本身不提供阻塞 API。
        """
        with self._lock:
            record = self._jobs.get(job_id)
            return dict(record) if record is not None else None

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
                if self._timeout_sec > 0:
                    # 执行超时看门狗：超时只改状态，不杀线程（Python 无法安全杀线程）
                    timer = threading.Timer(
                        self._timeout_sec,
                        lambda jid=job_id: self._update(
                            jid, status="failed",
                            error=f"TimeoutError: 任务执行超过 "
                                  f"{self._timeout_sec:.0f}s"))
                    timer.daemon = True
                    timer.start()
                result = func()
                elapsed = round(time.monotonic() - started, 1)
                # 看门狗可能已把任务判为超时失败：终态只写一次，
                # 否则慢任务跑完会把「failed(Timeout)」覆盖成 succeeded（复活）
                if self._update_if_running(job_id, status="succeeded",
                                           result=result, elapsed_sec=elapsed):
                    logger.info("job %s succeeded in %.1fs", job_id, elapsed)
                else:
                    logger.warning("job %s 已完成但已判超时，结果丢弃", job_id)
            except Exception as exc:  # noqa: BLE001 —— 任务边界：任何异常都要落到任务状态
                elapsed = round(time.monotonic() - started, 1)
                logger.exception("job %s failed", job_id)
                self._update(job_id, status="failed",
                             error=f"{type(exc).__name__}: {exc}",
                             elapsed_sec=elapsed)
            finally:
                if self._timeout_sec > 0:
                    timer.cancel()
                self._queue.task_done()

    def _update(self, job_id: str, **fields) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is not None:
                record.update(fields, updated=time.time())

    def _update_if_running(self, job_id: str, **fields) -> bool:
        """只在任务仍处于 running 时落终态（防看门狗/失败态被覆盖）。"""
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record["status"] != "running":
                return False
            record.update(fields, updated=time.time())
            return True

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
