# -*- coding: utf-8 -*-
"""第 8 轮审查（round8）网络/观看端域的修复回归：R8-NET-02、R8-UI-01/02/03。

每一类都对应 `docs/audit/round8/net.md` / `ui.md` 里的一条发现：

① R8-NET-02：`parse_video_auto` 的布局判据与布局不是一一映射。旧判据"解出的 NAL
   只要有 Annex-B 起始码就采信"，而 `expect_seq=False` 收到 v2.4 载荷时，按 v2.3
   解出的 `nal = seq(4) + 真 NAL`——`seq == 1` 或 `seq ∈ [256,511]` 时 seq 自己的
   前 3~4 字节就构成起始码，判据"成立"→ 返回错位 NAL。实测后果是参考链断裂、
   每 GOP 末段 6~10 帧解不出（libx264 下 520 帧丢 28 帧）。
   修法：两种布局都产出候选，按 (声明长度吻合, 起始码强度, 是否协商布局) 择优。
② R8-UI-01：`_apply_probe_proto` 对 `proto[1]` 缺类型保护，`[2, "4"]` / `[2, null]`
   会让 `proto[1] >= PROTO_CHAL_MIN_MINOR` 抛 TypeError 并逃穿 `_authenticate`。
③ R8-UI-02：`_video_seq_gaps` 全仓只写不读，接收路径的唯一接线零覆盖
   （审查实测：删掉它 874 例仍全绿）。
④ R8-UI-03：`net-046` 新增调用点的注释把可达路径写反（真正走到它的是 `Act.PROBE`）。

hermetic：不碰真实编码器/GPU；接收路径用真实 `_receiver_loop` + 本地回环 socket，
假共享端只按协议发字节。
"""
import os
import socket
import struct
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common
import viewer
from common import pack_video, parse_video_auto

# 真 NAL（Annex-B 4 字节起始码 + 假负载）。H.264/H.265 编码器输出恒为 4 字节起始码，
# 这正是"按强度择优"能区分两种布局的前提。
NAL = b"\x00\x00\x00\x01\x65\x88\x84\x00payload"
NAL_SHORT = b"\x00\x00\x01\x41\x9A"

# seq 取值：1 与 255 是"两边起始码强度同为 4"的歧义点，256/257/511 是
# `[256,511]` 区间里让判据误成立的点，512 是区间外第一个不受影响的奇数序，
# 65536/200001 覆盖高字节非零（前缀不构成起始码）的常规取值。
# seq 取值：256/257/511 是 `[256,511]` 区间里让修前判据误成立的点，512 是区间外
# 第一个不受影响的奇数序，65536/200001 覆盖高字节非零（前缀不构成起始码）的常规值。
# **不含 seq=1**：那是不可判定的真歧义（序号域与真 NAL 起始码逐字节相同），按既有
# 行为返回协商布局的结果，单列 `test_seq_one_ambiguity_keeps_negotiated_layout`。
SEQ_STEPS = (255, 256, 257, 511, 512, 65536, 200001)

#: `_assert_roundtrip` 的哨兵：按外层每个真实序号造 v2.4 帧（而不是固定一个序号）。
_VARY_SEQ = object()


def _declared_len(raw):
    """从整帧消息里取长度字段（保留给需要构造原始字节的用例）。"""
    return struct.unpack(">I", raw[1:5])[0]


