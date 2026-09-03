# -*- coding: utf-8 -*-
"""第 7 条回归：H.264/HEVC 批次必须按序解码全部帧以维持参考帧链。

旧路径：接收循环把一个 recv 批次里的所有帧折叠成单个 `latest`，只解码最新一帧、丢弃
中间帧。对 JPEG 无害（各帧独立可解），但对 H.264/HEVC 会打断参考帧链——被跳过的 NAL
不喂给有状态解码器，后续 P 帧对错误参考解码出花屏/拖影，直到下一个关键帧（约 2 秒）。

修复：视频帧收集成有序 `video_batch`，新增 `_decode_video_batch` 把批次内全部 NAL 依序
喂解码器维持参考链，只把最后一帧成功解码的结果存为显示帧；JPEG 仍只留最新一份。

hermetic：用 __new__ 构造 Channel，FakeDecoder 记录每次 decode 喂入的 nal 与 set_codec，
stub _store_frame/_request_keyframe，绕开真实 socket/解码器/Tk。
"""
import unittest

import viewer


class FakeDecoder:
    def __init__(self, available=True):
        self.available = available
        self.decode_calls = []   # 依序记录喂入的 nal
        self.codecs = []         # set_codec 调用记录
        self._n = 0

    def set_codec(self, c):
        self.codecs.append(c)

    def decode(self, nal):
        self.decode_calls.append(nal)
        self._n += 1
        return ["frame%d" % self._n]   # 非空 → 解码成功


def _channel(decoder, got_key=False):
    ch = viewer.Channel.__new__(viewer.Channel)
    ch.name = "test"
    ch._decoder = decoder
    ch.cur_codec = viewer.CODEC_H264
    ch._got_key = got_key
    ch.video_mode = False
    ch._video_unavailable_warned = False
    ch._retry_delay = 1.0
    ch._test_stored = []
    ch._test_reqs = []
    ch._store_frame = lambda frame, ts: ch._test_stored.append((frame, ts))
    ch._request_keyframe = lambda sock: ch._test_reqs.append(sock)
    return ch


def _batch(n, key_first=True, codec=None):
    """构造 n 帧批次：(ts, codec, flags, nal)。key_first 时首帧为关键帧。"""
    codec = viewer.CODEC_H264 if codec is None else codec
    out = []
    for i in range(n):
        flags = viewer.VIDEO_FLAG_KEY if (key_first and i == 0) else 0
        out.append((1000 + i, codec, flags, b"nal%d" % i))
    return out


class TestReferenceChainPreserved(unittest.TestCase):
    def test_all_frames_fed_to_decoder_in_order(self):
        # 核心：旧路径一个批次只 decode 1 次（最新帧），新路径必须按序 decode 全部 5 次。
        dec = FakeDecoder()
        ch = _channel(dec)
        ch._decode_video_batch(object(), _batch(5))
        self.assertEqual(dec.decode_calls,
                         [b"nal0", b"nal1", b"nal2", b"nal3", b"nal4"])
        self.assertEqual(len(dec.decode_calls), 5)   # 不是 1

    def test_stores_only_last_decoded_frame(self):
        dec = FakeDecoder()
        ch = _channel(dec)
        ch._decode_video_batch(object(), _batch(5))
        self.assertEqual(len(ch._test_stored), 1)            # 只存一帧显示
        self.assertEqual(ch._test_stored[0][0], "frame5")    # 最后一帧
        self.assertEqual(ch._test_stored[0][1], 1004)        # 最后一帧的 ts
        self.assertEqual(ch._retry_delay, 0.0)               # 成功后清零退避

    def test_single_frame_batch_still_decodes_and_stores(self):
        # 稳态常见情形：一个批次只有一帧（关键帧），照常解码并显示。
        dec = FakeDecoder()
        ch = _channel(dec)
        ch._decode_video_batch(object(), _batch(1))
        self.assertEqual(dec.decode_calls, [b"nal0"])
        self.assertEqual(len(ch._test_stored), 1)
        self.assertTrue(ch.video_mode)
        self.assertTrue(ch._got_key)


class TestKeyframeGate(unittest.TestCase):
    def test_p_frames_before_keyframe_fed_but_not_stored(self):
        # 关键帧在批次末尾：前 3 个 P 帧仍须喂解码器（维持链），但 _got_key=False 不显示，
        # 并各自请求关键帧；末尾关键帧置位后解码并显示。
        dec = FakeDecoder()
        ch = _channel(dec, got_key=False)
        batch = _batch(4, key_first=False)                   # 全 P 帧
        batch[3] = (1003, viewer.CODEC_H264, viewer.VIDEO_FLAG_KEY, b"nal3")  # 末帧改关键帧
        ch._decode_video_batch(object(), batch)
        self.assertEqual(len(dec.decode_calls), 4)           # 4 帧全部喂入
        self.assertEqual(len(ch._test_stored), 1)            # 只存关键帧那帧
        self.assertEqual(ch._test_stored[0][0], "frame4")
        self.assertEqual(len(ch._test_reqs), 3)              # 前 3 个 P 帧各请求一次关键帧
        self.assertTrue(ch._got_key)

    def test_decode_failure_requests_keyframe(self):
        # 解码返回空（缺参考）：不存帧，请求关键帧。
        class EmptyDecoder(FakeDecoder):
            def decode(self, nal):
                self.decode_calls.append(nal)
                return []                                    # 解码不出帧
        dec = EmptyDecoder()
        ch = _channel(dec, got_key=True)
        ch._decode_video_batch(object(), _batch(2, key_first=False))
        self.assertEqual(len(dec.decode_calls), 2)           # 仍按序喂入
        self.assertEqual(ch._test_stored, [])                # 无帧可存
        self.assertEqual(len(ch._test_reqs), 2)              # 每帧请求关键帧


class TestDecoderUnavailable(unittest.TestCase):
    def test_unavailable_feeds_nothing_and_warns_once(self):
        # 第 11 条行为保留：缺解码器时不喂帧、不显示、只告警一次、清 video_mode/_got_key。
        dec = FakeDecoder(available=False)
        ch = _channel(dec, got_key=True)
        ch.video_mode = True
        ch._decode_video_batch(object(), _batch(3))
        self.assertEqual(dec.decode_calls, [])               # 不喂解码器
        self.assertEqual(ch._test_stored, [])                # 不显示
        self.assertFalse(ch.video_mode)
        self.assertFalse(ch._got_key)
        self.assertTrue(ch._video_unavailable_warned)


class TestCodecSwitch(unittest.TestCase):
    def test_codec_switch_midbatch_rebuilds_decoder(self):
        # 批次内编码从 H.264 切到 HEVC：触发 set_codec 并复位关键帧门控。
        dec = FakeDecoder()
        ch = _channel(dec)                                   # cur_codec = H264
        batch = [
            (1000, viewer.CODEC_H264, viewer.VIDEO_FLAG_KEY, b"nal0"),
            (1001, viewer.CODEC_HEVC, viewer.VIDEO_FLAG_KEY, b"nal1"),
        ]
        ch._decode_video_batch(object(), batch)
        self.assertEqual(dec.codecs, [viewer.CODEC_HEVC])    # 切换时重建
        self.assertEqual(ch.cur_codec, viewer.CODEC_HEVC)
        self.assertEqual(len(dec.decode_calls), 2)           # 两帧都喂入


if __name__ == "__main__":
    unittest.main()
