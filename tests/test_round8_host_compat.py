# -*- coding: utf-8 -*-
"""net-047（第 8 轮审查 R8-NET-01，2026-10-10）"明确拒绝旧观看端"的回归用例。

背景：协议 v2.4 的视频帧载荷带 4 字节序号，而**显式声明**次版本 < 4 的观看端那条
老解析路径（10 字节头、无起始码判据、无回退）会把序号当成 NAL 前导；序号落在
`[256, 511]` 区间时这 4 字节恰好构成起始码 + NAL 头，参考链断裂、画面周期性
冻结。两端日志都正常，属最难排查的形态。

发送侧无法消除该损伤（`seq=None` 的 v2.3 出口在生产代码里不可达），用户选择
"明确拒绝"：认证期按对端自报的次版本直接断开并给出可执行的文案，而不是让它带着
损伤画面继续跑。

本文件钉住三件事：
  1. 判据口径（`common.video_seq_capable`）：哪些 proto 该拒、哪些不该；
  2. **刻意保留的缺口**：不带 proto 的对端仍然放行（否则打断 share.py 中继路径）；
  3. 三条入口的接线锁（net-047 的真正判定力：改回只告警必须让它们转红）。
"""
import socket
import threading
import unittest
from unittest import mock

import common
import host


class TestVideoSeqCapable(unittest.TestCase):
    """判据口径：只对"主版本相同 + 显式声明次版本 < VIDEO_SEQ_MINOR"判为不能解。"""

    def test_old_minor_is_incapable(self):
        """旧观看端（proto=[2,3]，即 1.4.0）必须判为不能解。"""
        self.assertEqual(common.video_seq_capable({"proto": [2, 3]}),
                         (True, False))

    def test_current_minor_is_capable(self):
        """同版本（[2,4]）必须放行——否则新版观看端全部连不上。"""
        self.assertEqual(common.video_seq_capable({"proto": [2, 4]}),
                         (True, True))

    def test_newer_minor_is_capable(self):
        """比服务端新的次版本同样放行（服务端按自己的版本发帧，对端会回退）。"""
        self.assertEqual(common.video_seq_capable({"proto": [2, 9]}),
                         (True, True))

    def test_different_major_is_not_this_gate(self):
        """主版本不同走既有的"协议版本不兼容"分支，不得在这里被当成"不能解"。"""
        self.assertEqual(common.video_seq_capable({"proto": [3, 3]}),
                         (True, True))

    def test_float_minor_still_rejected(self):
        """次版本口径有意放宽到 float：`[2, 3.0]` 声明的还是 v2.3，必须拒。"""
        self.assertEqual(common.video_seq_capable({"proto": [2, 3.0]}),
                         (True, False))

    def test_bool_minor_is_not_a_version(self):
        """`isinstance(True, int)` 为真——`[2, true]` 是畸形载荷，不当次版本读。"""
        self.assertEqual(common.video_seq_capable({"proto": [2, True]}),
                         (False, True))

    def test_malformed_proto_never_raises(self):
        """未认证对端可控字段：任何畸形形态都只返回 (False, True)，不得抛异常。

        抛出去会被认证期的兜底 except 记成"认证通信失败"并断开——把判据的健壮性
        问题升级成登录故障，且日志原因与真实原因不符。
        """
        for bad in ({"proto": [2, "4"]}, {"proto": [2, None]}, {"proto": [2]},
                    {"proto": []}, {"proto": "2.4"}, {"proto": None}, {},
                    [1, 2], None, "not-a-dict"):
            with self.subTest(msg=bad):
                self.assertEqual(common.video_seq_capable(bad), (False, True))

    def test_tuple_proto_is_a_legal_carrier(self):
        """元组是合法载体（JSON 往返不会产出，但内部调用可能传），须按值判。"""
        self.assertEqual(common.video_seq_capable({"proto": (2, 3)}),
                         (True, False))


