# -*- coding: utf-8 -*-
"""第 97 条回归：stop() 不得在仍存活的上传线程脚下关掉它的采集/编码资源。

机制：join 超时 2 秒 < 发送超时 5 秒。当上传线程卡在 sendall（死连接/慢链路）时，
`stop()` 的 `join(2.0)` 会超时返回而线程仍存活。旧 stop() 无条件 `_close_resources()`
→ 关掉编码器与 mss/dxcam 句柄，而线程仍在跑：随后 `_encode_send` 中途遇到
`_cap is None`/`_encoder is None`，甚至在「已停止」之后新建一个 VideoEncoder；两个线程
并发开关原生采集句柄。

修复：`stop()` 在 join 后判 `th.is_alive()`——仍存活则直接 return，**不**在此处关闭
资源；资源交由线程自身 `_run` 末尾的 finally 释放（线程被 signal_stop 置位后，卡住的
sendall 至多 5 秒超时即返回，循环复检 _stop 退出 → finally → _close_resources）。

hermetic：用伪线程（join 立即返回、is_alive 可控）离线驱动真实 `stop()`，确定性地
证明「线程仍存活 → 不关资源」「线程已退 → 关资源」，并对旧逻辑做负向对照。不起真实
线程/不卡真实 2 秒。范式同 test_item60_mainthread.py（__new__ 绕开采集/编码/Tk）。
"""
import ast
import inspect
import textwrap
import threading
import unittest

import share


class _FakeThread:
    """伪上传线程：join 立即返回（不真等 2 秒），is_alive 由构造参数控制。"""

    def __init__(self, alive):
        self._alive = alive
        self.join_calls = []

    def join(self, timeout=None):
        self.join_calls.append(timeout)

    def is_alive(self):
        return self._alive


def _session(thread):
    """绕开 __init__（不碰采集/编码/Tk），只装配 stop() 需要的最小状态。"""
    s = share.ScreenShareSession.__new__(share.ScreenShareSession)
    s._stop = threading.Event()
    s._thread = thread
    s.close_calls = []
    s._close_resources = lambda: s.close_calls.append(True)
    return s


def _stop_old(session):
    """负向对照：复刻旧 stop()（无 is_alive 守卫，无条件关闭资源）。"""
    session._stop.set()
    th = session._thread
    if th is not None:
        th.join(timeout=2.0)
        session._thread = None
    session._close_resources()


class TestStopSkipsCloseWhenThreadAlive(unittest.TestCase):
    def test_alive_thread_stop_does_not_close_resources(self):
        """核心：线程仍存活（卡 sendall）→ stop() 必须直接 return，不关资源。"""
        th = _FakeThread(alive=True)
        s = _session(th)
        s.stop()
        self.assertEqual(s.close_calls, [],
                         "线程仍存活时 stop() 关闭了资源（在运行线程脚下抽掉 cap/encoder）")
        self.assertEqual(th.join_calls, [2.0], "应以 2 秒超时 join")
        self.assertIsNone(s._thread, "stop() 应清掉 _thread 引用")

    def test_alive_thread_stop_still_signals_stop(self):
        """仍存活时也要置停止位：让卡住的线程 sendall 超时后复检 _stop 自行退出。"""
        th = _FakeThread(alive=True)
        s = _session(th)
        s.stop()
        self.assertTrue(s._stop.is_set(),
                        "stop() 未置停止位 → 卡住的线程永不退出、资源永不释放")

    def test_exited_thread_stop_closes_resources(self):
        """对照：线程已在 join 内退出 → stop() 正常关闭资源（守卫不误伤正常路径）。"""
        th = _FakeThread(alive=False)
        s = _session(th)
        s.stop()
        self.assertEqual(s.close_calls, [True],
                         "线程已退出却未关闭资源（守卫过度，泄漏 cap/encoder）")
        self.assertIsNone(s._thread)

    def test_no_thread_stop_closes_resources(self):
        """幂等/无线程：_thread 为 None 时直接关资源，不抛、不 join。"""
        s = _session(None)
        s.stop()
        self.assertEqual(s.close_calls, [True])


class TestOldLogicClosedUnderRunningThread(unittest.TestCase):
    def test_old_stop_closes_even_when_thread_alive(self):
        """负向对照：旧 stop()（无守卫）在线程仍存活时也关资源 → 正是报告的竞态。"""
        th = _FakeThread(alive=True)
        s = _session(th)
        _stop_old(s)
        self.assertEqual(s.close_calls, [True],
                         "对照失效：旧逻辑本应无条件关闭资源")
        # 修复后同样输入（线程存活）应不关资源 —— 与真实 stop() 形成对照
        th2 = _FakeThread(alive=True)
        s2 = _session(th2)
        s2.stop()
        self.assertEqual(s2.close_calls, [])


class TestRealSourceHasAliveGuard(unittest.TestCase):
    def test_stop_guards_close_with_is_alive(self):
        """真实源码结构：stop() 含 is_alive 守卫，且守卫命中时 return（跳过关闭）。"""
        src = textwrap.dedent(inspect.getsource(share.ScreenShareSession.stop))
        self.assertIn("is_alive", src, "stop() 缺少 is_alive 守卫")
        tree = ast.parse(src)
        fn = tree.body[0]
        # 找到 if th.is_alive(): return —— return 必须在 is_alive 测试的 body 内
        found_guard = False
        for node in ast.walk(fn):
            if isinstance(node, ast.If):
                test = node.test
                if (isinstance(test, ast.Call)
                        and isinstance(test.func, ast.Attribute)
                        and test.func.attr == "is_alive"):
                    if any(isinstance(b, ast.Return) for b in node.body):
                        found_guard = True
        self.assertTrue(found_guard,
                        "stop() 没有 `if th.is_alive(): return` 守卫（不会跳过 _close_resources）")

    def test_close_resources_after_guard(self):
        """_close_resources 调用应出现在 is_alive 守卫之后（守卫命中即 return 跳过它）。"""
        src = textwrap.dedent(inspect.getsource(share.ScreenShareSession.stop))
        guard_at = src.find("is_alive")
        close_at = src.find("_close_resources")
        self.assertGreaterEqual(guard_at, 0)
        self.assertGreaterEqual(close_at, 0)
        self.assertLess(guard_at, close_at,
                        "_close_resources 出现在 is_alive 守卫之前（顺序错误）")


if __name__ == "__main__":
    unittest.main()
