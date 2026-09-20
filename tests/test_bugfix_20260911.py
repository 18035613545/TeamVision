# -*- coding: utf-8 -*-
"""BUG审查报告-2026-09-11 修复回归测试（A0/A1/A2/A3 + B1–B10 + C 组）。

覆盖报告里可确定性复现的条目：

- A0  回环双栈监听：`127.0.0.1`/`localhost`/`::1` 同时监听 IPv4 与 IPv6 回环，
      否则樱花 frpc 以 `localhost` 回连（Windows 优先 `::1`）时隧道永远连不上本机。
- A1  上行令牌桶补充速率不得是名义值的 2 倍（补满 12500 字节后第 12501 字节必须被拒）。
- A2  采集后端启动即失败（cap is None）时 `set_params` 必须走重建而不是抛 AttributeError，
      否则 `host._capture_loop` 永远到不了 `grab()` 的自愈分支。
- A3  `_ask_credentials` 淘汰最旧请求时必须唤醒其等待者；等待必须可被 stop 打断。
- B1  `_str_field` 统一口径 + `_ctrl_watch` 拒绝非字符串 source。
- B2  `host.perf` 非对象时 `_perf_tick` 不得抛异常（否则每秒写一条堆栈且 ABR 永久失效）。
- B3  `FrpManager.stop()` 失败时不得谎报成功、不得提前摘句柄。
- B4  `viewer.hotkeys` 非对象不再让观看端起不来；热键线程异常也要注销。
- B5  `share.ScreenShareSession` 用安全转换读配置 + region 归一。
- B6  解码器内部复位后 `needs_keyframe` 置位，观看端据此作废 `_got_key` 门控。
- B7  `screen.ensure_vendor_on_path()` 把 vendor 接入 sys.path（源码运行也能用 dxgi）。
- B8  共享会话自行终止 → `exit_reason` + `Channel.check_share_alive` 关开关并发 unshare。
- B9  `route_frame` 返回实际投递数；无订阅者时 ABR 不回升、不强制 IDR。
- B10 认证窗口下界 ≥240 秒（覆盖观看端最坏 ≈180 秒等待）+ 迁移时一并抬高。
- C3  `logger._prune_old` 只按真正的 8 位日期分组（app_debug.log 不得挤掉真实日志）。
- C5  `_peer_key_at` 在成员注销时清理。
- C7  监听 backlog 按 max_clients 量级取（不再是固定 5）。
- C12 已是安全默认但缺版本号的配置也会静默补写 config_version。
- C16 `dxcam.create` 只有"未知关键字参数 output_color"才回退 RGB。
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import common
import host
import logger as logger_mod
import pipeline
import screen as screen_mod
import share as share_mod
import viewer
import codec as codec_mod


# ------------------------------------------------------------------ 公共替身

class FakeSock:
    """内存 socket 替身：只实现 settimeout/gettimeout/sendall/close/recv。"""

    def __init__(self):
        self._timeout = None
        self.sent = []
        self.closed = False

    def settimeout(self, t):
        self._timeout = t

    def gettimeout(self):
        return self._timeout

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True

    def fileno(self):
        return 0


class FakeCap:
    """伪采集器：只提供 CaptureSource 需要的接口。

    注意 `working` 在真实 CaptureManager 上是 **property**（`getattr(cap, "working", True)`），
    因此这里用布尔属性而不是方法——方法对象恒为真值。
    """

    def __init__(self, working=True, frames=None):
        self.working = working
        self._frames = list(frames or [])
        self.closed = False
        self.set_params_calls = []

    def grab(self):
        return self._frames.pop(0) if self._frames else None

    def set_params(self, backend, monitor, region):
        self.set_params_calls.append((backend, monitor, region))

    def close(self):
        self.closed = True


def _free_port():
    """取一个当前空闲的 TCP 端口（用于绑定测试）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _ipv6_loopback_ok():
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.bind(("::1", 0))
        s.close()
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ A0 回环双栈

