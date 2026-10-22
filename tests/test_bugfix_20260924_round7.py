# -*- coding: utf-8 -*-
"""第 7 轮全量审查（docs/BUG审查报告-2026-09-24.md）修复的回归用例。

覆盖条目（编号即报告编号）：
- net-038：pack_msg 对游离代理位必须兜底（否则整条 roster 广播中断）
- net-037：入册之后任何一步异常都必须走统一收尾（不泄漏 client/线程/fd）
- net-040：被配额限流的 UNSHARE 不得静默丢弃（挂起待补做）
- net-039：隧道自检线程幂等补建（GUI 启动 frpc 后也在跑）
- codec-015：host 侧静止期关键帧请求在编码器不可用时要走 JPEG 出口
- codec-018：share 侧兜底不得送 P 帧谎报成功
- codec-016：x264 风格 preset 不得原样喂给 NVENC
- codec-017：并发调码率不得各建一套上下文
- codec-019：PyAV 缺失必须留痕
- cfg-010：save_config 读基线失败不得塌缩整份配置
- cfg-011：首建/恢复写盘走原子替换（不留 tmp）
- cfg-012：非 OSError 的写失败也要清 tmp
- capture-008：normalize_region 允许负原点（副屏在主屏左侧）
- capture-009：share 侧空帧重建改时间判据
- capture-010：dxcam 输出匹配告警跨实例只报一次
- dist-018/019：诊断包回收 + 日志清理遇不可删文件继续
- net-041：RTT 用单调钟配对

hermetic：全部用假对象/临时目录，不依赖真实显示、真实网络与真实编码器。
"""
import json
import logging
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

import codec
import common
import host
import logger as logger_mod
import pipeline
import screen
import share
import viewer

# --------------------------------------------------------------- net-038

class TestPackMsgSurrogate(unittest.TestCase):
    def test_lone_surrogate_does_not_raise(self):
        """对端可控文本含游离代理位时打包不得抛（旧实现抛 UnicodeEncodeError）。"""
        msg = common.pack_msg({"action": "peers", "name": "\ud800x"})
        self.assertIsInstance(msg, bytes)
        self.assertEqual(msg[0], common.MSG_CTRL)
        # 解析回来仍是合法 JSON，且代理位被替换为 U+FFFD
        payload = msg[5:]
        obj = json.loads(payload.decode("utf-8"))   # 必须是可解析的合法 UTF-8 JSON
        self.assertIn("x", obj["name"])
        self.assertNotIn("\ud800", obj["name"], "游离代理位原样留在消息里")

    def test_sanitize_username_strips_surrogates_and_controls(self):
        self.assertEqual(host._sanitize_username("\ud800ab\nc"), "abc")
        self.assertEqual(host._sanitize_username(None), "")
        long_name = "u" * (host.MAX_USERNAME_LEN + 20)
        self.assertEqual(len(host._sanitize_username(long_name)), host.MAX_USERNAME_LEN)


# --------------------------------------------------------------- net-037

class _ClientRuntimeStub:
    """够跑通 _handle_client_inner 入册段的 runtime 替身。"""

    def __init__(self, roster_raises=False, handshake_ok=True):
        self.cfg = {"host": {"net": {}, "auth": {}}}
        self.clients = {}
        self.clients_lock = threading.Lock()
        self.max_clients = 8
        self.stop_event = threading.Event()
        self.msg_timeout = 1.0
        self._roster_lock = threading.Lock()
        self._roster_sending = False
        self._roster_dirty = False
        self._conn_count = 0
        self._roster_calls = 0
        self._handshake_ok = handshake_ok
        self._roster_raises = roster_raises
        self.acquire_pending = lambda: True
        self.release_pending = lambda: None

    def acquire_conn(self):
        return True

    def release_conn(self):
        pass

    def broadcast_roster(self, *a, **k):
        self._roster_calls += 1
        if self._roster_raises:
            raise UnicodeEncodeError("utf-8", "x", 0, 1, "boom")

    def _unregister_sharer(self, info):
        info.share_id = None


