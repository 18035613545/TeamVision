# -*- coding: utf-8 -*-
"""net-045（2026-10-09，协议 v2.4）：视频帧单调递增帧序号。

背景：此前视频帧载荷只有 ts/codec/flags，没有序号。缺序号时接收端一旦发现损伤
只能整体重置解码器，把本来还能解码的帧一并丢掉——裸 TCP 队头阻塞下，一个分片
丢失会连带其后所有帧在内核缓冲区等待重传。加序号是为了把"整批重置"降级为
"丢弃到下一个可解码点"。

编号说明：最初按 net-042 起草，但 `docs/audit/net.md` 里 net-042~net-044 已被
既有条目占用，故统一改用 net-045。

本文件钉住四件事：
  ① 线格式：v2.4 载荷头 14 字节（ts8|codec1|flags1|seq4），v2.3 仍为 10 字节；
     往返一致，且 codec/flags 位置不变（旧布局解析不受影响）。
  ② 混合版本安全：旧版 share.py 的共享者经新版 host 中继给新版观看端时，
     parse_video_auto 必须按 Annex-B 起始码自动回退，而不是把 NAL 前 4 字节
     当序号吃掉、整帧变花屏且无任何报错。
  ③ 序号语义：SeqCounter 单调递增、并发不重号、32 位回绕不误报缺口；
     观看端 _note_video_seq 的缺口统计按相对增量算。
  ④ 回绕边界真的丢帧时仍要计数（这是 SeqCounter 不跳过 0 的原因）。

hermetic：不碰网络、不碰解码器、不依赖真实编码器。
"""
import os
import struct
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common
from common import SeqCounter, pack_video, parse_video, parse_video_auto

# 真实形态的 NAL：Annex-B 4 字节零填充起始码 + 假负载
NAL = b"\x00\x00\x00\x01\x65\x88\x84\x00payload"
NAL_SHORT = b"\x00\x00\x01\x41\x9A"          # 3 字节起始码


class TestWireFormat(unittest.TestCase):
    def test_v23_layout_unchanged(self):
        """seq=None 必须逐字节等于旧布局，否则会打断未升级的对端。"""
        raw = pack_video(NAL, ts=42, keyframe=True, codec=common.CODEC_HEVC)
        self.assertEqual(raw[0], common.MSG_VIDEO)
        (length,) = struct.unpack(">I", raw[1:5])
        self.assertEqual(length, 10 + len(NAL))     # 载荷头仍是 10 字节
        ts, codec, flags, nal, seq = parse_video(raw[5:], expect_seq=False)
        self.assertEqual((ts, codec, flags, nal), (42, common.CODEC_HEVC, 1, NAL))
        self.assertIsNone(seq)

    def test_v24_layout_carries_seq(self):
        raw = pack_video(NAL, ts=42, keyframe=True, codec=common.CODEC_HEVC, seq=777)
        (length,) = struct.unpack(">I", raw[1:5])
        self.assertEqual(length, 14 + len(NAL))     # 载荷头 14 字节
        ts, codec, flags, nal, seq = parse_video(raw[5:], expect_seq=True)
        self.assertEqual((ts, codec, flags, nal, seq),
                         (42, common.CODEC_HEVC, 1, NAL, 777))

    def test_roundtrip_is_byte_exact(self):
        raw = pack_video(NAL, ts=7, keyframe=False, seq=2 ** 32 - 1)
        ts, codec, flags, nal, seq = parse_video(raw[5:], expect_seq=True)
        repacked = pack_video(nal, ts=ts, keyframe=bool(flags & 1),
                              codec=codec, seq=seq)
        self.assertEqual(repacked, raw)

    def test_seq_wraps_to_32bit(self):
        """超 32 位的 seq 必须被截断而不是让 struct 抛错（else 分支会静默丢帧）。"""
        raw = pack_video(NAL, ts=1, seq=2 ** 32 + 5)
        _, _, _, _, seq = parse_video(raw[5:], expect_seq=True)
        self.assertEqual(seq, 5)

    def test_truncated_payload_raises(self):
        for expect in (False, True):
            with self.assertRaises(ValueError):
                parse_video(b"\x00" * 9, expect_seq=expect)

    def test_return_arity_is_always_five(self):
        """元组长度必须恒定，否则调用点的解包会随协议版本错位。"""
        raw23 = pack_video(NAL, ts=1)
        raw24 = pack_video(NAL, ts=1, seq=3)
        self.assertEqual(len(parse_video(raw23[5:], expect_seq=False)), 5)
        self.assertEqual(len(parse_video(raw24[5:], expect_seq=True)), 5)