class TestA0LoopbackDualStack(unittest.TestCase):
    def test_is_loopback_alias(self):
        for value in ("127.0.0.1", "localhost", "LOCALHOST", "::1", " 127.0.0.1 "):
            self.assertTrue(host._is_loopback_alias(value), value)
        for value in ("0.0.0.0", "::", "", "192.168.1.5", None, 127):
            self.assertFalse(host._is_loopback_alias(value), repr(value))

    def test_listen_target_keeps_loopback_as_specific_address(self):
        cfg = {"host": {"listen_host": "127.0.0.1", "port": 5700}}
        listen_host, port, bind_all = host._listen_target(cfg)
        self.assertEqual(listen_host, "127.0.0.1")
        self.assertEqual(port, 5700)
        self.assertFalse(bind_all, "回环地址仍按‘指定地址’处理，只有绑定阶段走双栈")

    def test_has_frp_tunnel(self):
        self.assertTrue(host._has_frp_tunnel(
            {"host": {"frp": {"token": "abc", "tunnel_ids": ""}}}))
        self.assertTrue(host._has_frp_tunnel(
            {"host": {"frp": {"tunnel_ids": "1,2"}}}))
        self.assertTrue(host._has_frp_tunnel(
            {"host": {"frp": {"tunnel_ids": ["1", "2"]}}}))
        self.assertFalse(host._has_frp_tunnel({"host": {"frp": {"token": "", "tunnel_ids": ""}}}))
        self.assertFalse(host._has_frp_tunnel({"host": {"frp": 1}}))

    @unittest.skipUnless(_ipv6_loopback_ok(), "本机不支持 IPv6 回环")
    def test_bind_listener_listens_on_both_loopback_families(self):
        port = _free_port()
        rt = host.HostRuntime({"host": {"listen_host": "127.0.0.1", "port": port}})
        servers = host.bind_listener(rt)
        self.addCleanup(lambda: [s.close() for s in servers])
        self.assertEqual(len(servers), 2, "回环地址必须同时监听 127.0.0.1 与 [::1]")
        self.assertTrue(rt.listen_dual_loopback)
        self.assertEqual(rt._server, servers[0])
        # 两个地址族都必须真的可连（frpc 走 localhost → ::1 时才能连上）
        for family, addr in ((socket.AF_INET, ("127.0.0.1", port)),
                             (socket.AF_INET6, ("::1", port))):
            c = socket.socket(family, socket.SOCK_STREAM)
            c.settimeout(2.0)
            try:
                c.connect(addr)
            finally:
                c.close()

    @unittest.skipUnless(_ipv6_loopback_ok(), "本机不支持 IPv6 回环")
    def test_accept_loop_serves_both_listeners(self):
        port = _free_port()
        cfg = {"host": {"listen_host": "localhost", "port": port}}
        rt = host.HostRuntime(cfg)
        servers = host.bind_listener(rt)
        self.addCleanup(lambda: [s.close() for s in servers])
        got = []
        done = threading.Event()

        def spawn(sock, addr, runtime):
            got.append(addr)
            try:
                sock.close()
            except OSError:
                pass
            if len(got) >= 2:
                runtime.stop_event.set()
                done.set()

        t = threading.Thread(target=host._accept_loop, args=(servers, rt, spawn), daemon=True)
        t.start()
        for family, addr in ((socket.AF_INET, ("127.0.0.1", port)),
                             (socket.AF_INET6, ("::1", port))):
            c = socket.socket(family, socket.SOCK_STREAM)
            c.settimeout(2.0)
            try:
                c.connect(addr)
            finally:
                pass
            c.close()
        self.assertTrue(done.wait(5.0), "双栈监听未同时接受两个地址族的连接（got=%r）" % got)
        t.join(timeout=5.0)
        self.assertEqual(len(got), 2)

    @unittest.skipUnless(_ipv6_loopback_ok(), "本机不支持 IPv6 回环")
    def test_frpc_style_localhost_reconnect_reaches_listener(self):
        """A0 原始故障的复刻：以 `localhost` 回连（Windows 优先 `::1`）必须能打进共享端。"""
        port = _free_port()
        rt = host.HostRuntime({"host": {"listen_host": "127.0.0.1", "port": port}})
        servers = host.bind_listener(rt)
        self.addCleanup(lambda: [s.close() for s in servers])
        got = []
        done = threading.Event()

        def spawn(sock, addr, runtime):
            got.append(addr)
            try:
                sock.close()
            except OSError:
                pass
            runtime.stop_event.set()
            done.set()

        t = threading.Thread(target=host._accept_loop, args=(servers, rt, spawn), daemon=True)
        t.start()
        c = socket.create_connection(("localhost", port), timeout=3)
        c.close()
        self.assertTrue(done.wait(5.0), "以 localhost 回连未能打进共享端（A0 未修复）")
        t.join(timeout=5.0)
        # 若本机 localhost 优先解析为 ::1，则必须看到 IPv6 来源——正是 frpc 的实际形态
        first = socket.getaddrinfo("localhost", port, type=socket.SOCK_STREAM)[0]
        if first[0] == socket.AF_INET6:
            self.assertIn(":", got[0][0], "localhost 解析到 ::1，却收到 IPv4 来源：%r" % (got,))

    def test_multi_loop_tolerates_closed_listener(self):
        """多监听 socket 中有一个关闭时不拖垮另一条地址族。"""
        a, b = socket.socketpair() if hasattr(socket, "socketpair") else (None, None)
        if a is None:
            self.skipTest("无 socketpair")
        a.close()
        b.close()
        rt = host.HostRuntime({"host": {}})
        accept = mock.MagicMock()
        srv = mock.MagicMock()
        srv.fileno.return_value = -1
        srv.accept = accept

        class _Live:
            def fileno(self):
                return -1

            def accept(self):
                return ("s", ("1.2.3.4", 1))

        # 两个 socket 都显示已关闭 → 循环应干净退出而不是死循环
        host._accept_loop([_Live(), _Live()], rt, spawn=lambda *a: rt.stop_event.set())
        rt.stop_event.set()
        self.assertFalse(accept.called)


# ------------------------------------------------------------------ A1 令牌桶

class TestA1UpstreamTokenBucket(unittest.TestCase):
    def test_refill_rate_is_exactly_nominal(self):
        """100 Kbps = 12500 B/s：放干后 1 秒只能补 12500 字节，第 12501 字节必须被拒。"""
        info = host.ClientInfo(("1.2.3.4", 1))
        # 首帧填满 2 秒额度 = 25000 字节
        self.assertTrue(info.allow_upstream(25000, 100, 30, now=100.0))
        self.assertFalse(info.allow_upstream(1, 100, 30, now=100.0))
        # 1 秒后额度恰好 12500：全部可用
        self.assertTrue(info.allow_upstream(12500, 100, 30, now=101.0))
        # 再要 1 字节即超限（若补充速率是名义值的 2 倍，这里会被错误放行）
        self.assertFalse(info.allow_upstream(1, 100, 30, now=101.0))

    def test_long_run_throughput_matches_nominal(self):
        """持续上行 10 秒：放行总量不得超过「首桶 2 秒 + 10 秒补充」名义额度。"""
        info = host.ClientInfo(("1.2.3.4", 1))
        frame = 8000
        allowed = 0
        for i in range(300):          # 30fps × 10s，每帧 8KB
            now = 100.0 + i / 30.0
            if info.allow_upstream(frame, 600, 60, now=now):
                allowed += 1
        nominal_bytes = 600 * 125 * (2 + 10)
        self.assertLessEqual(allowed * frame, nominal_bytes + frame,
                             "长期放行量超过名义上限（补充速率仍偏大？）")

    def test_first_frame_still_never_dropped(self):
        info = host.ClientInfo(("1.2.3.4", 1))
        self.assertTrue(info.allow_upstream(500000, 2500, 30, now=1.0))


# ------------------------------------------------------------------ A2 采集自愈