class TestLayoutByteExactness(unittest.TestCase):
    """R8-NET-02：协商布局与实际布局不符时，原始 NAL 必须被逐字节原样取回。

    这里只覆盖**真实编码器形态**的帧（真 NAL 恒以 4 字节零填充起始码开头）——
    见 `test_three_byte_start_code_is_a_documented_dead_zone` 对判据边界的登记。
    """

    def _assert_roundtrip(self, send_seq, expect_seq, expect_seq_value):
        """造一帧 → 解一帧，断言 NAL 逐字节一致、seq 返回值符合**实际**布局。

        `send_seq` 取 `_VARY_SEQ` 表示"按外层每个真实序号造 v2.4 帧"（覆盖
        {1} ∪ [256,511] 这些修前会错解的取值），取 `None` 表示造 v2.3 帧。
        **不要传布尔**：`seq & 0xFFFFFFFF` 会把 `True` 变成 1，测试会退化成"只测
        seq=1"。
        """
        for seq in SEQ_STEPS:
            frame_seq = seq if send_seq is _VARY_SEQ else send_seq
            with self.subTest(send_seq=frame_seq, expect_seq=expect_seq, seq=seq):
                raw = pack_video(NAL, ts=1234, keyframe=True,
                                 codec=common.CODEC_H264, seq=frame_seq)
                ts, codec, flags, nal, got_seq = parse_video_auto(
                    raw[5:], expect_seq=expect_seq)
                self.assertEqual(
                    nal, NAL,
                    "seq=%d 被当成了 NAL 前导（expect_seq=%r，实际布局带 seq=%r）："
                    "解得 %s" % (seq, expect_seq, send_seq, nal[:8].hex(" ")))
                self.assertEqual(ts, 1234)
                self.assertEqual(codec, common.CODEC_H264)
                self.assertEqual(flags, common.VIDEO_FLAG_KEY)
                # 用 assertEqual 而不是 assertIs：真实序号可能正好是 1/None，
                # `assertIs(1, True)` 会因身份不同而假红。
                expected = frame_seq if expect_seq_value is _VARY_SEQ else expect_seq_value
                self.assertEqual(got_seq, expected)

    def test_sender_v24_viewer_negotiated_v23(self):
        """方向一（真实可达）：协商结果以为 v2.3，实际收到带真实序号的 v2.4 帧。

        生产里发送侧**无条件**带 seq（`host.py` 的 `local_seq` / `share.py` 的
        `SeqCounter`），而观看端协商到 v2.3 的路径是"旧共享端回包 proto=[2,3]"或
        序号协议协商缓存失效。此时必须按实际布局把 seq 解出来，NAL 不能被污染——
        修前 seq ∈ {1} ∪ [256,511] 会被错解（实测 libx264 下 520 帧丢 28 帧）。
        """
        self._assert_roundtrip(_VARY_SEQ, False, _VARY_SEQ)

    def test_sender_v23_viewer_negotiated_v24_is_a_documented_boundary(self):
        """方向二**是判据边界**，不是本轮能修的东西——如实登记而不假绿。

        协商结果以为 v2.4、实际收到 v2.3 帧时：按 v2.4 解会把真 NAL 的
        `00 00 00 01` 当 seq 吃掉（残余 NAL 强度 0 或 1），按 v2.3 解会把序号域
        当成 NAL 前导——**两边残留的起始码强度都不是 4**，而 `pack_video` 的载荷
        不带自描述字段，任何基于"解出的 NAL 好不好看"的判据都无法分辨。

        本条不做断言，只钉住两点事实：
          ① 本向在三处生产发送点里不可达（发送侧恒带 seq，`seq=None` 只有测试与
             `share.py` 的 `getattr` 兜底可达，见 `docs/audit/round8/net.md` 的
             R8-NET-01 穷举）；
          ② 判据在"真 NAL 用 4 字节起始码"这个硬约束下对**可达方向**是完备的。
        """
        raw = pack_video(NAL, ts=3)                     # 真 v2.3 帧
        self.assertEqual(len(raw) - 5, 10 + len(NAL), "v2.3 帧的载荷长度约定")
        # 不可判定：两份候选的强度都不是 4（上方推演），此处只固定推演本身
        self.assertEqual(common._annexb_strength(raw[5:][10:]), 4)   # v2.3 真 NAL
        self.assertEqual(
            common._annexb_strength(raw[5:][10:][4:] or b""), 0,
            "v2.3 帧被当 v2.4 解后残余的 NAL 已无起始码 → 判据无可采信之处")

    def test_three_byte_start_code_is_a_documented_dead_zone(self):
        """3 字节起始码的真 NAL 是判据的**真空区**——如实登记为已知限制。

        真 NAL 用 3 字节起始码时，"真 NAL 强度恒为 4"这条硬约束消失，判据失去支点。
        实测两种形态：
          · v2.4 残余 NAL 长于 4 字节（如 `seq = 0x00000101`）→ 残余以 `00 00 01`
            开头、强度同为 3，两端打平 → 按协商布局返回，交出错位 NAL；
          · v2.4 残余 NAL 只剩 1 字节 → 两端强度都为 0、一个候选都没有 →
            抛 ValueError（宁可丢帧也不交出错位 NAL）。
        本用例把这个限制**写成断言**而不是留成"没人知道"：真实编码器（H.264/H.265）
        恒用 4 字节起始码，`pack_video` 直接拼接编码器输出，所以生产不可达；若将来
        真要支持 3 字节起始码，必须先给载荷加自描述字段（版本或长度）。
        """
        nal = NAL_SHORT + b"padding-bytes"          # 21 字节
        seen = set()
        for seq in (1, 255, 256, 257, 511, 512, 65536, 200001):
            raw = pack_video(nal, ts=9, seq=seq)
            try:
                _, _, _, got, _ = parse_video_auto(raw[5:], expect_seq=False)
            except ValueError:
                seen.add("raise")
                continue
            if got == nal:
                seen.add("exact")
            else:
                # 错位形态只能是**协商布局的产物**：v2.3 侧 NAL = seq(4) + 真 NAL。
                # 这里不写死前 4 字节——seq 不同，前导就不同，只钉住"残留尾部就是真 NAL"。
                self.assertTrue(got.endswith(nal),
                                "出错位 NAL 的尾部不是真 NAL：%r" % (got,))
                self.assertGreater(len(got), len(nal))
                seen.add("misparse")
        # 两个方向都要如实记录：可判的取值必须原样取回，不可判的必须"要么抛错、
        # 要么是协商布局的产物"，不得出现别的形态。
        self.assertIn("exact", seen, "不与 seq 指纹撞车的取值仍必须原样取回")
        self.assertTrue(seen <= {"exact", "misparse", "raise"})

    def test_declared_length_beats_strength_when_both_look_annexb(self):
        """真 NAL 的两端强度差是判据的支点：v2.3 侧 4、被 seq 污染侧 3。

        这一条钉住"择优为什么成立"的硬约束：真 NAL（4 字节起始码）强度恒为 4，
        而按错布局解出的残余 NAL 顶多凑出 3 字节起始码。两端强度必须不同——不同才
        存在择优的支点，相同则判据无从下手（见 `seq == 1` 与 3 字节起始码两条登记）。
        """
        raw24 = pack_video(NAL, ts=7, seq=256)          # v2.4 帧
        payload = raw24[5:]
        as_v23 = common.parse_video(payload, expect_seq=False)[3]   # seq 被留在 NAL 前
        as_v24 = common.parse_video(payload, expect_seq=True)[3]
        self.assertEqual(common._annexb_strength(as_v24), 4, "真 NAL 强度恒为 4")
        self.assertEqual(common._annexb_strength(as_v23), 3,
                         "被 seq 前缀污染出的伪起始码只能是 3 字节")
        _, _, _, picked, picked_seq = parse_video_auto(payload, expect_seq=False)
        self.assertEqual(picked, NAL)
        self.assertEqual(picked_seq, 256)

    def test_annexb_strength_is_ordered(self):
        """`_annexb_strength` 是择优的排序键之一：4 字节起始码必须强于 3 字节。"""
        strength = common._annexb_strength
        self.assertEqual(strength(b"\x00\x00\x00\x01\x65"), 4)
        self.assertEqual(strength(b"\x00\x00\x01\x41"), 3)
        self.assertEqual(strength(b"\x00\x00\x00\x00"), 0)
        self.assertEqual(strength(b""), 0)
        self.assertEqual(strength(b"\x00\x00"), 0)

    def test_looks_like_annexb_semantics_unchanged(self):
        """既有判据的签名与返回值语义不得改变（可能有既有调用点）。"""
        self.assertTrue(common._looks_like_annexb(b"\x00\x00\x00\x01\x65"))
        self.assertTrue(common._looks_like_annexb(b"\x00\x00\x01\x41"))
        self.assertFalse(common._looks_like_annexb(b"\x00\x00\x02\x41"))
        self.assertFalse(common._looks_like_annexb(b"\x01\x65"))

    def test_seq_one_ambiguity_keeps_negotiated_layout(self):
        """`seq == 1` 的真歧义：仍按**协商布局**返回（既有行为不得翻转）。

        `seq == 1` 时序号域恰好是 `00 00 00 01`，与真 NAL 的起始码逐字节相同，
        两种布局解出的 NAL 强度同为 4——任何判据都无法区分，只能按协商结果定，
        这与修前行为一致（改动它会翻转既有判定）。
        """
        raw = pack_video(NAL, ts=5, seq=1)
        _, _, _, nal, got_seq = parse_video_auto(raw[5:], expect_seq=True)
        self.assertEqual(nal, NAL)
        self.assertEqual(got_seq, 1)
        _, _, _, nal23, got_seq23 = parse_video_auto(raw[5:], expect_seq=False)
        # 修前行为：按 v2.3 解出的 NAL 强度同为 4 → 第一个像起始码的就返回，
        # 于是这一向返回的是"协商布局"的产物（多带一截 seq）。逐字保持。
        self.assertEqual(nal23, b"\x00\x00\x00\x01" + NAL)
        self.assertIsNone(got_seq23)

    def test_three_byte_start_code_long_nal_is_byte_exact(self):
        """3 字节起始码的真 NAL：不与 seq 指纹撞车的取值仍必须原样取回。"""
        nal = NAL_SHORT + b"padding-bytes"
        for seq in (512, 65536, 200001):
            with self.subTest(seq=seq):
                raw = pack_video(nal, ts=9, seq=seq)
                _, _, _, got, got_seq = parse_video_auto(raw[5:], expect_seq=False)
                self.assertEqual(got, nal)
                self.assertEqual(got_seq, seq)

    def test_both_layouts_garbage_still_raises(self):
        """两种布局都不是 Annex-B 的载荷仍必须抛 ValueError（不得静默放行）。"""
        bad = struct.pack(">Q", 1) + b"\x01\x00" + b"\xff" * 20
        for expect in (True, False):
            with self.subTest(expect_seq=expect):
                with self.assertRaises(ValueError):
                    parse_video_auto(bad, expect_seq=expect)

    def test_short_payload_still_raises_for_both(self):
        with self.assertRaises(ValueError):
            parse_video_auto(b"\x00" * 9, expect_seq=False)
        with self.assertRaises(ValueError):
            parse_video_auto(b"\x00" * 9, expect_seq=True)


