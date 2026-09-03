# -*- coding: utf-8 -*-
"""第 60 条回归：退出/停止路径不得串行冻结主线程 2N 秒。

机制：share.stop() = signal_stop()（置标志，非阻塞）+ join(2.0)。旧退出路径对每个频道
顺序调用 stop()，N 个共享频道各 join 2 秒 → 最多冻结 2N 秒（「关不掉」）。修复后退出
先对所有频道 request_stop（并发发信号），再逐个 stop（join 此时基本即刻返回）→ 约 2 秒。

这些测试 hermetic：用 __new__ 构造 ScreenShareSession / Channel，绕开真实采集/编码/Tk，
只验证 signal_stop / request_stop 的非阻塞契约与两阶段停止的并发收益。
"""
import threading
import time
import unittest

import share as share_mod
import viewer


def _session():
    """构造一个只含停止机制的 ScreenShareSession（不碰真实 cap/encoder）。"""
    s = share_mod.ScreenShareSession.__new__(share_mod.ScreenShareSession)
    s._stop = threading.Event()
    s._thread = None
    s._close_resources = lambda: None
    return s


def _winding_thread(stop_evt, wind_s):
    """启动一个线程：收到 stop 信号后还需 wind_s 秒才退出（模拟 sendall/grab 收尾）。"""
    def worker():
        while not stop_evt.is_set():
            time.sleep(0.005)
        time.sleep(wind_s)
    th = threading.Thread(target=worker, daemon=True)
    th.start()
    return th


class TestSignalStopNonBlocking(unittest.TestCase):
    def test_signal_stop_sets_flag_without_joining(self):
        s = _session()
        th = _winding_thread(s._stop, wind_s=0.4)
        s._thread = th
        t0 = time.monotonic()
        s.signal_stop()
        elapsed = time.monotonic() - t0
        self.assertTrue(s._stop.is_set())       # 标志已置
        self.assertLess(elapsed, 0.1)           # 非阻塞：没有 join
        self.assertTrue(th.is_alive())          # 线程仍在收尾，signal_stop 不等它
        th.join(1.0)

    def test_stop_still_joins_and_closes(self):
        s = _session()
        th = _winding_thread(s._stop, wind_s=0.05)
        s._thread = th
        closed = []
        s._close_resources = lambda: closed.append(True)
        s.stop()
        self.assertFalse(th.is_alive())         # stop 仍 join 到线程退出
        self.assertEqual(closed, [True])        # 且释放了资源
        self.assertIsNone(s._thread)


class TestTwoPhaseStopIsConcurrent(unittest.TestCase):
    def test_signal_all_then_join_all_beats_sequential(self):
        WIND = 0.3
        N = 4

        def make():
            s = _session()
            s._thread = _winding_thread(s._stop, wind_s=WIND)
            return s

        # 两阶段：先并发 signal_stop（各线程同时开始收尾），再逐个 join。
        ss = [make() for _ in range(N)]
        t0 = time.monotonic()
        for s in ss:
            s.signal_stop()
        for s in ss:
            th = s._thread
            s._thread = None
            if th is not None:
                th.join(2.0)
            s._close_resources()
        two_phase = time.monotonic() - t0

        # 串行 stop：每个 signal+join，收尾互不重叠 → 约 N*WIND。
        ss2 = [make() for _ in range(N)]
        t0 = time.monotonic()
        for s in ss2:
            s.stop()
        sequential = time.monotonic() - t0

        # 两阶段收尾并发，应接近单个 WIND；串行应接近 N*WIND。4x 理论差距，留足余量。
        self.assertLess(two_phase, WIND + 0.4)
        self.assertGreater(sequential, WIND * N * 0.5)
        self.assertLess(two_phase, sequential)


class TestChannelRequestStop(unittest.TestCase):
    def _channel(self):
        ch = viewer.Channel.__new__(viewer.Channel)
        ch._running = True
        ch._share_lock = threading.Lock()
        ch._share = None
        return ch

    def test_request_stop_signals_without_stopping(self):
        ch = self._channel()

        class FakeShare:
            def __init__(self):
                self.signaled = False
                self.stopped = False

            def signal_stop(self):
                self.signaled = True

            def stop(self):
                self.stopped = True

        fs = FakeShare()
        ch._share = fs
        t0 = time.monotonic()
        ch.request_stop()
        elapsed = time.monotonic() - t0
        self.assertFalse(ch._running)        # 接收线程标志已置
        self.assertTrue(fs.signaled)         # 共享会话收到信号
        self.assertFalse(fs.stopped)         # request_stop 不 join/不 stop
        self.assertLess(elapsed, 0.05)       # 非阻塞

    def test_request_stop_without_share_is_noop(self):
        ch = self._channel()
        ch._share = None
        ch.request_stop()                    # 无共享会话也不应抛
        self.assertFalse(ch._running)


if __name__ == "__main__":
    unittest.main()