class TestA2CaptureSetParams(unittest.TestCase):
    def test_missing_handle_triggers_rebuild(self):
        with mock.patch.object(pipeline, "CaptureManager", side_effect=RuntimeError("都挂了")):
            src = pipeline.CaptureSource("dxgi", 1, None)
        self.assertIsNone(src.cap)
        self.assertFalse(src.working())
        rebuilt = FakeCap(working=True)
        with mock.patch.object(pipeline, "CaptureManager", return_value=rebuilt), \
                mock.patch.object(pipeline.time, "sleep", lambda _s: None):
            ok = src.set_params("mss", 1, None)
        self.assertTrue(ok, "句柄缺失时 set_params 必须重建并返回重建结果")
        self.assertIs(src.cap, rebuilt)
        self.assertTrue(src.working())

    def test_missing_handle_rebuild_failure_returns_false(self):
        with mock.patch.object(pipeline, "CaptureManager", side_effect=RuntimeError("都挂了")):
            src = pipeline.CaptureSource("dxgi", 1, None)
        with mock.patch.object(pipeline, "CaptureManager", side_effect=RuntimeError("仍失败")), \
                mock.patch.object(pipeline.time, "sleep", lambda _s: None):
            self.assertFalse(src.set_params("dxgi", 1, None))

    def test_missing_handle_warning_is_throttled(self):
        with mock.patch.object(pipeline, "CaptureManager", side_effect=RuntimeError("都挂了")):
            src = pipeline.CaptureSource("dxgi", 1, None)
        with mock.patch.object(pipeline, "CaptureManager", side_effect=RuntimeError("仍失败")), \
                mock.patch.object(pipeline.time, "sleep", lambda _s: None), \
                mock.patch.object(pipeline.log, "warning") as warn:
            for _ in range(5):
                src.set_params("dxgi", 1, None)
        self.assertLessEqual(warn.call_count, 1, "句柄缺失告警必须去重（否则 2 条/秒刷满日志）")


# ------------------------------------------------------------------ A3 凭据等待

class _CredOwner:
    def __init__(self):
        self.root = object()
        self._mainloop_ready = threading.Event()
        self._mainloop_ready.set()
        self._cred_requests = __import__("queue").Queue()


def _bare_channel(owner):
    ch = viewer.Channel.__new__(viewer.Channel)
    ch.name = "t"
    ch.owner = owner
    ch._prompt_blocked_until = 0.0
    ch.auth = {}
    ch._running = True
    ch._stop_evt = threading.Event()
    return ch


class TestA3CredentialWait(unittest.TestCase):
    def test_eviction_wakes_evicted_waiter(self):
        owner = _CredOwner()
        stale = []
        for i in range(4):
            done = threading.Event()
            box = {}
            owner._cred_requests.put((object(), done, box))
            stale.append((done, box))
        ch = _bare_channel(owner)
        t = threading.Thread(target=ch._ask_credentials, daemon=True)
        t.start()
        deadline = time.time() + 3.0
        while time.time() < deadline and not stale[0][0].is_set():
            time.sleep(0.02)
        self.assertTrue(stale[0][0].is_set(), "被淘汰的最旧请求必须被唤醒（A3）")
        self.assertIn("error", stale[0][1]["result"])
        self.assertLessEqual(owner._cred_requests.qsize(), 4)
        ch._stop_evt.set()          # 打断本请求的等待
        t.join(timeout=3.0)
        self.assertFalse(t.is_alive())

    def test_wait_is_interruptible_by_stop(self):
        owner = _CredOwner()
        ch = _bare_channel(owner)
        result = []
        t = threading.Thread(target=lambda: result.append(ch._ask_credentials()), daemon=True)
        t.start()
        time.sleep(0.3)
        ch._stop_evt.set()          # 等价于 Channel.stop()
        t.join(timeout=3.0)
        self.assertFalse(t.is_alive(), "stop() 必须能打断凭据等待（原来要空等 120 秒）")
        self.assertEqual(result, [None])

    def test_running_false_also_interrupts(self):
        owner = _CredOwner()
        ch = _bare_channel(owner)
        result = []
        t = threading.Thread(target=lambda: result.append(ch._ask_credentials()), daemon=True)
        t.start()
        time.sleep(0.3)
        ch._running = False
        t.join(timeout=3.0)
        self.assertFalse(t.is_alive())

    def test_wake_pending_credentials_marks_channel_requests(self):
        owner = _CredOwner()
        ch = _bare_channel(owner)
        done = threading.Event()
        box = {}
        owner._cred_requests.put((ch, done, box))
        ch._wake_pending_credentials("频道已停止")
        self.assertTrue(done.is_set())
        self.assertIn("error", box["result"])


# ------------------------------------------------------------------ B1 字段类型

