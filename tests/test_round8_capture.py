# -*- coding: utf-8 -*-
"""第 8 轮审查 R8-capture-01（2026-10-10）：`set_fps` 失败必须落独立退避。

缺陷形态（实测见 docs/audit/capture.md R8-capture-01）：`FrameEncoder._ensure_encoder`
有两条编码器构造失败路径，只有一条有限流——

- 编码器为 None 时的**构造失败**（`pipeline.py:146-150`）：`_retry_at`/`_backoff`
  指数退避（1→2→…→30s 封顶），期间走 JPEG 回退；
- 编码器已存在但**就地重建失败**（`set_fps` 返回 False）：没有任何退避。而
  codec-012 有意在失败时回滚 `_fps`（`codec.py:334-337`）让下一帧仍重试这一档——
  那个决定是对的，缺的是**重试频率**约束：下一帧 `fps != self._enc_fps` 依旧成立，
  于是每帧一次完整 `_create_ctx_with`（源码自述硬件编码器拆装 93–110ms，而 30fps
  每帧预算只有 33ms），且该状态下 `available` 恒 True、**永不回退 JPEG**。

修法（不得复用 `_retry_at`/`_backoff`，语义不同：那两个管"没有可用编码器→重建"）：
新增 `_fps_retry_at`/`_fps_backoff`，失败进窗口、窗口到期仍重试、换实例即复位。

本文件不依赖真实屏幕/GPU：替身编码器 + 替身 `codec.VideoEncoder` + 可注入时钟。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import codec  # noqa: E402
import pipeline  # noqa: E402

W, H = 854, 480


class _Clock:
    """可控单调时钟：替代 `pipeline` 内的 time.monotonic（不碰真实时钟）。"""

    def __init__(self, start=100.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class _StubEncoder:
    """替身编码器：available 恒 True（set_fps 失败时真实实现保留旧上下文）。

    `fps_ok=False` 复刻 codec-012 的失败落脚口径——rollback 后返回 False，旧上下文
    继续出帧。
    """

    codec_id = 0
    keyframe_sent = 0
    name = "libx264"
    available = True
    bitrate = 2_000_000

    def __init__(self, fps_ok=False, bitrate_ok=True):
        self.fps_ok = fps_ok
        self.bitrate_ok = bitrate_ok
        self.set_fps_calls = 0
        self.set_bitrate_calls = 0
        self.closed = False

    def set_fps(self, fps):
        self.set_fps_calls += 1
        return bool(self.fps_ok)

    def set_bitrate(self, bps):
        self.set_bitrate_calls += 1
        return bool(self.bitrate_ok)

    def encode(self, frame, raw_ts=None, force_key=False):
        return (b"\x00\x00\x00\x01P", False, 1.0, int(raw_ts or 0))

    def close(self):
        self.closed = True


def _frame(w=W, h=H):
    return np.zeros((h, w, 3), dtype=np.uint8)


def _encoder(clock, **kw):
    """构造 FrameEncoder，并把 `pipeline` 内的单调时钟替换为可控时钟。"""
    patcher = mock.patch.object(pipeline.time, "monotonic", clock)
    patcher.start()
    try:
        return pipeline.FrameEncoder(fps=30, bitrate=2_000_000, keyint=60,
                                     encoder_sel="x264", target_width=W,
                                     quality=70, name="r8", **kw)
    finally:
        patcher.stop()


def _feed(enc, clock, frames, fps=60, bitrate=2_000_000):
    """喂 frames 帧，每帧之间把时钟推进 34ms（> 30fps 的 33ms 帧预算）。"""
    out = []
    patcher = mock.patch.object(pipeline.time, "monotonic", clock)
    patcher.start()
    try:
        for i in range(1, frames + 1):
            out.append(enc.encode(_frame(), i, scale=1.0, quality=70,
                                  fps=fps, bitrate=bitrate))
            clock.advance(0.034)
    finally:
        patcher.stop()
    return out


class TestFpsRetryBackoff(unittest.TestCase):
    """R8-capture-01：`set_fps` 失败的独立负缓存退避。"""

    def test_backoff_limits_retries_per_frame(self):
        """8 帧只应尝试 1 次（修前为 8 次 = 每帧一次完整上下文重建）。"""
        clock = _Clock()
        st = _StubEncoder(fps_ok=False)
        fe = _encoder(clock)
        fe._encoder = st
        fe._enc_fps = 30

        out = _feed(fe, clock, 8, fps=60)

        self.assertEqual(st.set_fps_calls, 1,
                         "退避未生效：set_fps 被每帧调用（=%d）" % st.set_fps_calls)
        self.assertLess(st.set_fps_calls, 8, "重试次数必须小于帧数")
        self.assertEqual(fe._enc_fps, 30, "失败却把 _enc_fps 记成已生效")
        self.assertEqual(fe.fps_backoff, 2.0, "退避未按 1→2 推进")
        self.assertGreater(fe._fps_retry_at, clock.now,
                           "未进入退避窗口：_fps_retry_at 应在未来")
        self.assertEqual(fe.backoff, 1.0,
                         "fps 退避不得动构造退避（两条路径语义不同）")
        self.assertEqual(fe._retry_at, 0.0, "构造退避不应被 fps 失败启动")
        # 每帧仍要出图：该状态 available 恒 True，不得退化成"不出帧"
        self.assertTrue(all(x is not None for x in out))
        self.assertEqual(st.set_bitrate_calls, 8, "码率下发与 fps 重试无关，应每帧一次")

    def test_retries_again_after_window_expires(self):
        """窗口到期后仍重试这一档——不得退化成"永不重试"（codec-012 的意图）。"""
        clock = _Clock()
        st = _StubEncoder(fps_ok=False)
        fe = _encoder(clock)
        fe._encoder = st
        fe._enc_fps = 30

        _feed(fe, clock, 1, fps=60)
        self.assertEqual(st.set_fps_calls, 1)
        window = fe._fps_retry_at - clock.now
        self.assertGreater(window, 0.0, "失败后应进入退避窗口")

        # 窗口内：推进到边界之前，仍不得重试
        clock.advance(window * 0.5)
        _feed(fe, clock, 1, fps=60)
        self.assertEqual(st.set_fps_calls, 1, "退避窗口内仍重试了")

        # 推开窗口：到期后必须再试一次，且退避继续翻倍
        clock.advance(window)
        _feed(fe, clock, 1, fps=60)
        self.assertEqual(st.set_fps_calls, 2, "退避窗口到期后不再重试 → 永不重试")
        self.assertEqual(fe.fps_backoff, 4.0, "第二次失败后退避应翻倍到 4")

    def test_success_does_not_leave_backoff_armed(self):
        """反向对照：成功一次后必须复位，下一次失败从 1 秒重新起算。"""
        clock = _Clock()
        st = _StubEncoder(fps_ok=True)
        fe = _encoder(clock)
        fe._encoder = st
        fe._enc_fps = 30

        _feed(fe, clock, 1, fps=60)
        self.assertEqual(fe._enc_fps, 60, "成功却未落账")
        self.assertEqual(fe.fps_backoff, 1.0, "成功后未复位退避")
        self.assertEqual(fe._fps_retry_at, 0.0, "成功后仍留着退避窗口")

        st.fps_ok = False
        fe._enc_fps = 30          # 模拟配置又改回 60 这一档
        _feed(fe, clock, 2, fps=60)
        self.assertEqual(st.set_fps_calls, 2)
        self.assertEqual(fe.fps_backoff, 2.0,
                         "上一轮成功没复位 → 本次起点不是 1 秒")

    def test_new_encoder_instance_gets_its_own_retry_window(self):
        """退避状态属于"那个改不掉帧率的实例"，换实例即复位。"""
        clock = _Clock()
        st = _StubEncoder(fps_ok=False)
        fe = _encoder(clock)
        fe._encoder = st
        fe._enc_fps = 30

        _feed(fe, clock, 1, fps=60)
        self.assertEqual(fe.fps_backoff, 2.0)
        self.assertGreater(fe._fps_retry_at, clock.now)

        st2 = _StubEncoder(fps_ok=True)   # 新实例能改帧率
        fe._encoder = st2
        _feed(fe, clock, 1, fps=60)
        self.assertEqual(st2.set_fps_calls, 1,
                         "新实例的第一帧就被旧实例的退避窗口挡住了")
        self.assertEqual(fe._enc_fps, 60)
        self.assertEqual(fe.fps_backoff, 1.0, "换实例后退避未复位")

    def test_dropped_encoder_does_not_block_construction_path(self):
        """构造失败与就地失败**不共用**退避：fps 窗口不得挡住建新编码器。"""
        clock = _Clock()
        st = _StubEncoder(fps_ok=False)
        fe = _encoder(clock)
        fe._encoder = st
        fe._enc_fps = 30

        _feed(fe, clock, 1, fps=60)
        self.assertGreater(fe._fps_retry_at, clock.now)   # 已进入 fps 退避窗口

        st.available = False          # 编码器彻底失效
        built = []

        def _fake_ve(w, h, fps, bitrate, keyint, sel, preset):
            built.append((w, h, fps, bitrate, sel))
            return _StubEncoder(fps_ok=True)

        with mock.patch.object(codec, "VideoEncoder", _fake_ve):
            _feed(fe, clock, 1, fps=60)

        self.assertEqual(len(built), 1,
                         "编码器失效后重建被 fps 退避窗口挡死（_retry_at 语义被污染）")
        self.assertIsNotNone(fe.encoder, "失效编码器应被 drop 并重建")
        self.assertEqual(fe._enc_fps, 60, "重建后的新实例应可正常改帧率")
        self.assertEqual(fe.fps_backoff, 1.0, "换实例后 fps 退避未复位")

    def test_construction_failure_keeps_fps_backoff_out_of_it(self):
        """反向：构造失败只走 `_retry_at`/`_backoff`，不得顺带动 fps 退避状态。"""
        clock = _Clock()
        fe = _encoder(clock)
        calls = {"n": 0}

        def _boom(*a, **kw):
            calls["n"] += 1
            raise RuntimeError("显存不足")

        with mock.patch.object(codec, "VideoEncoder", _boom):
            _feed(fe, clock, 5, fps=30)

        self.assertEqual(calls["n"], 1, "构造失败未限流（5 帧应只试 1 次）")
        self.assertGreater(fe._retry_at, 0.0, "构造退避未启动")
        self.assertEqual(fe.backoff, 2.0, "构造退避未按 1→2 推进")
        self.assertEqual(fe.fps_backoff, 1.0, "构造失败污染了 fps 退避")
        self.assertEqual(fe._fps_retry_at, 0.0, "构造失败污染了 fps 退避窗口")


class TestSetBitrateFailureBudget(unittest.TestCase):
    """顺手核对（task-6 要求）：`set_bitrate` 失败路径不需要同类保护。"""

    def test_bitrate_failure_is_not_a_per_frame_rebuild(self):
        """`set_bitrate` 失败只丢一次码率下发，不阻塞本帧、不触发 JPEG 回退。

        与 `set_fps` 的差别是量级而非形态：它走 `_recreate(keep_old_on_failure=True)`
        且失败时保留旧上下文（`codec.py:254-272`），本帧照常出图，代价仅"新码率
        推迟到下次重建生效"，因此不需要独立退避（结论已写入汇报，未顺手改）。
        """
        clock = _Clock()
        st = _StubEncoder(fps_ok=True, bitrate_ok=False)
        fe = _encoder(clock)
        fe._encoder = st
        fe._enc_fps = 30

        out = _feed(fe, clock, 3, fps=30)

        self.assertEqual(st.set_bitrate_calls, 3, "码率下发被本该无关的退避吞掉")
        self.assertTrue(all(x is not None and x.is_video for x in out),
                        "码率下发失败不应把本帧降级成 JPEG/丢帧")
        self.assertEqual(fe._retry_at, 0.0, "码率失败不该启动构造退避")
        self.assertEqual(fe._fps_retry_at, 0.0, "码率失败不该启动 fps 退避")


if __name__ == "__main__":
    unittest.main()
