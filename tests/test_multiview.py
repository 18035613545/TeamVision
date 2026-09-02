# -*- coding: utf-8 -*-
"""观看组模式（MultiView v1）单元测试：订阅路由 / 关键帧门控 / roster / 模块抽取。"""
import socket
import unittest

from common import MSG_CTRL, parse_message

import screen
import host


class TestScreenExtraction(unittest.TestCase):
    """screen.py 抽取后 host.py 必须保持同名 re-export（既有测试 import host.xxx 依赖）。"""

    def test_host_reexports_screen_symbols(self):
        for name in ("CaptureManager", "STILL_STEP", "downsample_frame",
                     "motion_changed", "still_gate_update", "calc_output_size",
                     "encode_bgr"):
            self.assertIs(getattr(host, name), getattr(screen, name),
                          "host.%s 未 re-export screen.%s" % (name, name))

    def test_still_gate_semantics_unchanged(self):
        # 平移后行为回归抽查：连续无变化满 quiet_ms 判定静止，变化立即复位
        s, q = screen.still_gate_update(False, None, False, 100.0, 50.0)
        self.assertFalse(s)
        self.assertEqual(q, 100.0)
        s, q = screen.still_gate_update(s, q, False, 200.0, 50.0)
        self.assertTrue(s)
        s, q = screen.still_gate_update(s, q, True, 250.0, 50.0)
        self.assertFalse(s)
        self.assertIsNone(q)


class TestClientInfoMultiView(unittest.TestCase):

    def _make_info(self):
        import host
        return host.ClientInfo(("127.0.0.1", 12345))

    def test_defaults(self):
        info = self._make_info()
        self.assertEqual(info.watch_source, "local")
        self.assertTrue(info.need_key)
        self.assertIsNone(info.share_id)
        self.assertTrue(hasattr(info, "_write_lock"))

    def test_send_ctrl_framing(self):
        a, b = socket.socketpair()
        try:
            info = self._make_info()
            info.send_ctrl(a, {"action": "cap", "multiview": True})
            raw = b.recv(65536)
            consumed, kind, payload = parse_message(raw)
            self.assertEqual(kind, MSG_CTRL)
            import json
            self.assertEqual(json.loads(payload.decode("utf-8")), {"action": "cap", "multiview": True})
        finally:
            a.close(); b.close()


class _StubSender(object):
    """_sender_active=True 的假订阅者：captured 收集 enqueue_frame 的消息。"""

    def __init__(self, watch_source="local", need_key=True):
        self.watch_source = watch_source
        self.need_key = need_key
        self._sender_active = True
        self.captured = []

    def enqueue_frame(self, frame):
        self.captured.append(frame)


class TestRouteFrame(unittest.TestCase):

    def _route(self, clients, source, msg=b"MSG", is_key=False, is_jpeg=False):
        import host
        runtime = host.HostRuntime.__new__(host.HostRuntime)  # 不触发 __init__
        runtime.clients = {}
        runtime.clients_lock = __import__("threading").Lock()
        for k, v in clients.items():
            runtime.clients[k] = v
        host.route_frame(runtime, source, msg, is_key=is_key, is_jpeg=is_jpeg)
        return runtime

    def test_only_matching_subscribers(self):
        loc = _StubSender(watch_source="local")
        peer = _StubSender(watch_source="peer:1")
        other = _StubSender(watch_source="peer:2")
        self._route({"a": loc, "b": peer, "c": other}, "peer:1", b"X", is_key=True)
        self.assertEqual(len(peer.captured), 1)
        self.assertEqual(len(loc.captured), 0)
        self.assertEqual(len(other.captured), 0)

    def test_key_gating(self):
        peer = _StubSender(watch_source="peer:1", need_key=True)
        clients = {"a": peer}
        # 非关键帧被门控丢弃
        self._route(clients, "peer:1", b"P1", is_key=False)
        self.assertEqual(peer.captured, [])
        self.assertTrue(peer.need_key)
        # 关键帧放行并清除 need_key
        self._route(clients, "peer:1", b"K", is_key=True)
        self.assertEqual(peer.captured, [b"K"])
        self.assertFalse(peer.need_key)
        # 之后非关键帧放行
        self._route(clients, "peer:1", b"P2", is_key=False)
        self.assertEqual(peer.captured, [b"K", b"P2"])

    def test_jpeg_bypasses_gating(self):
        peer = _StubSender(watch_source="peer:1", need_key=True)
        self._route({"a": peer}, "peer:1", b"J", is_jpeg=True)
        self.assertEqual(peer.captured, [b"J"])
        self.assertTrue(peer.need_key)  # JPEG 自包含，不参与关键帧状态

    def test_watch_switch_resets_need_key(self):
        # 模拟 watch 处理后的状态（handle_client 置位）
        loc = _StubSender(watch_source="local", need_key=True)
        self._route({"a": loc}, "local", b"K", is_key=True)
        self.assertFalse(loc.need_key)
        loc.watch_source = "peer:1"
        loc.need_key = True  # 切换源重新门控
        self._route({"a": loc}, "peer:1", b"P", is_key=False)
        self.assertEqual(loc.captured, [b"K"])