class TestB1FieldTypeValidation(unittest.TestCase):
    def test_str_field(self):
        self.assertEqual(host._str_field({"user": "a"}, "user"), ("a", True))
        self.assertEqual(host._str_field({}, "user"), ("", True))
        self.assertEqual(host._str_field({"user": None}, "user"), ("", True))
        value, ok = host._str_field({"user": 123}, "user")
        self.assertFalse(ok)
        self.assertEqual(value, "")
        self.assertFalse(host._str_field({"user": ["x"]}, "user")[1])

    def test_ctrl_watch_rejects_non_string_source(self):
        info = host.ClientInfo(("1.2.3.4", 1), "tester")
        sock = FakeSock()
        rt = host.HostRuntime({"host": {}})
        fatal = host._ctrl_watch(sock, info, rt, {"action": "watch", "source": ["x"]},
                                 info.addr)
        self.assertIsNone(fatal, "非字符串 source 不得让连接断开")
        self.assertEqual(len(sock.sent), 1)
        _consumed, kind, payload = common.parse_message(bytearray(b"".join(sock.sent)))
        self.assertEqual(kind, common.MSG_CTRL)
        msg = json.loads(payload.decode("utf-8"))
        self.assertEqual(msg["action"], "watch_reject")
        self.assertEqual(msg["reason"], "invalid")

    def test_optional_auth_phase_ignores_non_string_user(self):
        sock = FakeSock()
        sent = []
        result = {}

        def _fake_recv(s, *a, **kw):
            return {"action": "probe", "user": 123}

        with mock.patch.object(host, "recv_msg", _fake_recv), \
                mock.patch.object(host, "send_msg",
                                  lambda s, o: sent.append(o)), \
                mock.patch.object(host.select, "select",
                                  lambda *a, **kw: ([sock], [], [])):
            ok, user = host._auth_optional_phase(sock, ("1.2.3.4", 1))
        result["ok"] = ok
        self.assertTrue(ok, "准入关闭时非字符串 user 应被忽略而不是断开连接")
        self.assertIsNone(user)
        self.assertEqual(sent[0]["action"], "auth_result")
        self.assertTrue(sent[0]["ok"])

    def test_required_phase_rejects_non_string_user(self):
        sock = FakeSock()
        sent = []

        def _fake_recv(s, *a, **kw):
            return {"action": "login", "user": 123, "pass": "x"}

        with mock.patch.object(host, "recv_msg", _fake_recv), \
                mock.patch.object(host, "send_msg", lambda s, o: sent.append(o)):
            ok, user = host._auth_required_phase(
                sock, ("1.2.3.4", 1), host.HostRuntime({"host": {}}),
                {"auth_timeout": 240})
        self.assertFalse(ok)
        self.assertFalse(sent[0]["ok"])
        self.assertTrue(sock.closed)


# ------------------------------------------------------------------ B2 perf 类型

class TestB2PerfCfgType(unittest.TestCase):
    def _tick(self, perf_value):
        cfg = {"host": {"perf": perf_value}}
        rt = host.HostRuntime(cfg)
        with mock.patch.object(host, "_perf_log_and_bandwidth",
                               return_value=(0.0, False)):
            host._perf_tick(rt, pipeline.PipelineParams(cfg))   # 不得抛异常

    def test_non_dict_perf_does_not_raise(self):
        self._tick(1)
        self._tick(False)
        self._tick("adaptive")

    def test_adaptive_off_returns_without_evaluating(self):
        cfg = {"host": {"perf": {"adaptive": False}}}
        rt = host.HostRuntime(cfg)
        with mock.patch.object(host, "_perf_log_and_bandwidth",
                               return_value=(0.0, False)), \
                mock.patch.object(host, "_abr_recover") as rec, \
                mock.patch.object(host, "_abr_degrade") as deg:
            host._perf_tick(rt, pipeline.PipelineParams(cfg))
        self.assertFalse(rec.called or deg.called)


# ------------------------------------------------------------------ B3 frpc 停止

class _FakeProc:
    def __init__(self, terminate_works=False, kill_works=False, pid=4321):
        self.terminate_works = terminate_works
        self.kill_works = kill_works
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.terminate_works:
            self.returncode = 0
        else:
            raise OSError("terminate 失败")

    def kill(self):
        self.killed = True
        if self.kill_works:
            self.returncode = -9
        else:
            raise OSError("kill 失败")

    def wait(self, timeout=None):
        if self.returncode is None:
            raise host.subprocess.TimeoutExpired("frpc", timeout)
        return self.returncode


class TestB3FrpStop(unittest.TestCase):
    def _mgr(self, proc):
        mgr = host.FrpManager({"host": {}})
        mgr._proc = proc
        return mgr

    def test_successful_terminate_clears_handle(self):
        proc = _FakeProc(terminate_works=True)
        mgr = self._mgr(proc)
        self.assertEqual(mgr.stop(), "frpc 已终止")
        self.assertIsNone(mgr._proc)

    def test_kill_escalation_reports_success(self):
        proc = _FakeProc(terminate_works=False, kill_works=True)
        mgr = self._mgr(proc)
        self.assertEqual(mgr.stop(), "frpc 已终止")
        self.assertTrue(proc.killed, "terminate 失败后必须升级为 kill")
        self.assertIsNone(mgr._proc)

    def test_stuck_process_is_reported_and_handle_kept(self):
        proc = _FakeProc(terminate_works=False, kill_works=False, pid=999)
        mgr = self._mgr(proc)
        msg = mgr.stop()
        self.assertIn("未能停止 frpc", msg)
        self.assertIn("999", msg)
        self.assertIs(mgr._proc, proc, "停止失败时不得提前摘掉句柄（否则再也停不掉）")

    def test_already_dead_process_is_cleared(self):
        proc = _FakeProc()
        proc.returncode = 0
        mgr = self._mgr(proc)
        self.assertEqual(mgr.stop(), "frpc 已终止")
        self.assertIsNone(mgr._proc)


# ------------------------------------------------------------------ B4 hotkeys 类型

class TestB4HotkeysCfg(unittest.TestCase):
    def test_helper_normalizes_non_dict(self):
        self.assertEqual(viewer._viewer_hotkeys_cfg({"viewer": {"hotkeys": False}}), {})
        self.assertEqual(viewer._viewer_hotkeys_cfg({"viewer": {"hotkeys": 1}}), {})
        self.assertEqual(viewer._viewer_hotkeys_cfg({}), {})
        self.assertEqual(viewer._viewer_hotkeys_cfg({"viewer": {"hotkeys": {"direct": False}}}),
                         {"direct": False})

    def test_hotkey_loop_unregisters_even_with_bad_config(self):
        class _User32:
            def __init__(self):
                self.registered = []
                self.unregistered = []

            def RegisterHotKey(self, hwnd, hid, mods, vk):
                self.registered.append(hid)
                return 1

            def GetMessageW(self, msg, hwnd, lo, hi):
                return 0

            def UnregisterHotKey(self, hwnd, hid):
                self.unregistered.append(hid)
                return 1

        u32 = _User32()

        class _Windll:
            user32 = u32

        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app.cfg = {"viewer": {"hotkeys": False}}      # 非对象：正是 B4 的触发条件
        app.running = False
        app._hotkey_tid = None
        app._hotkey_registered = set()
        app._hotkey_failed = []
        app._post_ui = lambda fn: None
        with mock.patch.object(viewer.ctypes, "windll", _Windll()):
            app._hotkey_loop()                        # 不得抛异常
        self.assertTrue(u32.registered)
        self.assertEqual(sorted(u32.unregistered), sorted(u32.registered),
                         "热键线程退出（含异常路径）必须注销全部已注册热键")
        self.assertEqual(app._hotkey_registered, set())


