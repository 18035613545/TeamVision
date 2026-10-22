# -*- coding: utf-8 -*-
"""第 9 轮（观看端体验）：悬浮窗布局预设——"大宽屏 / 标准 / 小地图"一键切换。

只钉**纯几何函数**与**应用顺序**，不构造 ViewerApp：
- fit_display_width 的边界（屏幕太窄、非法值、余量扣除）；
- 预设表本身自洽（每档都在合法范围内、展示名不重复、「标准」= DEFAULT_CONFIG）；
- apply_layout_preset 的落点：只改 width/alpha 两项，并走 set_* 的既有通道
  （去抖落盘 + 即时重绘），不自己去写 cfg——否则又会出现"改了不存/存了不生效"。
"""

import unittest
from unittest import mock

import common
import viewer


class TestFitDisplayWidth(unittest.TestCase):

    def test_uses_available_room(self):
        # 请求值不被抬高：1920 - 400 余量 = 1520 可用，400 在可用范围内 → 原样
        self.assertEqual(viewer.fit_display_width(1920, 400), 400)
        self.assertEqual(viewer.fit_display_width(1920, viewer.WIDTH_MAX + 100),
                         viewer.WIDTH_MAX)
        # 可用宽度不足上限时按可用宽度取（1280 - 400 = 880）
        self.assertEqual(viewer.fit_display_width(1280, viewer.WIDTH_MAX), 880)

    def test_never_exceeds_preferred(self):
        self.assertEqual(viewer.fit_display_width(3840, 400), 400)

    def test_never_goes_below_floor(self):
        self.assertEqual(viewer.fit_display_width(1920, 500), 500)
        self.assertEqual(viewer.fit_display_width(900, 800), 500)
        self.assertEqual(viewer.fit_display_width(100, 800), viewer.WIDTH_MIN)

    def test_junk_input_falls_back_to_default(self):
        self.assertEqual(viewer.fit_display_width(None, 400), 400)
        self.assertEqual(viewer.fit_display_width(1920, "abc"), 480)
        self.assertEqual(viewer.fit_display_width(1920, -5), viewer.WIDTH_MIN)


class TestPresets(unittest.TestCase):

    def test_every_preset_is_valid(self):
        self.assertIn("standard", viewer.LAYOUT_PRESETS)
        for key, (label, width, alpha) in viewer.LAYOUT_PRESETS.items():
            self.assertTrue(label, key)
            self.assertIsInstance(width, int)
            self.assertGreaterEqual(width, viewer.WIDTH_MIN, key)
            self.assertLessEqual(width, viewer.WIDTH_MAX, key)
            self.assertGreaterEqual(alpha, viewer.ALPHA_MIN, key)
            self.assertLessEqual(alpha, viewer.ALPHA_MAX, key)

    def test_labels_unique(self):
        labels = [v[0] for v in viewer.LAYOUT_PRESETS.values()]
        self.assertEqual(len(labels), len(set(labels)))


class TestApplyLayoutPreset(unittest.TestCase):

    def _app(self):
        app = object.__new__(viewer.ViewerApp)
        app.set_display_width = mock.Mock()
        app.set_alpha = mock.Mock()
        return app

    def test_applies_width_then_alpha(self):
        app = self._app()
        ok = app.apply_layout_preset("minimap", screen_w=1920)
        self.assertTrue(ok)
        label, width, alpha = viewer.LAYOUT_PRESETS["minimap"]
        app.set_display_width.assert_called_once_with(width)
        app.set_alpha.assert_called_once_with(alpha)

    def test_unknown_preset_is_noop(self):
        app = self._app()
        self.assertFalse(app.apply_layout_preset("nope"))
        app.set_display_width.assert_not_called()
        app.set_alpha.assert_not_called()

    def test_width_clamped_to_screen(self):
        app = self._app()
        app.apply_layout_preset("wide", screen_w=1000)
        # 1000 - 400 = 600 可用 → 960 的「大宽屏」被夹到 600
        self.assertEqual(app.set_display_width.call_args[0][0], 600)
        app2 = self._app()
        app2.apply_layout_preset("wide", screen_w=500)
        self.assertEqual(app2.set_display_width.call_args[0][0], viewer.WIDTH_MIN)

    def test_alpha_never_reaches_full_screen_cloak(self):
        # Windows 上 alpha=0 会让整窗点击穿透且不可见，滑块下界就是 30%——
        # 预设也必须守住同一条线，否则"一键小地图"能把悬浮窗变没
        app = self._app()
        app.apply_layout_preset("minimap", screen_w=1920)
        self.assertGreaterEqual(app.set_alpha.call_args[0][0], viewer.ALPHA_MIN)

    def test_standard_preset_matches_defaults(self):
        # 「标准」= 出厂形态：与 DEFAULT_CONFIG.viewer 的默认值同源，改过配置的用户
        # 点一下就能回到默认，不被这里另写的数字带偏
        _, width, alpha = viewer.LAYOUT_PRESETS["standard"]
        default = common.DEFAULT_CONFIG["viewer"]
        self.assertEqual(width, default["display_width"])
        self.assertEqual(alpha, default["alpha"])

    def test_setters_share_preset_bounds(self):
        # 第 9 轮把四处字面量收成常量：setter 的夹紧必须与滑块/预设同一条线，
        # 否则手改配置（或旧版写回的 0）仍能绕过下界
        app = object.__new__(viewer.ViewerApp)
        app._schedule_save = mock.Mock()
        app._refresh_overlay = mock.Mock()
        app.root = mock.Mock()
        app.cfg = {"viewer": {}}
        app.set_display_width(1)
        self.assertEqual(app.display_width, viewer.WIDTH_MIN)
        app.set_display_width(99999)
        self.assertEqual(app.display_width, viewer.WIDTH_MAX)
        app.set_alpha(0.0)
        self.assertEqual(app.alpha, viewer.ALPHA_MIN)
        app.set_alpha(5.0)
        self.assertEqual(app.alpha, viewer.ALPHA_MAX)


if __name__ == "__main__":
    unittest.main()