class TestProbeProtoRobustness(unittest.TestCase):
    """R8-UI-01：`_apply_probe_proto` 对非可比次版本不得抛异常。"""

    def _channel(self):
        ch = viewer.Channel.__new__(viewer.Channel)
        ch._host_minor = None
        ch._expect_seq = False
        ch._chal_capable = None
        return ch

    def test_non_comparable_minor_does_not_raise(self):
        """[2, "4"] / [2, null]：视为能力未知，既不抛也不误判为"不支持挑战-应答"。"""
        for minor in ("4", None, [], {}):
            with self.subTest(minor=minor):
                ch = self._channel()
                ch._apply_probe_proto({"ok": False, "proto": [2, minor]})
                self.assertIs(ch._chal_capable, False,
                              "非可比次版本必须落在 False（能力未知）而不是抛穿")
                self.assertFalse(ch._expect_seq)
                self.assertIsNone(ch._host_minor)

    def test_int_minor_is_still_capable(self):
        ch = self._channel()
        ch._apply_probe_proto({"ok": True, "proto": [2, 4]})
        self.assertIs(ch._chal_capable, True)
        self.assertTrue(ch._expect_seq)
        self.assertEqual(ch._host_minor, 4)

    def test_float_minor_judgement_is_not_flipped(self):
        """`_chal_capable` 的既有公式只比大小、不查类型——float 仍判 True。

        收窄成"必须真 int"会翻转既有部署的判定，这是本修复**刻意避免**的行为变化。
        """
        ch = self._channel()
        ch._apply_probe_proto({"ok": True, "proto": [2, 4.0]})
        self.assertIs(ch._chal_capable, True,
                      "把 float 次版本从 True 翻成 False 会改动既有部署判定")
        # 但 `_expect_seq` 的口径不变：非真 int 的次版本不采信
        self.assertIsNone(ch._host_minor)
        self.assertFalse(ch._expect_seq)

    def test_bool_minor_is_not_capable(self):
        """`isinstance(True, int)` 为 True——bool 不得被当成次版本。"""
        ch = self._channel()
        ch._apply_probe_proto({"ok": True, "proto": [2, True]})
        self.assertIs(ch._chal_capable, False)
        self.assertFalse(ch._expect_seq)
        self.assertIsNone(ch._host_minor)

    def test_wrong_major_or_missing_field(self):
        for reply in ({"ok": True, "proto": [3, 9]}, {"ok": True},
                      {"ok": True, "proto": [2]}, {"ok": True, "proto": "2.4"},
                      {"ok": True, "proto": None}):
            with self.subTest(reply=reply):
                ch = self._channel()
                ch._apply_probe_proto(reply)
                self.assertIs(ch._chal_capable, False)
                self.assertFalse(ch._expect_seq)
                self.assertIsNone(ch._host_minor)

    def test_non_dict_reply_is_safe(self):
        ch = self._channel()
        ch._apply_probe_proto(None)
        self.assertIs(ch._chal_capable, False)


