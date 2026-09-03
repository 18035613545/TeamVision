# -*- coding: utf-8 -*-
"""第 57 条回归：每连接缓冲改 bytearray（消除 O(n²) 重拷）+ MAX_PAYLOAD 收紧 + payload 统一 bytes。

这些测试是 hermetic 的（无真实 socket、无线程），直接复刻 host.py/viewer.py recv 循环
的解析模式，锁定三条不变量：
  1. parse_message 无论缓冲区是 bytes 还是 bytearray，都返回 bytes payload；
  2. 该 bytes payload 喂回 parse_video/unpack_frame → pack_video/pack_frame 的中继再打包
     路径不会触发 `bytes + bytearray` TypeError；
  3. bytearray + `del rx_buf[:consumed]` 的 recv 循环模式能正确切分跨 chunk 边界的多消息流。
"""
import unittest

from common import (
    MAX_PAYLOAD, MSG_CTRL, MSG_FRAME, MSG_VIDEO,
    parse_message, parse_video, pack_video, pack_frame, unpack_frame, pack_msg,
)


def _drain(rx_buf, out):
    """复刻 recv 循环内层：原地弹出已消费前缀，收集 (kind, payload)。"""
    while True:
        consumed, kind, payload = parse_message(rx_buf)
        if consumed == 0:
            break
        del rx_buf[:consumed]
        out.append((kind, payload))


class TestMaxPayloadBound(unittest.TestCase):
    def test_max_payload_is_32mb(self):
        self.assertEqual(MAX_PAYLOAD, 32 * 1024 * 1024)

    def test_over_max_payload_rejected_at_header(self):
        # 仅 5 字节头声明 length = MAX_PAYLOAD+1：必须在攒 body 前就 ValueError，
        # 否则恶意长度头能让单连接缓冲无界增长。
        header = bytes((MSG_VIDEO,)) + (MAX_PAYLOAD + 1).to_bytes(4, "big")
        with self.assertRaises(ValueError):
            parse_message(bytearray(header))


class TestPayloadIsAlwaysBytes(unittest.TestCase):
    def test_bytearray_buf_returns_bytes_payload(self):
        raw = pack_video(b"\x00\x00\x00\x01NAL", ts=123, keyframe=True)
        consumed, kind, payload = parse_message(bytearray(raw))
        self.assertEqual(kind, MSG_VIDEO)
        self.assertIs(type(payload), bytes)  # 不是 bytearray

    def test_bytes_buf_returns_bytes_payload(self):
        # 既有调用方（测试 helper）仍传 bytes，行为不得回退。
        raw = pack_frame(b"\xff\xd8JPEG", ts=999)
        consumed, kind, payload = parse_message(raw)
        self.assertEqual(kind, MSG_FRAME)
        self.assertIs(type(payload), bytes)

    def test_relay_repack_does_not_raise_typeerror(self):
        # host 中继上行帧：parse_message → parse_video/unpack_frame → pack_video/pack_frame。
        # 若 payload 是 bytearray，pack_video 内 `bytes((MSG_VIDEO,)) + ... + video_bytes`
        # 会抛 TypeError。用 bytearray 缓冲取出的 payload 走完整再打包，断言往返一致。
        vid_raw = pack_video(b"\x00\x00\x00\x01KEY", ts=42, keyframe=True)
        _, _, vid_payload = parse_message(bytearray(vid_raw))
        ts, codec, flags, nal = parse_video(vid_payload)
        repacked = pack_video(nal, ts=ts, keyframe=bool(flags & 1), codec=codec)
        self.assertEqual(repacked, vid_raw)

        frm_raw = pack_frame(b"\xff\xd8BODY\xff\xd9", ts=7)
        _, _, frm_payload = parse_message(bytearray(frm_raw))
        fts, jpeg = unpack_frame(frm_payload)
        self.assertEqual(pack_frame(jpeg, ts=fts), frm_raw)


class TestRecvLoopPattern(unittest.TestCase):
    def test_stream_split_across_chunk_boundaries(self):
        msgs = [
            pack_video(b"\x00\x00\x00\x01A", ts=1, keyframe=True),
            pack_frame(b"\xff\xd8B\xff\xd9", ts=2),
            pack_msg({"action": "ping", "seq": 3}),
            pack_video(b"\x00\x00\x00\x01C", ts=4, keyframe=False),
        ]
        stream = b"".join(msgs)
        rx_buf = bytearray()
        out = []
        # 故意用 7 字节小 chunk 喂入，确保消息边界横跨多次 recv（O(n²) 旧路径下也是这种切分）。
        for i in range(0, len(stream), 7):
            rx_buf += stream[i:i + 7]
            _drain(rx_buf, out)
        self.assertEqual([k for k, _ in out],
                         [MSG_VIDEO, MSG_FRAME, MSG_CTRL, MSG_VIDEO])
        self.assertEqual(out[0][1], parse_message(msgs[0])[2])
        self.assertEqual(out[3][1], parse_message(msgs[3])[2])
        self.assertEqual(len(rx_buf), 0)  # 全部消费干净

    def test_partial_trailing_message_stays_buffered(self):
        full = pack_frame(b"\xff\xd8WHOLE\xff\xd9", ts=5)
        rx_buf = bytearray()
        out = []
        rx_buf += full[:6]  # 头 5 字节 + body 1 字节：body 不足
        _drain(rx_buf, out)
        self.assertEqual(out, [])          # 不完整 → 不消费
        self.assertEqual(bytes(rx_buf), full[:6])  # 原样留在缓冲区
        rx_buf += full[6:]                 # 补齐
        _drain(rx_buf, out)
        self.assertEqual([k for k, _ in out], [MSG_FRAME])
        self.assertEqual(out[0][1], parse_message(full)[2])
        self.assertEqual(len(rx_buf), 0)

    def test_bytearray_extend_is_in_place_not_rebinding(self):
        # 锁定 O(n²)→O(1) 的机制前提：+= 与 del 都作用在同一 bytearray 对象上，
        # 不重新绑定到新对象（bytes 路径每次 += 都换新对象，正是退化根源）。
        rx_buf = bytearray()
        original_id = id(rx_buf)
        rx_buf += pack_msg({"action": "ping"})
        self.assertEqual(id(rx_buf), original_id)
        out = []
        _drain(rx_buf, out)
        self.assertEqual(id(rx_buf), original_id)
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
