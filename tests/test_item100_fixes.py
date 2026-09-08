# -*- coding: utf-8 -*-
"""第 100 条回归：本轮审查发现并修复的一批缺陷。

覆盖（每条对应审查报告里的编号）：
1) dxgi 采集帧是 RGB 却按 BGR 用 → 显式要求 output_color="BGR"（screen.py）
2) 握手阶段无超时 → handle_client 在 server_handshake 前把超时收紧（host.py）
3) 非数字 fps/质量/缩放等配置 → 采集/编码/性能线程不再被 TypeError 打死（host.py）
4) ViewerApp 在频道线程启动之后才建 _mainloop_ready → "等界面就绪"防护失效（viewer.py）
5) viewer 控制/心跳 sendall 无超时 → _send_locked 临时收紧并还原（viewer.py）
6) ABR 的 last_send_ms 无时间窗 → recent_send_ms 按时效过滤（host.py）
8) 越界采集区域静默全黑 → host_ui 提前校验（host_ui.py）
9) 迟到 >2s 的 probe 无人应答 → 主循环补 auth_result（host.py）
10) frp token 非字符串 → _sanitize 不再抛 TypeError（host.py）
11) viewer 缺 threading.excepthook → main() 安装（viewer.py）
12) --addr 在已有频道时无效 → 作为临时频道生效且不写回配置（viewer.py）
13) display_width/alpha 未校验 → 构造时夹紧（viewer.py）
14) region 缺键 → _region_text 不再 KeyError（host_ui.py）
"""
import ast
import copy
import inspect
import socket
import sys
import threading
import time
import unittest
from unittest import mock

import common
import host
import screen
import viewer
import host_ui


# ---------------------------------------------------------------- 1) dxgi 色彩

class _FakeCamera:
    def __init__(self):
        self.released = False

    def grab(self, region=None):
        return None

    def release(self):
        self.released = True


class TestDxgiColorOrder(unittest.TestCase):
    def _with_fake_dxcam(self, create):
        mod = type(sys)("dxcam")
        mod.create = create
        return mock.patch.dict(sys.modules, {"dxcam": mod})

    def test_requests_bgr_output(self):
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            return _FakeCamera()

        with self._with_fake_dxcam(create):
            cap = screen.CaptureManager("dxgi", 1, None)
        self.assertEqual(calls, [{"output_idx": 0, "output_color": "BGR"}])
        self.assertTrue(cap.working)
        self.assertEqual(cap._effective, "dxgi")

    def test_falls_back_when_output_color_unsupported(self):
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            if "output_color" in kwargs:
                raise TypeError("unexpected keyword argument 'output_color'")
            return _FakeCamera()

        with self._with_fake_dxcam(create):
            cap = screen.CaptureManager("dxgi", 2, None)
        self.assertEqual(calls, [{"output_idx": 1, "output_color": "BGR"},
                                 {"output_idx": 1}])
        self.assertTrue(cap.working)

    def test_source_passes_output_color(self):
        src = inspect.getsource(screen.CaptureManager._open)
        self.assertIn('output_color="BGR"', src)


# ------------------------------------------------------------ 2) 握手超时

class _RuntimeStub:
    def __init__(self, cfg=None):
        self.cfg = cfg or {"host": {"net": {}, "auth": {}}}
        # 第 103 条：handle_client 现在先占连接名额（B1 连接上限），替身需实现该接口
        self.max_clients = 32

    def acquire_conn(self):
        return True

    def release_conn(self):
        pass


class TestHandshakeTimeout(unittest.TestCase):
    def test_timeout_is_set_before_handshake(self):
        seen = {}

        def fake_handshake(sock):
            seen["timeout"] = sock.gettimeout()
            raise ConnectionError("模拟对端不发魔数")

        a, b = socket.socketpair()
        try:
            with mock.patch.object(host, "server_handshake", fake_handshake):
                host.handle_client(a, ("127.0.0.1", 1), _RuntimeStub())
        finally:
            b.close()
        self.assertEqual(seen["timeout"], host.HANDSHAKE_TIMEOUT_S,
                         "握手前未把 socket 超时收紧到 HANDSHAKE_TIMEOUT_S")

    def test_keepalive_is_enabled_before_handshake(self):
        """握手阶段就该有 SO_KEEPALIVE：否则半开连接连内核都不会回收。"""
        seen = {}

        def fake_handshake(sock):
            seen["keepalive"] = sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
            raise ConnectionError("stop")

        a, b = socket.socketpair()
        try:
            with mock.patch.object(host, "server_handshake", fake_handshake):
                host.handle_client(a, ("127.0.0.1", 1), _RuntimeStub())
        finally:
            b.close()
        self.assertEqual(seen.get("keepalive"), 1)


