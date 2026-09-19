# -*- coding: utf-8 -*-
"""第 136 条（实现方案 §8 C3）：UI 基建单点 ui_common 的回归。

覆盖：
1) install_excepthooks：线程钩子写日志不弹窗、SystemExit 跳过；主钩子
   KeyboardInterrupt 透传、弹窗带父窗（get_root）、弹窗失败退回 print；
2) post_to_ui：正常走 root.after(0)，投递异常就地执行；
3) 色板单一来源：锁定 10 个历史取值，且 viewer/host_ui 不得再自定义 COL_* 字面量；
4) build_style：clam + 基础控件 + Panel.* 并集的实际生效值（需真实 Tk，缺失则跳过）。
"""
import ast
import inspect
import io
import os
import sys
import threading
import unittest
from contextlib import redirect_stdout
from unittest import mock

import ui_common


def _install_and_capture():
    """安装共享钩子，返回 (旧 sys 钩子, 旧 threading 钩子) 供 finally 还原。"""
    return sys.excepthook, threading.excepthook


class _Args:
    exc_type = ValueError
    exc_value = ValueError("boom")
    exc_traceback = None
    thread = None


class TestInstallExcepthooks(unittest.TestCase):

    def setUp(self):
        self._old = _install_and_capture()
        ui_common.install_excepthooks("TestApp")

    def tearDown(self):
        sys.excepthook, threading.excepthook = self._old

    def test_thread_hook_logs_without_dialog(self):
        with mock.patch("tkinter.messagebox.showerror") as mb:
            threading.excepthook(_Args())
        mb.assert_not_called()

    def test_thread_hook_skips_systemexit(self):
        class _Exit(_Args):
            exc_type = SystemExit
        with mock.patch.object(ui_common.log, "error") as err:
            threading.excepthook(_Exit())
        err.assert_not_called()

    def test_main_hook_passes_keyboardinterrupt(self):
        recorder = []
        original = sys.__excepthook__
        try:
            sys.__excepthook__ = lambda *a: recorder.append(a)
            sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
        finally:
            sys.__excepthook__ = original
        self.assertEqual(len(recorder), 1)

    def test_main_hook_shows_dialog_with_parent(self):
        class _Root:
            def winfo_exists(self):
                return True
        root = _Root()
        ui_common.install_excepthooks("TestApp", get_root=lambda: root)
        with mock.patch("tkinter.messagebox.showerror") as mb:
            sys.excepthook(ValueError, ValueError("x"), None)
        mb.assert_called_once()
        self.assertIs(mb.call_args.kwargs.get("parent"), root)
        self.assertEqual(mb.call_args.args[0], "TestApp")

    def test_main_hook_broken_get_root_still_dialogs(self):
        def _boom():
            raise RuntimeError("tk 已销毁")
        ui_common.install_excepthooks("TestApp", get_root=_boom)
        with mock.patch("tkinter.messagebox.showerror") as mb:
            sys.excepthook(ValueError, ValueError("x"), None)
        mb.assert_called_once()  # 父窗拿不到不影响弹窗

    def test_main_hook_dialog_failure_falls_back_to_print(self):
        buf = io.StringIO()
        with mock.patch("tkinter.messagebox.showerror", side_effect=RuntimeError):
            with redirect_stdout(buf):
                sys.excepthook(ValueError, ValueError("x"), None)
        self.assertIn("程序遇到错误", buf.getvalue())


class TestPostToUi(unittest.TestCase):

    def test_schedules_via_after(self):
        calls = []

        class _Root:
            def after(self, delay, fn):
                calls.append((delay, fn))
        fn = lambda: None
        ui_common.post_to_ui(_Root(), fn)
        self.assertEqual(calls, [(0, fn)])

    def test_no_inline_execution_on_failure(self):
        """ui-004（2026-09-18）：投递失败**不得**在工作线程就地执行控件回调。"""
        class _DeadRoot:
            def after(self, delay, fn):
                raise RuntimeError("Tcl 解释器已销毁")
        hit = []
        ui_common.post_to_ui(_DeadRoot(), lambda: hit.append(1))   # 不得抛异常
        self.assertEqual(hit, [], "窗口已销毁时回调被就地执行（工作线程碰 Tk）")


class TestPaletteSingleSource(unittest.TestCase):

    _LOCKED = {
        "COL_BG": "#14141f", "COL_CARD": "#1e1e2e", "COL_CTRL": "#2a2a3c",
        "COL_FG": "#e6e6ef", "COL_DIM": "#9a9ab0", "COL_ACCENT": "#7c5cff",
        "COL_ONLINE": "#4ade80", "COL_CONNECTING": "#fbbf24",
        "COL_OFFLINE": "#f87171", "COL_ACTIVE_ROW": "#2a2440",
    }

    def test_locked_values(self):
        for name, value in self._LOCKED.items():
            self.assertEqual(getattr(ui_common, name), value, name)

    def test_consumers_do_not_redefine_palette(self):
        # C3 收拢后 viewer/host_ui 只能 import 色板，不得再出现 COL_* = "字面量"
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for mod in ("viewer", "host_ui"):
            with open(os.path.join(repo, mod + ".py"), encoding="utf-8") as f:
                src = f.read()
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Assign):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name) and tgt.id in self._LOCKED:
                            self.fail("%s 仍在本地定义 %s，色板单一来源在 ui_common"
                                      % (mod, tgt.id))


class TestBuildStyle(unittest.TestCase):
    """需要真实 Tk 解释器；无显示环境（CI）自动跳过。"""

    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        try:
            cls.root = tk.Tk()
        except Exception as e:
            raise unittest.SkipTest("无法创建 Tk 根窗口: %s" % e)
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "root", None) is not None:
            cls.root.destroy()

    def test_union_of_host_and_viewer_styles(self):
        style = ui_common.build_style(self.root)
        self.assertEqual(style.theme_use(), "clam")
        # host 基础控件块
        self.assertEqual(style.lookup("Treeview", "fieldbackground"), ui_common.COL_CTRL)
        self.assertEqual(style.lookup("Treeview.Heading", "background"), ui_common.COL_CARD)
        # viewer Panel.* 块
        self.assertEqual(style.lookup("Panel.TFrame", "background"), ui_common.COL_BG)
        self.assertEqual(style.lookup("Panel.Treeview", "fieldbackground"), ui_common.COL_CARD)
        self.assertEqual(style.lookup("Accent.TButton", "background"), ui_common.COL_ACCENT)

    def test_palette_override(self):
        style = ui_common.build_style(self.root, palette={"bg": "#ff0000"})
        self.assertEqual(style.lookup("Panel.TFrame", "background"), "#ff0000")
        ui_common.build_style(self.root)  # 还原默认，避免污染同类测试


if __name__ == "__main__":
    unittest.main()
