# -*- coding: utf-8 -*-
"""静止检测与输出尺寸计算的单元测试（不依赖真实屏幕/编码器）。"""
import unittest

import numpy as np

from host import calc_output_size, downsample_frame, motion_changed


def _img(w=640, h=360, val=100):
    # 640x360 抽稀(step14)后约 45x25=1125 采样点，足够区分“局部小变化”与“区域变化”
    return np.full((h, w, 3), val, dtype=np.uint8)


class TestCalcOutputSize(unittest.TestCase):
    def test_scale_only(self):
        # 未超 target_width（640 < 854）：只按 scale 缩放
        w, h = calc_output_size(2560, 1440, 0.25, 854)
        self.assertEqual((w, h), (640, 360))

    def test_target_cap(self):
        # 2K @ scale1.0 被 target_width 854 压制，保持宽高比且偶数对齐
        w, h = calc_output_size(2560, 1440, 1.0, 854)
        self.assertEqual(w, 854)
        self.assertEqual(h, 480)
        self.assertEqual(w % 2, 0)
        self.assertEqual(h % 2, 0)

    def test_ratio_preserved(self):
        # 1080p 源压制到 854：h = round(1080*854/1920)=480
        w, h = calc_output_size(1920, 1080, 1.0, 854)
        self.assertEqual((w, h), (854, 480))

    def test_disabled(self):
        # target_width=0 关闭压制
        w, h = calc_output_size(2560, 1440, 1.0, 0)
        self.assertEqual((w, h), (2560, 1440))


class TestMotionChanged(unittest.TestCase):
    def test_first_frame_changed(self):
        # 无参考帧：必须判为有变化（错误取向=多发）
        cur = downsample_frame(_img())
        changed, ref = motion_changed(None, cur)
        self.assertTrue(changed)

    def test_static_still(self):
        ref = downsample_frame(_img())
        cur = downsample_frame(_img())
        changed, _ = motion_changed(ref, cur)
        self.assertFalse(changed)

    def test_tiny_region_ignored(self):
        # 时钟秒针级微小区域变化：仅覆盖 1 个采样点（0.09%）→ 静止
        a = _img(); b = _img()
        b[13:16, 13:15] = 200
        changed, _ = motion_changed(downsample_frame(a), downsample_frame(b))
        self.assertFalse(changed)

    def test_local_region_triggered(self):
        # 游戏小地图级局部变化（100x100px ≈ 4.4% 采样点）：判有变化
        a = _img(); b = _img()
        b[100:200, 100:200] = 200
        changed, _ = motion_changed(downsample_frame(a), downsample_frame(b))
        self.assertTrue(changed)

    def test_full_change(self):
        a = _img(); b = _img(val=50)
        changed, _ = motion_changed(downsample_frame(a), downsample_frame(b))
        self.assertTrue(changed)

    def test_shape_change_fails_open(self):
        changed, _ = motion_changed(downsample_frame(_img(64, 36)), downsample_frame(_img(32, 18)))
        self.assertTrue(changed)


if __name__ == "__main__":
    unittest.main()