class TestMixedVersionSafety(unittest.TestCase):
    """布局无法自描述，混合版本必须靠 Annex-B 起始码自动回退。"""

    def test_auto_prefers_negotiated_v24(self):
        raw = pack_video(NAL, ts=3, seq=99)
        ts, codec, flags, nal, seq = parse_video_auto(raw[5:], expect_seq=True)
        self.assertEqual(seq, 99)
        self.assertEqual(nal, NAL)

    def test_auto_falls_back_to_v23_when_sender_is_legacy(self):
        """观看端谈成 v2.4，却收到旧版共享者经 host 中继来的 v2.3 帧。

        这是真实可达路径：MultiView 允许旧 share.py 连新版 host。若不回退，
        按 v2.4 解会把 NAL 前 4 字节当 seq 吃掉，剩下 `01 65 …` 花屏且无报错。
        """
        raw = pack_video(NAL, ts=3)                # 旧布局，无 seq
        ts, codec, flags, nal, seq = parse_video_auto(raw[5:], expect_seq=True)
        self.assertEqual(nal, NAL, "v2.4 期望下把 v2.3 帧解坏了")
        self.assertIsNone(seq)

    def test_auto_falls_back_to_v24_when_sender_is_new(self):
        """反向：观看端谈成 v2.3，但帧带了 seq。"""
        raw = pack_video(NAL, ts=3, seq=11)
        ts, codec, flags, nal, seq = parse_video_auto(raw[5:], expect_seq=False)
        self.assertEqual(nal, NAL)
        self.assertEqual(seq, 11)

    def test_auto_handles_three_byte_start_code(self):
        raw = pack_video(NAL_SHORT, ts=1, seq=4)
        _, _, _, nal, _ = parse_video_auto(raw[5:], expect_seq=True)
        self.assertEqual(nal, NAL_SHORT)

    def test_auto_rejects_garbage(self):
        """既不是 v2.3 也不是 v2.4 的载荷必须报错，不能静默放行坏 NAL。"""
        bad = struct.pack(">Q", 1) + b"\x01\x00" + b"\xff" * 20
        with self.assertRaises(ValueError):
            parse_video_auto(bad, expect_seq=True)


class TestSeqCounter(unittest.TestCase):
    def test_monotonic_and_starts_at_one(self):
        c = SeqCounter()
        self.assertEqual([c.next() for _ in range(3)], [1, 2, 3])

    def test_wraps_through_zero(self):
        """回绕必须经过 0，不能跳过。

        接收侧按 `(seq - last) & 0xFFFFFFFF` 算相对增量。若计数器跳过 0，
        `0xFFFFFFFF → 1` 会被算成 delta=2，凭空记一次丢帧——回绕反而制造假告警。
        """
        c = SeqCounter(start=0xFFFFFFFF)
        self.assertEqual([c.next() for _ in range(3)],
                         [0xFFFFFFFF, 0, 1])

    def test_thread_safety_no_duplicate(self):
        """并发取号不得重复——重复序号会让接收端把重传误判成丢帧。"""
        c = SeqCounter()
        got = []
        lock = threading.Lock()

        def worker():
            local = [c.next() for _ in range(200)]
            with lock:
                got.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(got), 800)
        self.assertEqual(len(set(got)), 800, "并发取号出现重复")