class TestClientTeardownCoversRoster(unittest.TestCase):
    def test_roster_exception_does_not_leak_client(self):
        """net-037：broadcast_roster 抛异常时必须走统一收尾（旧实现直接冒泡、泄漏）。"""
        a, b = socket.socketpair()
        rt = _ClientRuntimeStub(roster_raises=True)
        try:
            with mock.patch.object(host, "_do_handshake", lambda *a, **k: True), \
                    mock.patch.object(host, "_do_auth", lambda *a, **k: (True, "u")):
                host.handle_client(a, ("127.0.0.1", 5000), rt)   # 不应抛
            self.assertEqual(rt.clients, {}, "客户端泄漏在 runtime.clients（幽灵源）")
            # 收尾必须关掉 socket：对端 recv 立刻得到 EOF
            b.settimeout(1.0)
            self.assertEqual(b.recv(16), b"", "socket 未被关闭（fd 泄漏）")
        finally:
            b.close()
            try:
                a.close()
            except OSError:
                pass

    def test_normal_path_registers_and_sends_roster(self):
        """对照：正常路径照常入册并广播 roster（修复不得把正常流程吃掉）。"""
        a, b = socket.socketpair()
        rt = _ClientRuntimeStub()
        try:
            with mock.patch.object(host, "_do_handshake", lambda *a, **k: True), \
                    mock.patch.object(host, "_do_auth", lambda *a, **k: (True, "u")), \
                    mock.patch.object(host, "_client_recv_loop",
                                      lambda *a, **k: "测试结束"):
                host.handle_client(a, ("127.0.0.1", 5001), rt)
            self.assertEqual(rt._roster_calls, 1)
            self.assertEqual(rt.clients, {})   # 收尾后仍要清空
        finally:
            b.close()
            try:
                a.close()
            except OSError:
                pass


# --------------------------------------------------------------- net-040

class TestUnshareThrottle(unittest.TestCase):
    def _info(self, allowed):
        class _Info:
            def __init__(self):
                self.share_id = "peer:1"
                self.pending_unshare = False
                self.username = "u"
                self.addr = "1.2.3.4:1"
                self.watch_source = "local"

            def allow_ctrl_broadcast(self):
                return allowed

        return _Info()

    def test_throttled_unshare_is_deferred_not_dropped(self):
        rt = mock.Mock()
        info = self._info(allowed=False)
        out = host._dispatch_ctrl(None, info, rt, {"action": host.Act.UNSHARE},
                                  "1.2.3.4:1", {})
        self.assertIsNone(out)
        self.assertTrue(info.pending_unshare, "被限流的 unshare 被静默丢弃（幽灵源）")
        rt._unregister_sharer.assert_not_called()

    def test_allowed_unshare_unregisters_immediately(self):
        rt = mock.Mock()
        info = self._info(allowed=True)
        host._dispatch_ctrl(None, info, rt, {"action": host.Act.UNSHARE},
                            "1.2.3.4:1", {})
        rt._unregister_sharer.assert_called_once()
        self.assertFalse(info.pending_unshare)

    def test_ctrl_unshare_is_idempotent(self):
        rt = mock.Mock()
        rt._unregister_sharer.side_effect = lambda i: setattr(i, "share_id", None)
        info = self._info(allowed=True)
        info.pending_unshare = True
        host._ctrl_unshare(rt, info, "1.2.3.4:1")
        host._ctrl_unshare(rt, info, "1.2.3.4:1")
        self.assertEqual(rt._unregister_sharer.call_count, 1)
        self.assertFalse(info.pending_unshare)


# --------------------------------------------------------------- net-039