class TestAbsentProtoStillAllowed(unittest.TestCase):
    """刻意保留的缺口：没有 proto 字段的对端**不得**被这条门控拒绝。

    这是本条修复最容易引入回归的地方。不带 proto 的对端有两类，host 在认证期
    无法区分：
      - 旧版 share.py 中继出来的 peer：它只**发**帧、不解码，放行才对
        （`test_multiview_integration.py` 的 `join_hub` 就是这种形态）；
      - 更早的观看端：确实该拒，但识别它需要改握手，属于另一件事。
    判据选择"看不见就不拒"：宁可让极少数更早的观看端继续花屏（它同样会在
    挑战-应答门控上撞壁），也不能把中继路径打死。
    """

    def test_absent_proto_is_not_rejected(self):
        self.assertIsNone(host._legacy_viewer_seq_reason("10.0.0.9:1", {}))

    def test_absent_proto_emits_no_warning(self):
        """放行时不得留下任何 warning——否则每次旧 peer 入册都刷一条噪音日志。"""
        with mock.patch.object(host.log, "warning") as warn:
            host._legacy_viewer_seq_reason("10.0.0.9:1", {"action": "probe"})
        self.assertEqual(warn.call_args_list, [])

    def test_relay_peer_without_proto_is_admitted(self):
        """端到端：无 proto 的 probe 在准入关闭路径上必须被放行（回归护栏）。

        这是 share.py 中继路径的最小复现——删掉本用例断言的那行拒绝，peer 就
        连不上 hub，MultiView 整条功能失效。
        """
        a, b = socket.socketpair()
        try:
            b.settimeout(5.0)
            t = threading.Thread(
                target=host._auth_optional_phase, args=(a, ("127.0.0.1", 1)),
                daemon=True)
            t.start()
            common.send_msg(b, {"action": common.Act.PROBE, "user": "", "pass": ""})
            reply = common.recv_msg(b)
            self.assertTrue(reply.get("ok"), reply)
        finally:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass


class TestRejectReason(unittest.TestCase):
    """`_legacy_viewer_seq_reason` 的返回值与日志口径。"""

    def _call(self, addr, msg):
        with mock.patch.object(host.log, "warning") as warn:
            reason = host._legacy_viewer_seq_reason(addr, msg)
        return reason, warn.call_args_list

    def test_old_minor_returns_shared_message(self):
        """拒绝文案必须是协议层那一份常量（host 发它、viewer 认它，两端同源）。"""
        reason, calls = self._call("10.0.0.9:52341", {"proto": [2, 3]})
        self.assertEqual(reason, common.PROTO_SEQ_REJECT_MSG)
        self.assertEqual(len(calls), 1)

    def test_log_names_both_ends(self):
        """日志要能指名道姓：地址、对端次版本、服务端版本，缺一项都排障不动。"""
        _, calls = self._call("10.0.0.9:52341", {"proto": [2, 3]})
        rendered = calls[0][0][0] % calls[0][0][1:]
        self.assertIn("10.0.0.9:52341", rendered)
        # 服务端版本按 PROTOCOL_VERSION 元组原样渲染（协议层的单一真源，
        # 不在日志里另抄一份字符串——那才是下一次版本升级会漏改的地方）
        self.assertIn(str(common.PROTOCOL_VERSION), rendered)
        self.assertIn("[2, 3]", rendered)      # 对端自报的次版本
        self.assertIn("冻结", rendered)

    def test_capable_viewer_returns_none(self):
        reason, calls = self._call("10.0.0.9:1", {"proto": [2, 4]})
        self.assertIsNone(reason)
        self.assertEqual(calls, [])

    def test_reject_message_is_distinct_from_chal_gate(self):
        """与 `PROTO_CHAL_MIN_MINOR` 那条的文案必须不同。

        viewer 端按字面量分流："观看端版本过旧，请更新后重试"会复位挑战能力缓存
        并**立即重试**（对 v2.2/v2.3 观看端是正确的，它们重试一次就能连上）；
        本条是终态——换观看端才行。两个场景共用一个字面量会让前者误以为重试有用，
        或者让后者永远重试下去。
        """
        self.assertNotEqual(common.PROTO_SEQ_REJECT_MSG,
                            "观看端版本过旧，请更新后重试")

    def test_non_dict_msg_returns_none(self):
        reason, calls = self._call("10.0.0.9:1", ["proto", 2, 3])
        self.assertIsNone(reason)
        self.assertEqual(calls, [])


