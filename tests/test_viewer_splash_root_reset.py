# -*- coding: utf-8 -*-
"""回归：复用 splash root 时必须清掉它的显式尺寸，否则悬浮窗被钳在启动图大小。

A3（第 133 条）起观看端把 `show_fast_splash(root)` 建好的 root 直接改造成悬浮窗，
而 splash 用 `_center(root, _FAST_SIZE=380x120)` 下过**显式尺寸**。Tk 的
`wm geometry "+x+y"` 只改位置、不改尺寸——显式尺寸一旦设过，窗口就不再随内容自适应。
于是悬浮窗恒为 380x120：对方屏幕按 display_width 缩放后（361x203）只露出上面一条，
用户报"项目启动后右上角的屏幕小窗无法完整显示对方屏幕"（实测窗口尺寸精确等于
启动图尺寸，与 display_width=361/960 都无关）。

修复：移植分支里 `root.geometry("")` 交还内容请求尺寸（最小 Tk 实验：贴 361x203
图后窗口 380x120 → 清尺寸后 365x230）。

hermetic：`ViewerApp.__new__` + 记录型假 root，跑真实 `run()` 的移植分支
（_build_ui/_build_panel 等重活全部替换为桩）。范式同 test_item91/98。
"""
import inspect
import unittest
from unittest import mock

import viewer


class _FakeSplashChild:
    def __init__(self):
        self.destroyed = False

    def destroy(self):
        self.destroyed = True


class _FakeSplashRoot:
    """记录型假 root：只实现 run() 移植分支用到的接口。"""

    def __init__(self):
        self.calls = []
        self.children = [_FakeSplashChild(), _FakeSplashChild(), _FakeSplashChild()]

    def overrideredirect(self, v):
        self.calls.append(("overrideredirect", v))

    def winfo_children(self):
        return list(self.children)

    def geometry(self, spec):
        self.calls.append(("geometry", spec))

    def mainloop(self):
        self.calls.append(("mainloop",))

    def destroy(self):
        self.calls.append(("destroy",))

    def after(self, ms, fn=None):
        self.calls.append(("after", ms))


def _make_app(root):
    app = viewer.ViewerApp.__new__(viewer.ViewerApp)
    app.root = root
    app.channels = []
    app.cfg = {"viewer": {}}
    app.running = True
    import threading
    app._mainloop_ready = threading.Event()
    app._stop_hotkeys = lambda: None
    app._flush_pending_save = lambda: None
    return app


class TestSplashRootGeometryReset(unittest.TestCase):
    def _run(self, root):
        app = _make_app(root)
        app._build_ui = lambda: root.calls.append(("build_ui",))
        app._maybe_run_wizard = lambda: None
        app._build_panel = lambda: None
        app.start_hotkey = lambda: None
        app._refresh_overlay = lambda: None
        with mock.patch.object(viewer.update_check, "start_background_check",
                               lambda *a, **k: None):
            app.run(root=root)
        return app

    def test_geometry_reset_before_build_ui(self):
        root = _FakeSplashRoot()
        self._run(root)
        self.assertIn(("geometry", ""), root.calls,
                      "复用 splash root 未清显式尺寸（悬浮窗会被钳在启动图大小）")
        self.assertLess(root.calls.index(("geometry", "")),
                        root.calls.index(("build_ui",)),
                        "尺寸必须在 _build_ui（内含 _position_top_right）之前清掉")

    def test_splash_children_destroyed(self):
        root = _FakeSplashRoot()
        self._run(root)
        self.assertTrue(all(c.destroyed for c in root.children),
                        "splash 子控件未清理，会与悬浮窗内容叠在一起")

    def test_destroyed_root_does_not_crash(self):
        """splash root 已被外部销毁（TclError）时照常继续，不得把启动打断。"""
        import tkinter as tk

        class _DeadRoot(_FakeSplashRoot):
            def overrideredirect(self, v):
                raise tk.TclError("application has been destroyed")

        root = _DeadRoot()
        app = self._run(root)   # 不应抛
        # T-15（2026-09-24）：旧断言 `app.running is False or app.running is True` 恒真，
        # "不得把启动打断"的结论侧没有任何真实断言。改为钉住结论：移植分支必须走完
        # （_build_ui 被调用、进入 mainloop），而不是在异常处中断。
        self.assertIn(("build_ui",), root.calls, "移植分支中途中断（启动被打断）")
        self.assertIn(("mainloop",), root.calls, "没有进入主循环")
        self.assertFalse(app.running, "mainloop 收尾未复位 running")

    def test_geometry_reset_survives_child_destroy_failure(self):
        """G4b/ui-036：子控件销毁抛 TclError 时，尺寸复位不得被同一条 except 跳过。"""
        import tkinter as tk

        class _BadChildrenRoot(_FakeSplashRoot):
            def winfo_children(self):
                raise tk.TclError("bad widget tree")

        root = _BadChildrenRoot()
        self._run(root)
        self.assertIn(("geometry", ""), root.calls,
                      "尺寸复位被前一条语句的异常跳过（G4b 原症状：小窗只露上半截）")

    def test_run_source_guards_reset(self):
        src = inspect.getsource(viewer.ViewerApp.run)
        self.assertIn('root.geometry("")', src,
                      "run() 移植分支缺少显式尺寸清理（回归：小窗只露上半截）")


if __name__ == "__main__":
    unittest.main()