class TestViewerGapAccounting(unittest.TestCase):
    """观看端 _note_video_seq：按相对增量统计缺口，32 位回绕不得误报。"""

    def _channel(self):
        import viewer
        ch = viewer.Channel.__new__(viewer.Channel)
        ch._last_video_seq = None
        ch._video_seq_gaps = 0
        return ch

    def test_contiguous_has_no_gap(self):
        ch = self._channel()
        for s in (1, 2, 3):
            ch._note_video_seq(s)
        self.assertEqual(ch._video_seq_gaps, 0)

    def test_gap_is_counted(self):
        ch = self._channel()
        for s in (1, 2, 5):      # 缺 3、4
            ch._note_video_seq(s)
        self.assertEqual(ch._video_seq_gaps, 2)

    def test_duplicate_is_not_a_gap(self):
        """同一序号重传（delta=0）不是丢帧。"""
        ch = self._channel()
        ch._note_video_seq(10)
        ch._note_video_seq(10)
        self.assertEqual(ch._video_seq_gaps, 0)

    def test_clean_wraparound_is_not_a_gap(self):
        """0xFFFFFFFF → 0 → 1 是正常回绕，不得误报成"丢了 4 billion 帧"。"""
        ch = self._channel()
        for s in (0xFFFFFFFF, 0, 1):
            ch._note_video_seq(s)
        self.assertEqual(ch._video_seq_gaps, 0)

    def test_lost_frame_across_wrap_is_still_counted(self):
        """回绕边界真的丢帧时仍要计数——这正是 SeqCounter 不跳过 0 的原因：
        若跳过 0，0xFFFFFFFF → 1 会被算成 delta=2，回绕凭空造一次假告警。"""
        ch = self._channel()
        ch._note_video_seq(0xFFFFFFFF)
        ch._note_video_seq(1)          # 序号 0 真的丢了
        self.assertEqual(ch._video_seq_gaps, 1)

    def test_none_seq_is_ignored(self):
        """v2.3 对端没有 seq，此时不能产生任何缺口统计。"""
        ch = self._channel()
        ch._note_video_seq(None)
        self.assertEqual(ch._video_seq_gaps, 0)
        self.assertIsNone(ch._last_video_seq)


class _NoopDecoder:
    """`switch_source` 只会 `set_codec` + `reset`，不碰真实解码上下文。"""

    def __init__(self):
        self.calls = []

    def set_codec(self, codec):
        self.calls.append(("set_codec", codec))

    def reset(self):
        self.calls.append(("reset",))


