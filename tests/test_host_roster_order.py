# -*- coding: utf-8 -*-
"""host roster 广播顺序单元测试（第 55 条回归；阶段 A2 起叠加并发语义）。

缺陷：注册/注销先释放 clients_lock 再广播。线程 A 拿到旧快照 {peer1} 后卡在发送
循环里，线程 B 注册 peer2 后广播完 {peer1,peer2}，A 随后把陈旧的 {peer1} **最后**
送达 → 观看端源列表永久少一个正在共享的人。

修复：broadcast_roster 用 _roster_lock 防陈旧快照覆盖（每次发送都重新快照当前状态
→ 最后完成的广播必然反映最新名单）。阶段 A2（优化方案 2026-09-17）把「每个调用者
各排队发一轮」改为「单发送者 + 脏标记」：并发调用者置脏即返，发送者收尾时以最新
名单重发一轮——第 55 条不变量（最后送达=最新名单）保持，且调用线程不再被卡死
客户端挡住最坏 N×2s。

本测试确定性地复现该交错（纯内存 FakeSocket + join 的线程，不碰真实 socket/FD），
并锁定 A2 新语义：并发调用不阻塞、脏标记合并、异常后发送者标志不卡死。
"""
import threading
import time
import unittest

import host


class FakeSocket:
    def __init__(self):
        self._timeout = None
        self.sent = []

    def settimeout(self, t):
        self._timeout = t

    def gettimeout(self):
        return self._timeout

    def sendall(self, data):
        self.sent.append(data)


class RuntimeStub:
    def __init__(self, clients=None):
        self.clients_lock = threading.Lock()
        self._roster_lock = threading.Lock()
        self._roster_sending = False   # 阶段 A2：单发送者 + 脏标记
        self._roster_dirty = False
        self.clients = clients or {}


class Recorder:
    """记录 observer 收到的每个 roster 的 peer id 列表（保序）。"""

    def __init__(self):
        self.rosters = []
        self.lock = threading.Lock()

    def __call__(self, sock, obj, send_timeout=2.0):
        with self.lock:
            self.rosters.append([p["id"] for p in obj.get("peers", [])])


class SlowOnce:
    """第一次调用阻塞（模拟 A 卡在发送循环），后续调用立即返回（模拟 B 不受阻）。"""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self._calls = 0
        self._lock = threading.Lock()

    def __call__(self, sock, obj, send_timeout=2.0):
        with self._lock:
            self._calls += 1
            first = self._calls == 1
        if first:
            self.entered.set()
            self.release.wait(5.0)


