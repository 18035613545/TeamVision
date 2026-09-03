# -*- coding: utf-8 -*-
"""host roster 广播顺序单元测试（第 55 条回归）。

缺陷：注册/注销先释放 clients_lock 再广播。线程 A 拿到旧快照 {peer1} 后卡在发送
循环里，线程 B 注册 peer2 后广播完 {peer1,peer2}，A 随后把陈旧的 {peer1} **最后**
送达 → 观看端源列表永久少一个正在共享的人。

修复：broadcast_roster 整个「快照 + 发送」用 _roster_lock 串行化，且每次都在持锁时
重新快照当前状态 → 最后完成的广播必然反映最新名单。

本测试确定性地复现该交错：让 A 在向第一个客户端发送时阻塞（持锁），此时注册 peer2
并发起 B；放行 A 后断言 observer **最后**收到的是 {peer1,peer2}，且顺序为旧在前、
新在后。无锁实现下 B 会抢先送达 {peer1,peer2}、A 再补送陈旧 {peer1}，顺序相反。
纯内存 FakeSocket + 全部 join 的线程，不碰真实 socket/FD。
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
        time.sleep(0.3)  # 给 B 时间发起（若有锁，B 此刻被挡在 _roster_lock 外）
        with rec.lock:
            mid = list(rec.rosters)
        # 有锁：A 卡在 slow、B 被锁挡住 → observer 此刻一条都没收到
        self.assertEqual(mid, [], "B 不应在 A 完成前抢先把名单送达 observer")

        ss.release.set()  # 放行 A → A 投递 observer{peer1} 并释放锁 → B 重快照投递{peer1,peer2}
        tA.join(3.0)
        tB.join(3.0)
        self.assertFalse(tA.is_alive() or tB.is_alive(), "两个广播线程都应结束")

        with rec.lock:
            final = list(rec.rosters)
        # 关键断言：最后送达 observer 的是最新名单，且旧快照在前、新快照在后
        self.assertEqual(final, [["peer:1"], ["peer:1", "peer:2"]])
        self.assertEqual(final[-1], ["peer:1", "peer:2"],
                         "最后送达的必须是最新名单，不能被陈旧快照覆盖")


if __name__ == "__main__":
    unittest.main()
