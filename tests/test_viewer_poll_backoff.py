"""A4 §3.2（第 132 条）：viewer poll 空闲退避 15→33→50ms 封顶。

桩 root.after 记录每轮排定的间隔，注入"有帧/无帧"序列，断言：
  - 已连上（receiving）且无新帧 → 15, 33, 50, 50 …
  - 收到新帧即重置回 15；
  - 连接中/重连/认证/无频道等"需要灵敏"的状态不退避。
hermetic：ViewerApp.__new__ 绕开真实 Tk。
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import viewer


class _Root:
    def __init__(self):
        self.calls = []

    def after(self, ms, fn=None):
        self.calls.append(ms)
        return "after#%d" % len(self.calls)


class _Ch:
    def __init__(self, status="receiving"):
        self.lock = threading.Lock()
        self.frame_serial = 0
        self.last_frame = None
        self.status = status


def _app(status="receiving", with_channel=True):
    app = viewer.ViewerApp.__new__(viewer.ViewerApp)
    app.running = True
    app.root = _Root()
    app._poll_idle_ms = 15
    app.channels = [_Ch(status)] if with_channel else []
    app.active_idx = 0
    app._last_channel_idx = 0
    app._last_frame_serial = 0
    # ui-002（2026-09-18）：poll 判据新增状态维度，桩需同步簿记（与真实 _show_* 一致）
    app._last_channel_status = status
    app._drain_ui_queue = lambda: None
    app._sync_hotkey_notice_once = lambda: None
    app._process_cred_requests = lambda: None
    app._check_share_sessions = lambda: None
    app._show_text = lambda *a: None
    app._update_status_overlay = lambda: None

    def _show_frame(f, s):
        # 真实 _show_frame 会更新这两处簿记（poll 据此判断"无新帧"）
        app._last_frame_serial = s
        app._last_channel_idx = app.active_idx

    app._show_frame = _show_frame
    return app


def _poll_once(app, new_frame=False):
    """跑一轮 poll，返回本轮为下一次 poll 排定的间隔（ms）。"""
    if new_frame and app.channels:
        ch = app.channels[app.active_idx]
        with ch.lock:
            ch.frame_serial += 1
            ch.last_frame = "IMG"
    app.poll()
    return app.root.calls[-1]


class TestPollBackoff(unittest.TestCase):
    def test_idle_seq_15_33_50_capped(self):
        app = _app()
        seq = [_poll_once(app) for _ in range(5)]
        self.assertEqual(seq, [15, 33, 50, 50, 50])

    def test_new_frame_resets_to_15(self):
        app = _app()
        _poll_once(app)
        _poll_once(app)
        self.assertEqual(app._poll_idle_ms, 50)      # 已退避两级
        _poll_once(app, new_frame=True)              # 本轮排定仍是旧值
        self.assertEqual(app._poll_idle_ms, 15)      # 收帧即重置
        self.assertEqual(_poll_once(app), 15)        # 下一拍回到 15ms

    def test_reset_then_idle_reescalates(self):
        app = _app()
        _poll_once(app)                              # 15 → 33
        _poll_once(app, new_frame=True)              # 重置
        seq = [_poll_once(app) for _ in range(3)]
        self.assertEqual(seq, [15, 33, 50])

    def test_non_receiving_status_stays_15(self):
        for status in ("connecting", "auth", "disconnected"):
            app = _app(status=status)
            seq = [_poll_once(app) for _ in range(4)]
            self.assertEqual(seq, [15, 15, 15, 15],
                             "status=%s 不应退避" % status)

    def test_no_channel_stays_15(self):
        app = _app(with_channel=False)
        seq = [_poll_once(app) for _ in range(3)]
        self.assertEqual(seq, [15, 15, 15])

    def test_channel_source_switch_resets(self):
        app = _app()
        _poll_once(app)
        _poll_once(app)
        self.assertEqual(app._poll_idle_ms, 50)
        app._refresh_overlay()                       # 切频道立即刷新路径
        self.assertEqual(app._poll_idle_ms, 15)


if __name__ == "__main__":
    unittest.main()