class TestRejectWiringInAllThreeEntries(unittest.TestCase):
    """接线锁：认证期共有**三处**读 proto 的入口，三处都必须拒绝。

    这是 net-047 真正的判定力来源。旧实现只在 `_auth_required_phase` 与
    `_auth_optional_phase` 留了告警，第三处（`_dispatch_ctrl` 的迟到认证分支，
    第 100 条的兜底）没有——而高延迟/隧道场景下 probe 晚于 2 秒可选 auth 窗口
    才到，那条路径正是慢 probe 的必经之路。只改前两处等于给旧观看端留了一条
    每 2 秒重连一次的后门。
    """

    def _src(self, fn):
        import inspect
        return inspect.getsource(fn)

    def test_required_phase_rejects(self):
        src = self._src(host._auth_required_phase)
        self.assertIn("_legacy_viewer_seq_reason", src)
        self.assertIn("_reject_and_close", src)
        self.assertNotIn("legacy_warned", src)   # 去重标志随拒绝一起退休

    def test_optional_phase_rejects(self):
        """准入关闭路径：拒绝后必须 close + 返回 False（不是只发一句就放行）。"""
        src = self._src(host._auth_optional_phase)
        self.assertIn("_legacy_viewer_seq_reason", src)
        idx = src.index("_legacy_viewer_seq_reason")
        tail = src[idx:idx + 400]
        self.assertIn("AUTH_RESULT", tail)
        self.assertIn("sock.close()", tail)
        self.assertIn("return False, None", tail)

    def test_dispatch_ctrl_late_auth_rejects(self):
        """第 100 条的迟到认证分支——最容易被漏掉的那一处。"""
        src = self._src(host._dispatch_ctrl)
        self.assertIn("_legacy_viewer_seq_reason", src)
        self.assertIn("观看端版本过旧", src)

    def test_dispatch_ctrl_rejection_is_fatal(self):
        """迟到分支必须返回致命原因断开，不能只回一句就放行。

        否则旧观看端会按既有节奏每 2 秒重连一次，把未认证名额和日志刷满——
        拒绝反而比不拒绝更吵。
        """
        src = self._src(host._dispatch_ctrl)
        idx = src.index("_legacy_viewer_seq_reason")
        tail = src[idx:idx + 900]
        self.assertIn("return", tail)


class TestEndToEndRejectOverSocket(unittest.TestCase):
    """socket 级端到端：旧观看端真的拿不到 AUTH_RESULT 放行，而是被断开。"""

    def test_required_phase_closes_on_old_minor(self):
        """准入开启路径：首个 probe 就该被拒（不是登录失败、不是静默断连）。"""
        a, b = socket.socketpair()
        out = {}
        try:
            b.settimeout(5.0)
            rt = mock.Mock()
            rt.accounts = mock.Mock()
            t = threading.Thread(
                target=lambda: out.update(
                    result=host._auth_required_phase(
                        a, ("127.0.0.1", 1), rt, {"auth_timeout": 5})),
                daemon=True)
            t.start()
            common.send_msg(b, {"action": common.Act.PROBE, "user": "", "pass": "",
                                "proto": [2, 3]})
            reply = common.recv_msg(b)
            self.assertFalse(reply.get("ok"), reply)
            self.assertEqual(reply.get("msg"), common.PROTO_SEQ_REJECT_MSG)
            t.join(timeout=5)
            self.assertEqual(out.get("result"), (False, None))
        finally:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass

    def test_optional_phase_closes_on_old_minor(self):
        """准入关闭路径：同一条判定同样生效（这是"加了一处漏一处"的高发区）。"""
        a, b = socket.socketpair()
        try:
            b.settimeout(5.0)
            t = threading.Thread(
                target=host._auth_optional_phase, args=(a, ("127.0.0.1", 1)),
                daemon=True)
            t.start()
            common.send_msg(b, {"action": common.Act.PROBE, "user": "", "pass": "",
                                "proto": [2, 3]})
            reply = common.recv_msg(b)
            self.assertFalse(reply.get("ok"), reply)
            self.assertEqual(reply.get("msg"), common.PROTO_SEQ_REJECT_MSG)
        finally:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
