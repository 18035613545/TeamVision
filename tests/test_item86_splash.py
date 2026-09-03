# -*- coding: utf-8 -*-
"""第 86 条回归：启动画面配置中途抛异常时必须销毁已建窗口，不留关不掉的幽灵窗。

hermetic：用假 root / 假 Toplevel + patch _build/_center，不依赖真实显示。
"""
import unittest
from unittest import mock

import splash


class _FakeWin(object):
    """假 Toplevel：记录 destroy 是否被调用；其余配置方法为 no-op。"""

    def __init__(self):
        self.destroyed = False

    def overrideredirect(self, *a):
        pass

    def attributes(self, *a):
        pass

    def destroy(self):
        self.destroyed = True


class TestSplashFailSafe(unittest.TestCase):

    def test_show_on_root_destroys_window_when_build_raises(self):
        # 负向：_build 抛异常（旧代码 except: pass 吞掉、窗口不销毁 → 幽灵窗）
        fake = _FakeWin()
        fake_root = mock.Mock()
        with mock.patch.object(splash.tk, "Toplevel", return_value=fake), \
                mock.patch.object(splash, "_build", side_effect=RuntimeError("boom")):
            splash._show_on_root(fake_root)
        self.assertTrue(
            fake.destroyed,
            "配置抛异常时 Toplevel 未被销毁 → 会留下关不掉（无边框/置顶/无关闭按钮）的幽灵窗口")
        # _build 在 root.after 之前抛，故不应排定定时销毁
        fake_root.after.assert_not_called()

    def test_show_on_root_schedules_destroy_on_success(self):
        # 正向：成功路径不立即销毁，而是排定 SPLASH_MS 后由 win.destroy 销毁
        fake = _FakeWin()
        fake_root = mock.Mock()
        with mock.patch.object(splash.tk, "Toplevel", return_value=fake), \
                mock.patch.object(splash, "_build"), \
                mock.patch.object(splash, "_center"):
            splash._show_on_root(fake_root)
        self.assertFalse(fake.destroyed, "成功路径不应立即销毁（应由 after 定时销毁）")
        fake_root.after.assert_called_once()
        args = fake_root.after.call_args[0]
        self.assertEqual(args[0], splash.SPLASH_MS)
        # 回调即 win.destroy（绑定方法每次属性访问生成新对象，用 == 而非 is 比较）
        self.assertEqual(args[1], fake.destroy)


if __name__ == "__main__":
    unittest.main()
