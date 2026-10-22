# -*- coding: utf-8 -*-
"""第 11 轮（画质/延迟）：分辨率档位切换不再"每帧同步拆装一次编码器"。

背景：`codec.VideoEncoder._encode_locked` 里的"尺寸不符就地重建"是锁内同步的，
一次 1080p→4K 或缩放台阶变化会让**每一帧**都付一次完整上下文构造（硬件编码器
93–110ms，而 30fps 一帧预算只有 33ms）；构造失败时旧上下文已按新尺寸落账，
码流尺寸与实际画幅对不上。`pipeline.FrameEncoder` 则从不主动把尺寸变化告诉编码器。

本组用例用假编码器替身（记录 set_size 调用）钉住 pipeline 侧的契约：
首次构造按尺寸建、变化时调用 set_size、成功落账、失败按退避而不是每帧重试。
以及 codec 侧 set_size 的真实行为（用 PyAV 的 libx264 软编做端到端，缺 PyAV 时跳过）。
"""

import unittest
from unittest import mock

import pipeline
import screen

try:
    import av  # noqa: F401
    _HAS_AV = True
except Exception:
    _HAS_AV = False


class _FakeEncoder:
    """pipeline 用到的编码器最小面：available / set_size / set_fps / set_bitrate / encode。"""

    def __init__(self, ok=True):
        self.available = True
        self.size_ok = ok
        self.size_calls = []
        self.encoded_shapes = []
        self.codec_id = 1
        self.bitrate = 1000
        self.keyframe_sent = 0

    def set_size(self, w, h):
        self.size_calls.append((w, h))
        if self.size_ok:
            return True
        raise RuntimeError("nvenc open failed")

    def set_fps(self, fps):
        return True

    def set_bitrate(self, bps, hysteresis=0.15):
        return False

    def encode(self, bgr, raw_ts=None, force_key=False):
        self.encoded_shapes.append(bgr.shape[:2])
        return b"nal", force_key, 0.5, raw_ts or 0

    def close(self):
        pass


def _frame(w, h):
    import numpy as np
    return np.zeros((h, w, 3), dtype=np.uint8)


