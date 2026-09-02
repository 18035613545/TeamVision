# -*- coding: utf-8 -*-
"""观看组模式（MultiView v1）单元测试：订阅路由 / 关键帧门控 / roster / 模块抽取。"""
import json
import select
import socket
import threading
import time
import unittest

import numpy as np

from common import MSG_CTRL, MSG_FRAME, parse_message

import screen
import host
import share as share_mod


def recv_frames(sock, timeout):
    """收一段时间内到达的消息，返回 [(kind, payload)]（收集满整个窗口）。"""
    out = []
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([sock], [], [], 0.2)
        if not r:
            continue
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
        while True:
            consumed, kind, payload = parse_message(buf)
            if consumed == 0:
                break
            buf = buf[consumed:]
            out.append((kind, payload))
    return out


class TestScreenExtraction(unittest.TestCase):
    """screen.py 抽取后 host.py 必须保持同名 re-export（既有测试 import host.xxx 依赖）。"""

    def test_host_reexports_screen_symbols(self):
        for name in ("CaptureManager", "STILL_STEP", "downsample_frame",
                     "motion_changed", "still_gate_update", "calc_output_size",
                     "encode_bgr"):
            self.assertIs(getattr(host, name), getattr(screen, name),
                          "host.%s 未 re-export screen.%s" % (name, name))

    def test_still_gate_semantics_unchanged(self):
        # 平移后行为回归抽查：连续无变化满 quiet_ms 判定静止，变化立即复位
        s, q = screen.still_gate_update(False, None, False, 100.0, 50.0)
        self.assertFalse(s)
        self.assertEqual(q, 100.0)
        s, q = screen.still_gate_update(s, q, False, 200.0, 50.0)
        self.assertTrue(s)
        s, q = screen.still_gate_update(s, q, True, 250.0, 50.0)
        self.assertFalse(s)
        self.assertIsNone(q)


class TestClientInfoMultiView(unittest.TestCase):

    def _make_info(self):
        import host
        return host.ClientInfo(("127.0.0.1", 12345))

    def test_defaults(self):
        info = self._make_info()
        self.assertEqual(info.watch_source, "local")
        self.assertTrue(info.need_key)
        self.assertIsNone(info.share_id)
        self.assertTrue(hasattr(info, "_write_lock"))

    def test_send_ctrl_framing(self):
        a, b = socket.socketpair()
        try:
            info = self._make_info()
            info.send_ctrl(a, {"action": "cap", "multiview": True})
            raw = b.recv(65536)
            consumed, kind, payload = parse_message(raw)
            self.assertEqual(kind, MSG_CTRL)
            import json
            self.assertEqual(json.loads(payload.decode("utf-8")), {"action": "cap", "multiview": True})
        finally:
            a.close(); b.close()


class _StubSender(object):
    """_sender_active=True 的假订阅者：captured 收集 enqueue_frame 的消息。"""

    def __init__(self, watch_source="local", need_key=True):
        self.watch_source = watch_source
        self.need_key = need_key
        self._sender_active = True
        self.captured = []

    def enqueue_frame(self, frame):
        self.captured.append(frame)


