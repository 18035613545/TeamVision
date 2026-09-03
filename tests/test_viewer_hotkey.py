# -*- coding: utf-8 -*-
"""观看端画面源热键单元测试：Ctrl+Alt+↑/↓ 循环切换 local 与队友画面。

不建 Tk root、不起接收线程：直接构造 ViewerApp 并替换 channels 为桩对象，
调用 _step_source(±1) 断言目标源序列。
"""
import threading
import unittest

import viewer


def make_cfg(channels=None):
    """最小可用配置（ViewerApp.__init__ 只读 viewer 段的展示/网络键）。"""
    return {
        "viewer": {
            "server_addr": "127.0.0.1:5700",
            "channels": channels if channels is not None else [
                {"name": "测试频道", "addr": "127.0.0.1:5700"},
            ],
            "hotkeys": {"direct": True},
        },
    }


class StubChannel:
    """替身频道：只提供 _step_source / _describe_source 依赖的成员。"""

    def __init__(self, name, peer_ids=(), watch_source="local", accept=True):
        self.name = name
        self.lock = threading.Lock()
        self.peers = [{"id": pid, "name": "队友%s" % pid, "addr": "10.0.0.1:5700"}
                      for pid in peer_ids]
        self.watch_source = watch_source
        self.accept = accept
        self.calls = []

    def switch_source(self, source):
        self.calls.append(source)
        if not self.accept:
            return False
        self.watch_source = source
        return True


class TestHotkeyDefs(unittest.TestCase):

    def test_source_hotkeys_registered_with_ctrl_alt(self):
        defs = dict(viewer.HOTKEY_DEFS)
        self.assertIn(viewer.HOTKEY_ID_SRC_PREV, defs)
        self.assertIn(viewer.HOTKEY_ID_SRC_NEXT, defs)
        self.assertEqual(defs[viewer.HOTKEY_ID_SRC_PREV], viewer.VK_UP)
        self.assertEqual(defs[viewer.HOTKEY_ID_SRC_NEXT], viewer.VK_DOWN)

    def test_hotkey_ids_unique(self):
        ids = [hid for hid, _ in viewer.HOTKEY_DEFS]
        self.assertEqual(len(ids), len(set(ids)))
        # 直达频道 ID 段（10..18）不得与固定热键 ID 重叠
        self.assertTrue(all(hid < viewer.HOTKEY_ID_DIRECT_BASE for hid in ids))


class TestStepSource(unittest.TestCase):

    def _app(self, channels, active_idx=0):
        app = viewer.ViewerApp(make_cfg())
        app.channels = channels
        app.active_idx = active_idx
        self.assertIsNone(app.panel)  # 面板未建：验证热键路径的面板守卫
        return app

    def test_cycle_forward_and_wrap(self):
        ch = StubChannel("A", peer_ids=["peer:1", "peer:2"])
        app = self._app([ch])
        for expected in ("peer:1", "peer:2", "local", "peer:1"):
            app._step_source(1)
            self.assertEqual(ch.watch_source, expected)
        self.assertEqual(ch.calls, ["peer:1", "peer:2", "local", "peer:1"])

    def test_cycle_backward_wraps_to_last_peer(self):
        ch = StubChannel("A", peer_ids=["peer:1", "peer:2"])
        app = self._app([ch])
        app._step_source(-1)
        self.assertEqual(ch.watch_source, "peer:2")
        app._step_source(-1)
        self.assertEqual(ch.watch_source, "peer:1")
        app._step_source(-1)
        self.assertEqual(ch.watch_source, "local")

    def test_no_peers_does_not_switch(self):
        ch = StubChannel("A", peer_ids=[])
        app = self._app([ch])
        app._step_source(1)
        app._step_source(-1)
        self.assertEqual(ch.calls, [])
        self.assertEqual(ch.watch_source, "local")

    def test_stale_watch_source_restarts_from_local(self):
        # 当前源已不在 roster（队友刚退）：按 local 起算，下一个是首个队友
        ch = StubChannel("A", peer_ids=["peer:1", "peer:2"], watch_source="peer:9")
        app = self._app([ch])
        app._step_source(1)
        self.assertEqual(ch.watch_source, "peer:1")

    def test_switch_failure_keeps_source(self):
        ch = StubChannel("A", peer_ids=["peer:1"], accept=False)
        app = self._app([ch])
        app._step_source(1)
        self.assertEqual(ch.calls, ["peer:1"])
        self.assertEqual(ch.watch_source, "local")

    def test_targets_active_channel_only(self):
        a = StubChannel("A", peer_ids=["peer:1"])
        b = StubChannel("B", peer_ids=["peer:7"])
        app = self._app([a, b], active_idx=1)
        app._step_source(1)
        self.assertEqual(b.watch_source, "peer:7")
        self.assertEqual(a.calls, [])
        self.assertEqual(a.watch_source, "local")

    def test_no_channels_is_noop(self):
        app = self._app([])
        app._step_source(1)  # 不应抛异常


if __name__ == "__main__":
    unittest.main()