class TestSelfcheckIdempotent(unittest.TestCase):
    def _runtime(self, running=True):
        import types
        rt = types.SimpleNamespace()
        rt.stop_event = threading.Event()
        rt.cfg = {"host": {"port": 5700, "frp": {"public_addr": "127.0.0.1:1"},
                           "auth": {}}}
        rt._net_status = {"state": "unknown", "detail": "", "ts": 0.0}

        class _Frp:
            def is_running(self):
                return running

        rt.frp = _Frp()
        rt.set_net_status = lambda s, d: rt._net_status.update(
            {"state": s, "detail": d, "ts": time.time()})
        return rt

    def test_second_call_does_not_create_second_thread(self):
        rt = self._runtime()
        host._start_tunnel_selfcheck(rt, 5700, delay=0.05)
        th = getattr(rt, "_selfcheck_thread", None)
        self.assertIsNotNone(th, "自检线程未创建")
        rt.stop_event.set()
        th.join(3.0)
        # 线程已退出（frp 仍在跑但 stop_event 置位）：再次调用应允许补建
        host._start_tunnel_selfcheck(rt, 5700, delay=0.05)
        th2 = getattr(rt, "_selfcheck_thread", None)
        self.assertIsNot(th2, th, "线程已退出后不得复用旧句柄（frpc 重启后自检失效）")
        rt.stop_event.set()
        th2.join(3.0)

    def test_ensure_tunnel_selfcheck_is_used_by_gui(self):
        """net-039：GUI「启动隧道」成功回调必须触发补建（静态核对调用点）。"""
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "host_ui.py"), encoding="utf-8").read()
        self.assertIn("ensure_tunnel_selfcheck", src,
                      "GUI 启动 frpc 后不补建自检线程（默认配置下整条自检链不可达）")


# --------------------------------------------------------------- codec-015 / codec-018

class _StaticRuntime:
    def __init__(self):
        self.slot = {"quality": 80}
        self.slot_lock = threading.Lock()
        self.stats_lock = threading.Lock()
        self.stats = {}
        self.force_key = True
        self.woken = 0

        class _Evt:
            def __init__(self, outer):
                self._outer = outer

            def set(self):
                self._outer.woken += 1

        self.enc_evt = _Evt(self)


class _FeStub:
    def __init__(self, force_idr_result):
        self._res = force_idr_result

        class _Enc:
            codec_id = common.CODEC_H264
            bitrate = 2_000_000
            available = False

        self.encoder = _Enc()

    def force_idr(self, frame):
        return self._res


class TestStaticKeyframeJpegFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        host._load_heavy()   # host 的重依赖惰性加载（生产由 main/_load_heavy 提供）

    def test_encoder_unavailable_falls_back_to_jpeg(self):
        """codec-015：force_idr 返回 (None,None) 时必须用缓存帧出 JPEG（否则新观众永久黑屏）。"""
        rt = _StaticRuntime()
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        out = host._encode_static_keyframe(rt, _FeStub((None, None)), frame, 0.0)
        self.assertEqual(out, 0.0)
        with rt.slot_lock:
            jpeg = rt.slot.get("jpeg")
            ts = rt.slot.get("ts", 0)
        self.assertIsInstance(jpeg, bytes, "编码器不可用时没有走 JPEG 出口（新观众永久黑屏）")
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        self.assertGreater(ts, 0, "JPEG 缓存帧必须带新时间戳（否则发送端同帧去重会丢掉）")
        self.assertFalse(rt.force_key, "请求未被消费：每 50ms 重试会反复重编码")
        self.assertGreaterEqual(rt.woken, 1, "没有唤醒发送线程")

    def test_no_cached_frame_keeps_request(self):
        rt = _StaticRuntime()
        host._encode_static_keyframe(rt, _FeStub((None, None)), None, 0.0)
        self.assertTrue(rt.force_key, "无缓存帧时必须保留请求等首帧")


class _ShareStub(share.ScreenShareSession):
    """ScreenShareSession 的替身：只保留 _send_key_response 需要的成员。"""

    _name = "t"

    def __init__(self):
        self._quality = 80
        self.sent = []

    def _send_msg(self, msg):
        self.sent.append(msg)
        return True


class TestShareKeyResponseNoPFrame(unittest.TestCase):
    def test_fallback_sends_jpeg_not_pframe(self):
        """codec-018：force_idr 无输出时兜底必须是自包含 JPEG，不得送 P 帧谎报成功。"""
        sess = _ShareStub()
        sess._fe = _FeStub((None, None))
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        ok = share.ScreenShareSession._send_key_response(
            sess, frame, b"lastpacked", True)
        self.assertTrue(ok)
        self.assertEqual(len(sess.sent), 1)
        self.assertEqual(sess.sent[0][0], common.MSG_FRAME,
                         "兜底送出了视频帧（可能是 P 帧）而不是 JPEG")
        # pack_frame = [kind][4B len][8B ts][JPEG]
        self.assertEqual(sess.sent[0][13:15], b"\xff\xd8", "兜底帧不是 JPEG 数据")