class TestPipelineSizeAccounting(unittest.TestCase):

    def _enc(self, fake):
        fe = pipeline.FrameEncoder(fps=30, bitrate=1000, keyint=60,
                                   encoder_sel="nvenc", name="t")
        fe._encoder = fake
        fe._enc_fps = 30
        return fe

    def test_first_construction_registers_size(self):
        fake = _FakeEncoder()
        with mock.patch("codec.VideoEncoder", return_value=fake):
            fe = pipeline.FrameEncoder(fps=30, bitrate=1000, keyint=60,
                                       encoder_sel="nvenc", name="t")
            fe._ensure_encoder(640, 360, 30, 1000)
        self.assertEqual((fe._enc_w, fe._enc_h), (640, 360))
        self.assertEqual(fake.size_calls, [])

    def test_size_change_calls_set_size_once(self):
        fake = _FakeEncoder()
        fe = self._enc(fake)
        fe._enc_w, fe._enc_h = 640, 360
        fe._ensure_encoder(1920, 1080, 30, 1000)
        fe._ensure_encoder(1920, 1080, 30, 1000)   # 已落账：不应再调
        self.assertEqual(fake.size_calls, [(1920, 1080)])
        self.assertEqual((fe._enc_w, fe._enc_h), (1920, 1080))

    def test_set_size_failure_backs_off_instead_of_every_frame(self):
        fake = _FakeEncoder(ok=False)
        fe = self._enc(fake)
        fe._enc_w, fe._enc_h = 640, 360
        with mock.patch.object(pipeline.time, "monotonic", return_value=1000.0):
            for _ in range(30):
                fe._ensure_encoder(1920, 1080, 30, 1000)
        # 退避窗口内只尝试一次；失败的尺寸不落账
        self.assertEqual(len(fake.size_calls), 1)
        self.assertEqual((fe._enc_w, fe._enc_h), (640, 360))
        self.assertGreater(fe._size_backoff, 1.0)
        # 窗口到期后仍会重试这一档（不退化）
        with mock.patch.object(pipeline.time, "monotonic", return_value=2000.0):
            fe._ensure_encoder(1920, 1080, 30, 1000)
        self.assertEqual(len(fake.size_calls), 2)

    def test_size_backoff_resets_across_instances(self):
        fake1 = _FakeEncoder(ok=False)
        fe = self._enc(fake1)
        fe._enc_w, fe._enc_h = 640, 360
        with mock.patch.object(pipeline.time, "monotonic", return_value=1000.0):
            fe._ensure_encoder(1920, 1080, 30, 1000)
        self.assertGreater(fe._size_backoff, 1.0)
        fake2 = _FakeEncoder()
        fe._drop_encoder()
        fe._encoder = fake2
        fe._enc_w, fe._enc_h = 640, 360   # 新实例仍按新尺寸重建
        fe._ensure_encoder(1920, 1080, 30, 1000)
        self.assertEqual(fake2.size_calls, [(1920, 1080)])
        self.assertEqual(fe._size_backoff, 1.0)

    def test_size_reset_when_encoder_dropped(self):
        # 失败实例被丢弃后：尺寸记录一并作废，下一帧的退避不再按旧尺寸再缩/丢帧
        fake = _FakeEncoder(ok=False)
        fe = self._enc(fake)
        fe._enc_w, fe._enc_h = 1920, 1080
        out = fe.encode(_frame(320, 180), 1)        # set_size 失败 → 丢帧、不触编码
        self.assertIsNone(out)
        fe._drop_encoder()
        fe._ensure_encoder(640, 360, 30, 1000)      # 走重建（此处构造失败，仍在退避）
        self.assertIn((fe._enc_w, fe._enc_h), ((0, 0), (640, 360)))
        self.assertNotEqual((fe._enc_w, fe._enc_h), (1920, 1080))

    def test_encode_path_triggers_set_size(self):
        fake = _FakeEncoder()
        fe = self._enc(fake)
        fe._enc_w, fe._enc_h = 640, 360
        target = screen.calc_output_size(1920, 1080, 1.0, fe._target_width)
        out = fe.encode(_frame(1920, 1080), 123)
        self.assertIsNotNone(out)
        self.assertEqual(fake.size_calls, [target])
        self.assertEqual((out.w, out.h), target)

    def test_set_size_failure_keeps_encoder_size(self):
        # set_size 失败（退避中）：帧按编码器**实际**尺寸送出，EncodedFrame 也报实际尺寸，
        # 观看端不会按新尺寸的画幅去解旧尺寸的码流
        fake = _FakeEncoder(ok=False)
        fe = self._enc(fake)
        fe._enc_w, fe._enc_h = 640, 360
        out = fe.encode(_frame(1920, 1080), 123)
        self.assertIsNotNone(out)
        self.assertEqual(fake.encoded_shapes, [(360, 640)])
        self.assertEqual((out.w, out.h), (640, 360))

    def test_set_size_failure_on_small_frame_drops_frame(self):
        # 编码器仍在大尺寸、失败退避中，而新帧比它小：上采样毫无信息，直接丢帧
        fake = _FakeEncoder(ok=False)
        fe = self._enc(fake)
        fe._enc_w, fe._enc_h = 1920, 1080
        self.assertIsNone(fe.encode(_frame(320, 180), 123))
        self.assertEqual(fake.encoded_shapes, [])


@unittest.skipUnless(_HAS_AV, "需要 PyAV")
class TestCodecSetSizeReal(unittest.TestCase):

    def test_rebuild_to_new_size_and_rollback_on_failure(self):
        import codec
        enc = codec.VideoEncoder(320, 180, 30, 800, 30, "x264")
        if not enc.available:
            self.skipTest("libx264 不可用")
        if enc.name not in ("libx264", "libx265"):
            self.skipTest("本机选了硬件编码器（%s），不在此做软编端到端" % enc.name)
        try:
            self.assertTrue(enc.set_size(160, 90))
            self.assertEqual((enc._w, enc._h), (160, 90))
            res = enc.encode(_frame(160, 90), 1, force_key=True)
            self.assertIsNotNone(res)
            # 构造失败的路径：回滚尺寸，保留旧上下文
            with mock.patch.object(codec.VideoEncoder, "_create_ctx",
                                   side_effect=RuntimeError("boom")):
                self.assertFalse(enc.set_size(320, 180))
            self.assertEqual((enc._w, enc._h), (160, 90))
            self.assertTrue(enc.available)
        finally:
            enc.close()


if __name__ == "__main__":
    unittest.main()
