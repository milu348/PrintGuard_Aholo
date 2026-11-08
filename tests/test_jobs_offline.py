"""JobManager 离线单测：状态机 / 异常捕获 / 串行语义 / 清理策略。"""
import threading
import time
import unittest

from printguard_aholo.service.jobs import JobManager


def _drain(manager, job_id, timeout=5.0):
    """自旋等待终态（替代已删除的 manager.wait）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = manager.get(job_id)
        if record and record["status"] in ("succeeded", "failed"):
            return record
        time.sleep(0.02)
    return manager.get(job_id)


class JobManagerTests(unittest.TestCase):
    def test_submit_and_succeed(self):
        manager = JobManager()
        job_id = manager.submit("demo", lambda: {"answer": 42})
        record = _drain(manager, job_id)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["result"], {"answer": 42})
        self.assertIsNotNone(record["elapsed_sec"])

    def test_failure_captured(self):
        manager = JobManager()

        def boom():
            raise ValueError("炸了")

        job_id = manager.submit("demo", boom)
        record = _drain(manager, job_id)
        self.assertEqual(record["status"], "failed")
        self.assertIn("ValueError", record["error"])
        self.assertIn("炸了", record["error"])

    def test_serial_execution_order(self):
        manager = JobManager()
        order = []

        def make_job(n):
            def run():
                order.append(f"start{n}")
                time.sleep(0.03)
                order.append(f"end{n}")
                return n
            return run

        job_ids = [manager.submit("demo", make_job(i)) for i in range(3)]
        for job_id in job_ids:
            self.assertEqual(_drain(manager, job_id)["status"], "succeeded")
        self.assertEqual(order, ["start0", "end0", "start1", "end1",
                                 "start2", "end2"])

    def test_unknown_job(self):
        self.assertIsNone(JobManager().get("nope"))

    def test_prune_only_terminal_jobs(self):
        manager = JobManager(max_jobs=2)
        job_ids = [manager.submit("demo", lambda: 1) for _ in range(3)]
        for job_id in job_ids:
            _drain(manager, job_id)
        manager.submit("demo", lambda: 1)  # 触发容量清理（新任务处于 queued）
        self.assertIsNone(manager.get(job_ids[0]))   # 最旧的终态任务被清理
        self.assertIsNone(manager.get(job_ids[1]))
        self.assertIsNotNone(manager.get(job_ids[2]))

    def test_running_job_never_pruned(self):
        manager = JobManager(max_jobs=1)
        started = threading.Event()

        def slow():
            started.set()
            time.sleep(0.2)
            return 1

        first = manager.submit("slow", slow)
        self.assertTrue(started.wait(2))
        manager.submit("queued", lambda: 2)
        self.assertIsNotNone(manager.get(first))  # 运行中的任务不参与清理


if __name__ == "__main__":
    unittest.main()