# ------------------------------------------------------------------ B5 share 配置

class TestB5ShareCfgSafety(unittest.TestCase):
    def _session(self, cfg):
        return share_mod.ScreenShareSession(
            FakeSock(), threading.Lock(), cfg, "t", capture=FakeCap())

    def test_string_numbers_are_accepted(self):
        cfg = {"host": {
            "fps": "30", "jpeg_quality": "70",
            "capture": {"monitor": "2", "backend": "mss",
                        "region": {"left": "0", "top": "0", "width": "640", "height": "480"}},
            "codec": {"bitrate_kbps": "1800", "target_width": "854", "keyint": "30"},
            "perf": {"still": {"probe_fps": "5", "still_frames": "3", "ratio_thr": "0.01"}},
        }}
        sess = self._session(cfg)
        self.assertEqual(sess._monitor, 2)
        self.assertEqual(sess._fps, 30)
        self.assertEqual(sess._quality, 70)
        self.assertEqual(sess._bitrate, 1_800_000)
        self.assertEqual(sess._keyint, 30)
        self.assertAlmostEqual(sess._ratio_thr, 0.01)
        self.assertEqual(sess._region,
                         {"left": 0, "top": 0, "width": 640, "height": 480})

    def test_bad_values_fall_back_and_bad_region_becomes_none(self):
        cfg = {"host": {
            "fps": "abc", "jpeg_quality": None,
            "capture": {"monitor": "x", "region": {"left": 0}},
            "codec": {"bitrate_kbps": "oops", "target_width": "wide"},
            "perf": {"still": {"probe_fps": 0, "still_frames": "nan"}},
        }}
        sess = self._session(cfg)
        self.assertEqual(sess._fps, 30)
        self.assertEqual(sess._quality, 80)
        self.assertEqual(sess._monitor, 1)
        self.assertIsNone(sess._region, "损坏的 region 必须归一为全屏而不是每帧 KeyError")


# ------------------------------------------------------------------ B6 解码复位

class TestB6DecoderReset(unittest.TestCase):
    def test_needs_keyframe_is_set_on_decode_failure(self):
        dec = codec_mod.VideoDecoder(common.CODEC_H264)
        self.addCleanup(dec.close)
        self.assertFalse(dec.needs_keyframe)
        bad_ctx = mock.MagicMock()
        bad_ctx.decode.side_effect = RuntimeError("坏 NAL")
        dec._ctx = bad_ctx
        self.assertEqual(dec.decode(b"nal"), [])
        self.assertTrue(dec.needs_keyframe, "解码器内部复位后必须告知调用方")
        dec.clear_needs_keyframe()
        self.assertFalse(dec.needs_keyframe)

    def test_viewer_invalidates_got_key_when_decoder_reset(self):
        ch = viewer.Channel.__new__(viewer.Channel)
        ch.name = "t"
        ch._got_key = True
        ch.cur_codec = common.CODEC_H264
        ch._gen = 0
        ch.video_mode = True
        ch._dec_lock = lambda: threading.Lock()
        requested = []
        stored = []
        ch._request_keyframe = lambda sock: requested.append(1)
        ch._store_frame = lambda frame, ts: stored.append(ts)

        class _Dec:
            available = True
            needs_keyframe = True

            def decode(self, nal):
                return ["FRAME"]

            def clear_needs_keyframe(self):
                self.needs_keyframe = False

            def set_codec(self, codec):
                pass

        ch._decoder = _Dec()
        ch._decode_video_batch(None, [(1, common.CODEC_H264, 0, b"nal")])
        self.assertFalse(ch._got_key, "解码器已复位时必须作废‘仅关键帧后显示’门控")
        self.assertEqual(stored, [], "复位后的 P 帧不得被贴到悬浮窗（脏帧/花屏）")
        self.assertTrue(requested, "应主动请求关键帧快速恢复")


# ------------------------------------------------------------------ B7 vendor 路径

class TestB7VendorPath(unittest.TestCase):
    def test_ensure_vendor_on_path_adds_repo_vendor(self):
        vendor = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "vendor")
        if not os.path.isdir(vendor):
            self.skipTest("仓库无 vendor 目录")
        old_flag = screen_mod._VENDOR_ON_PATH
        screen_mod._VENDOR_ON_PATH = False
        saved = list(sys.path)
        try:
            screen_mod.ensure_vendor_on_path()
            self.assertIn(vendor, sys.path)
        finally:
            screen_mod._VENDOR_ON_PATH = old_flag
            sys.path[:] = saved


# ------------------------------------------------------------------ B8 共享终止

