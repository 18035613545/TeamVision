# -*- coding: utf-8 -*-
"""第 93 条回归：认证对话框不得嵌套，且结果不得跨请求串。

旧路径两处隐患：
- 嵌套：poll 先 `after(15, self.poll)` 再 `_process_cred_requests`；_credentials_dialog
  的 wait_window 开嵌套主循环，期间 after 定时器照常触发 poll → 再次 _process_cred_requests
  → 若另一频道也排了请求，就在第一个模态框上再叠一个（docstring"每次只处理一个"未被强制）。
- 串结果：_cred_result 是频道上的单一共享槽。done.wait(120) 超时后接收线程返回 None 但
  **不清槽**、对话框仍开着；用户最终提交时主线程把结果写进一个没人等的请求，下一次
  _ask_credentials 可能读走这个属于更旧提示的凭据。

修复：
1) ViewerApp._cred_busy 互斥：忙时 _process_cred_requests 直接返回（请求留队列），杜绝嵌套；
2) 结果改走"每请求独立 box"（入队 3 元组 (ch, done, box)），主线程只写自己出队那只 box、
   接收线程只读自己那只 box，彻底移除共享 _cred_result 槽 → 放弃的请求被晚提交也只写进
   它自己那只无人读的 box，绝不串给后续请求。

hermetic：ViewerApp.__new__ 绕开 Tk；_credentials_dialog 用 lambda 替身（返回/抛异常）；
AST 断言全模块不再存在 _cred_result 属性读写（防共享槽回归）。
"""
import ast
import os
import queue
import threading
import unittest

import viewer


_VIEWER_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "viewer.py")


class _FakeCh:
    def __init__(self, name):
        self.name = name


def _cred_app():
    a = viewer.ViewerApp.__new__(viewer.ViewerApp)
    a._cred_busy = False
    a._cred_requests = queue.Queue()
    return a


class TestCredBusyPreventsNesting(unittest.TestCase):
    def test_busy_returns_without_dequeue_or_dialog(self):
        # 嵌套核心不变量：对话框进行中（_cred_busy=True，等价于 wait_window 嵌套期间）
        # 再次进入 _process_cred_requests 必须什么都不做——不出队、不开框、不写结果。
        a = _cred_app()
        a._cred_busy = True
        box, done = {}, threading.Event()
        a._cred_requests.put((_FakeCh("chB"), done, box))
        opened = []
        a._credentials_dialog = lambda name: opened.append(name) or ("login", "u", "p")

        a._process_cred_requests()

        self.assertEqual(opened, [])                  # 未开第二个对话框
        self.assertEqual(box, {})                     # 未写结果
        self.assertFalse(done.is_set())               # 未 set done
        self.assertEqual(a._cred_requests.qsize(), 1)  # 请求仍留在队列（未出队）
        self.assertTrue(a._cred_busy)                 # 忙标志保持，待原对话框关闭再清

    def test_not_busy_dequeues_and_clears_busy_after(self):
        # 正控：空闲时正常出队、开框、写本请求 box、set done、完成后清忙。
        a = _cred_app()
        box, done = {}, threading.Event()
        a._cred_requests.put((_FakeCh("chA"), done, box))
        a._credentials_dialog = lambda name: ("login", "user", "pw")

        a._process_cred_requests()

        self.assertEqual(box["result"]["value"], ("login", "user", "pw"))
        self.assertTrue(done.is_set())
        self.assertFalse(a._cred_busy)                # 完成后清忙
        self.assertEqual(a._cred_requests.qsize(), 0)

    def test_dialog_exception_clears_busy_in_finally(self):
        # 对话框抛异常也必须经 finally 清忙——否则 _cred_busy 卡死 True，永不再弹任何框。
        a = _cred_app()
        box, done = {}, threading.Event()
        a._cred_requests.put((_FakeCh("chA"), done, box))

        def boom(name):
            raise RuntimeError("dialog failed")
        a._credentials_dialog = boom

        a._process_cred_requests()

        self.assertFalse(a._cred_busy)                # finally 清忙（关键）
        self.assertIn("error", box["result"])         # 异常被记进结果
        self.assertTrue(done.is_set())                # 仍唤醒接收线程（它会因 error 返回 None）


class TestPerRequestBoxNoCrossing(unittest.TestCase):
    def test_each_request_writes_only_its_own_box(self):
        # 串结果核心不变量：每次处理只写出队那只 box，其余 box 一律不碰。
        a = _cred_app()
        box1, box2 = {}, {}
        done1, done2 = threading.Event(), threading.Event()
        a._cred_requests.put((_FakeCh("chA"), done1, box1))
        a._cred_requests.put((_FakeCh("chB"), done2, box2))
        seq = []
        a._credentials_dialog = lambda name: (seq.append(name), ("login", name, "pw"))[1]

        a._process_cred_requests()                    # 第一次：只动 box1/done1
        self.assertEqual(box1["result"]["value"], ("login", "chA", "pw"))
        self.assertEqual(box2, {})                    # box2 完全未被触碰
        self.assertTrue(done1.is_set())
        self.assertFalse(done2.is_set())
        self.assertEqual(seq, ["chA"])

        a._process_cred_requests()                    # 第二次：只动 box2/done2
        self.assertEqual(box2["result"]["value"], ("login", "chB", "pw"))
        self.assertTrue(done2.is_set())
        self.assertEqual(seq, ["chA", "chB"])

    def test_abandoned_box_late_write_does_not_leak_to_new_request(self):
        # 复刻旧缺陷场景：请求1 超时被放弃（done1 未 set，接收线程已返回 None），但对话框
        # 仍开着；随后晚提交把结果写进 box1。新请求2 必须只读自己的 box2，读不到 box1 旧值。
        a = _cred_app()
        box1, done1 = {}, threading.Event()           # 请求1（将被放弃）
        a._cred_requests.put((_FakeCh("chA"), done1, box1))
        # 接收线程1 超时放弃：done1 仍未 set，box1 无人再读
        self.assertFalse(done1.is_set())

        # 主线程晚些处理请求1：用户提交 → 结果落进 box1（已放弃），done1.set()（无等待者）
        a._credentials_dialog = lambda name: ("login", "OLD", "OLD")
        a._process_cred_requests()
        self.assertEqual(box1["result"]["value"], ("login", "OLD", "OLD"))

        # 新请求2 入队（全新 box2/done2）
        box2, done2 = {}, threading.Event()
        a._cred_requests.put((_FakeCh("chB"), done2, box2))
        a._credentials_dialog = lambda name: ("login", "NEW", "NEW")
        a._process_cred_requests()

        # 请求2 只拿到 NEW；box1 的 OLD 不会串过来（共享槽时代 _cred_result 会被读走）
        self.assertEqual(box2["result"]["value"], ("login", "NEW", "NEW"))
        self.assertTrue(done2.is_set())
        self.assertEqual(box1["result"]["value"], ("login", "OLD", "OLD"))  # box1 未被请求2 改写


class TestSharedSlotRemoved(unittest.TestCase):
    def test_no_cred_result_attribute_anywhere(self):
        # 第 93 条：共享结果槽 _cred_result 已彻底移除；任何 ._cred_result 属性读写都算回归。
        with open(_VIEWER_PY, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=_VIEWER_PY)
        hits = [node.lineno for node in ast.walk(tree)
                if isinstance(node, ast.Attribute) and node.attr == "_cred_result"]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
