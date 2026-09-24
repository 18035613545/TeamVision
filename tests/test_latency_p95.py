# -*- coding: utf-8 -*-
"""E4（实现方案 §11.4）：观看端 30s 延迟 P50/P95 统计的回归。

零协议改动的断言面：
1) 最近秩百分位计算正确（20 个 10..29ms 样本 → p50=19ms、p95=28ms）；
2) 窗口只收 30s 内样本（100s 前的样本被剔除）；
3) 样本 <10 记 0（暂无），避免个别抖动霸榜；
4) 5 秒缓存：窗口内新样本不会立刻改变读数，5 秒后重算生效；
5) deque 封顶（长跑内存不漂移）——T-16（2026-09-24）：上限必须引用生产常量
   `viewer._LATENCY_RING_MAX`，并用**生产入口** `_store_frame` 灌满验证；
   旧用例自己 `deque(maxlen=900)` 再 append，把生产上限改成 90000 仍会绿（假绿）。
"""
import threading
import time
import unittest
from collections import deque

import viewer


def _mk_channel(samples, age_all_s=0.0):
    """不触网构造一个 Channel 实例，只填 _store_frame 之后的统计字段。"""
    ch = viewer.Channel.__new__(viewer.Channel)
    ch.lock = threading.Lock()
    now = time.monotonic() - age_all_s
    ch._latency_ring = deque(((now, us) for us in samples),
                             maxlen=viewer._LATENCY_RING_MAX)
    ch._p_stats_cache = None
    return ch


class TestLatencyPercentiles(unittest.TestCase):

    def test_nearest_rank(self):
        ch = _mk_channel([i * 1000 for i in range(10, 30)])  # 10..29 ms
        p50, p95, n = ch.latency_percentiles()
        # 最近秩：index = ceil(p*n/100)-1 → p50=第10个(19ms)、p95=第19个(28ms)
        self.assertEqual((p50, p95, n), (19.0, 28.0, 20))

    def test_window_prunes_old_samples(self):
        ch = _mk_channel([i * 1000 for i in range(10, 30)], age_all_s=100.0)
        self.assertEqual(ch.latency_percentiles(), (0.0, 0.0, 0))

    def test_insufficient_samples_reads_zero(self):
        ch = _mk_channel([5000] * 9)
        p50, p95, n = ch.latency_percentiles()
        self.assertEqual((p50, p95), (0.0, 0.0))
        self.assertEqual(n, 9)

    def test_five_second_cache(self):
        ch = _mk_channel([i * 1000 for i in range(10, 30)])
        first = ch.latency_percentiles()
        ch._latency_ring.append((time.monotonic(), 999_000))  # 999ms 突发
        self.assertEqual(ch.latency_percentiles(), first)     # 5s 内读数不变
        ch._p_stats_cache = (ch._p_stats_cache[0] - 5.1, *ch._p_stats_cache[1:])
        p50, p95, n = ch.latency_percentiles()               # 越过 5s 立即重算
        self.assertEqual(n, 21)
        # 21 样本 [10..29,999]：p50=第11个(20ms)、p95=第20个(29ms)；单个离群值不霸榜
        self.assertEqual((p50, p95), (20.0, 29.0))

    def test_ring_capped_by_production_limit(self):
        # 生产入口（_store_frame）灌满：上限改大/改小都会让本用例失败
        ch = viewer.Channel.__new__(viewer.Channel)
        ch.lock = threading.Lock()
        ch._fps_times = []
        ch._latency_samples = []
        ch._latency_ring = deque(maxlen=viewer._LATENCY_RING_MAX)
        ch._p_stats_cache = None
        ch.frame_serial = 0
        ch.last_frame = None
        ch.fps = 0.0
        ch.latency_ms = 0.0
        for _ in range(viewer._LATENCY_RING_MAX + 200):
            ch._store_frame(object(), time.time_ns() // 1000 - 40_000)
        self.assertEqual(len(ch._latency_ring), viewer._LATENCY_RING_MAX)


class TestStoreFrameFeedsRing(unittest.TestCase):
    """锁死 _store_frame 的两个入口条件：合法样本入环、时钟偏差样本不入环。"""

    def _ch(self):
        ch = viewer.Channel.__new__(viewer.Channel)
        ch.lock = threading.Lock()
        ch._fps_times = []
        ch._latency_samples = []
        ch._latency_ring = deque(maxlen=viewer._LATENCY_RING_MAX)
        ch._p_stats_cache = None
        ch.frame_serial = 0
        ch.last_frame = None
        ch.fps = 0.0
        ch.latency_ms = 0.0
        return ch

    def test_valid_frame_enters_ring(self):
        ch = self._ch()
        ch._store_frame(object(), time.time_ns() // 1000 - 40_000)  # 40ms 前采集
        self.assertEqual(len(ch._latency_ring), 1)

    def test_clock_skew_frame_excluded(self):
        ch = self._ch()
        ch._store_frame(object(), time.time_ns() // 1000 + 10 ** 9)  # 未来 1000s
        self.assertEqual(len(ch._latency_ring), 0)


if __name__ == "__main__":
    unittest.main()