class TestB8ShareTermination(unittest.TestCase):
    def test_exit_reason_set_when_capture_unavailable(self):
        sess = share_mod.ScreenShareSession(
            FakeSock(), threading.Lock(), {"host": {}}, "t", capture=FakeCap(working=False))
        sess._run()
        self.assertIn("采集后端不可用", sess.exit_reason)

    def test_channel_detects_dead_session(self):
        class _DeadSession:
            alive = False
            exit_reason = "采集后端不可用（dxgi/mss 均失败）"

        unshared = []
        notified = []
        stopped = []

        ch = viewer.Channel.__new__(viewer.Channel)
        ch.name = "t"
        ch._share_lock = threading.Lock()
        ch.share_enabled = True
        ch._share = _DeadSession()
        ch._active_sock = FakeSock()
        ch._send_locked = lambda sock, obj, timeout=5.0: unshared.append(obj)
        ch._stop_share = lambda: stopped.append(1)
        ch._notify_share_error = lambda text: notified.append(text)

        ch.check_share_alive()
        self.assertFalse(ch.share_enabled)
        self.assertTrue(stopped)
        self.assertEqual(unshared[0]["action"], "unshare")
        self.assertTrue(notified and "共享已停止" in notified[0])

    def test_roster_marks_stale_member(self):
        rt = host.HostRuntime({"host": {}})
        info = host.ClientInfo(("10.0.0.1", 1), "bob")
        info._sender_active = True
        sock = FakeSock()
        rt.clients[sock] = info
        rt._register_sharer(sock, info)
        socksent = []
        info.send_ctrl = lambda s, obj, send_timeout=2.0: socksent.append(obj)
        info.last_up_at = time.monotonic() - host._MEMBER_STALE_S - 1.0
        rt.broadcast_roster()
        peers = [m for m in socksent if m.get("action") == "peers"][-1]["peers"]
        self.assertTrue(peers[0].get("stale"), "长时间无上行帧的成员应标注为无信号")

    def test_peer_key_table_cleaned_on_unregister(self):
        rt = host.HostRuntime({"host": {}})
        sock = FakeSock()
        info = host.ClientInfo(("10.0.0.1", 1), "bob")
        info._sender_active = True
        rt.clients[sock] = info
        rt._register_sharer(sock, info)
        rt._peer_key_at[info.share_id] = 123.0
        rt._unregister_sharer(info)
        self.assertNotIn("peer:1", rt._peer_key_at, "C5：注销后冷却表必须清理")


# ------------------------------------------------------------------ B9 无观众 ABR

class TestB9NoSubscriberAbr(unittest.TestCase):
    def _rt(self):
        return host.HostRuntime({"host": {}})

    def test_route_frame_returns_delivery_count(self):
        rt = self._rt()
        self.assertEqual(host.route_frame(rt, "local", b"frame"), 0)
        info = host.ClientInfo(("10.0.0.1", 1), "w")
        info.watch_source = "local"
        info.need_key = False        # 门控：新连接默认在等关键帧，此处已就绪
        info._sender_active = False
        info._write_lock = threading.Lock()
        sock = FakeSock()
        rt.clients[sock] = info
        self.assertEqual(host.route_frame(rt, "local", b"frame"), 1)
        self.assertEqual(sock.sent, [b"frame"])

    def test_subscriber_count(self):
        rt = self._rt()
        info = host.ClientInfo(("10.0.0.1", 1), "w")
        info.watch_source = "peer:1"
        rt.clients[FakeSock()] = info
        self.assertEqual(host._subscriber_count(rt, "local"), 0)
        self.assertEqual(host._subscriber_count(rt, "peer:1"), 1)

    def test_bitrate_change_skips_forced_idr_without_subscribers(self):
        rt = self._rt()

        class _Enc:
            available = True
            name = "x"

            def set_bitrate(self, bps):
                return True

        rt.video_encoder = _Enc()
        rt.force_key = False
        with mock.patch.object(host, "_subscriber_count", lambda *a, **kw: 0):
            self.assertTrue(host._set_encoder_bitrate(rt, 3_000_000))
        self.assertFalse(rt.force_key, "无订阅者时不应白编码一个关键帧")

    def test_bitrate_change_forces_idr_with_subscribers(self):
        rt = self._rt()

        class _Enc:
            available = True
            name = "x"

            def set_bitrate(self, bps):
                return True

        rt.video_encoder = _Enc()
        rt.force_key = False
        with mock.patch.object(host, "_subscriber_count", lambda *a, **kw: 1):
            self.assertTrue(host._set_encoder_bitrate(rt, 3_000_000))
        self.assertTrue(rt.force_key)

    def test_perf_recover_requires_subscribers(self):
        cfg = {"host": {"fps": 30}}
        rt = host.HostRuntime(cfg)
        rt.stats.update({"capture_fps": 30, "encode_fps": 30, "send_fps": 30,
                         "encode_ms": 1.0, "avg_send_ms": 0.0, "still": False})
        with mock.patch.object(host, "_perf_log_and_bandwidth",
                               return_value=(0.0, False)), \
                mock.patch.object(host, "_subscriber_count", lambda *a, **kw: 0), \
                mock.patch.object(host, "_abr_recover") as rec:
            host._perf_tick(rt, pipeline.PipelineParams(cfg))
        self.assertFalse(rec.called, "零订阅者时 ABR 不得回升（否则码率被顶到上限）")


# ------------------------------------------------------------------ B10 认证窗口

class TestB10AuthTimeout(unittest.TestCase):
    def test_floor_clamps_small_value(self):
        sock = FakeSock()
        warned = []
        with mock.patch.object(host, "recv_msg", side_effect=socket.timeout("超时")), \
                mock.patch.object(host.log, "warning",
                                  side_effect=lambda *a, **kw: warned.append(a)):
            ok, user = host._auth_required_phase(
                sock, ("1.2.3.4", 1), host.HostRuntime({"host": {}}),
                {"auth_timeout": 60})
        self.assertFalse(ok)
        text = " ".join(str(x) for a in warned for x in a)
        self.assertIn(str(common.AUTH_TIMEOUT_MIN_S), text,
                      "认证窗口下界必须至少覆盖观看端最坏等待（≈180s）")

    def test_default_config_timeout(self):
        self.assertEqual(common.DEFAULT_CONFIG["host"]["auth"]["auth_timeout"],
                         common.AUTH_TIMEOUT_DEFAULT_S)
        self.assertGreaterEqual(common.AUTH_TIMEOUT_MIN_S, 180)

    def test_migration_raises_short_auth_timeout(self):
        raw = {"host": {"listen_host": "0.0.0.0",
                        "auth": {"enabled": False, "auth_timeout": 60}}}
        changes = common._upgrade_config(raw)
        self.assertTrue(raw["host"]["auth"]["enabled"])
        self.assertEqual(raw["host"]["auth"]["auth_timeout"],
                         common.AUTH_TIMEOUT_DEFAULT_S)
        self.assertTrue(any("认证窗口" in c for c in changes))

    def test_migration_keeps_long_enough_timeout(self):
        raw = {"host": {"listen_host": "0.0.0.0",
                        "auth": {"enabled": False, "auth_timeout": 600}}}
        common._upgrade_config(raw)
        self.assertEqual(raw["host"]["auth"]["auth_timeout"], 600)


