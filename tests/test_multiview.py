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