class TestNet046ReviewFixes(unittest.TestCase):
    """net-046（2026-10-10）代码审查发现的两处遗漏。

    ① 切源不重置帧序号基线 → 切到共享更久的队友时凭空记 ~20 万帧丢失；
    ② 准入关闭（`auth.enabled=false`）部署全程走 `Act.PROBE` 分支，而该分支
       从不读回包里的 `proto`，`_expect_seq` 终身停在 False。
    """

    def setUp(self):
        import viewer
        self.viewer = viewer

    # ---------- ① 切源基线 ----------

    def _switchable_channel(self):
        """造一个能真跑 `switch_source` 完整路径的 Channel。"""
        import threading
        ch = self.viewer.Channel.__new__(self.viewer.Channel)
        ch.lock = threading.RLock()
        ch.name = "t"
        ch.peers = [{"id": "p1"}, {"id": "p2"}]
        ch.watch_source = "local"
        ch._got_key = True
        ch.video_mode = True
        ch.cur_codec = self.viewer.CODEC_H264
        ch.last_frame = object()
        ch._gen = 3
        ch.frame_serial = 7
        ch._resync = True
        ch._fps_times = []
        ch._latency_samples = []
        ch.round_trip_ms = 12.0
        ch._lag_base_d = 0.5
        ch._active_sock = object()
        ch._decoder = _NoopDecoder()
        ch._send_locked = lambda sock, obj, timeout=None: None
        # net-045 序号状态：先在 local 上收到 50，再切到已共享很久的 peer（200001）
        ch._last_video_seq = 50
        ch._video_seq_gaps = 0
        return ch

    def test_switch_source_resets_seq_baseline(self):
        """切源后基线必须清空，否则下一帧的巨大 delta 会被算成丢帧。

        真调 `switch_source`（不是复刻它的几行）——复刻出来的测试无法证明生产代码
        真的做了这件事。
        """
        ch = self._switchable_channel()
        self.assertEqual(ch._last_video_seq, 50)
        self.assertTrue(ch.switch_source("p1"))
        self.assertIsNone(ch._last_video_seq,
                          "切源必须清序号基线：host 的 local_seq 与各 peer 的 "
                          "SeqCounter 是彼此独立的计数空间")
        self.assertEqual(ch._video_seq_gaps, 0)

    def test_switch_then_new_source_has_no_phantom_gap(self):
        """端到端：真切源后收到该源的首帧，不得产生任何缺口。"""
        ch = self._switchable_channel()
        ch._note_video_seq(50)              # local 上收到第 50 帧
        self.assertTrue(ch.switch_source("p1"))
        ch._note_video_seq(200001)          # 该 peer 已共享很久，首帧就到这个号
        self.assertEqual(ch._video_seq_gaps, 0,
                         "新源的序号空间与旧源无关，不得被算成丢了 199950 帧")

    def test_switch_source_keeps_unrelated_resets(self):
        """对照：切源原有的重置项一个都不能少（防止修复时误删邻近状态）。"""
        ch = self._switchable_channel()
        ch._note_video_seq(50)
        self.assertTrue(ch.switch_source("p1"))
        self.assertFalse(ch._got_key)
        self.assertFalse(ch.video_mode)
        self.assertIsNone(ch.last_frame)
        self.assertEqual(ch._gen, 4)
        self.assertEqual(ch.watch_source, "p1")

    # ---------- ② 匿名放行部署的 proto 协商 ----------

    def test_apply_probe_proto_enables_seq_for_v24(self):
        ch = self.viewer.Channel.__new__(self.viewer.Channel)
        ch._expect_seq = False
        ch._apply_probe_proto({"ok": True, "proto": [2, 4]})
        self.assertTrue(ch._expect_seq)
        self.assertEqual(ch._host_minor, 4)
        self.assertTrue(ch._chal_capable)

    def test_apply_probe_proto_disables_seq_for_v23(self):
        ch = self.viewer.Channel.__new__(self.viewer.Channel)
        ch._expect_seq = True
        ch._apply_probe_proto({"ok": True, "proto": [2, 3]})
        self.assertFalse(ch._expect_seq, "v2.3 对端仍按 10 字节头解")
        self.assertEqual(ch._host_minor, 3)

    def test_apply_probe_proto_missing_field_is_safe(self):
        """旧共享端不返 proto：不得抛异常，也不得误开 v2.4。"""
        ch = self.viewer.Channel.__new__(self.viewer.Channel)
        ch._expect_seq = False
        ch._apply_probe_proto({"ok": True})
        self.assertFalse(ch._expect_seq)
        self.assertIsNone(ch._host_minor)

    def test_apply_probe_proto_rejects_bool_minor(self):
        """`isinstance(True, int)` 为 True——proto=[2, true] 不得被当成次版本 1。"""
        ch = self.viewer.Channel.__new__(self.viewer.Channel)
        ch._expect_seq = False
        ch._apply_probe_proto({"ok": True, "proto": [2, True]})
        self.assertFalse(ch._expect_seq, "bool 不得被当成 int 次版本")
        self.assertIsNone(ch._host_minor)

    def test_anon_branch_calls_apply_probe_proto(self):
        """钉住 `Act.PROBE`（准入关闭）分支真的调了 `_apply_probe_proto`。

        按该分支的唯一指纹（`Act.WATCH` 之外的收包后紧跟赋值）定位，而不是靠
        `split("else:")`——后者会把前一个 else 的内容也圈进来，删了也照样绿。
        """
        import inspect
        src = inspect.getsource(self.viewer.Channel._authenticate)
        # 用**代码锚点**（而非字符窗口）定位准入关闭分支：那条分支独有的收包语句
        anchor = '"action": action, "user": user, "pass": password'
        self.assertEqual(src.count(anchor), 1,
                         "准入关闭分支应恰好有一处按原样透传 action/user/pass 的收包")
        idx = src.index(anchor)
        tail = src[idx:src.index("except Exception", idx)]
        self.assertIn("_apply_probe_proto", tail,
                      "准入关闭部署走的分支必须协商 proto（host.py:2150 的回包带 proto）")


if __name__ == "__main__":
    unittest.main()
