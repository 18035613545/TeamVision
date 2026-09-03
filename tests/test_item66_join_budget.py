# -*- coding: utf-8 -*-
"""第 66 条回归：run_server 收尾的 join 预算不得超过 shutdown 给 _server_thread 的预算。

旧路径：run_server 的 finally 对 capture/encode/send/perf 4 个线程各 `join(timeout=2)`
串行等待，理论最坏 8 秒；而 `shutdown()` 只给 `_server_thread.join(timeout=3)`。当 worker
收尾慢于 3 秒时，shutdown 的 join 超时返回，run_server 仍卡在 finally，_encode_loop 尾部的
`encoder.close()` 来不及执行就退出。

修复：finally 改用共享 deadline（`time.monotonic()+2.0`）依次 join accept+4 worker——
它们已被 stop_event 并发置位、同时收尾，共享 deadline 把收尾总耗时压到约 2 秒（而非串行的
N×timeout），落在 shutdown 的 3 秒预算内。

hermetic：复刻 finally 的两种 join 策略（共享 deadline vs 串行 timeout），用「挂起线程」
（忽略 stop、需 release 才退）确定性地证明共享 deadline 的总耗时被 deadline 界定、不随线程数
线性增长；用「协作线程」（stop 后 wind_s 退）证明线程配合时共享 deadline 不会空等满预算。
不绑定真实 socket / 不起真实 run_server。
"""
import threading
import time
import unittest


def _join_shared_deadline(threads, budget):
    """复刻修复后的 finally：共享 deadline 依次 join，超时即跳过其余。"""
    deadline = time.monotonic() + budget
    for t in threads:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            t.join(timeout=remaining)


def _join_serial(threads, timeout):
    """复刻旧 finally：每个线程各自 join(timeout)，串行累加。"""
    for t in threads:
        t.join(timeout=timeout)


class _HangThread(threading.Thread):
    """忽略 stop_event、挂起直到 release 被置位的线程（模拟收尾卡死的 worker）。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.release = threading.Event()

    def run(self):
        self.release.wait(30)


class _CoopThread(threading.Thread):
    """stop_event 置位后再 wind_s 秒退出的线程（模拟正常收尾的 worker）。"""

    def __init__(self, stop_event, wind_s):
        super().__init__(daemon=True)
        self._stop_evt = stop_event
        self._wind_s = wind_s

    def run(self):
        self._stop_evt.wait(30)
        time.sleep(self._wind_s)


class TestSharedDeadlineBoundsHang(unittest.TestCase):
    def test_shared_deadline_does_not_grow_with_thread_count(self):
        # 4 个挂起线程：共享 deadline=0.4 应把总耗时界定在约 0.4s，而非串行的 4×0.4=1.6s。
        hangs = [_HangThread() for _ in range(4)]
        for t in hangs:
            t.start()
            self.addCleanup(t.release.set)   # 断言后放行，避免残留
        try:
            t0 = time.monotonic()
            _join_shared_deadline(hangs, budget=0.4)
            shared = time.monotonic() - t0

            t0 = time.monotonic()
            _join_serial(hangs, timeout=0.4)
            serial = time.monotonic() - t0
        finally:
            for t in hangs:
                t.release.set()

        self.assertLess(shared, 0.4 + 0.35)   # 被 deadline 界定（含调度余量）
        self.assertGreater(serial, 0.4 * 4 * 0.5)  # 串行显著更久（>0.8s）
        self.assertLess(shared, serial)        # 共享 deadline 严格更快

    def test_shared_deadline_skips_remaining_after_budget(self):
        # 预算耗尽后不再 join 其余线程：第一个挂起线程吃满 budget，其余被跳过。
        hangs = [_HangThread() for _ in range(3)]
        for t in hangs:
            t.start()
            self.addCleanup(t.release.set)
        try:
            t0 = time.monotonic()
            _join_shared_deadline(hangs, budget=0.3)
            elapsed = time.monotonic() - t0
        finally:
            for t in hangs:
                t.release.set()
        # 若未跳过其余，3×0.3=0.9s；跳过则约 0.3s。
        self.assertLess(elapsed, 0.3 + 0.35)


class TestSharedDeadlineCooperative(unittest.TestCase):
    def test_cooperative_threads_finish_well_within_budget(self):
        # 线程配合（wind 0.05s）时，共享 deadline 收齐全部线程且远未空等满 2s 预算。
        stop = threading.Event()
        coops = [_CoopThread(stop, 0.05) for _ in range(5)]
        for t in coops:
            t.start()
        stop.set()                            # 并发置位，模拟 shutdown 的 stop_event.set()
        t0 = time.monotonic()
        _join_shared_deadline(coops, budget=2.0)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)         # 收齐即返回，不空等满预算
        for t in coops:
            self.assertFalse(t.is_alive())    # 全部已收尾（encoder.close() 得以执行）

    def test_cooperative_within_shutdown_budget(self):
        # 端到端预算校验：收尾约 0.1s < shutdown 的 3s 预算（旧串行最坏 8s 会超）。
        stop = threading.Event()
        coops = [_CoopThread(stop, 0.1) for _ in range(5)]
        for t in coops:
            t.start()
        stop.set()
        t0 = time.monotonic()
        _join_shared_deadline(coops, budget=2.0)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 3.0)         # 落在 shutdown 的 join 预算内


if __name__ == "__main__":
    unittest.main()