# --------------------------------------------------------------- codec-016/017/019

class TestCodecPresetAndLocks(unittest.TestCase):
    def _encoder(self, preset, sel="auto"):
        enc = codec.VideoEncoder.__new__(codec.VideoEncoder)
        enc._preset = preset
        enc._keyint = 30
        enc._encoder_sel = sel
        return enc

    def test_x264_preset_not_forwarded_to_nvenc(self):
        enc = self._encoder("veryfast")
        opts = enc._build_options("h264_nvenc", 2_000_000)
        self.assertEqual(opts["preset"], codec._NVENC_PRESET,
                         "x264 风格 preset 原样下发 NVENC（open 必失败 → 静默退软件编码）")
        enc2 = self._encoder("p5")
        self.assertEqual(enc2._build_options("h264_nvenc", 2_000_000)["preset"], "p5")

    def test_nvenc_preset_not_forwarded_to_x264(self):
        enc = self._encoder("p5")
        opts = enc._build_options("libx264", 2_000_000)
        self.assertEqual(opts["preset"], codec._X264_PRESET)

    def test_pyav_missing_warns_once(self):
        codec._WARNED_ONCE.discard("av-missing")
        enc = self._encoder("")
        with mock.patch.object(codec, "_AV_AVAILABLE", False):
            self.assertEqual(enc._pick_encoder(), [])
        self.assertIn("av-missing", codec._WARNED_ONCE,
                      "PyAV 缺失时零日志（永久跑 JPEG 却无线索）")

    def test_concurrent_set_bitrate_builds_one_ctx(self):
        """codec-017：两次并发调码率只允许一套上下文构造。"""
        enc = codec.VideoEncoder.__new__(codec.VideoEncoder)
        enc._lock = threading.RLock()
        enc._closed = False
        enc._permafailed = False
        enc._ctx = object()
        enc._last_bitrate = 1_000_000
        enc._reconfiguring = False
        enc._pick_encoder = lambda: ["libx264"]
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def fake_create(names, bps):
            calls.append(bps)
            entered.set()
            release.wait(3.0)
            return None, None

        enc._create_ctx_with = fake_create
        t = threading.Thread(target=enc.set_bitrate, args=(2_000_000,), daemon=True)
        t.start()
        self.assertTrue(entered.wait(3.0), "第一次调码率没有进入构造")
        second = enc.set_bitrate(2_000_000)     # 锁外构造期间的后到者
        release.set()
        t.join(3.0)
        self.assertEqual(calls, [2_000_000], "并发调码率为一次调整各建了一套上下文")
        self.assertFalse(second)


# --------------------------------------------------------------- cfg-010/011/012

