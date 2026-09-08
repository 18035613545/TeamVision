# -*- coding: utf-8 -*-
"""阶段 2 回归：第 110 条 —— 编码/回退逻辑统一到 pipeline.FrameEncoder。

背景（优化方案-2026-09-08.md 阶段 2 第 1 项）：host.py 与 share.py 各自维护一份
"编码器生命周期 + JPEG 回退"逻辑，并且已经漂移：

- share 有第 53 条的"构造失败负缓存退避"，host 没有 —— 编码器不可用（NVENC/x264 都
  打不开、GPU 驱动异常）时 host 的 `_encode_loop` 会**每帧**新建一次 VideoEncoder
  （4 次 av.Codec 查找 + 4 次 open + 最多 4 条告警，30 帧/秒），把 CPU 与日志打满且
  永不恢复；
- 两侧的"编码器失效自动重建"、静止期强制 IDR 的错误处理各不相同。

本测试锁定统一后的不变量：
1. 构造失败进入指数退避（1→2→4…→30s 封顶），退避期内不重复构造，期间走 JPEG 回退；
2. 编码器可用时只构造一次、按帧率变化调 set_fps；
3. 编码/回退异常不外抛（第 90 条：不得杀死编码线程）；
4. host 与 share 都只经 pipeline.FrameEncoder 使用编码器，不再各自实现。
"""
import inspect
import unittest
from unittest import mock

import numpy as np

import host
import pipeline
import share


def _frame(w=64, h=48):
    return np.zeros((h, w, 3), np.uint8)


class _DeadEncoder:
    """构造永远"成功"但不可用的编码器（模拟 NVENC/x264 全部打不开）。"""

    def __init__(self, *args, **kwargs):
        _DeadEncoder.calls.append(args)
        self.available = False
        self.name = "dead"

    calls = []

    def close(self):
        pass


class _LiveEncoder:
    """可用编码器替身：记录 encode/set_fps/set_bitrate 调用。"""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.available = True
        self.name = "fake"
        self.codec_id = 1
        self.bitrate = kwargs.get("bitrate", args[3] if len(args) > 3 else 0)
        self.keyframe_sent = 0
        self.encoded = []
        self.fps_calls = []
        self.closed = False

    def set_fps(self, fps):
        self.fps_calls.append(fps)

    def set_bitrate(self, bps):
        self.bitrate = bps
        return True

    def encode(self, frame, raw_ts=0, force_key=False):
        self.encoded.append((raw_ts, force_key))
        self.keyframe_sent += 1 if force_key else 0
        return (b"nal", bool(force_key), 1.5, raw_ts)

    def close(self):
        self.closed = True


class TestEncoderBackoff(unittest.TestCase):
    """构造失败负缓存：退避期内绝不重复构造（host 此前每帧都重建）。"""

    def test_failed_construction_is_negatively_cached(self):
        now = [0.0]
        _DeadEncoder.calls = []
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _DeadEncoder), \
                mock.patch.object(pipeline.time, "monotonic", lambda: now[0]):
            fe = pipeline.FrameEncoder(width=64, height=48, fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="auto")
            for i in range(10):
                ef = fe.encode(_frame(), i)
                self.assertIsNotNone(ef, "回退 JPEG 失败，本帧未产出")
                self.assertFalse(ef.is_video, "编码器不可用时应走 JPEG 回退")
            self.assertEqual(len(_DeadEncoder.calls), 1,
                             "退避期内重复构造了编码器（第 53 条：每帧新建会打满 CPU）")
            # 首次退避 1.0 秒到期后允许再试一次，然后翻倍到 2.0 秒
            now[0] = 1.0
            fe.encode(_frame(), 11)
            self.assertEqual(len(_DeadEncoder.calls), 2)
            now[0] = 2.9
            fe.encode(_frame(), 12)
            self.assertEqual(len(_DeadEncoder.calls), 2, "第二次退避（2 秒）未生效")
            now[0] = 3.0
            fe.encode(_frame(), 13)
            self.assertEqual(len(_DeadEncoder.calls), 3)

    def test_backoff_caps_at_30_seconds(self):
        now = [0.0]
        _DeadEncoder.calls = []
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _DeadEncoder), \
                mock.patch.object(pipeline.time, "monotonic", lambda: now[0]):
            fe = pipeline.FrameEncoder(width=64, height=48, fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="auto")
            for _ in range(10):
                fe.encode(_frame(), 0)
                now[0] += 100.0        # 每次都远超退避窗口，必然重试
            self.assertEqual(fe.backoff, 30.0, "退避未封顶 30 秒")

    def test_construction_exception_also_backs_off(self):
        calls = []

        def _boom(*a, **kw):
            calls.append(a)
            raise RuntimeError("PyAV 内部错误")

        now = [0.0]
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _boom), \
                mock.patch.object(pipeline.time, "monotonic", lambda: now[0]):
            fe = pipeline.FrameEncoder(width=64, height=48, fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="auto")
            for i in range(5):
                ef = fe.encode(_frame(), i)
                self.assertIsNotNone(ef)
                self.assertFalse(ef.is_video)
        self.assertEqual(len(calls), 1, "构造抛异常后未进入退避（每帧都会重试）")

    def test_jpeg_selector_never_builds_video_encoder(self):
        _DeadEncoder.calls = []
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _DeadEncoder):
            fe = pipeline.FrameEncoder(width=64, height=48, fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="jpeg")
            ef = fe.encode(_frame(), 1)
        self.assertFalse(ef.is_video)
        self.assertEqual(_DeadEncoder.calls, [], "encoder=jpeg 时不应尝试视频编码器")