# ------------------------------------------------- 3) 配置数值安全转换

class TestSafeConfigValues(unittest.TestCase):
    def test_safe_int_rejects_garbage(self):
        self.assertEqual(host._safe_int("60", 30), 60)      # 数字字符串可用
        self.assertEqual(host._safe_int("abc", 30), 30)
        self.assertEqual(host._safe_int(None, 30), 30)
        self.assertEqual(host._safe_int([], 30), 30)
        self.assertEqual(host._safe_int(-5, 30, lo=1), 1)
        self.assertEqual(host._safe_int(9999, 30, hi=240), 240)

    def test_safe_float_rejects_garbage(self):
        self.assertEqual(host._safe_float("0.5", 1.0), 0.5)
        self.assertEqual(host._safe_float("abc", 1.0), 1.0)
        self.assertEqual(host._safe_float(None, 1.0), 1.0)
        self.assertEqual(host._safe_float(9.0, 1.0, hi=2.0), 2.0)

    def test_slot_survives_string_fps(self):
        """旧代码：slot["fps"] 是 str → 采集线程 `fps_now > 0` TypeError → 线程死亡。"""
        cfg = copy.deepcopy(common.DEFAULT_CONFIG)
        cfg["host"]["fps"] = "60"
        cfg["host"]["jpeg_quality"] = "abc"
        cfg["host"]["scale"] = None
        cfg["host"]["capture"]["monitor"] = "1"
        rt = host.HostRuntime(cfg)
        self.assertEqual(rt.slot["fps"], 60)
        self.assertIsInstance(rt.slot["fps"], int)
        self.assertEqual(rt.slot["quality"], 80)
        self.assertEqual(rt.slot["scale"], 1.0)
        self.assertEqual(rt.slot["monitor"], 1)
        # 采集线程里的同一算式不再抛异常
        frame_interval = 1.0 / rt.slot["fps"] if rt.slot["fps"] > 0 else 0.0
        self.assertGreater(frame_interval, 0)

    def test_slot_normalizes_bad_region_and_backend(self):
        cfg = copy.deepcopy(common.DEFAULT_CONFIG)
        cfg["host"]["capture"]["region"] = {"left": 0, "top": 0}   # 缺 width/height
        cfg["host"]["capture"]["backend"] = "nonsense"
        rt = host.HostRuntime(cfg)
        self.assertIsNone(rt.slot["region"])
        self.assertEqual(rt.slot["backend"], "mss")

    def test_normalize_region_keeps_valid(self):
        self.assertEqual(host._normalize_region(
            {"left": 1, "top": 2, "width": 3, "height": 4}),
            {"left": 1, "top": 2, "width": 3, "height": 4})
        self.assertIsNone(host._normalize_region(
            {"left": 0, "top": 0, "width": 0, "height": 4}))
        self.assertIsNone(host._normalize_region({"left": "x", "top": 0,
                                                  "width": 1, "height": 1}))


# ----------------------------------------------------- 4) 频道线程启动时序

class _ChannelStub:
    """记录创建时 owner 上哪些属性已经存在（不启动真实接收线程）。"""

    observed = []

    def __init__(self, name, addr, auth=None, owner=None, ephemeral=False):
        self.name = name
        self.addr = addr
        self.auth = auth or {}
        self.ephemeral = bool(ephemeral)
        _ChannelStub.observed.append({
            "name": name,
            "addr": addr,
            "ephemeral": ephemeral,
            "mainloop_ready": hasattr(owner, "_mainloop_ready"),
            "cred_requests": hasattr(owner, "_cred_requests"),
            "running": hasattr(owner, "running"),
            "root": hasattr(owner, "root"),
        })