class TestRouteFrame(unittest.TestCase):

    def _route(self, clients, source, msg=b"MSG", is_key=False, is_jpeg=False):
        import host
        runtime = host.HostRuntime.__new__(host.HostRuntime)  # 不触发 __init__
        runtime.clients = {}
        runtime.clients_lock = __import__("threading").Lock()
        for k, v in clients.items():
            runtime.clients[k] = v
        host.route_frame(runtime, source, msg, is_key=is_key, is_jpeg=is_jpeg)
        return runtime

    def test_only_matching_subscribers(self):
        loc = _StubSender(watch_source="local")
        peer = _StubSender(watch_source="peer:1")
        other = _StubSender(watch_source="peer:2")
        self._route({"a": loc, "b": peer, "c": other}, "peer:1", b"X", is_key=True)
        self.assertEqual(len(peer.captured), 1)
        self.assertEqual(len(loc.captured), 0)
        self.assertEqual(len(other.captured), 0)

    def test_key_gating(self):
        peer = _StubSender(watch_source="peer:1", need_key=True)
        clients = {"a": peer}
        # 非关键帧被门控丢弃
        self._route(clients, "peer:1", b"P1", is_key=False)
        self.assertEqual(peer.captured, [])
        self.assertTrue(peer.need_key)
        # 关键帧放行并清除 need_key
        self._route(clients, "peer:1", b"K", is_key=True)
        self.assertEqual(peer.captured, [b"K"])
        self.assertFalse(peer.need_key)
        # 之后非关键帧放行
        self._route(clients, "peer:1", b"P2", is_key=False)
        self.assertEqual(peer.captured, [b"K", b"P2"])

    def test_jpeg_bypasses_gating(self):
        peer = _StubSender(watch_source="peer:1", need_key=True)
        self._route({"a": peer}, "peer:1", b"J", is_jpeg=True)
        self.assertEqual(peer.captured, [b"J"])
        self.assertTrue(peer.need_key)  # JPEG 自包含，不参与关键帧状态

    def test_watch_switch_resets_need_key(self):
        # 模拟 watch 处理后的状态（handle_client 置位）
        loc = _StubSender(watch_source="local", need_key=True)
        self._route({"a": loc}, "local", b"K", is_key=True)
        self.assertFalse(loc.need_key)
        loc.watch_source = "peer:1"
        loc.need_key = True  # 切换源重新门控
        self._route({"a": loc}, "peer:1", b"P", is_key=False)
        self.assertEqual(loc.captured, [b"K"])


def _read_ctrl(sock, timeout=1.0):
    """从 socket 读一条 MSG_CTRL 消息并解析为 dict（仅用于已连接的 socketpair）。"""
    import select
    import common
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([sock], [], [], 0.2)
        if not r:
            continue
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
        while True:
            consumed, kind, payload = common.parse_message(buf)
            if consumed == 0:
                break
            buf = buf[consumed:]
            if kind == common.MSG_CTRL:
                return json.loads(payload.decode("utf-8"))
    return None


class TestRoster(unittest.TestCase):

    def _runtime(self):
        import threading
        import host
        rt = host.HostRuntime.__new__(host.HostRuntime)
        rt.clients = {}
        rt.clients_lock = threading.Lock()
        rt._next_share_id = 0
        rt._register_sharer = host.HostRuntime._register_sharer.__get__(rt)
        rt._unregister_sharer = host.HostRuntime._unregister_sharer.__get__(rt)
        rt.broadcast_roster = host.HostRuntime.broadcast_roster.__get__(rt)
        return rt

    def _client(self, rt, name="alice"):
        import host
        a, b = socket.socketpair()
        info = host.ClientInfo(("10.0.0.1", 5701))
        info.username = name
        rt.clients[a] = info
        return a, b, info

    def test_register_broadcasts_roster_and_disconnect_clears(self):
        rt = self._runtime()
        a, ra, ia = self._client(rt, "alice")   # 旁观者 A
        b, rb, ib = self._client(rt, "bob")     # 共享者 B
        try:
            sid = rt._register_sharer(b, ib)
            self.assertEqual(sid, "peer:1")
            self.assertEqual(ib.share_id, "peer:1")
            msg_a = _read_ctrl(ra)
            msg_b = _read_ctrl(rb)
            self.assertEqual(msg_a["action"], "peers")
            self.assertEqual(msg_a["peers"], [{"id": "peer:1", "name": "bob", "addr": "10.0.0.1:5701"}])
            self.assertEqual(msg_b["action"], "peers")
            # 注销后 roster 清空
            rt._unregister_sharer(ib)
            self.assertIsNone(ib.share_id)
            msg_a2 = _read_ctrl(ra)
            self.assertEqual(msg_a2["peers"], [])
        finally:
            a.close(); b.close()

    def test_register_id_unique_and_not_reused(self):
        rt = self._runtime()
        a, ra, ia = self._client(rt)
        b, rb, ib = self._client(rt)
        try:
            self.assertEqual(rt._register_sharer(a, ia), "peer:1")
            rt._unregister_sharer(ia)
            self.assertEqual(rt._register_sharer(b, ib), "peer:2")  # 不复用 1
            _read_ctrl(rb)  # 排空 b 侧 roster
        finally:
            a.close(); b.close()


