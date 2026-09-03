# -*- coding: utf-8 -*-
"""第 96 条回归：共享会话不得永久改掉共享 socket 的超时不变量。

`ScreenShareSession._send_msg` 与接收线程复用同一条 socket：viewer 特意把它置回
阻塞（None，viewer.py:294），而上传线程需要 sendall 有界（防死连接上无限阻塞、
拖垮 stop() 的 join）。旧实现每帧 `settimeout(5.0)` 且从不还原，跨线程未同步地把
共享 socket 永久停在 5.0（_stop_share 后也不恢复）。修复=写锁内保存原超时、仅为
本次 sendall 临时设 5.0、finally 还原。

本测试用伪 socket / 伪锁离线驱动真实 `_send_msg`，证明：
- 发送后超时还原到进入前的值（None 或非 None 都还原，不是硬编码 None）
- sendall 期间超时确实被收紧到 5.0（上传线程仍有界）
- sendall 抛错时 finally 仍还原（不会把 5.0 泄漏给后续发送方）
- 保存→设 5.0→sendall→还原 整段都在 _tx 写锁内（与其它发送方串行）
- 负向对照：旧逻辑（无还原）发送后把 socket 永久停在 5.0
- 真实源码结构：_send_msg 含 gettimeout + finally 还原 + 临时 settimeout(5.0)
"""
import ast
import inspect
import textwrap
import threading
import unittest

import share


class _FakeSock:
    """记录每次 sendall 时生效的超时值；可选失败以触发 finally 还原路径。"""

    def __init__(self, timeout=None, fail=False):
        self._timeout = timeout
        self.fail = fail
        self.timeout_during_send = []  # 每次 sendall 时的 socket 超时
        self.sent = []

    def gettimeout(self):
        return self._timeout

    def settimeout(self, t):
        self._timeout = t

    def sendall(self, data):
        self.timeout_during_send.append(self._timeout)
        if self.fail:
            raise OSError("模拟发送失败")
        self.sent.append(data)


class _TraceSock(_FakeSock):
    """把 gettimeout/settimeout/sendall 事件写入共享 trace（验证锁内顺序）。"""

    def __init__(self, trace, timeout=None, fail=False):
        super().__init__(timeout=timeout, fail=fail)
        self.trace = trace

    def gettimeout(self):
        self.trace.append(("gettimeout", self._timeout))
        return self._timeout

    def settimeout(self, t):
        self.trace.append(("settimeout", t))
        self._timeout = t

    def sendall(self, data):
        self.trace.append(("sendall", self._timeout))
        if self.fail:
            raise OSError("模拟发送失败")
        self.sent.append(data)


class _TraceLock:
    """记录 acquire/release 到同一 trace，证明整段超时操作在写锁内。"""

    def __init__(self, trace):
        self.trace = trace
        self._lock = threading.Lock()

    def __enter__(self):
        self._lock.acquire()
        self.trace.append(("acquire",))
        return self

    def __exit__(self, *exc):
        self.trace.append(("release",))
        self._lock.release()
        return False


def _session(sock, tx=None):
    """绕过 __init__（不碰采集/编码/Tk），只装配 _send_msg 需要的最小状态。"""
    s = share.ScreenShareSession.__new__(share.ScreenShareSession)
    s._sock = sock
    s._tx = tx if tx is not None else threading.Lock()
    s._name = "test"
    s._stop = threading.Event()
    return s


