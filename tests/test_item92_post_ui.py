# -*- coding: utf-8 -*-
"""第 92 条回归：热键/诊断线程改走 _post_ui；_post_ui 拦截未就绪/退出/已销毁；
_collect_diagnostics 持锁取频道快照。

旧路径三处隐患：
- _hotkey_loop 与诊断 worker 直接调 self.root.after(0, ...)，仅在末尾 except: pass 兜
  Python 异常。退出瞬间 mainloop 已 destroy 但 self.root 仍非 None → 从外部线程调进
  已销毁的 Tcl 解释器，不只是抛异常，有崩溃/挂起风险（非 Python 异常 except 兜不住）。
- _post_ui 仅判 root is None，不判 running/_mainloop_ready，挡不住退出/未就绪窗口。
- _collect_diagnostics 在工作线程里 `for ch in self.channels` 无锁遍历，主线程
  add_channel/remove_channel 在 _channels_lock 下 .append/.pop 改它 → 遍历到一半列表
  被改，可能跳过/报告已不存在的频道（torn read）。

修复：
1) _post_ui 增加 `not self.running` 与 `_mainloop_ready 未置位` 两道前置拦截；
2) 热键六分支、诊断展示回调全部改走 self._post_ui；
3) _collect_diagnostics 先在 _channels_lock 下取 list 快照，再在锁外遍历（网络 I/O 不持锁）。

hermetic：用 ViewerApp.__new__ 绕开 Tk/线程；网络用 mock.patch 屏蔽；AST 结构断言锁住
"热键/诊断函数内无 self.root.after、有 self._post_ui"；负控复刻旧"无锁遍历活列表 +
循环体内 pop"证明会跳过元素，快照不会。
"""
import ast
import os
import queue
import threading
import unittest
from unittest import mock

import viewer


_VIEWER_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "viewer.py")


# ==================== Fix 1: _post_ui 前置拦截 ====================

class _FakeRoot:
    """记录 after 调用的假 root；可切换为抛异常模拟已销毁的 Tcl。"""

    def __init__(self):
        self.after_calls = []
        self.raise_on_after = False

    def after(self, ms, fn):
        if self.raise_on_after:
            raise RuntimeError("Tcl interpreter destroyed")
        self.after_calls.append((ms, fn))


def _app(running=True, ready=True, root="auto"):
    a = viewer.ViewerApp.__new__(viewer.ViewerApp)
    a.running = running
    a._mainloop_ready = threading.Event()
    if ready:
        a._mainloop_ready.set()
    a.root = _FakeRoot() if root == "auto" else root
    # 第 108 条（B14）：_post_ui 改为只入队，回调由主线程 poll 里的 _drain_ui_queue 执行
    a._ui_queue = queue.SimpleQueue()
    return a


class TestPostUiGuards(unittest.TestCase):
    def test_drops_when_not_running(self):
        # 退出中：running=False → 即便 root 在、ready 已置位也丢弃（绝不调进 Tcl）。
        a = _app(running=False, ready=True)
        a._post_ui(lambda: None)
        self.assertEqual(a.root.after_calls, [])
        self.assertTrue(a._ui_queue.empty())

    def test_drops_when_mainloop_not_ready(self):
        # 未就绪：mainloop 还没 set → 丢弃，避免在 root 真正可用前抢跑。
        a = _app(running=True, ready=False)
        a._post_ui(lambda: None)
        self.assertEqual(a.root.after_calls, [])
        self.assertTrue(a._ui_queue.empty())

    def test_drops_when_root_none(self):
        a = _app(running=True, ready=True, root=None)
        a._post_ui(lambda: None)          # root 为空也不应抛
        self.assertIsNone(a.root)

    def test_schedules_when_ready_and_running(self):
        # 正控：running=True + ready → 回调入队，且**不**从工作线程碰 Tk；
        # 主线程 _drain_ui_queue 执行它（第 108 条 / B14）。
        a = _app(running=True, ready=True)
        calls = []
        a._post_ui(lambda: calls.append("x"))
        self.assertEqual(a.root.after_calls, [])
        self.assertEqual(calls, [])
        a._drain_ui_queue()
        self.assertEqual(calls, ["x"])

    def test_swallows_after_exception(self):
        # root 的 Tcl 已销毁（after 会抛）也不影响入队——_post_ui 不再触碰 root。
        a = _app(running=True, ready=True)
        a.root.raise_on_after = True
        a._post_ui(lambda: None)          # 不应抛
        self.assertFalse(a._ui_queue.empty())


# ============ Fix 2/3: 热键/诊断改走 _post_ui（AST 结构断言）============