class TestViewerInitOrder(unittest.TestCase):
    def setUp(self):
        _ChannelStub.observed = []

    def _make_app(self, cfg=None, addr_override=None):
        cfg = cfg or copy.deepcopy(common.DEFAULT_CONFIG)
        with mock.patch.object(viewer, "Channel", _ChannelStub):
            app = viewer.ViewerApp(cfg, addr_override=addr_override)
        return app

    def test_state_exists_before_channels_are_created(self):
        self._make_app()
        self.assertTrue(_ChannelStub.observed)
        for rec in _ChannelStub.observed:
            self.assertTrue(rec["mainloop_ready"],
                            "频道线程启动时 owner._mainloop_ready 还不存在"
                            "（等界面就绪的防护会整段失效）")
            self.assertTrue(rec["cred_requests"])
            self.assertTrue(rec["running"])
            self.assertTrue(rec["root"], "频道线程启动时 owner.root 还不存在")

    def test_mainloop_ready_is_set_after_construction(self):
        app = self._make_app()
        self.assertIsInstance(app._mainloop_ready, threading.Event)
        self.assertFalse(app._mainloop_ready.is_set())


# --------------------------------------------- 5) viewer 发送有界 + 还原

class _TimeoutSock:
    def __init__(self, timeout=None, fail=False):
        self._timeout = timeout
        self.fail = fail
        self.timeout_during_send = []

    def gettimeout(self):
        return self._timeout

    def settimeout(self, t):
        self._timeout = t

    def sendall(self, data):
        self.timeout_during_send.append(self._timeout)
        if self.fail:
            raise socket.timeout("模拟发送超时")


class _PlainSock:
    """没有任何超时 API 的替身（部分既有测试用 object() 当 socket）。"""

    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)


class TestViewerSendBounded(unittest.TestCase):
    def _channel(self):
        ch = viewer.Channel.__new__(viewer.Channel)
        ch._tx_lock = threading.Lock()
        return ch

    def test_sendall_bounded_and_restored(self):
        sock = _TimeoutSock(timeout=None)
        ch = self._channel()
        sent = []
        with mock.patch.object(viewer, "send_msg", lambda s, o: s.sendall(b"x") or sent.append(o)):
            ch._send_locked(sock, {"action": "ping"})
        self.assertEqual(sock.timeout_during_send, [5.0], "sendall 未被收紧到 5.0")
        self.assertIsNone(sock.gettimeout(), "发送后未还原阻塞基线")

    def test_custom_timeout_restored_on_failure(self):
        sock = _TimeoutSock(timeout=3.0, fail=True)
        ch = self._channel()
        with mock.patch.object(viewer, "send_msg", lambda s, o: s.sendall(b"x")):
            with self.assertRaises(socket.timeout):
                ch._send_locked(sock, {"action": "watch"})
        self.assertEqual(sock.gettimeout(), 3.0, "异常路径未还原原超时")

    def test_tolerates_socket_without_timeout_api(self):
        sock = _PlainSock()
        ch = self._channel()
        with mock.patch.object(viewer, "send_msg", lambda s, o: s.sendall(b"x")):
            ch._send_locked(sock, {"action": "ping"})
        self.assertEqual(sock.sent, [b"x"])

    def test_control_paths_use_bounded_helper(self):
        for fn in (viewer.Channel._control_ping_loop, viewer.Channel._request_keyframe,
                   viewer.Channel.switch_source, viewer.Channel.set_share):
            self.assertIn("_send_locked", inspect.getsource(fn),
                          "%s 未走有界发送" % fn.__name__)


# ------------------------------------------- 6) ABR 发送耗时样本时效

