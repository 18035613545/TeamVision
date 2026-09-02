# -*- coding: utf-8 -*-
"""观看组 hub 集成测试：真实 TCP + HostRuntime/run_server，无需 GUI/真实上传。

- 用合成 JPEG / 合成 H.264 标记帧驱动共享成员行为（hub 只转发不做解码，
  因此 NAL 内容可为任意字节，门控只看 flags）
- host 本地采集流水线真实运行：perf.still 关闭 + fps=2 压低干扰流量
"""
import copy
import json
import select
import socket
import threading
import time
import unittest

import numpy as np

from common import (DEFAULT_CONFIG, client_handshake, send_msg, recv_msg,
                    pack_frame, pack_video, parse_message, parse_video,
                    MSG_FRAME, MSG_VIDEO, MSG_CTRL, CODEC_H264)
from host import HostRuntime

JPEG_BYTES = None  # 惰性生成：64x36 纯色 JPEG


def _jpeg():
    global JPEG_BYTES
    if JPEG_BYTES is None:
        import cv2
        img = np.full((36, 64, 3), 120, dtype=np.uint8)
        ok, buf = cv2.imencode(".jpg", img)
        JPEG_BYTES = buf.tobytes()
    return JPEG_BYTES


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Hub:
    """以线程方式启动真实 hub（HostRuntime.run_server）。"""

    def __init__(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["host"]["port"] = _free_port()
        cfg["host"]["fps"] = 2
        cfg["host"]["perf"]["still"]["enabled"] = False  # 本地流水线不静止，保底帧源
        cfg["host"]["codec"]["encoder"] = "jpeg"         # 本地编码仅 JPEG（测试无解码）
        cfg["host"]["auth"]["enabled"] = False
        self.runtime = HostRuntime(cfg)
        self.runtime.start_server()
        deadline = time.time() + 5
        while time.time() < deadline and self.runtime._server is None:
            time.sleep(0.05)
        self.port = self.runtime._server.getsockname()[1]

    def close(self):
        self.runtime.shutdown()

    def reset_ids(self):
        """每个用例独立编号空间：等待上一个用例成员的注销广播落定后复位序号，
        使各用例内部的共享成员从 peer:1 起编号（用例间成员互不残留）。"""
        time.sleep(0.05)
        self.runtime._next_share_id = 0


def join_hub(port, name=""):
    """连接 hub 并完成握手 + 可选 auth（准入关闭），返回已入册 socket。"""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    client_handshake(s)
    send_msg(s, {"action": "probe", "user": name, "pass": ""})
    reply = recv_msg(s)
    assert reply.get("ok"), reply
    return s


def recv_kinds(sock, timeout=3.0):
    """收集一段时间内到达的消息，返回 [(kind, payload)]（阻塞至首个消息或超时）。"""
    out = []
    buf = b""
    end = time.time() + timeout
    deadline_hit = False
    while not deadline_hit:
        remaining = end - time.time()
        if remaining <= 0:
            deadline_hit = True
            break
        r, _, _ = select.select([sock], [], [], min(remaining, 0.2))
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


def ctrls(messages):
    return [json.loads(p) for k, p in messages if k == MSG_CTRL]


def frames(messages, kind=MSG_VIDEO):
    return [(k, p) for k, p in messages if k == kind]


class TestHubMultiView(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.hub = Hub()

    @classmethod
    def tearDownClass(cls):
        cls.hub.close()

    def setUp(self):
        self.hub.reset_ids()

    def test_old_style_client_still_receives_local(self):
        """只发 ping 的旧式客户端：默认订阅 local，持续收到本地帧。"""
        s = join_hub(self.hub.port)
        try:
            msgs = recv_kinds(s, timeout=8.0)
            self.assertTrue(any(k == MSG_FRAME for k, _ in msgs),
                            "未收到 local JPEG 帧: %s" % [k for k, _ in msgs])
        finally:
            s.close()

    def test_member_first_frame_registers_and_broadcasts_roster(self):
        a = join_hub(self.hub.port, name="alice")
        b = join_hub(self.hub.port, name="bob")
        try:
            b.sendall(pack_video(b"FAKE-KEY", keyframe=True, codec=CODEC_H264))
            msgs = recv_kinds(a, timeout=3.0)
            peer_msgs = [m for m in ctrls(msgs) if m.get("action") == "peers"]
            self.assertTrue(peer_msgs)
            # roster addr 以服务端视角（成员本地端点 host:port）；服务端 accept 的
            # IPv4-mapped 前缀 ::ffff: 由 broadcast_roster 剥除（见 Task 6 实现）
            self.assertEqual(peer_msgs[-1]["peers"],
                             [{"id": "peer:1", "name": "bob",
                               "addr": "%s:%d" % (b.getsockname()[0],
                                                  b.getsockname()[1])}])
        finally:
            a.close(); b.close()

    def test_watch_relay_with_key_gating(self):
        a = join_hub(self.hub.port, name="alice")
        b = join_hub(self.hub.port, name="bob")
        try:
            b.sendall(pack_video(b"KEY", keyframe=True, codec=CODEC_H264))
            recv_kinds(a, timeout=2.0)  # 排空 roster
            send_msg(a, {"action": "watch", "source": "peer:1"})
            # B 先发 3 个非关键帧：门控应全部丢弃
            for i in range(3):
                b.sendall(pack_video(b"NONKEY%d" % i, codec=CODEC_H264))
            msgs = recv_kinds(a, timeout=1.5)
            self.assertEqual(frames(msgs, MSG_VIDEO), [], "非关键帧不应被转发")
            # 关键帧放行（且是 A 收到的第一个视频帧）
            b.sendall(pack_video(b"KEY2", keyframe=True, codec=CODEC_H264))
            msgs = recv_kinds(a, timeout=3.0)
            vids = frames(msgs, MSG_VIDEO)
            self.assertTrue(vids)
            ts, codec, flags, nal = parse_video(vids[0][1])
            self.assertEqual(nal, b"KEY2")
            self.assertTrue(flags & 1)
            # need_key 清除后非关键帧放行
            b.sendall(pack_video(b"P1", codec=CODEC_H264))
            msgs = recv_kinds(a, timeout=3.0)
            vids = frames(msgs, MSG_VIDEO)
            self.assertTrue(vids)
            _, _, _, nal = parse_video(vids[0][1])
            self.assertEqual(nal, b"P1")
        finally:
            a.close(); b.close()

    def test_watch_peer_triggers_immediate_key_request(self):
        a = join_hub(self.hub.port, name="alice")
        b = join_hub(self.hub.port, name="bob")
        try:
            b.sendall(pack_video(b"KEY", keyframe=True, codec=CODEC_H264))
            recv_kinds(a, timeout=2.0)
            recv_kinds(b, timeout=1.0)  # 排空 b（roster/local 帧）
            send_msg(a, {"action": "watch", "source": "peer:1"})
            msgs = recv_kinds(b, timeout=2.0)
            reqs = [m for m in ctrls(msgs) if m.get("action") == "req_keyframe"]
            self.assertTrue(reqs, "host 应向成员转达 req_keyframe")
        finally:
            a.close(); b.close()

    def test_jpeg_relay_no_gating(self):
        a = join_hub(self.hub.port, name="alice")
        b = join_hub(self.hub.port, name="bob")
        try:
            b.sendall(pack_video(b"KEY", keyframe=True, codec=CODEC_H264))
            recv_kinds(a, timeout=2.0)
            send_msg(a, {"action": "watch", "source": "peer:1"})
            # need_key 尚未满足时 JPEG 直达
            b.sendall(pack_frame(_jpeg()))
            msgs = recv_kinds(a, timeout=3.0)
            jpgs = frames(msgs, MSG_FRAME)
            self.assertTrue(jpgs, "JPEG 应绕过关键帧门控直接转发")
        finally:
            a.close(); b.close()

    def test_member_disconnect_updates_roster(self):
        a = join_hub(self.hub.port, name="alice")
        b = join_hub(self.hub.port, name="bob")
        try:
            b.sendall(pack_video(b"KEY", keyframe=True, codec=CODEC_H264))
            recv_kinds(a, timeout=2.0)
            b.close()
            msgs = recv_kinds(a, timeout=3.0)
            peer_msgs = [m for m in ctrls(msgs) if m.get("action") == "peers"]
            self.assertTrue(peer_msgs)
            self.assertEqual(peer_msgs[-1]["peers"], [])
        finally:
            a.close()

    def test_member_unshare_drops_from_roster(self):
        """成员主动停止共享（连接保留）：roster 移除该成员（观看端据此回落 local）。"""
        a = join_hub(self.hub.port, name="alice")
        b = join_hub(self.hub.port, name="bob")
        try:
            b.sendall(pack_video(b"KEY", keyframe=True, codec=CODEC_H264))
            recv_kinds(a, timeout=2.0)
            recv_kinds(b, timeout=0.5)
            send_msg(b, {"action": "unshare"})
            msgs = recv_kinds(a, timeout=3.0)
            peer_msgs = [m for m in ctrls(msgs) if m.get("action") == "peers"]
            self.assertTrue(peer_msgs)
            self.assertEqual(peer_msgs[-1]["peers"], [])
        finally:
            a.close(); b.close()

    def test_new_joiner_gets_current_roster(self):
        b = join_hub(self.hub.port, name="bob")
        try:
            b.sendall(pack_video(b"KEY", keyframe=True, codec=CODEC_H264))
            time.sleep(0.3)  # 等待 roster 广播完成
            c = join_hub(self.hub.port, name="carol")
            try:
                msgs = recv_kinds(c, timeout=3.0)
                peer_msgs = [m for m in ctrls(msgs) if m.get("action") == "peers"]
                self.assertTrue(peer_msgs)
                self.assertEqual(peer_msgs[-1]["peers"][0]["id"], "peer:1")
            finally:
                c.close()
        finally:
            b.close()

    def test_watch_invalid_source_keeps_local(self):
        a = join_hub(self.hub.port, name="alice")
        try:
            send_msg(a, {"action": "watch", "source": "peer:99"})
            msgs = recv_kinds(a, timeout=4.0)
            self.assertTrue(any(k == MSG_FRAME for k, _ in msgs),
                            "watch 无效源后仍应接收 local 帧")
        finally:
            a.close()

    def test_req_keyframe_routes_to_watched_peer(self):
        a = join_hub(self.hub.port, name="alice")
        b = join_hub(self.hub.port, name="bob")
        try:
            b.sendall(pack_video(b"KEY", keyframe=True, codec=CODEC_H264))
            recv_kinds(a, timeout=2.0)
            recv_kinds(b, timeout=1.0)
            send_msg(a, {"action": "watch", "source": "peer:1"})
            recv_kinds(b, timeout=1.0)  # 排空 watch 触发的转达
            # A 看 local 时 req_keyframe 不应到达 B
            send_msg(a, {"action": "watch", "source": "local"})
            time.sleep(0.3)
            send_msg(a, {"action": "req_keyframe"})
            msgs = recv_kinds(b, timeout=1.0)
            self.assertFalse([m for m in ctrls(msgs) if m.get("action") == "req_keyframe"],
                             "看 local 时关键帧请求不应转发给成员")
        finally:
            a.close(); b.close()
