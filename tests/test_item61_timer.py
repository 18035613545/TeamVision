# -*- coding: utf-8 -*-
"""第 61 条回归：退出后兜底定时器不得新開上传会话。

旧路径：`threading.Timer(2.0, _share_sync_after_connect)` 是非守护线程，stop() 既不 cancel
它也不清 _active_sock。共享中重连（定时器已 armed）后 2 秒内退出 → 定时器在 teardown 之后
触发，guard `_active_sock is not sock` 因 _active_sock 未清而通过 → 调 _start_share 新開采集
上传并向正在关闭的 socket 发送；非守护定时器还让解释器多等 2 秒。

修复三层：(1) 定时器 daemon=True + 登记句柄；(2) stop() cancel 定时器并清 _active_sock；
(3) _share_sync_after_connect 增加 `not self._running` guard（request_stop/stop 都置 False）。

hermetic：用 __new__ 构造 Channel，绕开真实接收线程/采集/Tk。
"""
import threading
import unittest

import viewer


class FakeTimer:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def _channel():
    ch = viewer.Channel.__new__(viewer.Channel)
    ch.name = "test"
    ch._running = True
    ch._active_sock = None
    ch._share_lock = threading.RLock()
    ch._share = None
    ch.share_enabled = False
    ch.multiview = False
    ch._share_sync_timer = None
    return ch


class TestStopCancelsTimer(unittest.TestCase):
    def test_stop_cancels_timer_and_clears_sock(self):
        ch = _channel()
        sock = object()
        ch._active_sock = sock
        timer = FakeTimer()
        ch._share_sync_timer = timer
        ch.stop()
        self.assertFalse(ch._running)
        self.assertTrue(timer.cancelled)            # 定时器被 cancel
        self.assertIsNone(ch._share_sync_timer)     # 句柄清空
        self.assertIsNone(ch._active_sock)          # _active_sock 清空

    def test_stop_without_timer_is_safe(self):
        ch = _channel()
        ch._share_sync_timer = None
        ch.stop()                                   # 无定时器也不应抛
        self.assertFalse(ch._running)
        self.assertIsNone(ch._active_sock)


class TestSyncCallbackNeuteredAfterStop(unittest.TestCase):
    def _wire_start_share(self, ch):
        calls = []
        ch._start_share = lambda: (calls.append(1), (True, ""))[1]
        ch._notify_share_error = lambda text: None
        return calls

    def test_callback_returns_early_when_not_running(self):
        # 核心竞态：_running=False 但 _active_sock 仍等于 sock（stop 尚未清或清了之前触发）。
        ch = _channel()
        sock = object()
        ch._active_sock = sock
        ch.share_enabled = True
        ch.multiview = True
        calls = self._wire_start_share(ch)
        ch._running = False                         # 模拟 stop()/request_stop() 已置位
        ch._share_sync_after_connect(sock)
        self.assertEqual(calls, [])                 # 绝不调用 _start_share

    def test_callback_runs_when_running_and_sock_matches(self):
        # 正控：连接仍活、_running=True、_active_sock==sock 时兜底逻辑照常工作。
        ch = _channel()
        sock = object()
        ch._active_sock = sock
        ch.share_enabled = True
        ch.multiview = True
        ch._running = True
        calls = self._wire_start_share(ch)
        ch._share_sync_after_connect(sock)
        self.assertEqual(calls, [1])                # 正常路径仍会 _start_share

    def test_callback_returns_when_sock_superseded(self):
        # 已重连：_active_sock 指向新 socket，旧定时器作废（既有 guard 不回退）。
        ch = _channel()
        old_sock = object()
        ch._active_sock = object()                  # 新连接
        ch.share_enabled = True
        ch.multiview = True
        ch._running = True
        calls = self._wire_start_share(ch)
        ch._share_sync_after_connect(old_sock)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