class TestRecentSendMs(unittest.TestCase):
    def test_stale_sample_is_dropped(self):
        info = host.ClientInfo(("127.0.0.1", 1), "slow")
        info.record_sent(1024, now=100.0, send_ms=400.0)
        self.assertEqual(info.recent_send_ms(now=101.0), 400.0)
        self.assertEqual(info.recent_send_ms(now=100.0 + host.SEND_MS_WINDOW_S + 0.1), 0.0,
                         "过期样本仍被计入 ABR 拥塞判据")

    def test_no_sample_returns_zero(self):
        info = host.ClientInfo(("127.0.0.1", 1), "new")
        self.assertEqual(info.recent_send_ms(now=1.0), 0.0)


# --------------------------------------------- 8) 越界采集区域校验

class TestRegionBounds(unittest.TestCase):
    def _console(self, rects):
        c = host_ui.HostConsole.__new__(host_ui.HostConsole)
        c.monitor_rects = rects
        return c

    def test_out_of_bounds_detected(self):
        c = self._console([{"left": 0, "top": 0, "width": 2560, "height": 1440}])
        self.assertIsNone(c._region_out_of_bounds(
            {"left": 0, "top": 0, "width": 1280, "height": 720}))
        self.assertEqual(c._region_out_of_bounds(
            {"left": 99999, "top": 99999, "width": 100, "height": 100}), (2560, 1440))
        self.assertEqual(c._region_out_of_bounds(
            {"left": 0, "top": 0, "width": 2561, "height": 100}), (2560, 1440))

    def test_second_monitor_offset_is_not_rejected(self):
        c = self._console([{"left": 0, "top": 0, "width": 3840, "height": 1080},
                           {"left": 1920, "top": 0, "width": 1920, "height": 1080}])
        self.assertIsNone(c._region_out_of_bounds(
            {"left": 1920, "top": 0, "width": 1920, "height": 1080}))

    def test_no_monitors_means_no_validation(self):
        c = self._console([])
        self.assertIsNone(c._region_out_of_bounds(
            {"left": 99999, "top": 0, "width": 10, "height": 10}))


# ------------------------------------- 14) region 缺键不再让 GUI 起不来

class TestRegionTextRobust(unittest.TestCase):
    def test_missing_keys_return_empty(self):
        self.assertEqual(host_ui.HostConsole._region_text({"left": 0, "top": 0}), "")
        self.assertEqual(host_ui.HostConsole._region_text(None), "")
        self.assertEqual(host_ui.HostConsole._region_text("bogus"), "")
        self.assertEqual(host_ui.HostConsole._region_text(
            {"left": 1, "top": 2, "width": 3, "height": 4}), "1,2,3,4")


# ------------------------------------------------ 10) frp token 类型

class TestSanitizeTokenType(unittest.TestCase):
    def test_numeric_token_does_not_raise(self):
        self.assertEqual(host._sanitize("frpc login ok", 123456), "frpc login ok")
        self.assertEqual(host._sanitize("frpc login ok", None), "frpc login ok")
        self.assertEqual(host._sanitize("abc 123456 def", 123456),
                         "abc " + host.logger.mask_secret("123456") + " def")

    def test_non_string_text_passthrough(self):
        self.assertIsNone(host._sanitize(None, "tok"))
        self.assertEqual(host._sanitize(42, "tok"), 42)


# ------------------------------------------- 9) 迟到 probe 有应答

class TestLateProbeAnswered(unittest.TestCase):
    """可选 auth 窗口（2s）之后到达的 probe 必须得到 auth_result。

    旧行为：主循环没有 probe/login 分支 → 观看端把随后的 roster/pong 当认证回复
    → 在"无需登录"的服务端上弹登录框、最终报"需要登录"并反复重连。
    """

    def test_probe_after_window_gets_auth_result(self):
        rt = host.HostRuntime(copy.deepcopy(common.DEFAULT_CONFIG))
        a, b = socket.socketpair()
        b.settimeout(10.0)
        th = threading.Thread(target=host.handle_client,
                              args=(a, ("127.0.0.1", 1), rt), daemon=True)
        try:
            th.start()
            b.sendall(common.MAGIC_CLIENT)
            self.assertEqual(common.recv_exact(b, len(common.MAGIC_SERVER)),
                             common.MAGIC_SERVER)
            # 等可选 auth 窗口（2.0s）过去，让 host 按"旧客户端"放行
            time.sleep(2.3)
            common.send_msg(b, {"action": "probe", "user": "", "pass": ""})
            deadline = time.time() + 8.0
            got = None
            while time.time() < deadline:
                msg = common.recv_msg(b)
                if isinstance(msg, dict) and msg.get("action") == "auth_result":
                    got = msg
                    break
            self.assertIsNotNone(got, "迟到的 probe 没有收到 auth_result")
            self.assertTrue(got.get("ok"), "准入关闭时迟到 probe 应被放行：%r" % (got,))
        finally:
            try:
                b.close()
            except OSError:
                pass
            th.join(timeout=5.0)