class _FakeServer:
    """按协议在本地回环上扮演共享端：握手 → 认证回包 → 逐帧发视频。

    只做协议层，不做编码——观看端的接收泵才是被测对象。
    """

    def __init__(self, frames, proto=(2, 4)):
        self._frames = frames
        self._proto = list(proto)
        # 发完帧后停在这里等测试收尾——否则服务端一关连接，观看端的 `finally`
        # 会立刻 `_invalidate_media()` 把序号状态清零，断言就变成计时竞态。
        self.release = threading.Event()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        conn = None
        try:
            conn, _ = self._srv.accept()
            # 握手顺序与生产一致（`server_handshake`）：先读 MAGIC_CLIENT 再回 MAGIC_SERVER。
            # 反了会让观看端在认证期读到被 RST 的连接。
            common.server_handshake(conn)
            # 认证阶段：观看端先发一条 PROBE，这里回带 proto 的放行回包
            common.recv_msg(conn)
            common.send_msg(conn, {"ok": True, "msg": "", "proto": self._proto})
            for frame in self._frames:
                conn.sendall(frame)
            # 等测试驱动完接收路径再断开（最多 15 秒，避免用例异常时线程泄漏）
            self.release.wait(15.0)
            try:
                conn.shutdown(socket.SHUT_WR)
                conn.recv(4096)
            except OSError:
                pass
        except OSError:
            pass
        finally:
            for s in (conn, self._srv):
                try:
                    if s is not None:
                        s.close()
                except OSError:
                    pass

    def close(self):
        """放行服务端线程去关连接（用例收尾时调）。"""
        self.release.set()

    def join(self, timeout=10.0):
        self._thread.join(timeout)


