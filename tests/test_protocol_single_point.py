# -*- coding: utf-8 -*-
"""第 135 条（C2 §7 第 1 步）回归：action 常量表单点，两端不再出现裸字面量。

AST 锁三条纪律：
1. `{"action": ...}` 字典字面量的取值必须是 `Act.X` 属性访问，不能是裸字符串；
2. 对名为 action 的局部变量做 `==`/`in` 比较时，右操作数不能是协议动作裸字符串；
3. `Act` 常量的值必须与线上协议字符串逐一对应（防止重构时手滑改值）。
"""
import ast
import unittest
from unittest import mock

import common
from common import MSG_CTRL, MSG_FRAME, MSG_VIDEO, Act, message_pump

_ACTIONS = {"auth_result", "req_keyframe", "watch_reject", "watch", "unshare",
            "register", "login", "probe", "ping", "pong", "peers", "cap",
            "cap_probe"}


def _scan(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    hits = []
    for node in ast.walk(tree):
        # 规则 1：{"action": <必须 Act.X>}
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values, strict=True):
                if isinstance(k, ast.Constant) and k.value == "action":
                    # 只禁"裸协议字面量"；`{"action": action}` 这类运行期变量的
                    # 取值源头（登录框返回值）由规则 2 + 替换点保证已是 Act.X
                    if isinstance(v, ast.Constant) and v.value in _ACTIONS:
                        hits.append("%s:%d dict action=%r"
                                    % (path, node.lineno, v.value))
        # 规则 2：action == "字面量" / action in ("字面量", ...)
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) \
                and node.left.id == "action":
            for comp in node.comparators:
                strs = []
                if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    strs.append(comp.value)
                if isinstance(comp, (ast.Tuple, ast.List)):
                    strs = [e.value for e in comp.elts
                            if isinstance(e, ast.Constant)
                            and isinstance(e.value, str)]
                for s in strs:
                    if s in _ACTIONS:
                        hits.append("%s:%d compare action==%r"
                                    % (path, node.lineno, s))
        # 规则 2b：action = "字面量"（赋值源头也必须走 Act，第 1 步落地时
        # 曾因正则 \b 边界漏掉三处赋值——这条锁就是防它复发）
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "action" in targets:
                v = node.value
                if isinstance(v, ast.Tuple):
                    for e in v.elts:
                        if isinstance(e, ast.Constant) and e.value in _ACTIONS:
                            hits.append("%s:%d assign action=%r"
                                        % (path, node.lineno, e.value))
                elif isinstance(v, ast.Constant) and v.value in _ACTIONS:
                    hits.append("%s:%d assign action=%r"
                                % (path, node.lineno, v.value))
    return hits


class TestActionSinglePoint(unittest.TestCase):

    def test_no_bare_action_literals_in_host_and_viewer(self):
        hits = _scan("host.py") + _scan("viewer.py")
        self.assertEqual(hits, [], "存在裸 action 字面量: %s" % hits)

    def test_act_values_match_wire_protocol(self):
        self.assertEqual(Act.AUTH_RESULT, "auth_result")
        self.assertEqual(Act.REQ_KEYFRAME, "req_keyframe")
        self.assertEqual(Act.WATCH_REJECT, "watch_reject")
        self.assertEqual(Act.WATCH, "watch")
        self.assertEqual(Act.UNSHARE, "unshare")
        self.assertEqual(Act.REGISTER, "register")
        self.assertEqual(Act.LOGIN, "login")
        self.assertEqual(Act.PROBE, "probe")
        self.assertEqual(Act.PING, "ping")
        self.assertEqual(Act.PONG, "pong")
        self.assertEqual(Act.PEERS, "peers")
        self.assertEqual(Act.CAP, "cap")
        self.assertEqual(Act.CAP_PROBE, "cap_probe")
        # D1（实现方案 §10）：挑战-应答认证
        self.assertEqual(Act.CHAL, "chal")
        self.assertEqual(Act.AUTH_PROOF, "auth_proof")
        self.assertEqual(Act.REG_PROOF, "reg_proof")

    def test_protocol_version_is_single_carrier(self):
        # C2：PROTOCOL_VERSION 是唯一的版本载体；2.3 = proof 绑定用户名（auth-002），
        # 2.4 = 视频帧带单调递增帧序号（net-045），
        # 2.5 = 观看端→共享端的质量反馈（net-050，控制消息，**不动视频载荷布局**）
        self.assertEqual(Act.__dict__.get("AUTH_RESULT"), "auth_result")
        self.assertEqual(common.PROTOCOL_VERSION, (2, 5))
        self.assertEqual(common.MAGIC_CLIENT, b"FS02")
        # net-050：2.5 只加了控制消息，**视频载荷布局的门槛不得跟着抬**——
        # 抬了 VIDEO_SEQ_MINOR 就等于让所有 1.5.0 观看端被认证期拒绝（net-047 的先例）。
        self.assertEqual(common.VIDEO_SEQ_MINOR, 4)
        self.assertEqual(common.PROTO_CHAL_MIN_MINOR, 3)


class _FakeSock:
    """按脚本喂 recv 块的假 socket：块序列耗尽后 EOF 或抛 OSError。"""

    def __init__(self, chunks, tail="eof"):
        self._chunks = list(chunks)
        self._tail = tail

    def recv(self, n):
        if self._chunks:
            return self._chunks.pop(0)
        if self._tail == "eof":
            return b""
        raise OSError("boom")