def _module_tree():
    with open(_VIEWER_PY, "r", encoding="utf-8") as f:
        return ast.parse(f.read(), filename=_VIEWER_PY)


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _count_self_root_after(func):
    """统计函数体内 `self.root.after(...)` 直连调用次数（不含局部 root 变量）。"""
    n = 0
    for node in ast.walk(func):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "after"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "root"):
            n += 1
    return n


def _count_self_post_ui(func):
    """统计函数体内 `self._post_ui(...)` 调用次数。"""
    n = 0
    for node in ast.walk(func):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_post_ui"):
            n += 1
    return n


class TestRoutingThroughPostUi(unittest.TestCase):
    def setUp(self):
        self.tree = _module_tree()

    def test_hotkey_loop_no_direct_root_after(self):
        f = _find_func(self.tree, "_hotkey_loop")
        self.assertIsNotNone(f)
        self.assertEqual(_count_self_root_after(f), 0)     # 不再直连 self.root.after
        self.assertGreaterEqual(_count_self_post_ui(f), 1)  # 改走 _post_ui

    def test_run_diagnostics_no_direct_root_after(self):
        # worker 嵌套在 _run_diagnostics 内；ast.walk 会进入嵌套函数体。
        f = _find_func(self.tree, "_run_diagnostics")
        self.assertIsNotNone(f)
        self.assertEqual(_count_self_root_after(f), 0)
        self.assertGreaterEqual(_count_self_post_ui(f), 1)


# ============ Fix 4: _collect_diagnostics 持锁取快照 ============

class _FakeChannel:
    """诊断遍历到的假频道；effective_latency_ms 被调用即视为"循环体已进入该频道"。"""

    def __init__(self, name, addr, on_visit=None):
        self.name = name
        self.addr = addr
        self.round_trip_ms = 0
        self._on_visit = on_visit

    def effective_latency_ms(self):
        if self._on_visit is not None:
            self._on_visit()
        return 0


def _diag_app(channels):
    a = viewer.ViewerApp.__new__(viewer.ViewerApp)
    a.channels = channels
    a._channels_lock = threading.RLock()
    a.cfg = {"host": {"frp": {"frpc_path": ""}}, "viewer": {}}
    return a


class TestCollectDiagnosticsSnapshot(unittest.TestCase):
    def test_concurrent_mutation_does_not_break_iteration(self):
        # 循环体内主线程并发 remove：诊断遍历的是快照，应完整处理原始频道、不抛、不跳过。
        visited = []
        a = _diag_app([])

        def on_visit():
            visited.append(ch.name)
            # 模拟主线程在 _channels_lock 下 pop 掉当前频道（旧无锁遍历会跳过/读到撕裂状态）
            if a.channels:
                a.channels.pop(0)

        ch = _FakeChannel("chA", "127.0.0.1:1", on_visit=on_visit)
        a.channels = [ch]

        with mock.patch.object(viewer.socket, "create_connection",
                               side_effect=OSError("blocked")), \
                mock.patch.object(viewer, "parse_addr",
                                  return_value=("127.0.0.1", 1)):
            lines = a._collect_diagnostics()

        self.assertIsInstance(lines, list)
        self.assertEqual(visited, ["chA"])                  # 快照里的频道被完整处理
        self.assertTrue(any("chA" in ln for ln in lines))   # 报告里出现该频道

    def test_snapshot_taken_under_lock(self):
        # 记录型锁证明快照确实持 _channels_lock（旧代码从不加锁）。
        acquired = []

        class _RecLock:
            def __enter__(self):
                acquired.append("enter")
                return self

            def __exit__(self, *a):
                acquired.append("exit")
                return False

        a = _diag_app([])
        a._channels_lock = _RecLock()
        a.channels = []
        with mock.patch.object(viewer.socket, "create_connection",
                               side_effect=OSError("blocked")):
            a._collect_diagnostics()
        self.assertIn("enter", acquired)   # 进入了 with self._channels_lock
        self.assertIn("exit", acquired)    # 且正常释放（不持锁做网络 I/O）

    def test_old_unlocked_pattern_skips_on_concurrent_pop(self):
        # 负控：复刻旧 `for ch in self.channels` + 循环体内 pop → 跳过元素；快照不会。
        live = [0, 1, 2, 3]
        seen_old = []
        for x in live:
            seen_old.append(x)
            if x == 0:
                live.pop(0)               # 主线程并发删除：活列表收缩，迭代器跳过 1
        self.assertEqual(seen_old, [0, 2, 3])
        self.assertNotIn(1, seen_old)     # 旧模式漏掉了 1（torn read）

        live2 = [0, 1, 2, 3]
        snap = list(live2)                # 修复模式：先取快照
        seen_new = []
        for x in snap:
            seen_new.append(x)
            if x == 0:
                live2.pop(0)
        self.assertEqual(seen_new, [0, 1, 2, 3])   # 快照模式不漏


if __name__ == "__main__":
    unittest.main()