def _video_frames(seqs, ts0=1_000_000):
    """按 v2.4 布局造一串视频帧消息。"""
    return [pack_video(NAL, ts=ts0 + i, keyframe=(i == 0), seq=s)
            for i, s in enumerate(seqs)]


class _UnavailableDecoder:
    """`available=False` 的解码器替身：让批次解码走"不可用"分支，不碰 FFmpeg。

    假 NAL 本来也解不出画面，但真实解码器每次都要建上下文——替身让用例既不依赖
    PyAV/FFmpeg，也不把解码器日志混进接收路径的断言。
    """

    available = False

    def set_codec(self, codec):
        pass

    def reset(self):
        pass

    def decode(self, nal):
        return []


class _RecvHarness(unittest.TestCase):
    """跑真实 `_receiver_loop` 的公共骨架（真实 Channel + 回环 socket）。"""

    def setUp(self):
        self.servers = []
        self.channels = []
        self.threads = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for srv in self.servers:
            srv.close()
        for ch in self.channels:
            try:
                ch._running = False
                ch.request_stop()
                ch.stop()
            except Exception:
                pass
        for t in self.threads:
            t.join(5.0)
        for srv in self.servers:
            srv.join(2.0)

    def _start(self, frames, name):
        # `Channel.__init__` 会 new 一个解码器，而 viewer 的重依赖（cv2/np/codec）
        # 走 `_load_heavy()` 惰性加载（A3/第 133 条）。本用例跑的是真实接收路径，
        # 所以必须先把重依赖加载起来；加载不了（CI 无 PyAV 等）就跳过而不是假绿。
        try:
            viewer._load_heavy()
        except Exception as exc:      # noqa: BLE001 - 环境缺依赖 → 跳过
            self.skipTest("viewer 重依赖不可用，无法构造真实 Channel：%s" % (exc,))
        srv = _FakeServer(frames)
        self.servers.append(srv)
        ch = viewer.Channel(name, "127.0.0.1:%d" % srv.port)
        self.channels.append(ch)
        # 假 NAL 不喂真实解码器：换成 `available=False` 的替身，让
        # `_decode_video_batch` 走既有的"解码器不可用 → 只告警一次"分支，
        # 既不把真实解码噪声混进断言，也不依赖 PyAV/FFmpeg 是否装好。
        ch._decoder = _UnavailableDecoder()
        ch._video_unavailable_warned = True      # 抑制一次性的 error 日志
        t = threading.Thread(target=ch._receiver_loop, name="t8-recv", daemon=True)
        self.threads.append(t)
        t.start()
        return ch, srv, t

    def _wait(self, predicate, timeout=12.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()


class TestSeqGapWiring(_RecvHarness):
    """R8-UI-02：接收路径必须真的把序号喂进缺口统计。

    与 `tests/test_net045_frame_seq.py` 的 `TestViewerGapAccounting` 的区别：
    那边直调 `_note_video_seq`，锁的是方法算术；这里跑**真实 `_receiver_loop`**
    ——删掉接收路径里那行接线，本类必红。
    """

    def test_receive_path_feeds_gap_accounting(self):
        """真实接收路径：序号 1、2、5 到达 → 缺口累计 2。"""
        ch, srv, t = self._start(_video_frames([1, 2, 5]), "gap")
        self.assertTrue(
            self._wait(lambda: ch._video_seq_gaps >= 2),
            "接收路径没有把序号喂进缺口统计（接线可能被删）："
            "（last=%r gaps=%r status=%r）"
            % (ch._last_video_seq, ch._video_seq_gaps, ch.status))
        self.assertEqual(ch._video_seq_gaps, 2, "缺 3、4 应记 2 帧缺口")
        self.assertEqual(ch._last_video_seq, 5)
        self.assertEqual(ch.status, "receiving")

    def test_contiguous_stream_records_no_gap(self):
        """对照：连续序号不得记任何缺口（防止把接线写成无条件累加）。"""
        ch, srv, t = self._start(_video_frames([1, 2, 3, 4]), "nocap")
        self.assertTrue(self._wait(lambda: ch._last_video_seq == 4),
                        "接收路径没有收到全部帧")
        self.assertEqual(ch._video_seq_gaps, 0)

    def test_reconnect_resets_gap_baseline(self):
        """跨连接：断线（`_invalidate_media`）必须把缺口与基线归零。

        序号空间随连接重开（host 的 `local_seq` 每进程从 1 起），不清基线的话
        新连接的序号会被算成巨大缺口。
        """
        ch, srv, t = self._start(_video_frames([10, 11, 20]), "reconnect")   # 缺口 8
        self.assertTrue(self._wait(lambda: ch._video_seq_gaps >= 8),
                        "第一轮连接没有累计出缺口")
        ch._running = False          # 服务端只服务一条连接；不让它继续重连
        self.assertTrue(
            self._wait(lambda: ch._video_seq_gaps == 0 and ch._last_video_seq is None,
                       timeout=8.0),
            "断线没有清序号基线（gaps=%r last=%r）"
            % (ch._video_seq_gaps, ch._last_video_seq))

    def test_gap_warning_is_logged_once_per_connection(self):
        """累计缺口跨过阈值时必须留一条带频道名与累计值的 warning，且只报一次。

        断言只落在**日志事件**上：接收路径与警告是同一个调用栈里的同步动作，
        而 `_video_seq_gaps` 会随后续连接/`_invalidate_media` 归零，用状态做断言
        会变成计时竞态。
        """
        ch, srv, t = self._start(_video_frames([1, 5, 20, 100]), "warnme")  # 缺口 96
        # 直接替换模块级 `log` 引用：`_maybe_warn_video_seq_gaps` 内部就查这个名字，
        # 用记录器替身比 patch 真实 logger 的方法更稳（真实 logger 可能被别处重绑）。
        records = []

        class _LogRecorder:
            def warning(self, fmt, *args):
                records.append(fmt % args if args else fmt)

            def __getattr__(self, _name):
                return lambda *a, **k: None

        with mock.patch.object(viewer, "log", _LogRecorder()):
            self.assertTrue(self._wait(lambda: ch._gap_warned),
                            "缺口过阈值没有置位一次性告警标志（last=%r gaps=%r）"
                            % (ch._last_video_seq, ch._video_seq_gaps))
            self.assertTrue(self._wait(lambda: len(records) >= 1, timeout=2.0),
                            "缺口过阈值没有产生带频道名的 warning")
            hits = [r for r in records if "warnme" in r]
            self.assertEqual(len(hits), 1,
                             "同一条连接只应报一次缺口告警（防刷屏），实得 %r" % hits)
            # 告警带的是"是时"的累计缺口：跨过阈值（8）的那一刻 ≥ 8，
            # 不写死具体数字（取决于 TCP 分块把哪几帧合进同一批）。
            num = int(hits[0].split("累计 ")[1].split(" 帧")[0])
            self.assertGreaterEqual(num, 8, "告警必须带上是时的累计缺口值")

    def test_switch_source_resets_gap_warning_state(self):
        """切源同样换序号空间：缺口与"已告警"标志都要归零，新源可以再报一次。"""
        ch = _switchable_channel()
        ch._video_seq_gaps = 40
        ch._gap_warned = True
        self.assertTrue(ch.switch_source("p1"))
        self.assertEqual(ch._video_seq_gaps, 0)
        self.assertFalse(ch._gap_warned,
                         "告警标志不复位会让新源永远报不出缺口告警")


class _NoopDecoder:
    """`switch_source` 只会 `set_codec` + `reset`，不碰真实解码上下文。"""

    def __init__(self):
        self.calls = []

    def set_codec(self, codec):
        self.calls.append(("set_codec", codec))

    def reset(self):
        self.calls.append(("reset",))


def _switchable_channel():
    """造一个能真跑 `switch_source` 完整路径的 Channel（真锁、真解码器替身）。"""
    ch = viewer.Channel.__new__(viewer.Channel)
    ch.lock = threading.RLock()
    ch.name = "sw"
    ch.peers = [{"id": "p1"}]
    ch.watch_source = "local"
    ch._got_key = False
    ch.video_mode = False
    ch.cur_codec = viewer.CODEC_H264
    ch.last_frame = None
    ch._gen = 0
    ch.frame_serial = 0
    ch._resync = False
    ch._fps_times = []
    ch._latency_samples = []
    ch.round_trip_ms = 0.0
    ch._lag_base_d = None
    ch._active_sock = object()
    ch._decoder = _NoopDecoder()
    ch._send_locked = lambda sock, obj, timeout=None: None
    ch._last_video_seq = None
    ch._video_seq_gaps = 0
    return ch


class TestNet046CommentAccuracy(unittest.TestCase):
    """R8-UI-03：`net-046` 新增调用点的注释必须与真实可达路径一致。"""

    def _branch_comment(self):
        """取"按原样透传 action/user/pass"那条分支收包之后的注释块。

        该分支的 `send_msg` 是**多行**调用，其后的 `reply = recv_msg(sock)` 之后再跟
        注释，然后是 `_apply_probe_proto(reply)`。
        """
        import inspect
        src = inspect.getsource(viewer.Channel._authenticate)
        anchor = '"action": action, "user": user, "pass": password'
        self.assertEqual(src.count(anchor), 1,
                         "该分支应恰好有一处按原样透传 action/user/pass 的收包")
        start = src.index(anchor)
        send_at = src.rindex("send_msg", 0, start)
        recv_marker = "reply = recv_msg(sock)"
        recv_at = src.index(recv_marker, send_at) + len(recv_marker)
        block = []
        for line in src[recv_at:].splitlines():
            if line.strip().startswith("#"):
                block.append(line)
            elif block:
                break
        return "\n".join(block)

    def test_comment_states_probe_only_reachability(self):
        block = self._branch_comment()
        self.assertIn("Act.PROBE", block,
                      "注释必须写明本分支只在 action == Act.PROBE 时可达")
        self.assertIn("LOGIN", block,
                      "注释必须说明带凭据的 LOGIN/REGISTER 走的是 1086 分支的探针")
        self.assertIn("1086", block,
                      "注释必须给出 LOGIN/REGISTER 的真实协商入口行号")

    def test_comment_no_longer_claims_credential_login(self):
        """旧注释声称"准入关闭部署全程走这条分支"——那是被写反的可达路径。"""
        block = self._branch_comment()
        self.assertNotIn("全程走这条分支", block,
                         "旧注释的可达路径描述必须被订正")

    def test_call_site_itself_is_preserved(self):
        """修复只订正注释，不得顺手删掉这个（必要且正确的）调用点。"""
        import inspect
        src = inspect.getsource(viewer.Channel._authenticate)
        anchor = '"action": action, "user": user, "pass": password'
        idx = src.index(anchor)
        tail = src[idx:src.index("except Exception", idx)]
        self.assertIn("_apply_probe_proto(reply)", tail)


if __name__ == "__main__":
    unittest.main()
