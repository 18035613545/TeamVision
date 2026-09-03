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

    def test_extreme_small_never_zero(self):
        # 第 88 条：极小 target_width / scale 曾使宽或高变 0，cv2.resize 抛异常
        # 直接打断 share.py 上传线程。任何输入下宽高恒 >=2 且为偶数。
        for src_w, src_h, scale, tw in [
            (2560, 1440, 1.0, 1),     # 压制下限 target_width=1
            (2560, 1440, 1.0, 3),     # 奇数压制值，向下对齐后仍 >=2
            (2560, 1440, 0.0, 0),     # scale=0
            (1920, 1080, 0.001, 0),   # 极小 scale
            (1, 1, 1.0, 1),           # 极小源
        ]:
            w, h = calc_output_size(src_w, src_h, scale, tw)
            self.assertGreaterEqual(w, 2, (src_w, src_h, scale, tw))
            self.assertGreaterEqual(h, 2, (src_w, src_h, scale, tw))
            self.assertEqual(w % 2, 0, (src_w, src_h, scale, tw))
            self.assertEqual(h % 2, 0, (src_w, src_h, scale, tw))


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


def _gate_step(state, changed, now, quiet_ms=0.6):
    """推进一步静止闸门状态机（(in_still, quiet_since) + 本帧是否有变化 + 时刻）。"""
    from host import still_gate_update
    return still_gate_update(state[0], state[1], changed, now, quiet_ms)


class TestStillGateUpdate(unittest.TestCase):
    """静止闸门状态机回归：连续无变化满 quiet_ms 才入静止；任何变化帧立即复位/恢复。

    quiet_ms 默认 0.6 = 原语义 still_frames(3) × 探测间隔(0.2s)，仅连续累计。
    """

    def test_change_resets_accumulation(self):
        # 打字等间歇内容：无变化帧不得跨“变化帧”累计入静止（原缺陷：still_hits 不重置）
        s = (False, None)
        s = _gate_step(s, True, 0.0)     # 击键
        s = _gate_step(s, False, 0.05)   # 短暂停顿
        s = _gate_step(s, True, 0.3)     # 再次击键：必须复位累计
        self.assertEqual(s, (False, None))
        s = _gate_step(s, False, 0.5)
        s = _gate_step(s, False, 0.8)    # 连续无变化仅 0.3s < 0.6s
        self.assertEqual(s, (False, 0.5))

    def test_true_still_enters_after_quiet_ms(self):
        s = (False, None)
        s = _gate_step(s, False, 0.0)
        s = _gate_step(s, False, 0.3)
        self.assertEqual(s, (False, 0.0))
        s = _gate_step(s, False, 0.6)    # 满 0.6s 连续无变化 → 静止
        self.assertTrue(s[0])

    def test_recovery_on_change(self):
        s = (True, 0.6)
        s = _gate_step(s, True, 0.9)
        self.assertEqual(s, (False, None))

    def test_unchanged_keeps_still(self):
        s = (True, 0.6)
        s = _gate_step(s, False, 1.2)
        self.assertTrue(s[0])

    def test_change_between_quiet_frames_no_still(self):
        # 变化帧夹在两个无变化帧之间不得入静止（对应 19:57 日志 ~0.55s 的“假静止”振荡）
        s = (False, None)
        s = _gate_step(s, False, 0.0)
        s = _gate_step(s, True, 0.3)
        s = _gate_step(s, False, 0.9)
        self.assertEqual(s, (False, 0.9))
        s = _gate_step(s, False, 1.4)    # 本次连续无变化 0.5s < 0.6s 仍不静止
        self.assertEqual(s, (False, 0.9))


class TestEncodeBgrTarget(unittest.TestCase):
    # 第 80 条：原测试只断言 len(jpeg)>100，从不解码核对分辨率——target_width
    # 这个主要省带宽特性整体失效（照样输出完整 2560x1440）也是绿灯。这里改为
    # 真正 cv2.imdecode 回读像素尺寸，并补上「关闭压制」「源本就小于上限」两个对照。
    def _decode_size(self, jpeg):
        import cv2
        arr = cv2.imdecode(
            np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(arr, "返回字节不是可解码的 JPEG")
        return arr.shape[1], arr.shape[0]  # (宽, 高)

    def test_encode_bgr_caps_size(self):
        from host import encode_bgr
        frame = _img(2560, 1440)
        jpeg, _ = encode_bgr(frame, 1.0, 80, target_width=854)
        self.assertGreater(len(jpeg), 100)  # 有效 JPEG 输出
        # 2K 源 @ scale1.0 被 target_width=854 压制：宽恰为 854，高按比例
        # round(1440*854/2560)=480，均偶数对齐。
        w, h = self._decode_size(jpeg)
        self.assertEqual(w, 854)
        self.assertEqual(h, 480)
        self.assertEqual(w % 2, 0)
        self.assertEqual(h % 2, 0)

    def test_encode_bgr_no_cap_when_disabled(self):
        from host import encode_bgr
        frame = _img(2560, 1440)
        # 负向对照：target_width=0 关闭压制，输出应保持完整源分辨率。
        jpeg, _ = encode_bgr(frame, 1.0, 80, target_width=0)
        self.assertEqual(self._decode_size(jpeg), (2560, 1440))

    def test_encode_bgr_below_threshold_unchanged(self):
        from host import encode_bgr
        frame = _img(640, 360)
        # 对照：源宽 640 < target_width 854，不触发压制，尺寸原样保留。
        jpeg, _ = encode_bgr(frame, 1.0, 80, target_width=854)
        self.assertEqual(self._decode_size(jpeg), (640, 360))


if __name__ == "__main__":
    unittest.main()
