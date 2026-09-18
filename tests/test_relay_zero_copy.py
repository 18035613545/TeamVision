"""A4 §3.1（第 132 条）：中继路径整帧物化 2→1 的正确性锁定。

约束：
  - parse_message_raw 返回的原始消息与线上字节逐字节相同；
  - _forward_upstream_frame(raw=...) 时中继入队的就是那段原始 bytes（同一对象，
    零重打包）；raw=None 时回退路径产物与旧实现逐字节一致；
  - _client_recv_loop 上行帧路径端到端不篡改任何字节。
CPU 收益 <0.1%，不单测性能，只锁正确性。
"""
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common
import host


def _mk_runtime():
    rt = host.HostRuntime.__new__(host.HostRuntime)
    rt.stop_event = threading.Event()
    rt.up_max_kbps = 20000
    rt.up_max_fps = 60
    rt._register_sharer = lambda sock, info: None
    return rt


def _mk_info():
    info = mock.MagicMock()
    info.share_id = "peer:1"
    info.allow_upstream.return_value = True
    info._up_warned_at = 0.0
    return info


class TestParseMessageRaw(unittest.TestCase):
    def test_two_messages_materialized_byte_exact(self):
        m1 = common.pack_video(b"\x00\x00\x01nalu", ts=111, keyframe=True)
        m2 = common.pack_frame(b"\xff\xd8jpegbytes", ts=222)
        buf = bytearray(m1 + m2)
        consumed, kind, raw = common.parse_message_raw(buf)
        self.assertEqual(kind, common.MSG_VIDEO)
        self.assertEqual(raw, m1)
        self.assertEqual(consumed, len(m1))
        del buf[:consumed]
        consumed, kind, raw = common.parse_message_raw(buf)
        self.assertEqual(kind, common.MSG_FRAME)
        self.assertEqual(raw, m2)

    def test_incomplete_returns_zero(self):
        m = common.pack_video(b"ABCDEFGH", ts=1)
        consumed, kind, raw = common.parse_message_raw(bytearray(m[:6]))
        self.assertEqual((consumed, kind, raw), (0, None, None))

    def test_invalid_header_raises(self):
        with self.assertRaises(ValueError):
            common.parse_message_raw(bytearray(b"\x09\x00\x00\x00\x02ab"))
        bad = bytearray(b"\x01") + common._LEN_U32.pack(common.MAX_PAYLOAD + 1)
        with self.assertRaises(ValueError):
            common.parse_message_raw(bad)

    def test_matches_repacked_bytes(self):
        # 原始物化与旧"payload 切片 + 重打包"路径逐字节等价（回退安全网）
        m = common.pack_video(b"nal-bytes-here", ts=999, keyframe=False)
        _, kind, payload = common.parse_message(bytearray(m))
        repacked = common._MSG_HDR.pack(kind, len(payload)) + payload
        _, _, raw = common.parse_message_raw(bytearray(m))
        self.assertEqual(repacked, raw)


