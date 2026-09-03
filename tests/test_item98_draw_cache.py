# -*- coding: utf-8 -*-
"""第 98 条回归：绘制路径不得每帧重算/下发不变的位置与状态文本。

两处冗余（均与解码/贴图抢主线程和 DWM，60fps 下放大为微卡顿）：
1. `_show_frame` 每帧调 `_position_top_right()`（update_idletasks + winfo_width/height/
   screenwidth 查询 + geometry 下发），但窗口位置只取决于尺寸（=照片 display_width×new_h）；
   源分辨率稳定时每帧重复同一 geometry。修复=按 (display_width, new_h) 缓存，尺寸未变即跳过。
2. `_update_status_overlay` 被 poll（每 15ms）+ 每帧调用，无条件 `configure(text=...)`
   即使文本没变。修复=缓存上次文本，未变即跳过（沿用 `_show_text` 的 `_last_text` 同款范式）。

并验证 `_show_text`（画面→文字占位）作废位置缓存：窗口尺寸随内容改变，下一帧画面恢复时
必须重新定位，否则沿用照片尺寸旧位置而错位。

hermetic：`ViewerApp.__new__` 绕开真实 Tk/接收线程，cv2/Image/ImageTk 用桩替换，
label/status_overlay/_position_top_right 用记录型假对象。范式同 test_item91/92。
"""
import threading
import unittest
from unittest import mock

import viewer


class _FakeArr:
    def __init__(self, shape):
        self.shape = shape


class _FakeImg:
    def __init__(self, w, h):
        self.size = (w, h)

    def resize(self, size, *a):
        return _FakeImg(size[0], size[1])


class _FakePhoto:
    pass


class _FakeLabel:
    def __init__(self):
        self.configs = []

    def configure(self, **kw):
        self.configs.append(kw)


class _FakeOverlay:
    def __init__(self):
        self.texts = []

    def configure(self, **kw):
        self.texts.append(kw.get("text"))


class _FakeCh:
    def __init__(self, fps=0.0, lat=0.0, src="local"):
        self.fps = fps
        self._lat = lat
        self.watch_source = src
        self.lock = threading.Lock()

    def effective_latency_ms(self):
        return self._lat


def _draw_app():
    """构造只含绘制路径状态的 ViewerApp（不碰真实 Tk/线程/网络）。"""
    app = viewer.ViewerApp.__new__(viewer.ViewerApp)
    app.label = _FakeLabel()
    app.status_overlay = _FakeOverlay()
    app.display_width = 854
    app.active_idx = 0
    app.channels = []
    app.user_moved = False
    app.has_frame = False
    app._last_text = None
    app._photo = None
    app._last_frame_serial = -1
    app._last_channel_idx = -1
    app._last_pos_size = None
    app._last_status_text = None
    app.position_calls = []
    app._position_top_right = lambda: app.position_calls.append(1)
    app._update_status_overlay = lambda: None
    app._describe_source = lambda ch, src: "peer"
    return app


def _fake_pil():
    """返回替换 viewer.cv2/Image/ImageTk 的桩（fromarray 读 .shape，cvtColor 保形）。"""
    fake_cv2 = mock.MagicMock()
    fake_cv2.cvtColor = lambda arr, code: _FakeArr(arr.shape)
    fake_Image = mock.MagicMock()
    fake_Image.fromarray = lambda rgb: _FakeImg(rgb.shape[1], rgb.shape[0])
    fake_ImageTk = mock.MagicMock()
    fake_ImageTk.PhotoImage = lambda img: _FakePhoto()
    return fake_cv2, fake_Image, fake_ImageTk


class TestStatusOverlayCache(unittest.TestCase):
    def test_none_overlay_no_crash(self):
        app = _draw_app()
        app.status_overlay = None
        app._update_status_overlay = viewer.ViewerApp._update_status_overlay.__get__(app)
        app._update_status_overlay()  # 不应抛

    def test_same_text_configures_once(self):
        """文本不变时第二次跳过 configure（poll 每 15ms 多数周期文本相同）。"""
        app = _draw_app()
        real = viewer.ViewerApp._update_status_overlay.__get__(app)
        app.channels = []          # text 恒为 "-"
        real()
        real()
        real()
        self.assertEqual(app.status_overlay.texts, ["-"],
                         "文本未变却重复 configure")
        self.assertEqual(app._last_status_text, "-")

    def test_changed_text_configures_again(self):
        """文本变化时再次 configure（缓存不得吞掉真实更新）。"""
        app = _draw_app()
        real = viewer.ViewerApp._update_status_overlay.__get__(app)
        app.channels = []
        real()                     # "-"
        app.channels = [_FakeCh(fps=30, lat=50)]
        real()                     # "30fps · 50ms"
        real()                     # 跳过
        self.assertEqual(app.status_overlay.texts, ["-", "30fps · 50ms"])


