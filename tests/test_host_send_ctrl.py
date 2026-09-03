# -*- coding: utf-8 -*-
"""host 控制消息发送超时单元测试（第 54 条回归）。

缺陷：send_ctrl 在 _write_lock 内对停止读取的对端做**无超时**阻塞 sendall，
socket timeout 仍是 None → sendall 永久阻塞 → 持锁不放 → 任何 roster 事件都挂住，
新客户端 handle_client 进 recv 循环前就卡死、永不回 pong → 线程堆积。

修复：send_ctrl 发前 `sock.settimeout(send_timeout)`（默认 2.0s，与帧路径
host.py:281/936 一致），超时抛 socket.timeout（OSError 子类），调用方 except
OSError 接住。「有限超时能中断被阻塞的 sendall」是 SO_SNDTIMEO 的 OS 契约，且同
仓库帧发送路径已在生产依赖；Windows 回环会立即吸入任意大 sendall（实测 24MB
0.002s 返回、对端可完整 drain），无法用缓冲耗尽造真实阻塞。

这里用**纯内存 FakeSocket**（不碰真实 socket/FD）确定性地验证代码改动本身：
send_ctrl 把 timeout 从 None 切成有限值并仍完成发送，且两个真实调用方在
send_ctrl 抛 socket.timeout 时不崩、不误拆健康连接。避免真实 socketpair 的
TIME_WAIT 残留扰动同套件里的多连接集成测试。
"""
import socket
import threading
import unittest

import host


class FakeSocket:
    """内存替身 socket：只实现 send_ctrl/send_msg 用到的 settimeout/gettimeout/sendall。"""

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
    """只提供 broadcast_roster / _send_ctrl_safe 依赖的成员。"""

    def __init__(self, clients=None):
        self.clients_lock = threading.Lock()
        self._roster_lock = threading.Lock()  # 第 55 条：broadcast_roster 串行化锁
        self.clients = clients or {}


class TestSendCtrlTimeout(unittest.TestCase):

    def test_switches_socket_from_blocking_to_finite_timeout(self):
        s = FakeSocket()
        s.settimeout(None)  # 模拟 handle_client 进 recv 循环前置 None（旧行为根因）
        self.assertIsNone(s.gettimeout())
        info = host.ClientInfo(("127.0.0.1", 1), "tester")
        info.send_ctrl(s, {"action": "pong", "t": 1})
        self.assertEqual(s.gettimeout(), 2.0)  # 不再是无限阻塞
        self.assertEqual(len(s.sent), 1)       # 消息仍被实际发出

    def test_custom_timeout_is_passed_through(self):
        s = FakeSocket()
        info = host.ClientInfo(("127.0.0.1", 2), "tester")
        info.send_ctrl(s, {"action": "cap", "multiview": True}, send_timeout=0.5)
        self.assertEqual(s.gettimeout(), 0.5)
        self.assertEqual(len(s.sent), 1)

    def test_socket_timeout_is_oserror_subclass(self):
        # 调用方靠 except OSError 接住超时，这是整条容错链的前提
        self.assertTrue(issubclass(socket.timeout, OSError))


class TestCallersTolerateTimeout(unittest.TestCase):

    def test_broadcast_roster_survives_stuck_client(self):
        stuck = host.ClientInfo(("10.0.0.9", 1), "stuck")
        stuck.share_id = "peer:1"
        healthy = host.ClientInfo(("10.0.0.2", 2), "healthy")

        def boom(sock, obj, send_timeout=2.0):
            raise socket.timeout("simulated stuck peer")

        stuck.send_ctrl = boom
        delivered = []
        healthy.send_ctrl = lambda sock, obj, send_timeout=2.0: delivered.append(obj)

        rt = RuntimeStub({FakeSocket(): stuck, FakeSocket(): healthy})
        host.HostRuntime.broadcast_roster(rt)  # 不得外抛
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].get("action"), "peers")

    def test_send_ctrl_safe_swallows_timeout(self):
        # 第 54/56 条：转达失败绝不能外抛，否则观看者自己的连接被 finally 拆掉
        info = host.ClientInfo(("10.0.0.3", 3), "watcher")
        called = []

        def boom(s, obj, send_timeout=2.0):
            called.append(obj)
            raise socket.timeout("simulated stuck sharer")

        info.send_ctrl = boom
        sock = FakeSocket()  # 必须与 dict 键同一对象：_send_ctrl_safe 按身份查 ClientInfo
        rt = RuntimeStub({sock: info})
        host._send_ctrl_safe(rt, sock, {"action": "req_keyframe"})  # 不得外抛
        self.assertEqual(len(called), 1)  # 确实走到了 boom，而非因查不到而空跑


if __name__ == "__main__":
    unittest.main()
