# -*- coding: utf-8 -*-
"""第 117 条回归：热键注册结果上屏、频道步进回绕、穿透样式不抛、退出释放热键。"""
import threading
import unittest
from unittest import mock

import viewer


class TestStepChannelWrap(unittest.TestCase):
    def _app(self, n=3, active_idx=0):
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app.channels = [object() for _ in range(n)]
        app.active_idx = active_idx
        app.panel = None
        calls = []
        app.switch_channel = lambda i: calls.append(i)
        return app, calls

    def test_forward_wraps_to_first(self):
        app, calls = self._app(3, active_idx=2)
        app._step_channel(1)
        self.assertEqual(calls, [0])

    def test_backward_wraps_to_last(self):
        app, calls = self._app(3, active_idx=0)
        app._step_channel(-1)
        self.assertEqual(calls, [2])

    def test_middle_steps_normally(self):
        app, calls = self._app(3, active_idx=1)
        app._step_channel(1)
        app._step_channel(-1)
        self.assertEqual(calls, [2, 0])

    def test_empty_channels_noop(self):
        app, calls = self._app(0)
        app._step_channel(1)
        self.assertEqual(calls, [])


class TestHotkeyConstants(unittest.TestCase):
    def test_mod_norepeat_defined(self):
        self.assertEqual(viewer.MOD_NOREPEAT, 0x4000)

    def test_wm_quit_defined(self):
        self.assertEqual(viewer.WM_QUIT, 0x0012)


class TestHotkeyNotice(unittest.TestCase):
    def _app(self, panel=True):
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app.running = True
        app.panel = object() if panel else None
        app._hotkey_registered = set()
        app._hotkey_failed = []
        app._hint_label = None
        app._hotkey_notice_synced = False
        statuses = []
        app._panel_set_status = lambda text, error=False: statuses.append(
            (text, error))
        hints = []
        app._update_hotkey_hint = lambda: hints.append(True)
        return app, statuses, hints

    def test_all_fail_shows_status(self):
        app, statuses, hints = self._app()
        app._notify_hotkey_result([], ["Ctrl+Alt+X 切换穿透", "Ctrl+Alt+← 上一个频道"])
        self.assertTrue(statuses)
        self.assertIn("全部注册失败", statuses[0][0])
        self.assertTrue(statuses[0][1])
        self.assertTrue(hints)

    def test_partial_fail_shows_status(self):
        app, statuses, _ = self._app()
        app._notify_hotkey_result([10, 11], ["Ctrl+Alt+X 切换穿透"])
        self.assertTrue(statuses)
        self.assertIn("部分热键被占用", statuses[0][0])

    def test_all_ok_no_error_status(self):
        app, statuses, _ = self._app()
        app._notify_hotkey_result([1, 2, 3, 10], [])
        self.assertEqual(statuses, [])

    def test_sync_once_skips_until_results(self):
        app, statuses, _ = self._app()
        app._sync_hotkey_notice_once()
        self.assertFalse(app._hotkey_notice_synced)
        self.assertEqual(statuses, [])
        app._hotkey_failed = ["Alt+1 直达频道"]
        app._hotkey_registered = set()
        app._sync_hotkey_notice_once()
        self.assertTrue(app._hotkey_notice_synced)
        self.assertTrue(statuses)
        statuses.clear()
        app._sync_hotkey_notice_once()
        self.assertEqual(statuses, [])

    def test_no_panel_does_not_throw(self):
        app, statuses, _ = self._app(panel=False)
        app._notify_hotkey_result([], ["X"])
        self.assertEqual(statuses, [])


class TestApplyClickThroughNoThrow(unittest.TestCase):
    def test_missing_hwnd_is_noop(self):
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app.hwnd = None
        app.click_through = True
        app.alpha = 1.0
        app.apply_click_through()  # 不应抛

    def test_win32_error_is_swallowed(self):
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app.hwnd = 12345
        app.click_through = True
        app.alpha = 0.9
        with mock.patch.object(viewer.win32gui, "GetWindowLong",
                               side_effect=RuntimeError("dead hwnd")):
            app.apply_click_through()  # 不应抛


class TestStopHotkeys(unittest.TestCase):
    def test_no_tid_is_noop(self):
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app._hotkey_tid = None
        app._stop_hotkeys()

    def test_posts_wm_quit(self):
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app._hotkey_tid = 4242
        with mock.patch.object(viewer.ctypes, "windll") as windll:
            app._stop_hotkeys()
            windll.user32.PostThreadMessageW.assert_called_once()
            args = windll.user32.PostThreadMessageW.call_args[0]
            self.assertEqual(args[0], 4242)
            self.assertEqual(args[1], viewer.WM_QUIT)


if __name__ == "__main__":
    unittest.main()
