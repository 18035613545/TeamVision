# -*- coding: utf-8 -*-
"""第 64 条回归：拖动滑块不得每次都全盘写配置。

旧路径：ttk.Scale 的 command 在拖动过程中连续触发（一次拖动几十到上百次），
set_display_width/set_alpha 每次都 save_config 整份 JSON（写 tmp + os.replace）→
拖动卡顿、设置按像素级持久化。

修复：四个 setter（display_width/alpha/click_through/panel_topmost）改调 _schedule_save，
用 root.after(500) 去抖——每次改动重置定时器，只在停手 0.5s 后落盘一次；退出时
quit()/mainloop finally 调 _flush_pending_save 兜底，避免最后一次改动丢失。
画面重绘（_refresh_overlay）与 -alpha/穿透的 win32 效果仍即时。

hermetic：用 __new__ 构造 ViewerApp，FakeRoot 记录 after/after_cancel 并可手动触发回调，
patch viewer.save_config 统计真实落盘次数，绕开真实 Tk/采集/win32。
"""
import unittest
from unittest import mock

import viewer


class FakeRoot:
    """记录 root.after / after_cancel，并可手动 fire 当前挂起的回调。"""

    def __init__(self):
        self._next_id = 0
        self.pending = {}        # aid -> (delay, fn, args)
        self.cancelled = []      # 被 after_cancel 的 aid
        self.after_count = 0     # after() 总调用次数

    def after(self, delay, fn=None, *args):
        self._next_id += 1
        aid = self._next_id
        self.pending[aid] = (delay, fn, args)
        self.after_count += 1
        return aid

    def after_cancel(self, aid):
        self.cancelled.append(aid)
        self.pending.pop(aid, None)

    def attributes(self, *a, **k):
        pass

    def fire_all(self):
        """触发当前所有挂起回调（快照后清空，模拟 0.5s 到期）。"""
        snapshot = list(self.pending.items())
        self.pending = {}
        for aid, (delay, fn, args) in snapshot:
            fn(*args)


def _app(root):
    app = viewer.ViewerApp.__new__(viewer.ViewerApp)
    app.root = root
    app.cfg = {"viewer": {}}
    app._save_after_id = None
    app.label = None
    app.panel = None
    app.display_width = 320
    app.alpha = 1.0
    app.click_through = False
    app.panel_topmost = False
    # 隔离与去抖无关的副作用：重绘 / win32 穿透
    app._refresh_overlay = lambda: None
    app.apply_click_through = lambda: None
    return app


class TestSliderDragCoalesces(unittest.TestCase):
    def test_rapid_display_width_calls_save_once(self):
        root = FakeRoot()
        app = _app(root)
        with mock.patch("viewer.save_config") as save:
            for v in range(160, 660, 10):       # 模拟一次拖动：50 个连续刻度
                app.set_display_width(v)
            self.assertEqual(save.call_count, 0)  # 拖动过程中尚未落盘
            root.fire_all()                       # 0.5s 到期
            self.assertEqual(save.call_count, 1)  # 只写一次
        self.assertEqual(app.cfg["viewer"]["display_width"], 650)  # 落盘的是最终值

    def test_rapid_alpha_calls_save_once(self):
        root = FakeRoot()
        app = _app(root)
        with mock.patch("viewer.save_config") as save:
            for v in range(30, 101, 5):
                app.set_alpha(v / 100.0)
            self.assertEqual(save.call_count, 0)
            root.fire_all()
            self.assertEqual(save.call_count, 1)
        self.assertAlmostEqual(app.cfg["viewer"]["alpha"], 1.0)

    def test_each_change_resets_timer_not_stacks(self):
        # 去抖应重置同一个定时器：N 次改动只留 1 个挂起回调，前 N-1 个被 cancel。
        root = FakeRoot()
        app = _app(root)
        with mock.patch("viewer.save_config"):
            for v in (200, 300, 400):
                app.set_display_width(v)
            self.assertEqual(len(root.pending), 1)        # 只剩一个挂起
            self.assertEqual(len(root.cancelled), 2)      # 前两个被取消
            self.assertEqual(root.after_count, 3)         # 每次都重排


class TestFlushOnExit(unittest.TestCase):
    def test_flush_pending_saves_and_cancels(self):
        root = FakeRoot()
        app = _app(root)
        with mock.patch("viewer.save_config") as save:
            app.set_display_width(480)
            self.assertEqual(save.call_count, 0)
            pending_id = app._save_after_id
            self.assertIsNotNone(pending_id)
            app._flush_pending_save()                     # 退出兜底
            self.assertEqual(save.call_count, 1)          # 立即落盘
            self.assertIn(pending_id, root.cancelled)     # 取消未到期定时器
            self.assertIsNone(app._save_after_id)

    def test_flush_is_noop_when_nothing_pending(self):
        root = FakeRoot()
        app = _app(root)
        with mock.patch("viewer.save_config") as save:
            app._flush_pending_save()                     # 无挂起 → 不写
            self.assertEqual(save.call_count, 0)
        self.assertIsNone(app._save_after_id)

    def test_flush_after_fire_is_noop(self):
        # 定时器已到期落盘后，退出兜底不应重复写。
        root = FakeRoot()
        app = _app(root)
        with mock.patch("viewer.save_config") as save:
            app.set_display_width(480)
            root.fire_all()
            self.assertEqual(save.call_count, 1)
            app._flush_pending_save()                     # _save_after_id 已清
            self.assertEqual(save.call_count, 1)


class TestNoRootIsSafe(unittest.TestCase):
    def test_schedule_save_noop_without_root(self):
        app = _app(None)
        with mock.patch("viewer.save_config") as save:
            app.set_display_width(480)                    # root=None：不崩溃、不排程
            self.assertEqual(save.call_count, 0)
            self.assertIsNone(app._save_after_id)
            app._flush_pending_save()                     # 同样安全
            self.assertEqual(save.call_count, 0)


if __name__ == "__main__":
    unittest.main()