class TestConfigWrites(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")
        self._patch = mock.patch.object(common, "_config_path", lambda: self.path)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _tmp_leftovers(self):
        return [f for f in os.listdir(self.tmp.name) if ".tmp" in f]

    def test_baseline_read_failure_abandons_write(self):
        """cfg-010：读基线失败时不得把整份配置塌缩成"只含本进程这一节"。"""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        cfg = {"host": {"port": 6500}}
        self.assertFalse(common.save_config(cfg, "host"))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{ not json", "读不到基线仍覆盖了整份配置")
        self.assertEqual(self._tmp_leftovers(), [])

    def test_first_create_is_atomic(self):
        """cfg-011：首建走 tmp + os.replace（不留 .tmp，内容为默认配置）。"""
        cfg = common.load_config()
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(self._tmp_leftovers(), [], "首建留下临时文件")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["config_version"], cfg["config_version"])

    def test_non_oserror_write_failure_cleans_tmp(self):
        """cfg-012：json.dump 抛非 OSError 时也要清掉临时文件（含明文 token 残渣）。"""
        cfg = {"host": {"port": 6500}, "viewer": {"junk": "\ud800"}}
        common.save_config(cfg, "viewer")   # 不应抛
        self.assertEqual(self._tmp_leftovers(), [], "写失败留下含明文配置的临时文件")

    def test_corrupt_recovery_is_atomic(self):
        """cfg-011：损坏恢复写回默认配置同样原子（不留 tmp、留存损坏件）。"""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ nope")
        cfg = common.load_config()
        self.assertEqual(cfg["config_version"], common.DEFAULT_CONFIG["config_version"])
        self.assertEqual(self._tmp_leftovers(), [])
        names = os.listdir(self.tmp.name)
        self.assertTrue(any(".corrupt-" in n for n in names), "损坏件未留存")


# --------------------------------------------------------------- cfg-014 / cfg-015

class TestListenHostNormalization(unittest.TestCase):
    def test_whitespace_padded_host_is_normalized(self):
        """cfg-014：粘贴成 " 0.0.0.0" 必须仍按全网卡处理（旧实现原样交给 bind）。"""
        host_str, port, bind_all = host._listen_target(
            {"host": {"listen_host": " 0.0.0.0", "port": 5700}})
        self.assertEqual(host_str, "0.0.0.0")
        self.assertTrue(bind_all)
        host_str2, _, bind_all2 = host._listen_target(
            {"host": {"listen_host": " 127.0.0.1 ", "port": 5700}})
        self.assertEqual(host_str2, "127.0.0.1")
        self.assertFalse(bind_all2)


class TestUpgradeNotesVsChanges(unittest.TestCase):
    def test_informational_note_is_not_a_change(self):
        """cfg-015：本就合规的 v1 配置（回环 + frp）不得落盘/告警成"配置已升级"。"""
        raw = {"config_version": 1,
               "host": {"listen_host": "127.0.0.1",
                        "auth": {"enabled": True},
                        "frp": {"token": "t", "tunnel_ids": "1"}}}
        changes, notes = common._upgrade_config(raw)
        self.assertEqual(changes, [], "纯说明被当成迁移改动（误导排障）")
        self.assertEqual(len(notes), 1, "说明性文案被丢弃")


# --------------------------------------------------------------- capture-008/009/010

class TestCaptureFixes(unittest.TestCase):
    def test_negative_region_allowed(self):
        """capture-008：副屏在主屏左侧时坐标准为负，不得判非法。"""
        got = common.normalize_region({"left": -1920, "top": -200,
                                       "width": 800, "height": 600})
        self.assertEqual(got, {"left": -1920, "top": -200, "width": 800, "height": 600})
        self.assertIsNone(common.normalize_region({"left": 0, "top": 0,
                                                   "width": 0, "height": 600}))

    def test_share_rebuild_threshold_is_time_based(self):
        """capture-009：share 侧空帧重建改为时间判据（与 host 对齐），不再每 1.5 秒一次。"""
        class _Cap:
            def grab(self):
                return None

            def close(self):
                pass

            def set_params(self, *a):
                pass

            def working(self):
                return True

        src = pipeline.CaptureSource(None, 1, None, capture=_Cap(), owned=True,
                                     none_rebuild_s=5.0)
        now = time.monotonic()
        src._nones = 150                 # 旧口径 max(10, fps*5)=150 帧 ≈1.5 秒即触发
        src._none_since = now - 1.5
        self.assertFalse(src._should_rebuild_none(now),
                         "仍是按帧数触发（静止屏幕下每 1.5 秒重建 DXGI）")
        src._none_since = now - 5.1
        self.assertTrue(src._should_rebuild_none(now))

    def test_pick_output_warn_once_is_module_level(self):
        """capture-010：告警标记必须在模块级（每次重建新建实例仍只报一次）。"""
        screen._PICK_WARNED.discard("no-match-1")
        records = []

        class _Handler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Handler()
        logger_mod.get_logger().addHandler(handler)
        self.addCleanup(logger_mod.get_logger().removeHandler, handler)

        def _make():
            cap = screen.CaptureManager.__new__(screen.CaptureManager)
            cap.monitor = 1
            # capture-012：`_pick_output_idx` 先判 `_mss is None`（几何无基准），
            # 再取矩形与 DXGI 输出做几何匹配。本用例测的是后者——几何不匹配时
            # 返回 None 且告警只报一次——所以必须给出可用的 mss 句柄。
            cap._mss = object()
            cap._monitor_rect = lambda m: (0, 0, 1920, 1080)
            return cap

        class _Desc:
            class DesktopCoordinates:
                left, top, right, bottom = 10, 10, 100, 100

        class _Out:
            desc = _Desc()

        fake_dxcam = type(os.sys)("dxcam")
        fake_dxcam._DXFactory__factory = type("F", (), {"outputs": [[_Out()]]})()
        with mock.patch.dict(os.sys.modules, {"dxcam": fake_dxcam}):
            self.assertIsNone(_make()._pick_output_idx())
            self.assertIsNone(_make()._pick_output_idx())
        warns = [r for r in records if "没有与显示器" in r.getMessage()]
        self.assertEqual(len(warns), 1, "每次重建都重报一条（capture-006 的'只报一次'失效）")


# --------------------------------------------------------------- dist-018/019

class TestLogPruning(unittest.TestCase):
    def test_prune_skips_undeletable_and_continues(self):
        """dist-019：首个删不掉的文件不得让整轮清理退出。

        ui-023（2026-10-09，回归夹具修正）：原夹具把 125 个文件命名成
        `app_20260901..28.log`，而 cutoff 是「今天往前推 _KEEP_DAYS-1 天」。
        这些日期全部早于 cutoff，于是阶段①（按日期整组删）把它们一次删光，
        阶段②（120 文件/64MiB 上界）**一个文件都没见过**——while 条件根本不成立，
        dist-019 要守的那段 `except OSError: continue` 从未执行过一次。
        同函数同日换一组"当天"日期即崩（NameError），说明这条用例一直是假绿。

        这里改用当天日期，让文件真正进入阶段②；再用 mock 让指定文件删不掉，
        保证错误分支在任何文件系统上都确定被执行（真实句柄锁依赖 Windows 语义）。
        """
        logs = tempfile.TemporaryDirectory()
        self.addCleanup(logs.cleanup)
        today = time.strftime("%Y%m%d")
        created = logger_mod._KEEP_FILES + 5
        names = []
        for i in range(created):
            name = "app_%s_pid%03d.log" % (today, i)
            with open(os.path.join(logs.name, name), "w", encoding="utf-8") as f:
                f.write("x" * 16)
            names.append(name)
        # 人为让排序最前的那个删不掉：阶段②按 (日期, 文件名) 排序后 pop(0)，
        # 所以 names[0] 正是"第一个候选"。
        locked = os.path.join(logs.name, names[0])
        real_remove = os.remove

        def remove_or_fail(path):
            if os.path.abspath(path) == os.path.abspath(locked):
                raise OSError(13, "Permission denied", path)
            return real_remove(path)

        # 构造 handler 本身就会调一次 _prune_old（logger.py 的 __init__），
        # 所以这里只需等它构造完，无需再手动触发。
        with mock.patch("logger.os.remove", remove_or_fail):
            handler = logger_mod._DateRotatingFileHandler(logs.name)
            self.addCleanup(handler.close)

        remaining = [f for f in os.listdir(logs.name) if f.endswith(".log")]
        self.assertIn(names[0], remaining,
                      "被占用的文件本就不该被删掉，测试夹具没生效")
        deleted = created - len(remaining)
        # R8-D-04（2026-10-10）：原来断言 `deleted >= 2`，比本夹具的确定值宽一倍——
        # "删到 2 个就提前收工"这一整类改动照样绿，而 dist-019 要守的恰恰是"不能提前收工"。
        # 确定值：created = _KEEP_FILES + 5 个当天文件进阶段②，其中 1 个被 mock 成删不掉，
        # 阶段②保留上界 _KEEP_FILES → 应删 created - 1 - _KEEP_FILES = 4 个。
        # （审查实测 `_tmp_audit_dist_prune_exact.py`：created=125、remaining=121、deleted=4。）
        self.assertEqual(
            deleted, created - 1 - logger_mod._KEEP_FILES,
            "阶段②没删到确定值 %d 个（提前退出或多删）：created=%d remaining=%d deleted=%d"
            % (created - 1 - logger_mod._KEEP_FILES, created, len(remaining), deleted))

    def test_prune_reports_skip_without_crash(self):
        """ui-023：阶段②跳过分支不得引用未定义名。

        旧实现写的是 `log.debug(...)`，而 `log` 在 logger 模块从未定义（ruff F821）。
        NameError 从 `_DateRotatingFileHandler.__init__` 一路冒到 setup_logger 的
        `except Exception: pass` 被吞掉——文件 handler 根本没挂上，而 README 恰恰
        引导用户提交 logs\\app_*.log 排障。此用例直接断言"跳过时构造不抛"。
        """
        logs = tempfile.TemporaryDirectory()
        self.addCleanup(logs.cleanup)
        today = time.strftime("%Y%m%d")
        created = logger_mod._KEEP_FILES + 5
        for i in range(created):
            with open(os.path.join(logs.name, "app_%s_pid%03d.log" % (today, i)),
                      "w", encoding="utf-8") as f:
                f.write("x" * 16)
        locked = os.path.join(logs.name, "app_%s_pid000.log" % today)
        real_remove = os.remove

        def remove_or_fail(path):
            if os.path.abspath(path) == os.path.abspath(locked):
                raise OSError(13, "Permission denied", path)
            return real_remove(path)

        with mock.patch("logger.os.remove", remove_or_fail):
            handler = logger_mod._DateRotatingFileHandler(logs.name)  # 旧实现抛 NameError
            handler.close()

    def test_diag_zips_are_pruned(self):
        """dist-018：logs 目录里的诊断包也在回收口径内。"""
        logs = tempfile.TemporaryDirectory()
        self.addCleanup(logs.cleanup)
        for i in range(logger_mod._KEEP_DIAGS + 3):
            p = os.path.join(logs.name, "TeamVision-diag-20260920-0000%02d-pid1.zip" % i)
            with open(p, "wb") as f:
                f.write(b"PK")
            os.utime(p, (time.time() + i, time.time() + i))   # 新的在后
        lg = logger_mod._DateRotatingFileHandler(logs.name)
        lg._prune_old()
        zips = [f for f in os.listdir(logs.name) if f.endswith(".zip")]
        self.assertEqual(len(zips), logger_mod._KEEP_DIAGS)


# --------------------------------------------------------------- net-041

class TestRttMonotonic(unittest.TestCase):
    def test_ping_pairing_uses_monotonic(self):
        ch = viewer.Channel.__new__(viewer.Channel)
        ch._ping_mono = {}
        ch.round_trip_ms = 0.0
        t = time.time_ns()
        viewer.Channel._note_ping_sent(ch, t, time.monotonic() - 0.05)
        t0 = viewer.Channel._pop_ping_sent(ch, t)
        self.assertIsNotNone(t0)
        rtt = (time.monotonic() - t0) * 1000.0
        self.assertGreater(rtt, 0, "配对失败时 RTT 会走墙钟差（时钟回拨即假显示）")
        self.assertIsNone(viewer.Channel._pop_ping_sent(ch, t), "配对项未弹出（表会涨）")

    def test_table_is_bounded(self):
        ch = viewer.Channel.__new__(viewer.Channel)
        ch._ping_mono = {}
        for i in range(10):
            viewer.Channel._note_ping_sent(ch, 1000 + i, time.monotonic())
        self.assertLessEqual(len(ch._ping_mono), 4)


# --------------------------------------------------------------- T-18

class TestRunnerContract(unittest.TestCase):
    """T-18：日常用 pytest、发布/CI 用 unittest discover 两套 runner 无交叉——
    `unittest discover` 对模块级 pytest 风格用例**静默不收集且 RC=0**，
    run_tests.bat 仍打印"全部通过"。这里把它变成显式失败信号。"""

    def test_no_module_level_pytest_style_cases(self):
        import ast
        import glob
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for path in glob.glob(os.path.join(repo, "tests", "test*.py")):
            with open(path, encoding="utf-8-sig") as f:   # 容忍 BOM（test_item93 带 BOM）
                tree = ast.parse(f.read(), filename=path)
            for node in tree.body:      # 只看模块顶层
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))                         and node.name.startswith("test"):
                    offenders.append("%s::%s" % (os.path.basename(path), node.name))
        self.assertEqual(offenders, [],
                         "模块级 pytest 风格用例不会被 unittest discover 收集"
                         "（run_tests.bat/CI 会静默假绿）；请写成 TestCase 方法：" +
                         ", ".join(offenders))


if __name__ == "__main__":
    unittest.main()