class TestShowFramePositionCache(unittest.TestCase):
    def _show(self, app, shape, serial=1):
        cv2s, img, imgtk = _fake_pil()
        with mock.patch.object(viewer, "cv2", cv2s), \
                mock.patch.object(viewer, "Image", img), \
                mock.patch.object(viewer, "ImageTk", imgtk):
            viewer.ViewerApp._show_frame(app, _FakeArr(shape), serial)

    def test_first_frame_positions(self):
        app = _draw_app()
        self._show(app, (1080, 1920, 3))
        self.assertEqual(len(app.position_calls), 1, "首帧未定位")
        self.assertEqual(app._last_pos_size, (854, 480))

    def test_same_size_frames_skip_reposition(self):
        """同尺寸连续帧只定位一次（60fps 下省去每帧 update_idletasks+winfo+geometry）。"""
        app = _draw_app()
        self._show(app, (1080, 1920, 3), serial=1)
        self._show(app, (1080, 1920, 3), serial=2)
        self._show(app, (1080, 1920, 3), serial=3)
        self.assertEqual(len(app.position_calls), 1,
                         "同尺寸帧重复定位（缓存失效）")

    def test_different_output_size_repositions(self):
        """输出尺寸变化（源宽高比变）→ 重新定位。"""
        app = _draw_app()
        self._show(app, (1080, 1920, 3), serial=1)   # new_h=480
        self._show(app, (1080, 1440, 3), serial=2)   # new_h=640 → 不同
        self.assertEqual(len(app.position_calls), 2)
        self.assertEqual(app._last_pos_size, (854, 640))

    def test_same_output_size_from_different_source_skips(self):
        """不同源分辨率但缩放后输出尺寸相同 → 仍跳过（位置只取决于输出尺寸）。"""
        app = _draw_app()
        self._show(app, (1080, 1920, 3), serial=1)   # 854x480
        self._show(app, (720, 1280, 3), serial=2)    # 854x480（同比）
        self.assertEqual(len(app.position_calls), 1)

    def test_user_moved_never_positions(self):
        """用户拖动后（user_moved）不再自动定位，缓存也不应触发。"""
        app = _draw_app()
        app.user_moved = True
        self._show(app, (1080, 1920, 3), serial=1)
        self._show(app, (1080, 1440, 3), serial=2)
        self.assertEqual(app.position_calls, [])


class TestShowTextInvalidatesCache(unittest.TestCase):
    def _show_frame(self, app, shape, serial=1):
        cv2s, img, imgtk = _fake_pil()
        with mock.patch.object(viewer, "cv2", cv2s), \
                mock.patch.object(viewer, "Image", img), \
                mock.patch.object(viewer, "ImageTk", imgtk):
            viewer.ViewerApp._show_frame(app, _FakeArr(shape), serial)

    def test_text_then_frame_repositions(self):
        """画面→文字占位→画面恢复：文字作废缓存，恢复帧必须重新定位（否则错位）。"""
        app = _draw_app()
        self._show_frame(app, (1080, 1920, 3), serial=1)
        self.assertEqual(len(app.position_calls), 1)
        self.assertEqual(app._last_pos_size, (854, 480))
        # 同尺寸再来一帧：缓存生效 → 跳过定位
        self._show_frame(app, (1080, 1920, 3), serial=2)
        self.assertEqual(len(app.position_calls), 1, "同尺寸帧应跳过定位")
        # 切到文字占位：作废位置缓存（文字本身因尺寸不同也会定位一次）
        viewer.ViewerApp._show_text(app, "连接中…")
        self.assertIsNone(app._last_pos_size, "_show_text 未作废位置缓存")
        before = len(app.position_calls)
        # 同尺寸画面恢复：因缓存已作废，必须重新定位（而非沿用旧位置跳过）
        self._show_frame(app, (1080, 1920, 3), serial=3)
        self.assertEqual(len(app.position_calls), before + 1,
                         "文字占位后恢复帧未重新定位（会沿用旧位置错位）")


class TestRealSourceHasCache(unittest.TestCase):
    def test_show_frame_guards_position_with_size_cache(self):
        import inspect
        src = inspect.getsource(viewer.ViewerApp._show_frame)
        self.assertIn("_last_pos_size", src, "_show_frame 未用尺寸缓存守卫定位")
        self.assertIn("size_key", src)

    def test_status_overlay_guards_configure_with_text_cache(self):
        import inspect
        src = inspect.getsource(viewer.ViewerApp._update_status_overlay)
        self.assertIn("_last_status_text", src, "_update_status_overlay 未用文本缓存守卫 configure")

    def test_show_text_invalidates_position_cache(self):
        import inspect
        src = inspect.getsource(viewer.ViewerApp._show_text)
        self.assertIn("_last_pos_size = None", src,
                      "_show_text 未作废位置缓存（画面恢复会错位）")


if __name__ == "__main__":
    unittest.main()