class _FakeCapture(object):
    """可编程伪采集器：按帧序返回静止或运动帧。"""

    def __init__(self):
        self._h = 0
        self.closed = False

    def feed(self, frames):
        self._frames = list(frames)

    def grab(self):
        if not self._frames:
            return None
        self._h += 1
        return self._frames.pop(0)

    def close(self):
        self.closed = True


def _motion_frames(n=12, w=160, h=90):
    out = []
    for i in range(n):
        img = np.full((h, w, 3), (30 + i * 5) % 200, dtype=np.uint8)
        img[::4, :, :] = (i * 7) % 255  # 大面积抽稀可见差异，保证 motion_changed 判变
        out.append(img)
    return out


def _static_frame(w=160, h=90):
    img = np.full((h, w, 3), 100, dtype=np.uint8)
    img[::4, :, :] = 200
    return img


def _share_cfg(fps=10, probe_fps=10):
    return {"host": {
        "fps": fps,
        "jpeg_quality": 70,
        "capture": {"monitor": 1, "region": None, "backend": "mss"},
        "codec": {"encoder": "jpeg", "bitrate_kbps": 500, "keyint": 30,
                  "target_width": 0, "preset": ""},
        "perf": {"still": {"enabled": True, "probe_fps": probe_fps,
                           "still_frames": 2, "point_thr": 10,
                           "ratio_thr": 0.005}},
    }}


class TestScreenShareSession(unittest.TestCase):
    """JPEG 回退模式（无 PyAV 环境等价路径）：发送/静止停发/恢复/请求响应。"""

    def _session(self, sock_a, sock_b, frames, still=False):
        tx = threading.Lock()
        cap = _FakeCapture()
        if still:
            cap.feed([_static_frame()] * 30)
        else:
            cap.feed(frames)
        sess = share_mod.ScreenShareSession(sock_a, tx, _share_cfg(), "test", capture=cap)
        sess.start()
        return sess

    def _collect(self, sock, timeout):
        return recv_frames(sock, timeout)

    def test_sends_frames_while_moving_then_silent_then_resume(self):
        a, b = socket.socketpair()
        try:
            sess = self._session(a, b, _motion_frames(8))
            # 运动期：应收到 ≥2 帧
            msgs = self._collect(b, 2.0)
            self.assertGreaterEqual(len([1 for k, _ in msgs if k == MSG_FRAME]), 2)
            # 供完运动帧后变为静止（grab 返回 None 视为静止）：不再有新帧
            time.sleep(1.0)
            msgs2 = self._collect(b, 1.0)
            self.assertEqual([1 for k, _ in msgs2 if k == MSG_FRAME], [])
            sess.stop()
            self.assertFalse(sess.alive)
        finally:
            a.close(); b.close()

    def test_stop_closes_capture(self):
        a, b = socket.socketpair()
        try:
            cap = _FakeCapture()
            cap.feed(_motion_frames(2))
            sess = share_mod.ScreenShareSession(
                a, threading.Lock(), _share_cfg(), "test", capture=cap)
            sess.start()
            sess.stop()
            self.assertFalse(sess.alive)
            self.assertTrue(cap.closed)
        finally:
            a.close(); b.close()

    def test_request_keyframe_while_idle_responds_once(self):
        """JPEG 模式静止/空闲期收到 req_keyframe：重发缓存帧一次（自包含可起解）。"""
        a, b = socket.socketpair()
        try:
            sess = self._session(a, b, _motion_frames(4), still=True)
            self._collect(b, 2.0)  # 首帧（或几帧）已发，之后静止
            sess.request_keyframe()
            msgs = self._collect(b, 1.5)
            n = len([1 for k, _ in msgs if k == MSG_FRAME])
            self.assertGreaterEqual(n, 1)
            # 之后不再自发
            msgs2 = self._collect(b, 1.0)
            self.assertEqual([1 for k, _ in msgs2 if k == MSG_FRAME], [])
            sess.stop()
        finally:
            a.close(); b.close()