def _msg(kind, payload):
    return common._MSG_HDR.pack(kind, len(payload)) + payload


class TestMessagePump(unittest.TestCase):
    """C2 §7 第 2 步：共享接收泵的分派/退出语义逐分支锁。"""

    def _run(self, sock, **kw):
        kw.setdefault("should_stop", lambda: False)
        kw.setdefault("on_idle", lambda now, buf: None)
        kw.setdefault("on_message", lambda k, p, raw: None)
        ready = kw.pop("ready", True)
        with mock.patch("common.select.select",
                        side_effect=lambda *a: ([sock] if ready else [],
                                                [], [])):
            return message_pump(sock, **kw)

    def test_drain_order_and_hooks(self):
        ctrl1 = _msg(MSG_CTRL, b'{"action":"ping"}')
        video = _msg(MSG_VIDEO, b"x" * 10)
        ctrl2 = _msg(MSG_CTRL, b'{"action":"pong"}')
        events = []
        reason = self._run(
            _FakeSock([ctrl1 + video[:4], video[4:] + ctrl2]),
            on_message=lambda k, p, raw: events.append(("msg", k)) or None,
            on_batch=lambda buf: events.append(("batch", bytes(buf))) or None,
            on_rx=lambda now: events.append(("rx",)),
            on_buf=lambda ev, buf, now, n=0: events.append(
                ("buf", ev, bytes(buf), n)),
        )
        self.assertIsNone(reason)  # 之后 EOF 且无 on_eof → 以 None 结束
        kinds = [e for e in events if e[0] == "msg"]
        self.assertEqual([k[1] for k in kinds],
                         [MSG_CTRL, MSG_VIDEO, MSG_CTRL])
        # 第 1 块：视频消息跨块攒不到头 → 排空只出 1 条；pre_empty 只在空缓冲触发
        bufs = [e for e in events if e[0] == "buf"]
        self.assertEqual([b[1] for b in bufs],
                         ["pre_empty", "post", "post"])
        # 第 1 次排空后缓冲区仍留有半截视频头 → post 时 buf 非空
        self.assertTrue(bufs[1][2])
        self.assertFalse(bufs[2][2])
        # net-015：post 需带"本轮消费的完整消息条数"（0=仍是同一截半截消息）
        self.assertEqual([b[3] for b in bufs], [0, 1, 2])
        # 每块 recv 一次 batch；EOF 块不再触发
        self.assertEqual(sum(1 for e in events if e[0] == "batch"), 2)

    def test_raw_channel_payload_memoryview_and_identical_bytes(self):
        video = _msg(MSG_VIDEO, b"y" * 20)
        got = {}

        def on_message(kind, payload, raw):
            got["kind"], got["payload"], got["raw"] = kind, payload, raw
        self._run(_FakeSock([video]), on_message=on_message,
                  raw_kinds=(MSG_VIDEO, MSG_FRAME))
        self.assertIsInstance(got["raw"], bytes)
        self.assertEqual(got["raw"], video)  # A4：与线上字节逐字节相同
        self.assertEqual(bytes(got["payload"]), b"y" * 20)

    def test_non_raw_channel_gives_bytes_payload(self):
        ctrl = _msg(MSG_CTRL, b"{}")
        got = {}
        self._run(_FakeSock([ctrl]),
                  on_message=lambda k, p, raw: (got.update(p=p, r=raw)) or None,
                  raw_kinds=(MSG_VIDEO, MSG_FRAME))
        self.assertIsInstance(got["p"], bytes)
        self.assertIsNone(got["r"])

    def test_on_message_reason_stops_pump_immediately(self):
        two = _msg(MSG_CTRL, b"{}") * 2
        seen = []
        reason = self._run(
            _FakeSock([two]),
            on_message=lambda k, p, raw: seen.append(k) or "致命",
            on_batch=lambda buf: self.fail("提前退出时不应触发 on_batch"))
        self.assertEqual(reason, "致命")
        self.assertEqual(len(seen), 1)

    def test_idle_reason_ends_pump(self):
        reason = self._run(_FakeSock([]), ready=False,
                           on_idle=lambda now, buf: "空闲超时")
        self.assertEqual(reason, "空闲超时")

    def test_should_stop_wins(self):
        self.assertIsNone(self._run(_FakeSock([]),
                                    should_stop=lambda: True))

    def test_recv_error_mapping(self):
        self.assertEqual(
            self._run(_FakeSock([], tail="err"),
                      on_recv_error=lambda e: "接收错误"), "接收错误")
        # 无回调：原样抛（viewer 靠异常到外层做重连分类）
        with self.assertRaises(OSError):
            self._run(_FakeSock([], tail="err"))

    def test_eof_reason_or_raise(self):
        self.assertEqual(self._run(_FakeSock([]),
                                   on_eof=lambda: "断开"), "断开")
        with self.assertRaises(ConnectionError):
            self._run(_FakeSock([]),
                      on_eof=lambda: (_ for _ in ()).throw(
                          ConnectionError("连接关闭")))


if __name__ == "__main__":
    unittest.main()
