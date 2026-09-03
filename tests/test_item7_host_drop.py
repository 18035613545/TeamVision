# -*- coding: utf-8 -*-
"""第 7 条（host 侧）回归：拥塞丢帧 must 触发「丢帧→强制关键帧」愈合。

旧路径：`enqueue_frame`（maxsize=1 队列）在观看端/网络慢时丢弃旧帧，但不通知任何人；
被丢帧是后续 P 帧的参考 → 观看端解出花屏/拖影，直到下一个周期关键帧（约 2 秒）才清。
`route_frame` 的 need_key 门控只覆盖新连接/切换源，不覆盖拥塞丢帧。

修复：`enqueue_frame` 返回是否丢帧；`route_frame` 对「视频帧 + 发生丢帧 + 本帧非关键帧」
的订阅者置 `need_key=True`（门控丢弃中间 P 帧）并 `runtime.force_key=True`（强制编码器
下一帧出 IDR），把花屏窗口从约 2 秒压到约 1 帧。JPEG 自包含、关键帧本身即可愈合，均不处理。

hermetic：用 __new__ 构造 ClientInfo/HostRuntime，假订阅者控制 enqueue_frame 的丢帧返回值，
绕开真实 socket/编码器/发送线程。
"""
import queue
import threading
import unittest

import host


class _DropSender:
    """_sender_active 假订阅者：enqueue_frame 返回预设的丢帧标志并收集帧。"""

    def __init__(self, watch_source="peer:1", need_key=False, dropped=True):
        self.watch_source = watch_source
        self.need_key = need_key
        self._sender_active = True
        self._dropped = dropped
        self.captured = []

    def enqueue_frame(self, frame):
        self.captured.append(frame)
        return self._dropped


def _runtime(clients):
    rt = host.HostRuntime.__new__(host.HostRuntime)
    rt.clients = dict(clients)
    rt.clients_lock = threading.Lock()
    rt.slot_lock = threading.Lock()
    rt.force_key = False
    return rt


def _route(clients, source, frame=b"F", is_key=False, is_jpeg=False):
    rt = _runtime(clients)
    host.route_frame(rt, source, frame, is_key=is_key, is_jpeg=is_jpeg)
    return rt


class TestEnqueueFrameDropSignal(unittest.TestCase):
    def _info(self):
        info = host.ClientInfo.__new__(host.ClientInfo)
        info.send_queue = queue.Queue(maxsize=1)
        info._lock = threading.Lock()
        info.dropped_frames = 0
        info.last_drop_at = 0.0
        return info

    def test_returns_false_when_queue_empty(self):
        info = self._info()
        self.assertFalse(info.enqueue_frame(b"A"))     # 队列空，无丢帧
        self.assertEqual(info.dropped_frames, 0)

    def test_returns_true_when_queue_full_drops_old(self):
        info = self._info()
        info.enqueue_frame(b"A")                       # 填满 maxsize=1
        self.assertTrue(info.enqueue_frame(b"B"))      # 再入队 → 丢旧帧 A
        self.assertEqual(info.dropped_frames, 1)
        self.assertGreater(info.last_drop_at, 0.0)
        # 队列里现在是最新的 B（旧帧 A 被丢）
        self.assertEqual(info.send_queue.get_nowait(), b"B")


class TestRouteFrameDropHeals(unittest.TestCase):
    def test_video_drop_forces_keyframe_and_gates(self):
        sub = _DropSender(watch_source="peer:1", need_key=False, dropped=True)
        rt = _route({"a": sub}, "peer:1", b"P", is_key=False, is_jpeg=False)
        self.assertTrue(sub.need_key)        # 门控到下一个关键帧
        self.assertTrue(rt.force_key)        # 强制编码器出 IDR
        self.assertEqual(sub.captured, [b"P"])

    def test_jpeg_drop_does_not_heal(self):
        # JPEG 各帧独立，丢帧无需强制关键帧/门控。
        sub = _DropSender(watch_source="peer:1", need_key=False, dropped=True)
        rt = _route({"a": sub}, "peer:1", b"J", is_key=False, is_jpeg=True)
        self.assertFalse(sub.need_key)
        self.assertFalse(rt.force_key)

    def test_keyframe_drop_does_not_regate(self):
        # 本帧就是关键帧：自身即可愈合，且门控上方刚清过 need_key，绝不能再置回。
        sub = _DropSender(watch_source="peer:1", need_key=True, dropped=True)
        rt = _route({"a": sub}, "peer:1", b"K", is_key=True, is_jpeg=False)
        self.assertFalse(sub.need_key)       # route_frame 关键帧门控已清，未被重新置位
        self.assertFalse(rt.force_key)

    def test_no_drop_does_not_heal(self):
        sub = _DropSender(watch_source="peer:1", need_key=False, dropped=False)
        rt = _route({"a": sub}, "peer:1", b"P", is_key=False, is_jpeg=False)
        self.assertFalse(sub.need_key)
        self.assertFalse(rt.force_key)

    def test_only_dropping_subscriber_is_gated(self):
        # 多订阅者：只有发生丢帧的那个被门控，其余不受影响（force_key 为全局编码信号）。
        drop = _DropSender(watch_source="peer:1", need_key=False, dropped=True)
        ok = _DropSender(watch_source="peer:1", need_key=False, dropped=False)
        rt = _route({"a": drop, "b": ok}, "peer:1", b"P", is_key=False, is_jpeg=False)
        self.assertTrue(drop.need_key)
        self.assertFalse(ok.need_key)
        self.assertTrue(rt.force_key)


if __name__ == "__main__":
    unittest.main()