# ------------------------------------------------------------------ 第 133 条 登录框可用性

class TestB13CredentialRetryUX(unittest.TestCase):
    """第 133 条（2026-09-11）：输错凭据后不得长时间"藏起"登录框。

    用户实测现象：host 开了账户准入，viewer 端"连登录页面都没有"。根因是凭据被拒后
    `_prompt_blocked_until = now + 30`，下一次重连 `_ask_credentials` 直接返回 None 并被
    当成"用户取消"，叠加重连退避最长再等 30 秒——输错一次就长时间看不到登录框。
    """

    def _channel(self, **attrs):
        ch = viewer.Channel.__new__(viewer.Channel)
        ch.name = "频道1"
        ch.owner = None
        ch.username = ""
        ch._password = None
        ch.auth = {}
        ch._anon_ok = False
        ch._auth_probed = True          # 已探测过：下一轮直接弹框
        # auth-003（2026-09-18）：明文口令直发路径已废除；本组锁定的是"服务端回包 →
        # 观看端状态映射"，走挑战流即可（recv_msg 替身回 auth_result 而非 chal，
        # _auth_via_challenge 对非 chal 回包原样返回）
        ch._chal_capable = True
        ch._prompt_blocked_until = 0.0
        ch._retry_delay = 12.5          # 假装退避已涨到 12.5 秒
        ch._auth_error_text = ""
        ch._last_auth_user = ""
        ch.auth_msg = ""
        for k, v in attrs.items():
            setattr(ch, k, v)
        return ch

    def test_rejected_credentials_reopen_prompt_quickly(self):
        ch = self._channel()
        ch._ask_credentials = lambda: ("login", "duanbad", "pw")
        replies = [{"ok": False, "msg": "账号不存在"}]

        with mock.patch.object(viewer, "send_msg") as send, \
                mock.patch.object(viewer, "recv_msg", side_effect=lambda s: replies.pop(0)):
            ok = ch._authenticate(FakeSock())

        self.assertFalse(ok)
        self.assertIn("账号不存在", ch._auth_error_text)
        self.assertEqual(ch._prompt_blocked_until, 0.0,
                         "凭据错误不得设置弹框冷却（否则登录框长时间不出现）")
        self.assertEqual(ch._retry_delay, 0.0, "必须复位重连退避，让登录框约 1 秒后重现")
        self.assertEqual(ch._last_auth_user, "duanbad", "应记住上次用户名以便预填")
        self.assertEqual(send.call_args.args[1]["user"], "duanbad")

    def test_success_clears_error_text(self):
        ch = self._channel(_auth_error_text="账号不存在", _last_auth_user="alice")
        ch.username = "alice"
        ch._password = "pw"
        with mock.patch.object(viewer, "send_msg"), \
                mock.patch.object(viewer, "recv_msg",
                                  return_value={"ok": True, "msg": ""}), \
                mock.patch.object(viewer, "_dpapi_encrypt", return_value=None):
            self.assertTrue(ch._authenticate(FakeSock()))
        self.assertEqual(ch._auth_error_text, "")

    def test_ask_credentials_logs_when_cooling_down(self):
        # ui-011（2026-09-18）：冷却比较已改单调钟，测试同步
        ch = self._channel(_prompt_blocked_until=time.monotonic() + 30)
        with mock.patch.object(viewer.log, "info") as info:
            self.assertIsNone(ch._ask_credentials())
        text = " ".join(str(a) for c in info.call_args_list for a in c.args)
        self.assertIn("冷却", text, "被冷却挡下时必须留日志，否则用户以为没有登录入口")

    def test_ask_credentials_logs_when_prompted(self):
        owner = _CredOwner()
        ch = self._channel(owner=owner, _running=True,
                           _stop_evt=threading.Event())
        t = threading.Thread(target=ch._ask_credentials, daemon=True)
        with mock.patch.object(viewer.log, "info") as info:
            t.start()
            deadline = time.time() + 3.0
            while time.time() < deadline and owner._cred_requests.qsize() == 0:
                time.sleep(0.02)
            text = " ".join(str(a) for c in info.call_args_list for a in c.args)
            ch._stop_evt.set()
        t.join(timeout=3.0)
        self.assertIn("已弹出登录框", text)

    def test_process_cred_requests_passes_error_and_default_user(self):
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app._cred_busy = False
        app._cred_requests = __import__("queue").Queue()
        ch = self._channel(_auth_error_text="账号不存在（用户名须与共享端账户完全一致）",
                           _last_auth_user="duanbad")
        app.channels = [ch]
        seen = {}

        def _dialog(name, error_text="", default_user=""):
            seen["args"] = (name, error_text, default_user)
            return ("login", "alice", "pw")

        app._credentials_dialog = _dialog
        box, done = {}, threading.Event()
        app._cred_requests.put((ch, done, box))

        app._process_cred_requests()

        self.assertEqual(seen["args"], ("频道1",
                                        "账号不存在（用户名须与共享端账户完全一致）",
                                        "duanbad"))
        self.assertEqual(box["result"]["value"], ("login", "alice", "pw"))
        self.assertEqual(ch._auth_error_text, "", "原因只提示一次，避免下次弹框显示陈旧报错")

    def test_register_closed_also_reopens_with_reason(self):
        ch = self._channel()
        ch._ask_credentials = lambda: ("register", "Newbie", "pw")
        replies = [{"ok": False, "msg": "共享端已有账户，自助注册已关闭（请向共享端索取账号）"}]

        with mock.patch.object(viewer, "send_msg"), \
                mock.patch.object(viewer, "recv_msg", side_effect=lambda s: replies.pop(0)):
            self.assertFalse(ch._authenticate(FakeSock()))
        self.assertIn("注册失败", ch._auth_error_text)
        self.assertEqual(ch._prompt_blocked_until, 0.0)


