# -*- coding: utf-8 -*-
"""优化方案-2026-09-08 阶段 1 回归测试（分批推进，见 docs/优化方案-2026-09-08.md）。

已覆盖：
  B12  host.handle_client 的配置健壮性——`host`/`net`/`auth` 被手改成非对象时，
       旧实现在**任何 try 之外**抛 AttributeError → 连接线程死亡且 socket 未关闭
       （每连接泄漏一个 fd）；`keepalive_idle_s: "abc"` 则在接收循环入口抛
       ValueError，表现为"每个客户端连上就断"。

hermetic：用 socket.socketpair() 提供真实 fd（select 需要真 socket），runtime 用
MagicMock 提供 clients/clients_lock/stop_event/broadcast_roster。
"""
import copy
import json
import os
import queue
import shutil
import socket
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from unittest import mock

import numpy as np

import common
import host
import viewer


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Hub:
    """以线程方式启动真实 HostRuntime（真实 TCP），供 B1 集成测试使用。"""

    def __init__(self, max_clients=None, msg_timeout_s=None, auth_enabled=False,
                 accounts_file=None, upstream_max_kbps=None, upstream_max_fps=None):
        cfg = copy.deepcopy(common.DEFAULT_CONFIG)
        cfg["host"]["port"] = _free_port()
        cfg["host"]["fps"] = 2
        cfg["host"]["codec"]["encoder"] = "jpeg"   # 测试不解码，省掉编码器依赖
        cfg["host"]["auth"]["enabled"] = bool(auth_enabled)
        if max_clients is not None:
            cfg["host"]["net"]["max_clients"] = max_clients
        if msg_timeout_s is not None:
            cfg["host"]["net"]["msg_timeout_s"] = msg_timeout_s
        if upstream_max_kbps is not None:
            cfg["host"]["net"]["upstream_max_kbps"] = upstream_max_kbps
        if upstream_max_fps is not None:
            cfg["host"]["net"]["upstream_max_fps"] = upstream_max_fps
        # accounts_file：把账户表指向临时文件，避免测试污染仓库根目录的 accounts.json
        patcher = (mock.patch.object(host, "accounts_path", return_value=accounts_file)
                   if accounts_file else nullcontext())
        with patcher:
            self.runtime = host.HostRuntime(cfg)
        self.runtime.start_server()
        deadline = time.time() + 5
        while time.time() < deadline and self.runtime._server is None:
            time.sleep(0.05)
        self.port = self.runtime._server.getsockname()[1]

    def join(self):
        """连接 + 握手 + probe（准入关闭），返回 (socket, auth_result)。"""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        common.client_handshake(s)
        common.send_msg(s, {"action": "probe", "user": "", "pass": ""})
        return s, common.recv_msg(s)

    def close(self):
        self.runtime.shutdown()