class TestRosterBroadcastOrder(unittest.TestCase):

    def test_stale_snapshot_cannot_be_delivered_last(self):
        stub = RuntimeStub()
        slow_sock, obs_sock, slow2_sock = FakeSocket(), FakeSocket(), FakeSocket()

        # slow 排在 observer 之前：A 会先卡在 slow 上、尚未投递 observer
        slow = host.ClientInfo(("10.0.0.2", 2), "slow")
        slow.share_id = "peer:1"
        ss = SlowOnce()
        slow.send_ctrl = ss

        observer = host.ClientInfo(("10.0.0.1", 1), "obs")  # 纯观看者，share_id=None
        rec = Recorder()
        observer.send_ctrl = rec

        stub.clients = {slow_sock: slow, obs_sock: observer}

        tA = threading.Thread(target=host.HostRuntime.broadcast_roster, args=(stub,))
        tA.start()
        self.assertTrue(ss.entered.wait(2.0), "A 应已进入对 slow 的阻塞发送")
        # A 持锁阻塞在 slow（observer 尚未投递）；此时注册 peer2 并发起 B
        slow2 = host.ClientInfo(("10.0.0.3", 3), "slow2")
        slow2.share_id = "peer:2"
        slow2.send_ctrl = lambda sock, obj, send_timeout=2.0: None
        with stub.clients_lock:
            stub.clients = {slow_sock: slow, obs_sock: observer, slow2_sock: slow2}

        tB = threading.Thread(target=host.HostRuntime.broadcast_roster, args=(stub,))
        tB.start()
        time.sleep(0.3)  # 给 B 时间发起（A2 语义：B 置脏即返，不发送任何东西）
        with rec.lock:
            mid = list(rec.rosters)
        # 单发送者：A 卡在 slow、B 只置脏 → observer 此刻一条都没收到
        self.assertEqual(mid, [], "B 不应在 A 完成前抢先把名单送达 observer")

        ss.release.set()  # 放行 A → A 投递 observer{peer1}，见脏标记再重发{peer1,peer2}
        tA.join(3.0)
        tB.join(3.0)
        self.assertFalse(tA.is_alive() or tB.is_alive(), "两个广播线程都应结束")

        with rec.lock:
            final = list(rec.rosters)
        # 关键断言：最后送达 observer 的是最新名单，且旧快照在前、新快照在后
        self.assertEqual(final, [["peer:1"], ["peer:1", "peer:2"]])
        self.assertEqual(final[-1], ["peer:1", "peer:2"],
                         "最后送达的必须是最新名单，不能被陈旧快照覆盖")
        self.assertFalse(stub._roster_sending, "收尾后发送者身份必须让出")

    def test_concurrent_callers_do_not_wait_for_stalled_sender(self):
        """阶段 A2：并发调用者置脏即返——旧实现里它们要排队等卡死的 slow 走完 2s 超时。"""
        stub = RuntimeStub()
        slow_sock, obs_sock, slow2_sock = FakeSocket(), FakeSocket(), FakeSocket()
        slow = host.ClientInfo(("10.0.0.2", 2), "slow")
        slow.share_id = "peer:1"
        ss = SlowOnce()
        slow.send_ctrl = ss
        observer = host.ClientInfo(("10.0.0.1", 1), "obs")
        rec = Recorder()
        observer.send_ctrl = rec
        stub.clients = {slow_sock: slow, obs_sock: observer}

        tA = threading.Thread(target=host.HostRuntime.broadcast_roster, args=(stub,))
        tA.start()
        self.assertTrue(ss.entered.wait(2.0), "A 应已进入对 slow 的阻塞发送")

        # A 仍卡在 slow 上，peer2 已入册：三个并发调用者都必须立刻返回
        slow2 = host.ClientInfo(("10.0.0.3", 3), "slow2")
        slow2.share_id = "peer:2"
        slow2.send_ctrl = lambda sock, obj, send_timeout=2.0: None
        with stub.clients_lock:
            stub.clients = {slow_sock: slow, obs_sock: observer, slow2_sock: slow2}
        callers = [threading.Thread(target=host.HostRuntime.broadcast_roster, args=(stub,))
                   for _ in range(3)]
        for t in callers:
            t.start()
        for t in callers:
            t.join(1.0)
            self.assertFalse(t.is_alive(),
                             "并发广播调用被卡死客户端阻塞（A2：应置脏即返）")

        ss.release.set()
        tA.join(3.0)
        self.assertFalse(tA.is_alive())
        with rec.lock:
            final = list(rec.rosters)
        # 三个脏标记合并为一轮重发：observer 总共恰好收到 2 条（旧一轮 + 新一轮）
        self.assertEqual(final, [["peer:1"], ["peer:1", "peer:2"]])

    def test_exception_does_not_wedge_sending_flag(self):
        """发送中非 OSError 异常外抛后，_roster_sending 必须复位（否则此后无人能发送）。"""

        def _boom(sock, obj, send_timeout=2.0):
            raise RuntimeError("boom")

        stub = RuntimeStub()
        bad_sock = FakeSocket()
        bad = host.ClientInfo(("10.0.0.2", 2), "bad")
        bad.share_id = "peer:1"
        bad.send_ctrl = _boom
        stub.clients = {bad_sock: bad}
        with self.assertRaises(RuntimeError):
            host.HostRuntime.broadcast_roster(stub)
        self.assertFalse(stub._roster_sending, "异常后发送者标志卡死")

        # 复位后仍能正常广播
        good_sock = FakeSocket()
        good = host.ClientInfo(("10.0.0.1", 1), "good")
        good.share_id = "peer:2"
        sent = []
        good.send_ctrl = lambda sock, obj, send_timeout=2.0: sent.append(obj)
        stub.clients = {good_sock: good}
        host.HostRuntime.broadcast_roster(stub)
        self.assertTrue(sent, "异常复位后广播应恢复")


if __name__ == "__main__":
    unittest.main()