# ------------------------------------------------------------------ C 组

class TestC3LogPrune(unittest.TestCase):
    def test_non_date_files_do_not_evict_real_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            # dist-011（2026-09-20）：保留窗口改为"距今 N 天"，因此夹具的日期必须
            # 相对今天生成——原先写死的 202601xx 在新口径下全部超龄（那正是新行为）。
            dates = [time.strftime("%Y%m%d", time.localtime(
                        time.time() - d * 86400)) for d in range(6, -1, -1)]
            names = ["app_debug.log"] + [
                "app_%s_pid1.log" % ds for ds in dates]
            for n in names:
                open(os.path.join(tmp, n), "w", encoding="utf-8").close()
            handler = logger_mod._DateRotatingFileHandler.__new__(
                logger_mod._DateRotatingFileHandler)
            handler._log_dir = tmp
            handler._prune_old()
            left = set(os.listdir(tmp))
            self.assertIn("app_debug.log", left, "非日期命名文件不得被当成日期组")
            date_files = sorted(n for n in left if n != "app_debug.log")
            self.assertEqual(len(date_files), logger_mod._KEEP_DAYS)
            # 保留的是最新的 5 天，最旧的两天被删
            self.assertNotIn("app_%s_pid1.log" % dates[0], left)
            self.assertIn("app_%s_pid1.log" % dates[-1], left)

    def test_stale_group_dropped_even_when_fewer_than_keep_days(self):
        # dist-011（2026-09-20）：旧实现 `sorted(groups)[-5:]` 保留的是"目录里现存的
        # 后 5 个日期组"而不是"距今 5 天"——只开过一天的机器隔 9 天再用，最旧那组
        # 照样完整保留、实删 0 个文件（本机现存 5 组、最旧 9 天前即为此）。
        with tempfile.TemporaryDirectory() as tmp:
            old = time.strftime("%Y%m%d", time.localtime(time.time() - 9 * 86400))
            for n in ("app_%s_pid1.log" % old,
                      "app_%s_pid2.log" % old,
                      "app_%s_pid3.log" % time.strftime("%Y%m%d")):
                open(os.path.join(tmp, n), "w", encoding="utf-8").close()
            handler = logger_mod._DateRotatingFileHandler.__new__(
                logger_mod._DateRotatingFileHandler)
            handler._log_dir = tmp
            handler._prune_old()
            left = set(os.listdir(tmp))
            self.assertNotIn("app_%s_pid1.log" % old, left,
                             "超龄日期组必须整组删除，哪怕现存组数不足 KEEP_DAYS")
            self.assertIn("app_%s_pid3.log" % time.strftime("%Y%m%d"), left)


class TestC7ListenBacklog(unittest.TestCase):
    def test_backlog_scales_with_max_clients(self):
        rt = host.HostRuntime({"host": {}})
        rt.max_clients = 32
        self.assertEqual(host._listen_backlog(rt), 64)
        rt.max_clients = 4096
        self.assertEqual(host._listen_backlog(rt), 128)
        rt.max_clients = 1
        self.assertEqual(host._listen_backlog(rt), 16)


class TestC12ConfigVersionWriteback(unittest.TestCase):
    def test_version_written_even_without_changes(self):
        raw = {"host": {"listen_host": "127.0.0.1", "auth": {"enabled": True}}}
        self.assertEqual(common._upgrade_config(raw), [])
        self.assertEqual(raw.get("config_version"), common.CONFIG_VERSION)


class TestC16DxcamTypeError(unittest.TestCase):
    def _manager(self, exc, only_with_output_color):
        fake = types.ModuleType("dxcam")
        calls = []

        def create(**kw):
            calls.append(kw)
            should_raise = exc is not None and (
                ("output_color" in kw) if only_with_output_color else True)
            if should_raise:
                raise exc
            return object()

        fake.create = create
        return fake, calls

    def test_unknown_keyword_falls_back_to_rgb(self):
        # 只有"带 output_color 调用"才报错 → 去掉参数重试成功（真正的旧版 dxcam 行为）
        fake, calls = self._manager(
            TypeError("create() got an unexpected keyword argument 'output_color'"),
            only_with_output_color=True)
        with mock.patch.dict(sys.modules, {"dxcam": fake}):
            cm = screen_mod.CaptureManager("dxgi", 1, None)
        self.addCleanup(lambda: cm._mss and cm._mss.close())
        self.assertEqual(cm._effective, "dxgi")
        self.assertEqual(len(calls), 2)
        self.assertIn("output_color", calls[0])
        self.assertNotIn("output_color", calls[1])

    def test_unrelated_type_error_is_not_treated_as_unsupported_param(self):
        # 第三方栈内部 TypeError（与 output_color 无关）→ 不得去掉参数重试、不得当成
        # "版本不支持" 而拿到 RGB 帧当 BGR 用
        fake, calls = self._manager(TypeError("internal comtypes failure"),
                                    only_with_output_color=False)
        with mock.patch.dict(sys.modules, {"dxcam": fake}), \
                mock.patch.object(screen_mod.log, "warning") as warn:
            cm = screen_mod.CaptureManager("dxgi", 1, None)
        self.addCleanup(lambda: cm._mss and cm._mss.close())
        self.assertEqual(len(calls), 1, "非 output_color 的 TypeError 不得触发无参重试")
        self.assertEqual(cm._effective, "mss", "应回退 mss 而不是拿 RGB 帧当 BGR 用")
        texts = " ".join(str(a) for c in warn.call_args_list for a in c.args)
        self.assertIn("dxgi 后端不可用", texts)


if __name__ == "__main__":
    unittest.main()