class TestEncoderLifecycle(unittest.TestCase):
    """可用编码器：只构造一次、帧率变化调 set_fps、on_build 回调一次。"""

    def test_builds_once_and_reports_via_callback(self):
        built = []
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _LiveEncoder):
            fe = pipeline.FrameEncoder(
                width=64, height=48, fps=30, bitrate=1000, keyint=30,
                encoder_sel="auto",
                on_build=lambda enc, w, h, br: built.append((w, h, br)))
            ef1 = fe.encode(_frame(), 1)
            ef2 = fe.encode(_frame(), 2)
        self.assertTrue(ef1.is_video and ef2.is_video)
        self.assertEqual(len(built), 1, "编码器被重复构造")
        self.assertEqual(built[0][2], 1000)
        self.assertIs(fe.encoder, fe.encoder)

    def test_fps_change_reconfigures_encoder(self):
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _LiveEncoder):
            fe = pipeline.FrameEncoder(width=64, height=48, fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="auto")
            fe.encode(_frame(), 1, fps=30)
            enc = fe.encoder
            fe.encode(_frame(), 2, fps=15)
        self.assertEqual(enc.fps_calls, [15], "帧率档变化未调用 set_fps")

    def test_encode_error_is_swallowed_and_reported(self):
        class _RaisingEncoder(_LiveEncoder):
            def encode(self, frame, raw_ts=0, force_key=False):
                raise RuntimeError("GPU 重置")

        errors = []
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _RaisingEncoder):
            fe = pipeline.FrameEncoder(width=64, height=48, fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="auto",
                                       on_error=errors.append)
            ef = fe.encode(_frame(), 1)          # 不得抛（第 90 条）
        self.assertIsNone(ef)
        self.assertEqual(len(errors), 1)

    def test_force_idr_without_encoder_returns_none(self):
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _DeadEncoder):
            fe = pipeline.FrameEncoder(width=64, height=48, fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="auto")
            fe.encode(_frame(), 1)
            res, err = fe.force_idr(_frame())
        self.assertIsNone(res)
        self.assertIsNone(err)

    def test_force_idr_reports_exception_instead_of_raising(self):
        class _RaisingEncoder(_LiveEncoder):
            def encode(self, frame, raw_ts=0, force_key=False):
                raise RuntimeError("GPU 重置")

        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _RaisingEncoder):
            fe = pipeline.FrameEncoder(width=64, height=48, fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="auto")
            fe.encode(_frame(), 1)
            res, err = fe.force_idr(_frame())    # 不得抛
        self.assertIsNone(res)
        self.assertIsInstance(err, RuntimeError)

    def test_close_releases_encoder(self):
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _LiveEncoder):
            fe = pipeline.FrameEncoder(width=64, height=48, fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="auto")
            fe.encode(_frame(), 1)
            enc = fe.encoder
            fe.close()
        self.assertTrue(enc.closed)
        self.assertIsNone(fe.encoder)


class TestSharedEncoderWiring(unittest.TestCase):
    """host 与 share 都只经 pipeline.FrameEncoder 使用编码器（不再各自实现）。"""

    def test_host_uses_pipeline_encoder(self):
        src = inspect.getsource(host)
        self.assertIn("pipeline.FrameEncoder", src)
        self.assertNotIn("codec_mod.VideoEncoder", src)

    def test_share_uses_pipeline_encoder(self):
        src = inspect.getsource(share)
        self.assertIn("pipeline.FrameEncoder", src)
        self.assertNotIn("codec_mod.VideoEncoder", src)
        self.assertNotIn("_enc_retry_at", src, "share 自维护的退避状态应已删除")

    def test_share_session_holds_shared_encoder(self):
        sess = share.ScreenShareSession(
            None, None, {"host": {"fps": 5, "jpeg_quality": 50,
                                  "codec": {"encoder": "jpeg", "target_width": 0}}},
            "t", capture=mock.MagicMock())
        self.assertIsInstance(sess._fe, pipeline.FrameEncoder)


if __name__ == "__main__":
    unittest.main()