class TestForwardZeroCopy(unittest.TestCase):
    def test_video_raw_passthrough_same_object(self):
        rt = _mk_runtime()
        info = _mk_info()
        raw = common.pack_video(b"\x00\x00\x01nalu" * 8, ts=12345, keyframe=True)
        captured = {}

        def _route(runtime, source, frame, is_key=False, is_jpeg=False, **kw):
            captured.update(source=source, frame=frame, is_key=is_key)
            return 1

        with mock.patch.object(host, "route_frame", _route):
            host._forward_upstream_frame(
                rt, None, info, common.MSG_VIDEO,
                memoryview(raw)[5:], ("1.2.3.4", 1), raw=raw)
        self.assertIs(captured["frame"], raw)   # 同一对象：零重打包
        self.assertEqual(captured["frame"], raw)  # 逐字节相等
        self.assertTrue(captured["is_key"])

    def test_jpeg_raw_passthrough_byte_equal(self):
        rt = _mk_runtime()
        info = _mk_info()
        raw = common.pack_frame(b"\xff\xd8" + b"\x01\x02\x03" * 100, ts=77)
        frames = []
        with mock.patch.object(host, "route_frame",
                               lambda rt, s, f, **kw: frames.append(f) or 0):
            host._forward_upstream_frame(
                rt, None, info, common.MSG_FRAME,
                memoryview(raw)[5:], ("1.2.3.4", 1), raw=raw)
        self.assertEqual(frames[0], raw)

    def test_fallback_without_raw_rebuilds_identically(self):
        # 直接调用（测试替身/旧签名）：raw=None 回退重打包，产物与原消息逐字节一致
        rt = _mk_runtime()
        info = _mk_info()
        msg = common.pack_video(b"nal", ts=42)
        _, kind, payload = common.parse_message(bytearray(msg))
        frames = []
        with mock.patch.object(host, "route_frame",
                               lambda rt, s, f, **kw: frames.append(f) or 0):
            host._forward_upstream_frame(rt, None, info, kind, payload,
                                         ("1.2.3.4", 1))
        self.assertEqual(frames[0], msg)

    def test_corrupt_payload_never_forwarded(self):
        # 校验逻辑不变：视频 payload < 10B / JPEG 缺 SOI 均不得转发
        rt = _mk_runtime()
        info = _mk_info()
        for kind, payload in ((common.MSG_VIDEO, b"short"),
                              (common.MSG_FRAME, b"\x00" * 8 + b"\x00\xd8")):
            raw = common._MSG_HDR.pack(kind, len(payload)) + payload
            with mock.patch.object(host, "route_frame") as mroute:
                host._forward_upstream_frame(
                    rt, None, info, kind, memoryview(raw)[5:],
                    ("1.2.3.4", 1), raw=raw)
                mroute.assert_not_called()

    def test_rate_limited_frame_never_forwarded(self):
        rt = _mk_runtime()
        info = _mk_info()
        info.allow_upstream.return_value = False
        raw = common.pack_video(b"nal", ts=1)
        with mock.patch.object(host, "route_frame") as mroute:
            host._forward_upstream_frame(
                rt, None, info, common.MSG_VIDEO, memoryview(raw)[5:],
                ("1.2.3.4", 1), raw=raw)
            mroute.assert_not_called()


class TestRecvLoopEndToEnd(unittest.TestCase):
    """走真实 _client_recv_loop：上行帧经中继出队后与入队原始消息逐字节相等。"""

    def _run_loop(self, wire):
        class _Sock:
            def __init__(self, chunks):
                self._chunks = list(chunks)

            def recv(self, _n):
                return self._chunks.pop(0) if self._chunks else b""

        sock = _Sock([wire])
        info = mock.MagicMock()
        info.share_id = None
        info.allow_upstream.return_value = True
        rt = _mk_runtime()
        registered = []
        rt._register_sharer = lambda s, i: registered.append(i)
        frames = []
        with mock.patch("host.select.select", return_value=([sock], [], [], )), \
                mock.patch.object(host, "route_frame",
                                  lambda rt, s, f, **kw: frames.append(f) or 0):
            reason = host._client_recv_loop(
                sock, info, rt, ("1.2.3.4", 9), {}, {})
        self.assertEqual(reason, "断开")   # recv 空 → 正常对端关闭
        return frames

    def test_video_and_jpeg_frames_byte_identical(self):
        msg_v = common.pack_video(b"\x00\x00\x01nalfinal" * 40, ts=99,
                                  keyframe=True, codec=common.CODEC_H264)
        msg_j = common.pack_frame(b"\xff\xd8" + bytes(range(256)) * 3, ts=98)
        frames = self._run_loop(msg_v + msg_j + msg_v)
        self.assertEqual(frames, [msg_v, msg_j, msg_v])
        for got, want in zip(frames, (msg_v, msg_j, msg_v)):
            self.assertIsInstance(got, bytes)
            self.assertEqual(bytes(got), want)   # 逐字节相等

    def test_ctrl_frame_interleaved_not_relayed(self):
        msg_c = common.pack_msg({"action": "ping"})
        msg_v = common.pack_video(b"nal-data!!", ts=5)
        frames = self._run_loop(msg_c + msg_v + msg_c)
        self.assertEqual(frames, [msg_v])


if __name__ == "__main__":
    unittest.main()