# ---------------------------------------- 11) viewer 工作线程异常兜底

class TestViewerThreadHook(unittest.TestCase):
    def test_main_installs_threading_excepthook(self):
        src = inspect.getsource(viewer.main)
        self.assertIn("threading.excepthook = _thread_excepthook", src)

    def test_thread_hook_logs_without_dialog(self):
        class _Args:
            exc_type = ValueError
            exc_value = ValueError("boom")
            exc_traceback = None
            thread = None

        with mock.patch.object(viewer, "messagebox") as mb:
            viewer._thread_excepthook(_Args())     # 不得抛异常
        mb.showerror.assert_not_called()


# ------------------------------------------------- 12) --addr 真正生效

class TestAddrOverride(unittest.TestCase):
    def setUp(self):
        _ChannelStub.observed = []

    def test_override_becomes_first_channel(self):
        cfg = copy.deepcopy(common.DEFAULT_CONFIG)
        cfg["viewer"]["channels"] = [{"name": "已有", "addr": "10.0.0.1:5700"}]
        with mock.patch.object(viewer, "Channel", _ChannelStub):
            app = viewer.ViewerApp(cfg, addr_override="10.0.0.9:5700")
        self.assertEqual(app.active_idx, 0)
        self.assertEqual(len(app.channels), 2)
        # 覆盖频道插在首位并标记为临时（不落盘）
        self.assertEqual(_ChannelStub.observed[-1]["addr"], "10.0.0.9:5700")
        self.assertTrue(_ChannelStub.observed[-1]["ephemeral"])
        self.assertTrue(app.channels[0].ephemeral)
        self.assertEqual(app.channels[0].addr, "10.0.0.9:5700")

    def test_ephemeral_channel_not_persisted(self):
        class _Ch:
            def __init__(self, name, addr, ephemeral):
                self.name = name
                self.addr = addr
                self.auth = {}
                self.ephemeral = ephemeral

        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app.cfg = {"viewer": {}}
        app._channels_lock = threading.RLock()
        app.channels = [_Ch("临时", "2.2.2.2:5700", True),
                        _Ch("正式", "1.1.1.1:5700", False)]
        with mock.patch.object(viewer, "save_config") as save:
            app._save_channels()
        saved = app.cfg["viewer"]["channels"]
        self.assertEqual([c["addr"] for c in saved], ["1.1.1.1:5700"])
        save.assert_called_once()


# -------------------------------------------- 13) display_width/alpha 校验

class TestViewerDisplayClamp(unittest.TestCase):
    def setUp(self):
        _ChannelStub.observed = []

    def _app(self, **viewer_over):
        cfg = copy.deepcopy(common.DEFAULT_CONFIG)
        cfg["viewer"].update(viewer_over)
        with mock.patch.object(viewer, "Channel", _ChannelStub):
            return viewer.ViewerApp(cfg)

    def test_zero_width_is_clamped(self):
        self.assertEqual(self._app(display_width=0).display_width, 1)
        self.assertEqual(self._app(display_width=-50).display_width, 1)
        self.assertEqual(self._app(display_width="abc").display_width, 480)
        self.assertEqual(self._app(display_width="361").display_width, 361)

    def test_alpha_is_clamped(self):
        self.assertEqual(self._app(alpha="abc").alpha, 0.9)
        self.assertEqual(self._app(alpha=5).alpha, 1.0)
        self.assertEqual(self._app(alpha=-1).alpha, 0.0)


if __name__ == "__main__":
    unittest.main()
