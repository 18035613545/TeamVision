# -*- coding: utf-8 -*-
"""第 91 条回归：面板列表/预览刷新函数体必须有异常兜底，且兜底后续排定时器不得丢。

旧路径：`_panel_refresh_list` / `_panel_refresh_preview` 的函数体（tree.delete/insert、
cv2.cvtColor/Image.fromarray/resize/PhotoImage/configure 等）全裸奔，只有末尾续排的
`self.panel.after(...)` 包了 try。任一函数体操作抛异常（一帧异常形状的解码图像、Treeview
内部 Tcl 错误）会在续排 after 之前逃出 → 该刷新永久停更（fps/状态陈旧、源列表不再更新、
预览定格），而悬浮窗仍正常动 → 程序看着「半活」。对照 `_show_frame` 本就有 try 兜底。

修复：把函数体裹进 try/except/finally——except 记录异常（log.warning, exc_info）并跳过本次，
finally **保证**续排 `after` 定时器（列表分支还复位 `_panel_suppress_select`，避免中途抛出后
选中抑制卡死）。

hermetic：用 ViewerApp.__new__ 构造实例（绕开真实 Tk/接收线程），mock 出会在函数体中途抛异常
的依赖（tree.delete / cv2.cvtColor），断言——异常被吞且记录、续排 after 仍被调用、抑制标志复位。
范式同 test_item61_timer.py（__new__ + lambda/MagicMock）。预览成功路径需真实 Tk（ImageTk），
故只测异常路径（在触及任何 Tk 调用之前即抛出）。
"""
import threading
import unittest
from unittest import mock

import viewer


class _FakePanel:
    """记录 after(ms, fn) 调用的假面板 widget。"""

    def __init__(self):
        self.calls = []

    def after(self, ms, fn=None):
        self.calls.append((ms, fn))
        return "after#1"


def _snapshot():
    return [{"status": "receiving", "name": "alice", "addr": "1.2.3.4:5700",
             "fps": 30.0, "latency": 0, "video": False, "codec": None}]


def _list_app(tree):
    """构造一个最小可用的 ViewerApp（仅 _panel_refresh_list 所需属性）。"""
    app = viewer.ViewerApp.__new__(viewer.ViewerApp)
    app.running = True
    app.panel = _FakePanel()
    app._panel_visible = lambda: True
    app.get_channels_snapshot = lambda: _snapshot()
    app._panel_known_count = 1
    app.active_idx = 0
    app._panel_selected = 0
    app._panel_suppress_select = False
    app._tree = tree
    app._sync_share_button = lambda: None
    app._panel_refresh_sources = lambda: None
    return app


class TestPanelRefreshListGuard(unittest.TestCase):
    def test_reschedules_after_when_body_raises(self):
        # 核心：tree.delete 在 _panel_suppress_select=True 之后抛异常 → 续排 after 仍须执行，
        # 抑制标志须被 finally 复位，异常须被记录（非静默杀死刷新循环）。
        tree = mock.MagicMock()
        tree.yview.return_value = (0.0,)
        tree.get_children.return_value = ()
        tree.delete.side_effect = RuntimeError("boom")   # 函数体中途抛出
        app = _list_app(tree)
        app._panel_suppress_select = True                # 模拟体内已置 True（delete 在其后抛）
        with mock.patch.object(viewer, "log") as mlog:
            app._panel_refresh_list()
        self.assertEqual(len(app.panel.calls), 1)        # 续排 after 仍被调用（未永久停更）
        self.assertEqual(app.panel.calls[0][0], 500)
        self.assertFalse(app._panel_suppress_select)     # finally 复位抑制标志（未卡死选中）
        self.assertTrue(mlog.warning.called)             # 异常被记录（非静默）

    def test_normal_path_reschedules_and_clears_suppress(self):
        # 正控：无异常时照常续排，抑制标志为 False。
        tree = mock.MagicMock()
        tree.yview.return_value = (0.0,)
        tree.get_children.return_value = ()
        app = _list_app(tree)
        app._panel_refresh_list()
        self.assertEqual(len(app.panel.calls), 1)
        self.assertEqual(app.panel.calls[0][0], 500)
        self.assertFalse(app._panel_suppress_select)


class TestPanelRefreshPreviewGuard(unittest.TestCase):
    def _preview_app(self):
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app.running = True
        app.panel = _FakePanel()
        app._panel_visible = lambda: True
        app.active_idx = 0
        app._panel_selected = None
        app._panel_last_idx = -1
        app._panel_last_serial = -1
        app._panel_photo = None
        app._preview_label = mock.MagicMock()
        app._preview_info = mock.MagicMock()
        ch = mock.MagicMock()
        ch.lock = threading.RLock()
        ch.name = "alice"
        ch.status = "receiving"
        ch.frame_serial = 5
        ch.last_frame = mock.MagicMock()
        ch.last_frame.copy.return_value = object()       # 任意「帧」对象，cvtColor 会被 patch 抛异常
        app.channels = [ch]
        return app

    def test_reschedules_after_when_decode_raises(self):
        # 核心：cv2.cvtColor 对异常形状帧抛异常（在触及任何 Tk 调用前）→ 续排 after 仍须执行、
        # 异常被记录，预览刷新循环不死。
        app = self._preview_app()
        with mock.patch.object(viewer.cv2, "cvtColor",
                               side_effect=RuntimeError("bad shape")), \
                mock.patch.object(viewer, "log") as mlog:
            app._panel_refresh_preview()
        self.assertEqual(len(app.panel.calls), 1)        # 续排 after 仍被调用（未永久停更）
        self.assertEqual(app.panel.calls[0][0], 200)
        self.assertTrue(mlog.warning.called)             # 异常被记录（非静默）


if __name__ == "__main__":
    unittest.main()
