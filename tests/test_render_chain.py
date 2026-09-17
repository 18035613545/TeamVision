# -*- coding: utf-8 -*-
"""阶段 A1（优化方案 2026-09-17）回归：绘制链必须"先缩放、后转换"。

旧链对全分辨率帧做 cvtColor，再把结果交给 PIL resize——转换输出的 99.9% 随即被丢弃
（实测 1080p→854px 每帧 9.1ms，新链 3.5ms）。这里锁定：
1. 调用顺序 resize → cvtColor（主画面与面板预览两处）；
2. resize 的目标尺寸（主画面宽=display_width 等比；面板预览 320×165 封顶）；
3. resize 用 INTER_AREA（下采样抗锯齿，画质不低于原 PIL.BILINEAR）；
4. cvtColor 收到的正是 resize 的返回值（先缩放后转换，无整帧中转）；
5. 退化帧（0 宽/高）提前返回，不调用任何缩放/转换。

hermetic：ViewerApp.__new__ + 记录型桩替换 viewer.cv2/Image/ImageTk（同 test_item98 范式）。
"""
import threading
import unittest
from unittest import mock

import cv2 as real_cv2

import viewer


class _Arr:
    def __init__(self, shape):
        self.shape = shape


class _RecordingCv2:
    """记录 resize/cvtColor 调用（含顺序与实参），返回保形数组。"""

    INTER_AREA = real_cv2.INTER_AREA
    COLOR_BGR2RGB = real_cv2.COLOR_BGR2RGB

    def __init__(self):
        self.calls = []
        self.resize_return = None

    def resize(self, arr, size, interpolation=None):
        self.calls.append(("resize", size, interpolation))
        self.resize_return = _Arr((size[1], size[0]) + tuple(arr.shape[2:]))
        return self.resize_return

    def cvtColor(self, arr, code):
        self.calls.append(("cvtColor", arr, code))
        return arr


def _frames():
    fake_cv2 = _RecordingCv2()
    fake_Image = mock.MagicMock()
    fake_Image.fromarray = lambda rgb: _Arr(rgb.shape)
    fake_ImageTk = mock.MagicMock()
    fake_ImageTk.PhotoImage = lambda img: mock.MagicMock()
    return fake_cv2, fake_Image, fake_ImageTk


def _draw_app():
    app = viewer.ViewerApp.__new__(viewer.ViewerApp)
    app.label = mock.MagicMock()
    app.display_width = 854
    app.active_idx = 0
    app.channels = []
    app.user_moved = True          # 跳过定位路径，聚焦绘制链本身
    app.has_frame = False
    app._last_text = None
    app._photo = None
    app._last_frame_serial = -1
    app._last_channel_idx = -1
    app._last_pos_size = None
    app._last_status_text = None
    app._update_status_overlay = lambda: None
    app._position_top_right = lambda: None
    return app


def _panel_app(last_frame):
    app = viewer.ViewerApp.__new__(viewer.ViewerApp)
    app.running = True
    app.panel = mock.MagicMock()
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
    ch.auth_msg = ""
    ch.frame_serial = 1
    ch.last_frame = last_frame
    app.channels = [ch]
    return app


class TestShowFrameResizeFirst(unittest.TestCase):
    def test_resize_before_convert_with_target_size(self):
        app = _draw_app()
        cv2s, img, imgtk = _frames()
        with mock.patch.object(viewer, "cv2", cv2s), \
                mock.patch.object(viewer, "Image", img), \
                mock.patch.object(viewer, "ImageTk", imgtk):
            viewer.ViewerApp._show_frame(app, _Arr((1080, 1920, 3)), serial=1)

        self.assertEqual([c[0] for c in cv2s.calls], ["resize", "cvtColor"],
                         "绘制链必须先缩放、后转换")
        _, size, interp = cv2s.calls[0]
        self.assertEqual(size, (854, 480), "resize 目标尺寸应为 display_width×等比高")
        self.assertIs(interp, real_cv2.INTER_AREA, "下采样应用 INTER_AREA（抗锯齿）")
        self.assertIs(cv2s.calls[1][1], cv2s.resize_return,
                      "cvtColor 应直接消费缩放结果（先缩放后转换）")

    def test_degenerate_frame_skips_resize_and_convert(self):
        app = _draw_app()
        cv2s, img, imgtk = _frames()
        with mock.patch.object(viewer, "cv2", cv2s), \
                mock.patch.object(viewer, "Image", img), \
                mock.patch.object(viewer, "ImageTk", imgtk):
            viewer.ViewerApp._show_frame(app, _Arr((0, 1920, 3)), serial=1)   # 高 0
            viewer.ViewerApp._show_frame(app, _Arr((1080, 0, 3)), serial=2)   # 宽 0

        self.assertEqual(cv2s.calls, [], "退化帧不得进入缩放/转换")
        self.assertFalse(app.has_frame)


class TestPanelPreviewResizeFirst(unittest.TestCase):
    def test_resize_before_convert_with_cap(self):
        app = _panel_app(_Arr((1080, 1920, 3)))
        cv2s, img, imgtk = _frames()
        with mock.patch.object(viewer, "cv2", cv2s), \
                mock.patch.object(viewer, "Image", img), \
                mock.patch.object(viewer, "ImageTk", imgtk):
            app._panel_refresh_preview()

        self.assertEqual([c[0] for c in cv2s.calls], ["resize", "cvtColor"],
                         "预览链必须先缩放、后转换")
        _, size, interp = cv2s.calls[0]
        # 1080p：先按宽 320 得高 180 > 165，改按高 165 收缩 → 293×165（等比）
        self.assertEqual(size, (293, 165))
        self.assertIs(interp, real_cv2.INTER_AREA)
        self.assertIs(cv2s.calls[1][1], cv2s.resize_return,
                      "cvtColor 应直接消费缩放结果（先缩放后转换）")

    def test_no_resize_when_frame_unchanged(self):
        """serial 未变时跳过整条绘制链（C4 既有语义不得被本次改动破坏）。"""
        app = _panel_app(_Arr((1080, 1920, 3)))
        app._panel_last_serial = 1      # 与 ch.frame_serial 相同 → changed=False
        app._panel_last_idx = 0
        cv2s, img, imgtk = _frames()
        with mock.patch.object(viewer, "cv2", cv2s), \
                mock.patch.object(viewer, "Image", img), \
                mock.patch.object(viewer, "ImageTk", imgtk):
            app._panel_refresh_preview()

        self.assertEqual(cv2s.calls, [], "帧未变化时不得重绘")


if __name__ == "__main__":
    unittest.main()