class TestRestoreInvariant(unittest.TestCase):
    def test_restores_blocking_after_send(self):
        """viewer 基线：进入前 None（阻塞），发送后必须还原回 None。"""
        sock = _FakeSock(timeout=None)
        s = _session(sock)
        self.assertTrue(s._send_msg(b"frame"))
        self.assertEqual(sock.sent, [b"frame"])
        self.assertIsNone(sock.gettimeout(),
                          "发送后未还原阻塞不变量（仍停在 %r）" % sock.gettimeout())

    def test_sendall_bounded_at_5s(self):
        """上传线程仍有界：sendall 实际生效的超时必须被收紧到 5.0。"""
        sock = _FakeSock(timeout=None)
        s = _session(sock)
        s._send_msg(b"frame")
        self.assertEqual(sock.timeout_during_send, [5.0],
                         "sendall 期间未把超时收紧到 5.0")

    def test_restores_on_sendall_failure(self):
        """sendall 抛错时 finally 仍还原：5.0 不得泄漏给后续发送方。"""
        sock = _FakeSock(timeout=None, fail=True)
        s = _session(sock)
        self.assertFalse(s._send_msg(b"frame"))
        self.assertTrue(s._stop.is_set(), "发送失败应置停止位")
        self.assertIsNone(sock.gettimeout(),
                          "sendall 失败后未还原超时（泄漏 5.0）")

    def test_preserves_nonblocking_original(self):
        """还原到“进入前观测值”而非硬编码 None：原值 3.0 → 还原回 3.0。"""
        sock = _FakeSock(timeout=3.0)
        s = _session(sock)
        s._send_msg(b"frame")
        self.assertEqual(sock.timeout_during_send, [5.0])
        self.assertEqual(sock.gettimeout(), 3.0,
                         "应还原到进入前的 3.0，而非 None 或 5.0")


class TestMutationUnderLock(unittest.TestCase):
    def test_save_set_send_restore_all_under_tx_lock(self):
        """保存→设 5.0→sendall→还原 整段必须在 _tx 写锁内（与其它发送方串行）。"""
        trace = []
        sock = _TraceSock(trace, timeout=None)
        tx = _TraceLock(trace)
        s = _session(sock, tx=tx)
        s._send_msg(b"frame")
        self.assertEqual(trace, [
            ("acquire",),
            ("gettimeout", None),
            ("settimeout", 5.0),
            ("sendall", 5.0),
            ("settimeout", None),
            ("release",),
        ])


class TestOldLogicBrokeInvariant(unittest.TestCase):
    def test_old_logic_leaves_socket_at_5s(self):
        """负向对照：旧实现（无还原）发送后把共享 socket 永久停在 5.0。"""
        sock = _FakeSock(timeout=None)
        tx = threading.Lock()
        stop = threading.Event()

        def _send_msg_old(msg):
            try:
                with tx:
                    sock.settimeout(5.0)
                    sock.sendall(msg)
                return True
            except OSError:
                stop.set()
                return False

        self.assertTrue(_send_msg_old(b"frame"))
        self.assertEqual(sock.gettimeout(), 5.0,
                         "旧逻辑本应把 socket 停在 5.0（对照失效）")
        # 修复后同样输入应还原为 None —— 与上面的真实 _send_msg 行为形成对照
        sock2 = _FakeSock(timeout=None)
        _session(sock2)._send_msg(b"frame")
        self.assertIsNone(sock2.gettimeout())


class TestRealSourceRestores(unittest.TestCase):
    def test_send_msg_has_gettimeout_and_finally_restore(self):
        """真实源码结构：_send_msg 含 gettimeout 保存 + finally 还原 + 临时 5.0。"""
        src = inspect.getsource(share.ScreenShareSession._send_msg)
        self.assertIn("gettimeout", src, "未保存原超时")
        self.assertIn("finally", src, "还原不在 finally（sendall 抛错会泄漏 5.0）")
        self.assertIn("settimeout(5.0)", src, "未临时收紧到 5.0")

    def test_restore_is_inside_finally(self):
        """AST 断言：还原 settimeout 调用位于 try/finally 的 finalbody 内。"""
        src = textwrap.dedent(
            inspect.getsource(share.ScreenShareSession._send_msg))
        tree = ast.parse(src)
        fn = tree.body[0]
        finally_calls = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Try):
                for fb in node.finalbody:
                    for c in ast.walk(fb):
                        if (isinstance(c, ast.Call)
                                and isinstance(c.func, ast.Attribute)
                                and c.func.attr == "settimeout"):
                            finally_calls.append(c)
        self.assertTrue(finally_calls, "finally 内没有 settimeout 还原调用")


if __name__ == "__main__":
    unittest.main()
