# -*- coding: utf-8 -*-
"""第 95 条回归：准入关闭分支不得把"流已错位"的连接带进接收循环。

旧路径（host.py 准入关闭 else 分支）：
    try:
        sock.settimeout(2.0); msg = recv_msg(sock)
        ...成功回复...
    except socket.timeout:
        log.debug("...按旧客户端直连处理")     # fall-through 进接收循环
    except Exception as e:
        log.debug("...可选 auth 处理忽略")      # fall-through 进接收循环

recv_msg 在「读完 5 字节头才发现 kind != MSG_CTRL」或「半截消息超时」时抛异常**之前已消费
头部（甚至半截负载）**，流已错位。旧代码两个 except 都 fall-through，残留负载字节被接收循环
的 parse_message 当成下一条消息的头解析 → 非法 kind → ValueError → 客户端被以「协议错误」踢掉。
触发需非标准客户端（准入关闭下先发非 CTRL 消息）或消息正好跨 2 秒超时（低置信）。

修复：先 select.select([sock],[],[],2.0) 探测有无数据——
  无数据（旧客户端不发 auth）→ 零消费、流对齐、直接放行（保留兼容）；
  有数据才 recv_msg，一旦抛异常 → 流已错位无法恢复 → sock.close() + return（不进接收循环）。

hermetic：用假 socket 驱动**真实** common.recv_msg / common.parse_message，复刻新旧两套
Site 2 决策，证明旧逻辑会把错位流带进接收循环并被 parse_message 判非法、新逻辑直接断开；
并对真实 host.py 源码做字符串断言，把复刻锚定到实际改动（防回退）。
"""
import os
import socket
import struct
import unittest

import common


_HOST_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "host.py")


class _FakeSock:
    """模拟 socket：recv 从预置缓冲取数据；缓冲空时按 Site 2 的 2s 超时抛 socket.timeout。"""

    def __init__(self, data=b""):
        self._buf = bytearray(data)
        self.consumed = 0
        self.closed = False

    def settimeout(self, t):
        pass

    def recv(self, n):
        if self.closed:
            raise OSError("socket closed")
        if not self._buf:
            raise socket.timeout("no data within 2s")   # 旧客户端：无数据
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        self.consumed += len(chunk)
        return chunk

    def close(self):
        self.closed = True

    def pending(self):
        return bytes(self._buf)


def _select_readable(sock):
    """复刻 select.select([sock],[],[],2.0)：有残留数据即可读。"""
    return ([sock], [], []) if sock.pending() else ([], [], [])


def _site2_old(sock):
    """旧 Site 2：recv_msg + 两个 fall-through except；恒返回 True（带病进接收循环）。"""
    try:
        sock.settimeout(2.0)
        common.recv_msg(sock)
    except socket.timeout:
        pass            # fall-through（旧客户端路径，但无法区分是否已消费字节）
    except Exception:
        pass            # fall-through（错位也照样进接收循环）
    return True


def _site2_new(sock):
    """新 Site 2：select 门控。返回 True=进接收循环 / False=已断开。"""
    try:
        rlist, _, _ = _select_readable(sock)
    except (OSError, ValueError):
        rlist = []
    if not rlist:
        return True                         # 无数据：零消费、对齐、放行
    try:
        sock.settimeout(2.0)
        common.recv_msg(sock)
    except Exception:
        sock.close()
        return False                        # 有数据但解析失败：流错位，断开
    return True                             # 成功解析整条消息，继续


def _non_ctrl_frame():
    """构造一个非 CTRL 消息（FRAME），其负载首字节 0xFF 为非法 kind——
    recv_msg 吃掉 5 字节头后，残留负载会被 parse_message 当成头解析出非法 kind。"""
    payload = b"\xff" + b"\x00" * 9
    return bytes((common.MSG_FRAME,)) + struct.pack(">I", len(payload)) + payload


class TestRecvMsgConsumesHeaderBeforeRaising(unittest.TestCase):
    def test_non_ctrl_leaves_payload_unconsumed(self):
        # 前提确证：recv_msg 遇到非 CTRL 时已消费 5 字节头、负载残留 → 流错位根源。
        sock = _FakeSock(_non_ctrl_frame())
        with self.assertRaises(ConnectionError):
            common.recv_msg(sock)
        self.assertEqual(sock.consumed, 5)              # 头部已消费
        self.assertEqual(len(sock.pending()), 10)       # 负载残留（10 字节）


class TestOldLogicMisalignsAndKicks(unittest.TestCase):
    def test_old_proceeds_then_parse_message_raises(self):
        # 旧逻辑：非标准客户端发 FRAME → recv_msg 抛被忽略 → 进接收循环 →
        # parse_message 读残留负载（首字节 0xFF 非法 kind）→ ValueError → 客户端被踢。
        sock = _FakeSock(_non_ctrl_frame())
        proceeded = _site2_old(sock)
        self.assertTrue(proceeded)                      # 带病进接收循环
        self.assertEqual(sock.consumed, 5)
        with self.assertRaises(ValueError):             # 接收循环里的 parse_message
            common.parse_message(bytearray(sock.pending()))


class TestNewLogicClosesInsteadOfMisaligning(unittest.TestCase):
    def test_non_ctrl_closes_not_proceeds(self):
        sock = _FakeSock(_non_ctrl_frame())
        proceeded = _site2_new(sock)
        self.assertFalse(proceeded)                     # 不进接收循环
        self.assertTrue(sock.closed)                    # 直接断开

    def test_old_client_no_data_proceeds_aligned(self):
        # 兼容：旧客户端不发 auth → select 无数据 → 零消费、对齐、放行（不调 recv_msg）。
        sock = _FakeSock(b"")
        proceeded = _site2_new(sock)
        self.assertTrue(proceeded)
        self.assertEqual(sock.consumed, 0)              # 零消费，流对齐
        self.assertFalse(sock.closed)

    def test_valid_ctrl_auth_proceeds_fully_consumed(self):
        # 兼容：新版观看端发合法 CTRL auth → 整条消费干净、放行、不断开。
        sock = _FakeSock(common.pack_msg({"action": "probe", "user": ""}))
        proceeded = _site2_new(sock)
        self.assertTrue(proceeded)
        self.assertFalse(sock.closed)
        self.assertEqual(sock.pending(), b"")           # 整条消息消费完，流对齐


class TestRealHostSourceMatchesReplica(unittest.TestCase):
    """把复刻锚定到真实 host.py：select 门控存在、旧的 fall-through 忽略日志已移除。"""

    def setUp(self):
        with open(_HOST_PY, "r", encoding="utf-8") as f:
            self.src = f.read()

    def test_select_gate_present(self):
        self.assertIn("select.select([sock], [], [], 2.0)", self.src)

    def test_old_ignore_fallthrough_removed(self):
        # 旧 buggy 分支的标志性日志（有数据但解析失败仍忽略放行）必须消失。
        self.assertNotIn("可选 auth 处理忽略", self.src)

    def test_misalignment_close_present(self):
        self.assertIn("流已错位", self.src)              # 断开分支注释/日志锚点


if __name__ == "__main__":
    unittest.main()