def _wait_for(predicate, timeout=5.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _wait_action(sock, action, timeout=6.0, state=None):
    """在 timeout 内读取消息，直到出现指定 action。

    不能用 common.recv_msg：它遇到视频/JPEG 帧会抛 ConnectionError（host 默认在
    给订阅者推 JPEG 帧），这里自己按协议逐条解析、跳过帧与其它广播。
    state 用于在同一条连接上跨多次调用保留未解析完的半截字节，避免下次从消息
    中间开始解析（否则会把 payload 当消息头，报"非法的消息类型"）。
    """
    state = state if state is not None else {}
    buf = state.get("buf", b"")
    deadline = time.time() + timeout
    while time.time() < deadline:
        sock.settimeout(max(0.2, deadline - time.time()))
        try:
            chunk = sock.recv(65536)
        except (socket.timeout, OSError):
            state["buf"] = buf
            return None
        if not chunk:
            state["buf"] = buf
            return None
        buf += chunk
        while True:
            consumed, kind, payload = common.parse_message(buf)
            if consumed == 0:
                break
            buf = buf[consumed:]
            if kind != common.MSG_CTRL:
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("action") == action:
                state["buf"] = buf
                return msg
    state["buf"] = buf
    return None


# ---------------------------------------------------------------- B1：连接上限

class TestConnectionLimit(unittest.TestCase):
    def setUp(self):
        self.hub = _Hub(max_clients=2)
        self.addCleanup(self.hub.close)

    def test_third_connection_is_rejected_and_first_two_keep_working(self):
        s1, r1 = self.hub.join()
        s2, r2 = self.hub.join()
        self.addCleanup(s1.close)
        self.addCleanup(s2.close)
        self.assertTrue(r1.get("ok"), r1)
        self.assertTrue(r2.get("ok"), r2)
        self.assertTrue(_wait_for(lambda: self.hub.runtime.conn_count() == 2))

        # 第三个连接：握手必须失败（服务端立即关闭，不产生第 3 条连接线程/缓冲）
        s3 = socket.create_connection(("127.0.0.1", self.hub.port), timeout=5)
        s3.settimeout(5)
        try:
            with self.assertRaises((ConnectionError, OSError)):
                common.client_handshake(s3)
        finally:
            s3.close()
        # 被拒连接不留残额
        self.assertTrue(_wait_for(lambda: self.hub.runtime.conn_count() == 2))

        # 老连接仍然可用（ping → pong；期间可能夹带 roster/peers 广播）
        common.send_msg(s1, {"action": "ping", "t": 123})
        self.assertIsNotNone(_wait_action(s1, "pong"), "老连接未收到 pong")

    def test_slot_is_freed_after_client_leaves(self):
        s1, _ = self.hub.join()
        s2, _ = self.hub.join()
        self.addCleanup(s2.close)
        self.assertTrue(_wait_for(lambda: self.hub.runtime.conn_count() == 2))
        s1.close()
        self.assertTrue(_wait_for(lambda: self.hub.runtime.conn_count() == 1))
        s4, r4 = self.hub.join()          # 腾出的名额可被复用
        self.addCleanup(s4.close)
        self.assertTrue(r4.get("ok"), r4)


class TestConnectionSlotAccounting(unittest.TestCase):
    def test_acquire_release_semantics(self):
        cfg = copy.deepcopy(common.DEFAULT_CONFIG)
        cfg["host"]["net"]["max_clients"] = 2
        rt = host.HostRuntime(cfg)
        self.assertEqual(rt.conn_count(), 0)
        self.assertTrue(rt.acquire_conn())
        self.assertTrue(rt.acquire_conn())
        self.assertFalse(rt.acquire_conn())      # 超限
        self.assertEqual(rt.conn_count(), 2)
        rt.release_conn()
        self.assertEqual(rt.conn_count(), 1)
        self.assertTrue(rt.acquire_conn())
        rt.release_conn()
        rt.release_conn()
        rt.release_conn()                        # 幂等：不会变成负数
        self.assertEqual(rt.conn_count(), 0)

    def test_invalid_config_falls_back_to_default(self):
        cfg = copy.deepcopy(common.DEFAULT_CONFIG)
        cfg["host"]["net"]["max_clients"] = "abc"
        cfg["host"]["net"]["msg_timeout_s"] = None
        rt = host.HostRuntime(cfg)
        self.assertEqual(rt.max_clients, 32)
        self.assertEqual(rt.msg_timeout, 15.0)


# ---------------------------------------------------------------- B1：单条消息总时限

class TestMessageDeadline(unittest.TestCase):
    def setUp(self):
        self.hub = _Hub(msg_timeout_s=1.0)
        self.addCleanup(self.hub.close)

    def test_partial_message_is_dropped(self):
        # 只发 2 字节（不足 5 字节头）后静默：旧实现可无限期占用线程与缓冲，
        # 且每次收到字节都刷新 last_rx_at，半开检测永远不触发。
        s, reply = self.hub.join()
        self.addCleanup(s.close)
        self.assertTrue(reply.get("ok"), reply)
        self.assertTrue(_wait_for(lambda: self.hub.runtime.conn_count() == 1))
        s.sendall(b"\x02\x00")

        closed = False
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                s.settimeout(0.5)
                if not s.recv(65536):
                    closed = True
                    break
            except socket.timeout:
                continue
            except OSError:
                closed = True
                break
        self.assertTrue(closed, "半截消息未被服务端断开")
        self.assertTrue(_wait_for(lambda: self.hub.runtime.conn_count() == 0))

    def test_complete_message_resets_deadline(self):
        # 正控：正常发送完整控制消息（ping）不应被误断
        s, reply = self.hub.join()
        self.addCleanup(s.close)
        self.assertTrue(reply.get("ok"), reply)
        state = {}
        for i in range(3):
            common.send_msg(s, {"action": "ping", "t": i})
            time.sleep(1.2)                       # 超过 msg_timeout，但每条消息都完整
            self.assertIsNotNone(_wait_action(s, "pong", state=state),
                                 "第 %d 次 ping 未收到 pong" % i)
        self.assertEqual(self.hub.runtime.conn_count(), 1)


def _run_client(host_cfg, client_magic=b"FS02", settle=0.4):
    """启动一个真实 handle_client 线程，握手后返回 (线程, 服务端socket, runtime, 响应)。

    握手成功后立即补发一条 probe：真实观看端就是这么做的（viewer.py 连接后先发
    probe），也让服务端"可选 auth 等待窗口"（2 秒）立刻结束、客户端被登记入册，
    否则测试要空等 2 秒。
    """
    server_sock, client_sock = socket.socketpair()
    rt = mock.MagicMock()
    rt.cfg = {"host": host_cfg}
    rt.clients = {}
    rt.clients_lock = threading.RLock()
    rt.stop_event = threading.Event()

    th = threading.Thread(target=host.handle_client,
                          args=(server_sock, ("127.0.0.1", 1), rt), daemon=True)
    th.start()
    client_sock.settimeout(3)
    try:
        client_sock.sendall(client_magic)
        reply = client_sock.recv(4)
        if client_magic == b"FS02":
            client_sock.sendall(common.pack_msg({"action": "probe"}))
    except Exception:
        reply = b""
    time.sleep(settle)          # 让接收循环跑一个 select 周期
    return th, server_sock, client_sock, rt, reply


def _finish(th, server_sock, client_sock, rt):
    rt.stop_event.set()
    th.join(timeout=5)
    try:
        client_sock.close()
    except OSError:
        pass


class TestHandleClientConfigRobustness(unittest.TestCase):
    def test_net_not_dict_does_not_kill_connection(self):
        # 第 102 条：旧实现在 tune_socket 之前的 `cfg["host"].get("net")` 就抛
        # AttributeError（在 try 之外）→ 不握手、不关闭 socket。
        th, server_sock, client_sock, rt, reply = _run_client({"net": "not-a-dict"})
        try:
            self.assertEqual(reply, b"OK02")          # 握手确实完成了
            self.assertEqual(len(rt.clients), 1)      # 已注册，连接存活
            self.assertNotEqual(server_sock.fileno(), -1)   # socket 未被泄漏式关闭
        finally:
            _finish(th, server_sock, client_sock, rt)
        self.assertEqual(server_sock.fileno(), -1)    # 结束后确实关闭（finally 生效）
        self.assertEqual(len(rt.clients), 0)

    def test_host_not_dict_does_not_kill_connection(self):
        th, server_sock, client_sock, rt, reply = _run_client("not-a-dict")
        try:
            self.assertEqual(reply, b"OK02")
            self.assertEqual(len(rt.clients), 1)
        finally:
            _finish(th, server_sock, client_sock, rt)

    def test_auth_not_dict_treated_as_disabled(self):
        th, server_sock, client_sock, rt, reply = _run_client(
            {"net": {}, "auth": "not-a-dict"})
        try:
            self.assertEqual(reply, b"OK02")
            self.assertEqual(len(rt.clients), 1)
        finally:
            _finish(th, server_sock, client_sock, rt)

    def test_bad_keepalive_idle_does_not_drop_client(self):
        # 旧实现 `idle_s = float("abc")` 在接收循环入口抛 ValueError → 立刻断开
        th, server_sock, client_sock, rt, reply = _run_client(
            {"net": {"keepalive_idle_s": "abc"}})
        try:
            self.assertEqual(reply, b"OK02")
            self.assertEqual(len(rt.clients), 1)      # 仍然在线（未被"连上就断"）
        finally:
            _finish(th, server_sock, client_sock, rt)
        self.assertEqual(len(rt.clients), 0)

    def test_bad_magic_is_rejected_and_socket_closed(self):
        th, server_sock, client_sock, rt, reply = _run_client(
            {"net": {}}, client_magic=b"XXXX", settle=0.2)
        try:
            self.assertEqual(reply, b"")
        finally:
            _finish(th, server_sock, client_sock, rt)
        self.assertEqual(server_sock.fileno(), -1)
        self.assertEqual(len(rt.clients), 0)


# ---------------------------------------------------------------- B2：入队原子性

class _LockProbe:
    """记录加锁/解锁顺序的替身锁。"""

    def __init__(self, events):
        self._events = events
        self._lock = threading.Lock()

    def __enter__(self):
        self._events.append("enter")
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._events.append("exit")
        self._lock.release()
        return False


class _RecordingQueue:
    """容量 1 的假队列，把 get/put 记进同一个事件序列。"""

    def __init__(self, events):
        self._events = events
        self.items = []

    def get_nowait(self):
        self._events.append("get")
        if not self.items:
            raise queue.Empty
        return self.items.pop(0)

    def put_nowait(self, item):
        self._events.append("put")
        if len(self.items) >= 1:
            raise queue.Full
        self.items.append(item)


class TestEnqueueAtomicity(unittest.TestCase):
    def test_get_and_put_happen_inside_the_lock(self):
        # B2：本地发送线程与 peer 转发线程会并发入队，get+put 必须在同一临界区，
        # 否则交错时帧被静默丢弃而 dropped=False（愈合门控不触发 → 花屏）。
        events = []
        info = host.ClientInfo(("1.2.3.4", 1))
        info._lock = _LockProbe(events)
        info.send_queue = _RecordingQueue(events)

        info.enqueue_frame(b"A")

        self.assertEqual(events, ["enter", "get", "put", "exit"])

    def test_concurrent_enqueues_leave_single_latest_frame(self):
        info = host.ClientInfo(("1.2.3.4", 1))
        errors = []

        def worker(tag):
            try:
                for i in range(300):
                    info.enqueue_frame(("%s-%d" % (tag, i)).encode())
            except Exception as e:      # noqa: BLE001 - 测试收集所有异常
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b", "c", "d")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(info.send_queue.qsize(), 1)     # 容量 1，始终只留最新帧
        self.assertGreater(info.dropped_frames, 0)


# ---------------------------------------------------------------- B3：peer 关键帧愈合

class _FakeSock:
    """记录 sendall 字节的假 socket（供 ClientInfo.send_ctrl 使用）。"""

    def __init__(self):
        self.sent = []

    def settimeout(self, t):
        pass

    def gettimeout(self):
        return None

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        pass


def _actions(sock):
    """把假 socket 收到的字节解析成 action 列表。"""
    buf = b"".join(sock.sent)
    out = []
    while True:
        consumed, kind, payload = common.parse_message(buf)
        if consumed == 0:
            break
        buf = buf[consumed:]
        if kind == common.MSG_CTRL:
            out.append(json.loads(payload.decode("utf-8")).get("action"))
    return out


class TestPeerKeyframeHealing(unittest.TestCase):
    def _setup(self, source="peer:1", dropped=True, is_key=False, is_jpeg=False):
        rt = host.HostRuntime(copy.deepcopy(common.DEFAULT_CONFIG))
        sharer_sock = _FakeSock()
        sharer = host.ClientInfo(("1.1.1.1", 1))
        sharer.share_id = "peer:1"
        sub_sock = _FakeSock()
        sub = host.ClientInfo(("2.2.2.2", 2))
        sub.watch_source = "peer:1"
        sub.need_key = False        # 门控已打开（否则 route_frame 直接跳过该帧）
        sub._sender_active = True
        sub.enqueue_frame = lambda frame: dropped
        rt.clients = {sharer_sock: sharer, sub_sock: sub}
        return rt, sharer_sock, sub_sock, sub

    def test_peer_drop_forwards_keyframe_request_to_sharer(self):
        rt, sharer_sock, _, sub = self._setup()
        host.route_frame(rt, "peer:1", b"P", is_key=False, is_jpeg=False)
        self.assertIn("req_keyframe", _actions(sharer_sock))   # 队友被要求补关键帧
        self.assertTrue(sub.need_key)                          # 订阅者门控到关键帧

    def test_peer_keyframe_request_is_deduplicated(self):
        rt, sharer_sock, _, _ = self._setup()
        for _ in range(5):
            host.route_frame(rt, "peer:1", b"P", is_key=False, is_jpeg=False)
        self.assertEqual(_actions(sharer_sock).count("req_keyframe"), 1)

    def test_local_drop_does_not_send_peer_request(self):
        # 本地源丢帧：只置 force_key（不向任何 peer 转发请求）
        rt = host.HostRuntime(copy.deepcopy(common.DEFAULT_CONFIG))
        sub = host.ClientInfo(("2.2.2.2", 2))
        sub.watch_source = "local"
        sub.need_key = False
        sub._sender_active = True
        sub.enqueue_frame = lambda frame: True
        rt.clients = {_FakeSock(): sub}
        host.route_frame(rt, "local", b"P", is_key=False, is_jpeg=False)
        self.assertTrue(rt.force_key)
        self.assertEqual(rt._peer_key_at, {})            # 没有 peer 请求记录

    def test_jpeg_drop_does_not_request_keyframe(self):
        rt, sharer_sock, _, sub = self._setup(is_jpeg=True)
        host.route_frame(rt, "peer:1", b"J", is_key=False, is_jpeg=True)
        self.assertEqual(_actions(sharer_sock), [])
        self.assertFalse(sub.need_key)

    def test_keyframe_drop_does_not_request_keyframe(self):
        rt, sharer_sock, _, _ = self._setup(is_key=True)
        host.route_frame(rt, "peer:1", b"K", is_key=True, is_jpeg=False)
        self.assertEqual(_actions(sharer_sock), [])


# ---------------------------------------------------------------- B10/B11：认证

class TestAuthFieldValidation(unittest.TestCase):
    """B11：认证消息的 user/pass 必须是字符串，非法类型给明确回复而非"认证异常"。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="tv_auth_")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.hub = _Hub(auth_enabled=True,
                        accounts_file=os.path.join(self._tmp, "accounts.json"))
        self.addCleanup(self.hub.close)

    def _raw_connect(self):
        s = socket.create_connection(("127.0.0.1", self.hub.port), timeout=5)
        self.addCleanup(s.close)
        common.client_handshake(s)
        return s

    def test_non_string_user_is_rejected_with_clear_message(self):
        s = self._raw_connect()
        common.send_msg(s, {"action": "login", "user": 123, "pass": "x"})
        reply = common.recv_msg(s)
        self.assertFalse(reply.get("ok"))
        self.assertIn("格式非法", reply.get("msg", ""))
        s.settimeout(3)
        self.assertEqual(s.recv(100), b"")        # 服务端断开

    def test_non_string_pass_is_rejected_with_clear_message(self):
        s = self._raw_connect()
        common.send_msg(s, {"action": "register", "user": "alice", "pass": 456})
        reply = common.recv_msg(s)
        self.assertFalse(reply.get("ok"))
        self.assertIn("格式非法", reply.get("msg", ""))
        s.settimeout(3)
        self.assertEqual(s.recv(100), b"")

    def test_string_credentials_still_work(self):
        # 正控：字符串凭据走正常注册/登录流程
        s = self._raw_connect()
        common.send_msg(s, {"action": "register", "user": "carol", "pass": "pw123456"})
        reply = common.recv_msg(s)
        self.assertTrue(reply.get("ok"), reply)


class TestAuthMessageVisible(unittest.TestCase):
    """B10：认证失败原因必须能显示出来（原来 auth_msg 只写不读）。"""

    def test_snapshot_carries_auth_msg(self):
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        ch = mock.MagicMock()
        ch.lock = threading.RLock()
        ch.auth_msg = "用户名或密码错误"
        app.channels = [ch]
        snap = app.get_channels_snapshot()
        self.assertEqual(snap[0]["auth_msg"], "用户名或密码错误")

    def test_panel_preview_shows_auth_reason(self):
        # 不建真实 Tk：同一进程里反复创建/销毁 Tcl 解释器会在 GC 时触发
        # "Tcl_AsyncDelete: async handler deleted by the wrong thread" 硬崩溃，
        # 这里用 MagicMock 断言 configure 的实参即可（真实 Tk 路径由 stage0 覆盖）。
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app.running = True
        app.panel = mock.MagicMock()
        app._panel_visible = lambda: True
        app.active_idx = 0
        app._panel_selected = None
        app._panel_last_idx = -1
        app._panel_last_serial = -1
        app._panel_photo = None
        app._preview_label = mock.MagicMock()
        app._preview_info = mock.MagicMock()
        ch = mock.MagicMock()
        ch.lock = threading.RLock()
        ch.name = "alice"
        ch.status = "auth"
        ch.auth_msg = "用户名或密码错误"
        ch.frame_serial = 0
        ch.last_frame = None
        app.channels = [ch]

        app._panel_refresh_preview()

        info_texts = [c.kwargs.get("text") for c in app._preview_info.configure.call_args_list]
        self.assertTrue(any(t and "用户名或密码错误" in t for t in info_texts), info_texts)
        # 同时验证 A3 的清理写法（image="" 而不是 None）
        self.assertEqual(app._preview_label.configure.call_args.kwargs.get("image"), "")


# ---------------------------------------------------------------- B4：控制消息写入失败

class TestControlWriteFailureDropsClient(unittest.TestCase):
    """B4：sendall 超时/短写后可能已写出半截消息，连接必须移除（否则字节流永久错位）。"""

    def test_send_ctrl_or_drop_removes_client_on_oserror(self):
        rt = host.HostRuntime(copy.deepcopy(common.DEFAULT_CONFIG))
        sock = _FakeSock()
        info = host.ClientInfo(("1.2.3.4", 1))
        info.send_ctrl = mock.MagicMock(side_effect=OSError("模拟写失败"))
        rt.clients = {sock: info}

        self.assertFalse(host._send_ctrl_or_drop(rt, sock, info, {"action": "pong"}))
        self.assertEqual(rt.clients, {})            # 已从广播列表移除
        self.assertTrue(info.send_stop.is_set())    # 发送线程已通知退出

    def test_broadcast_roster_drops_failing_client_without_deadlock(self):
        # 失败的客户端带 share_id：_drop_client → _unregister_sharer → broadcast_roster
        # 会再次取 _roster_lock；若在持锁时移除就会自死锁（本用例即回归点）。
        rt = host.HostRuntime(copy.deepcopy(common.DEFAULT_CONFIG))
        good_sock = _FakeSock()
        good = host.ClientInfo(("1.1.1.1", 1))
        good.share_id = "peer:1"
        bad_sock = _FakeSock()
        bad = host.ClientInfo(("2.2.2.2", 2))
        bad.share_id = "peer:2"
        bad.send_ctrl = mock.MagicMock(side_effect=OSError("模拟写失败"))
        rt.clients = {good_sock: good, bad_sock: bad}

        rt.broadcast_roster()

        self.assertIn(good_sock, rt.clients)
        self.assertNotIn(bad_sock, rt.clients)
        self.assertIn("peers", _actions(good_sock))  # 正常客户端照常收到名单

    def test_handle_client_disconnects_when_reply_fails(self):
        # ping 的 pong 写失败 → 立即断开，而不是留在列表里继续收发错位字节
        with mock.patch.object(host.ClientInfo, "send_ctrl",
                               side_effect=OSError("模拟写失败")):
            th, server_sock, client_sock, rt, reply = _run_client({"net": {}}, settle=0.3)
            try:
                self.assertEqual(reply, b"OK02")
                client_sock.sendall(common.pack_msg({"action": "ping", "t": 1}))
                self.assertTrue(_wait_for(lambda: len(rt.clients) == 0),
                                "控制消息写失败后连接未被断开")
            finally:
                _finish(th, server_sock, client_sock, rt)
        self.assertEqual(server_sock.fileno(), -1)


# ---------------------------------------------------------------- B5：去冗余拷贝

class TestNoRedundantFrameCopy(unittest.TestCase):
    """B5：last_frame 只整体替换、从不原地修改，读取方只取引用即可。"""

    def test_refresh_overlay_passes_same_object(self):
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        frame = np.zeros((4, 4, 3), np.uint8)
        ch = mock.MagicMock()
        ch.lock = threading.RLock()
        ch.last_frame = frame
        ch.frame_serial = 1
        ch.status = "receiving"
        app.channels = [ch]
        app.active_idx = 0
        app._last_channel_idx = -1
        app._last_frame_serial = -1
        got = []
        app._show_frame = lambda f, s: got.append(f)

        app._refresh_overlay()

        self.assertTrue(got)
        self.assertIs(got[0], frame)        # 旧实现是 copy()，这里必须是同一对象

    def test_panel_preview_passes_same_object(self):
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app.running = True
        app.panel = mock.MagicMock()
        app._panel_visible = lambda: True
        app.active_idx = 0
        app._panel_selected = None
        app._panel_last_idx = -1
        app._panel_last_serial = -1
        app._panel_photo = None
        app._preview_label = mock.MagicMock()
        app._preview_info = mock.MagicMock()
        frame = np.zeros((4, 4, 3), np.uint8)
        ch = mock.MagicMock()
        ch.lock = threading.RLock()
        ch.name = "alice"
        ch.status = "receiving"
        ch.auth_msg = ""
        ch.frame_serial = 1
        ch.last_frame = frame
        app.channels = [ch]

        captured = []

        def fake_cvt(src, code):
            captured.append(src)
            raise RuntimeError("stop here")

        with mock.patch.object(viewer.cv2, "cvtColor", fake_cvt):
            app._panel_refresh_preview()

        self.assertTrue(captured)
        self.assertIs(captured[0], frame)


# ---------------------------------------------------------------- B6：解码代数校验

class _FakeDecoder:
    """可注入行为的假解码器：记录喂进去的 NAL，返回一个"帧"对象。"""

    available = True

    def __init__(self, on_decode=None):
        self.nals = []
        self._on_decode = on_decode

    def decode(self, nal):
        self.nals.append(nal)
        if self._on_decode is not None:
            self._on_decode()
        return [object()]

    def set_codec(self, codec):
        pass


class TestDecoderGeneration(unittest.TestCase):
    def _channel(self, decoder):
        ch = viewer.Channel.__new__(viewer.Channel)
        ch.name = "t"
        ch._gen = 0
        ch._got_key = False
        ch.cur_codec = common.CODEC_H264
        ch.video_mode = False
        ch._video_unavailable_warned = False
        ch._retry_delay = 1.0
        ch._decoder = decoder
        ch._request_keyframe = lambda sock: None
        ch.stored = []
        ch._store_frame = lambda frame, ts: ch.stored.append((frame, ts))
        return ch

    def test_batch_is_dropped_when_source_switches_mid_batch(self):
        ch = self._channel(None)
        dec = _FakeDecoder(on_decode=lambda: setattr(ch, "_gen", ch._gen + 1))
        ch._decoder = dec
        batch = [(1, common.CODEC_H264, common.VIDEO_FLAG_KEY, b"nal-a"),
                 (2, common.CODEC_H264, 0, b"nal-b")]

        ch._decode_video_batch(None, batch)

        self.assertEqual(dec.nals, [b"nal-a"])   # 第二批帧未喂给新解码器
        self.assertEqual(ch.stored, [])          # 也不落盘显示帧（旧源画面不上屏）

    def test_batch_is_processed_when_generation_unchanged(self):
        ch = self._channel(_FakeDecoder())
        batch = [(1, common.CODEC_H264, common.VIDEO_FLAG_KEY, b"nal-a"),
                 (2, common.CODEC_H264, 0, b"nal-b")]

        ch._decode_video_batch(None, batch)

        self.assertEqual(ch._decoder.nals, [b"nal-a", b"nal-b"])
        self.assertEqual(len(ch.stored), 1)
        self.assertEqual(ch.stored[0][1], 2)     # 存的是最后一帧的时间戳

    def test_switch_source_bumps_generation(self):
        ch = viewer.Channel.__new__(viewer.Channel)
        ch.name = "t"
        ch.lock = threading.RLock()
        ch.peers = []
        ch.watch_source = "peer:1"        # 与目标源不同，才会走到重置块
        ch._got_key = True
        ch.video_mode = True
        ch.cur_codec = common.CODEC_HEVC
        ch.last_frame = object()
        ch.frame_serial = 0
        ch._gen = 0
        ch._active_sock = None            # 无连接 → 发包失败回滚，但代数已递增
        with mock.patch.object(viewer.codec_mod, "VideoDecoder",
                               lambda codec=None: _FakeDecoder()):
            self.assertFalse(ch.switch_source("local"))
        self.assertEqual(ch._gen, 1)


# ---------------------------------------------------------------- B7：共享锁不阻塞接收线程

class _LockState:
    """记录"当前是否持锁"的替身锁（用于断言阻塞操作发生在锁外）。"""

    def __init__(self):
        self.held = False
        self.enters = 0

    def __enter__(self):
        self.held = True
        self.enters += 1
        return self

    def __exit__(self, *exc):
        self.held = False
        return False


class _FakeShare:
    def __init__(self, lock_state=None):
        self.key_requests = 0
        self.stopped = 0
        self.held_during_stop = None
        self.alive = True
        self._lock_state = lock_state

    def request_keyframe(self):
        self.key_requests += 1

    def stop(self):
        self.stopped += 1
        if self._lock_state is not None:
            self.held_during_stop = self._lock_state.held
        self.alive = False


class TestShareLockDoesNotBlockReceiver(unittest.TestCase):
    """B7：接收线程路径上的共享操作不得等 2 秒的 join。"""

    def _channel(self, lock_state, session):
        ch = viewer.Channel.__new__(viewer.Channel)
        ch.name = "t"
        ch._share_lock = lock_state
        ch._share = session
        ch.share_enabled = True
        ch.multiview = True
        ch.status = "receiving"
        ch._active_sock = None
        ch._cap_event = threading.Event()
        return ch

    def test_request_keyframe_does_not_take_share_lock(self):
        class _Exploding:
            def __enter__(self):
                raise AssertionError("request_keyframe 不应取 _share_lock")

            def __exit__(self, *exc):
                return False

        session = _FakeShare()
        ch = self._channel(_Exploding(), session)
        ch.request_keyframe()                    # 不得抛
        self.assertEqual(session.key_requests, 1)

    def test_stop_share_joins_outside_lock(self):
        lock = _LockState()
        session = _FakeShare(lock)
        ch = self._channel(lock, session)

        ch._stop_share()

        self.assertEqual(session.stopped, 1)
        self.assertFalse(session.held_during_stop, "stop() 仍在持锁时执行（会阻塞接收线程）")
        self.assertIsNone(ch._share)

    def test_set_share_off_joins_outside_lock(self):
        lock = _LockState()
        session = _FakeShare(lock)
        ch = self._channel(lock, session)

        ok, err = ch.set_share(False)

        self.assertTrue(ok, err)
        self.assertFalse(session.held_during_stop)
        self.assertFalse(ch.share_enabled)

    def test_remove_channel_stops_outside_channels_lock(self):
        lock = _LockState()
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app._channels_lock = lock
        app.active_idx = 0
        app._refresh_overlay = lambda: None
        app._save_channels = lambda: None
        ch = mock.MagicMock()
        ch.lock = threading.RLock()
        ch.held_during_stop = None
        ch.stop = lambda: setattr(ch, "held_during_stop", lock.held)
        app.channels = [ch]

        app.remove_channel(0)

        self.assertEqual(app.channels, [])
        self.assertFalse(ch.held_during_stop, "stop() 仍在持 _channels_lock（会阻塞接收线程）")


# ---------------------------------------------------------------- B8：上行限速

class TestUpstreamRateLimitUnit(unittest.TestCase):
    def test_byte_bucket_throttles_then_refills(self):
        info = host.ClientInfo(("1.2.3.4", 1))
        # 100 Kbps → 桶容量 25000 字节（2 秒额度，容忍关键帧突发）
        self.assertTrue(info.allow_upstream(10000, 100, 30, now=100.0))
        self.assertTrue(info.allow_upstream(10000, 100, 30, now=100.0))
        self.assertFalse(info.allow_upstream(6000, 100, 30, now=100.0))
        self.assertEqual(info._up_dropped, 1)
        # 1 秒后补 12500 字节额度
        self.assertTrue(info.allow_upstream(6000, 100, 30, now=101.0))

    def test_frame_bucket_limits_frame_rate(self):
        info = host.ClientInfo(("1.2.3.4", 1))
        self.assertTrue(info.allow_upstream(1, 1000, 1, now=100.0))
        self.assertFalse(info.allow_upstream(1, 1000, 1, now=100.0))
        self.assertTrue(info.allow_upstream(1, 1000, 1, now=101.0))

    def test_first_frame_is_never_dropped(self):
        # 桶初始填满：成员的首帧（通常是较大的关键帧）必须放行
        info = host.ClientInfo(("1.2.3.4", 1))
        self.assertTrue(info.allow_upstream(500000, 2500, 30, now=1.0))


class TestUpstreamRateLimitIntegration(unittest.TestCase):
    def setUp(self):
        self.hub = _Hub(upstream_max_fps=2, upstream_max_kbps=50)
        self.addCleanup(self.hub.close)

    def test_member_upload_is_throttled(self):
        jpeg = b"\xff\xd8\xff\xd9"
        member, r1 = self.hub.join()
        self.addCleanup(member.close)
        watcher, r2 = self.hub.join()
        self.addCleanup(watcher.close)
        self.assertTrue(r1.get("ok") and r2.get("ok"))

        # 成员发一帧 → 登记为 peer:1
        member.sendall(common.pack_frame(jpeg, 1))
        self.assertTrue(_wait_for(lambda: any(
            getattr(c, "share_id", None) for c in self.hub.runtime.clients.values())))

        common.send_msg(watcher, {"action": "watch", "source": "peer:1"})
        time.sleep(0.3)

        # 半秒内猛发 20 帧，远超 2 fps 限额
        for i in range(20):
            member.sendall(common.pack_frame(jpeg, 100 + i))

        got = _count_frames(watcher, 1.5)
        self.assertGreaterEqual(got, 1, "正常帧也被误伤")
        self.assertLess(got, 20, "超额帧未被限速")

        member_info = [c for c in self.hub.runtime.clients.values()
                       if getattr(c, "share_id", None)]
        self.assertTrue(member_info)
        self.assertGreater(member_info[0]._up_dropped, 0)


def _count_frames(sock, duration):
    """统计 duration 秒内收到的 JPEG/视频帧数（跳过控制消息）。"""
    buf = b""
    frames = 0
    deadline = time.time() + duration
    while time.time() < deadline:
        sock.settimeout(max(0.05, deadline - time.time()))
        try:
            chunk = sock.recv(65536)
        except (socket.timeout, OSError):
            break
        if not chunk:
            break
        buf += chunk
        while True:
            consumed, kind, payload = common.parse_message(buf)
            if consumed == 0:
                break
            buf = buf[consumed:]
            if kind in (common.MSG_FRAME, common.MSG_VIDEO):
                frames += 1
    return frames


# ---------------------------------------------------------------- B16：channels 类型校验

class TestChannelItemsNormalization(unittest.TestCase):
    """B16：手改 config.json 的 channels 不得让启动崩溃。"""

    def test_non_list_channels_falls_back_to_default(self):
        items = viewer._normalize_channel_items(
            {"channels": "192.168.1.5:5700", "server_addr": "10.0.0.1:5700"})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["addr"], "10.0.0.1:5700")

    def test_dict_channels_falls_back_to_default(self):
        items = viewer._normalize_channel_items({"channels": {"a": 1}})
        self.assertEqual(items[0]["name"], "默认频道")

    def test_non_dict_items_are_skipped(self):
        items = viewer._normalize_channel_items(
            {"channels": ["oops", {"name": "ok", "addr": "1.2.3.4:5700"}, 42]})
        self.assertEqual([i["name"] for i in items], ["ok"])

    def test_valid_channels_pass_through(self):
        raw = [{"name": "a", "addr": "1.1.1.1:5700"}, {"addr": "2.2.2.2:5700"}]
        items = viewer._normalize_channel_items({"channels": raw})
        self.assertEqual(items, raw)


# ---------------------------------------------------------------- B13：退避可中断

class TestInterruptibleBackoff(unittest.TestCase):
    def _bare_channel(self):
        ch = viewer.Channel.__new__(viewer.Channel)
        ch.name = "t"
        ch._stop_evt = threading.Event()
        ch._running = True
        ch._share_lock = threading.RLock()
        ch._share = None
        ch._active_sock = None
        ch._share_sync_timer = None
        return ch

    def test_stop_sets_event_and_unblocks_waiter(self):
        ch = self._bare_channel()
        started = threading.Event()
        unblocked = threading.Event()

        def waiter():
            started.set()
            if ch._stop_evt.wait(30.0):     # 模拟退避等待
                unblocked.set()

        th = threading.Thread(target=waiter, daemon=True)
        th.start()
        self.assertTrue(started.wait(2.0))
        ch.stop()
        self.assertTrue(unblocked.wait(2.0), "stop() 未打断退避等待（线程会睡满退避）")

    def test_request_stop_also_interrupts(self):
        ch = self._bare_channel()
        ch.request_stop()
        self.assertTrue(ch._stop_evt.is_set())

    def test_real_channel_thread_is_reclaimed_after_stop(self):
        def recv_threads():
            return sum(1 for t in threading.enumerate() if t.name == "channel-recv")

        before = recv_threads()
        ch = viewer.Channel("t", "127.0.0.1:1", owner=None)   # 端口 1 必然被拒
        time.sleep(0.3)
        ch.stop()
        self.assertTrue(
            _wait_for(lambda: recv_threads() <= before, timeout=3.0),
            "stop() 后 channel-recv 线程未回收")


# ---------------------------------------------------------------- B14：UI 回调队列

class TestPostUiQueue(unittest.TestCase):
    def _app(self):
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app.running = True
        app._mainloop_ready = threading.Event()
        app._mainloop_ready.set()
        app._ui_queue = queue.SimpleQueue()
        app.root = mock.MagicMock()
        return app

    def test_post_ui_enqueues_and_drain_runs_in_order(self):
        app = self._app()
        calls = []
        app._post_ui(lambda: calls.append("a"))
        app._post_ui(lambda: calls.append("b"))

        app._drain_ui_queue()

        self.assertEqual(calls, ["a", "b"])
        self.assertFalse(app.root.after.called, "不应再从工作线程直接调 root.after")

    def test_post_ui_drops_when_not_running_or_not_ready(self):
        app = self._app()
        app.running = False
        app._post_ui(lambda: None)
        app.running = True
        app._mainloop_ready.clear()
        app._post_ui(lambda: None)
        self.assertTrue(app._ui_queue.empty())

    def test_drain_survives_callback_exception(self):
        app = self._app()
        calls = []

        def boom():
            raise RuntimeError("boom")

        app._post_ui(boom)
        app._post_ui(lambda: calls.append("after-boom"))
        with mock.patch.object(viewer, "log") as mlog:
            app._drain_ui_queue()
        self.assertEqual(calls, ["after-boom"])
        self.assertTrue(mlog.warning.called)


# ---------------------------------------------------------------- B15：凭据队列失效校验

class _FakeEvent:
    """wait() 立即返回的假事件（避免测试真的等 120 秒）。"""

    def __init__(self):
        self._set = False

    def set(self):
        self._set = True

    def is_set(self):
        return self._set

    def wait(self, timeout=None):
        return self._set


class TestCredentialQueueHygiene(unittest.TestCase):
    def _app(self, channels):
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app._cred_busy = False
        app._cred_requests = queue.Queue()
        app.channels = channels
        return app

    def test_stale_request_is_skipped(self):
        app = self._app([])
        done = threading.Event()
        done.set()                     # 等待者已超时放弃
        app._cred_requests.put(("ch", done, {}))
        app._credentials_dialog = mock.MagicMock(side_effect=AssertionError("不该弹框"))
        app._process_cred_requests()
        self.assertFalse(app._cred_busy)

    def test_request_for_removed_channel_is_answered_not_shown(self):
        app = self._app([])            # 频道已不在列表里
        done = threading.Event()
        box = {}
        app._cred_requests.put(("ch", done, box))
        app._credentials_dialog = mock.MagicMock(side_effect=AssertionError("不该弹框"))

        app._process_cred_requests()

        self.assertTrue(done.is_set())          # 立刻放行等待的接收线程
        self.assertIn("error", box["result"])

    def test_valid_request_shows_dialog(self):
        ch = mock.MagicMock()
        ch.name = "alice"
        app = self._app([ch])
        done = threading.Event()
        box = {}
        app._cred_requests.put((ch, done, box))
        app._credentials_dialog = mock.MagicMock(return_value=("login", "u", "p"))

        app._process_cred_requests()

        self.assertTrue(done.is_set())
        self.assertEqual(box["result"]["value"], ("login", "u", "p"))

    def test_queue_is_capped(self):
        owner = mock.MagicMock()
        owner.root = object()
        owner._mainloop_ready = threading.Event()
        owner._mainloop_ready.set()
        owner._cred_requests = queue.Queue()
        for _ in range(6):
            owner._cred_requests.put(("stale", _FakeEvent(), {}))

        ch = viewer.Channel.__new__(viewer.Channel)
        ch.name = "t"
        ch.owner = owner
        ch._prompt_blocked_until = 0.0
        ch.auth = {}
        with mock.patch.object(viewer.threading, "Event", _FakeEvent):
            ch._ask_credentials()

        items = list(owner._cred_requests.queue)
        self.assertEqual(len(items), 4)          # 上限 4，最旧的被丢弃
        self.assertIs(items[-1][0], ch)


if __name__ == "__main__":
    unittest.main()
