# -*- coding: utf-8 -*-
"""阶段 2 回归：第 110/111/112 条 —— 三条流水线统一 + ABR 闭环。

第 110 条（编码/回退）与第 111 条（采集恢复 + 静止门控）统一到 pipeline.py；第 112 条
（C1）把 ABR 从开环改成闭环：按实测吞吐定码率、锁外预建上下文、只在真重建时打日志。
"""
import inspect
import threading
import time
import unittest
from unittest import mock

import numpy as np

import codec
import host
import pipeline
import share


def _frame(w=64, h=48):
    return np.zeros((h, w, 3), np.uint8)


class _DeadEncoder:
    """构造永远"成功"但不可用的编码器（模拟 NVENC/x264 全部打不开）。"""

    def __init__(self, *args, **kwargs):
        _DeadEncoder.calls.append(args)
        self.available = False
        self.name = "dead"

    calls = []

    def close(self):
        pass


class _LiveEncoder:
    """可用编码器替身：记录 encode/set_fps/set_bitrate 调用。"""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.available = True
        self.name = "fake"
        self.codec_id = 1
        self.bitrate = kwargs.get("bitrate", args[3] if len(args) > 3 else 0)
        self.keyframe_sent = 0
        self.encoded = []
        self.fps_calls = []
        self.closed = False

    def set_fps(self, fps):
        self.fps_calls.append(fps)

    def set_bitrate(self, bps):
        self.bitrate = bps
        return True

    def encode(self, frame, raw_ts=0, force_key=False):
        self.encoded.append((raw_ts, force_key))
        self.keyframe_sent += 1 if force_key else 0
        return (b"nal", bool(force_key), 1.5, raw_ts)

    def close(self):
        self.closed = True


class TestEncoderBackoff(unittest.TestCase):
    """构造失败负缓存：退避期内绝不重复构造（host 此前每帧都重建）。"""

    def test_failed_construction_is_negatively_cached(self):
        now = [0.0]
        _DeadEncoder.calls = []
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _DeadEncoder), \
                mock.patch.object(pipeline.time, "monotonic", lambda: now[0]):
            fe = pipeline.FrameEncoder(fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="auto")
            for i in range(10):
                ef = fe.encode(_frame(), i)
                self.assertIsNotNone(ef, "回退 JPEG 失败，本帧未产出")
                self.assertFalse(ef.is_video, "编码器不可用时应走 JPEG 回退")
            self.assertEqual(len(_DeadEncoder.calls), 1,
                             "退避期内重复构造了编码器（第 53 条：每帧新建会打满 CPU）")
            # 首次退避 1.0 秒到期后允许再试一次，然后翻倍到 2.0 秒
            now[0] = 1.0
            fe.encode(_frame(), 11)
            self.assertEqual(len(_DeadEncoder.calls), 2)
            now[0] = 2.9
            fe.encode(_frame(), 12)
            self.assertEqual(len(_DeadEncoder.calls), 2, "第二次退避（2 秒）未生效")
            now[0] = 3.0
            fe.encode(_frame(), 13)
            self.assertEqual(len(_DeadEncoder.calls), 3)

    def test_backoff_caps_at_30_seconds(self):
        now = [0.0]
        _DeadEncoder.calls = []
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _DeadEncoder), \
                mock.patch.object(pipeline.time, "monotonic", lambda: now[0]):
            fe = pipeline.FrameEncoder(fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="auto")
            for _ in range(10):
                fe.encode(_frame(), 0)
                now[0] += 100.0        # 每次都远超退避窗口，必然重试
            self.assertEqual(fe.backoff, 30.0, "退避未封顶 30 秒")

    def test_construction_exception_also_backs_off(self):
        calls = []

        def _boom(*a, **kw):
            calls.append(a)
            raise RuntimeError("PyAV 内部错误")

        now = [0.0]
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _boom), \
                mock.patch.object(pipeline.time, "monotonic", lambda: now[0]):
            fe = pipeline.FrameEncoder(fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="auto")
            for i in range(5):
                ef = fe.encode(_frame(), i)
                self.assertIsNotNone(ef)
                self.assertFalse(ef.is_video)
        self.assertEqual(len(calls), 1, "构造抛异常后未进入退避（每帧都会重试）")

    def test_jpeg_selector_never_builds_video_encoder(self):
        _DeadEncoder.calls = []
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _DeadEncoder):
            fe = pipeline.FrameEncoder(fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="jpeg")
            ef = fe.encode(_frame(), 1)
        self.assertFalse(ef.is_video)
        self.assertEqual(_DeadEncoder.calls, [], "encoder=jpeg 时不应尝试视频编码器")


class TestEncoderLifecycle(unittest.TestCase):
    """可用编码器：只构造一次、帧率变化调 set_fps、on_build 回调一次。"""

    def test_builds_once_and_reports_via_callback(self):
        built = []
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _LiveEncoder):
            fe = pipeline.FrameEncoder(
                fps=30, bitrate=1000, keyint=30,
                encoder_sel="auto",
                on_build=lambda enc, w, h, br: built.append((w, h, br)))
            ef1 = fe.encode(_frame(), 1)
            ef2 = fe.encode(_frame(), 2)
        self.assertTrue(ef1.is_video and ef2.is_video)
        self.assertEqual(len(built), 1, "编码器被重复构造")
        self.assertEqual(built[0][2], 1000)
        self.assertIs(fe.encoder, fe.encoder)

    def test_fps_change_reconfigures_encoder(self):
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _LiveEncoder):
            fe = pipeline.FrameEncoder(fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="auto")
            fe.encode(_frame(), 1, fps=30)
            enc = fe.encoder
            fe.encode(_frame(), 2, fps=15)
        self.assertEqual(enc.fps_calls, [15], "帧率档变化未调用 set_fps")

    def test_encode_error_is_swallowed_and_reported(self):
        class _RaisingEncoder(_LiveEncoder):
            def encode(self, frame, raw_ts=0, force_key=False):
                raise RuntimeError("GPU 重置")

        errors = []
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _RaisingEncoder):
            fe = pipeline.FrameEncoder(fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="auto",
                                       on_error=errors.append)
            ef = fe.encode(_frame(), 1)          # 不得抛（第 90 条）
        self.assertIsNone(ef)
        self.assertEqual(len(errors), 1)

    def test_force_idr_without_encoder_returns_none(self):
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _DeadEncoder):
            fe = pipeline.FrameEncoder(fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="auto")
            fe.encode(_frame(), 1)
            res, err = fe.force_idr(_frame())
        self.assertIsNone(res)
        self.assertIsNone(err)

    def test_force_idr_reports_exception_instead_of_raising(self):
        class _RaisingEncoder(_LiveEncoder):
            def encode(self, frame, raw_ts=0, force_key=False):
                raise RuntimeError("GPU 重置")

        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _RaisingEncoder):
            fe = pipeline.FrameEncoder(fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="auto")
            fe.encode(_frame(), 1)
            res, err = fe.force_idr(_frame())    # 不得抛
        self.assertIsNone(res)
        self.assertIsInstance(err, RuntimeError)

    def test_close_releases_encoder(self):
        with mock.patch.object(pipeline.codec_mod, "VideoEncoder", _LiveEncoder):
            fe = pipeline.FrameEncoder(fps=30, bitrate=1000,
                                       keyint=30, encoder_sel="auto")
            fe.encode(_frame(), 1)
            enc = fe.encoder
            fe.close()
        self.assertTrue(enc.closed)
        self.assertIsNone(fe.encoder)


class TestSharedEncoderWiring(unittest.TestCase):
    """host 与 share 都只经 pipeline.FrameEncoder 使用编码器（不再各自实现）。"""

    def test_host_uses_pipeline_encoder(self):
        src = inspect.getsource(host)
        self.assertIn("pipeline.FrameEncoder", src)
        self.assertNotIn("codec_mod.VideoEncoder", src)

    def test_share_uses_pipeline_encoder(self):
        src = inspect.getsource(share)
        self.assertIn("pipeline.FrameEncoder", src)
        self.assertNotIn("codec_mod.VideoEncoder", src)
        self.assertNotIn("_enc_retry_at", src, "share 自维护的退避状态应已删除")

    def test_share_session_holds_shared_encoder(self):
        sess = share.ScreenShareSession(
            None, None, {"host": {"fps": 5, "jpeg_quality": 50,
                                  "codec": {"encoder": "jpeg", "target_width": 0}}},
            "t", capture=mock.MagicMock())
        self.assertIsInstance(sess._fe, pipeline.FrameEncoder)


class _FakeCap:
    """可编程伪采集器：按脚本返回帧/None/抛异常，并记录 close 与 set_params。"""

    def __init__(self, script=None, working=True):
        self.script = list(script or [])
        self.working = working
        self.closed = False
        self.set_params_calls = []

    def grab(self):
        item = self.script.pop(0) if self.script else None
        if isinstance(item, Exception):
            raise item
        return item

    def set_params(self, backend, monitor, region):
        self.set_params_calls.append((backend, monitor, region))

    def close(self):
        self.closed = True


class TestCaptureSourceRecovery(unittest.TestCase):
    """第 111 条：采集恢复机制统一到 pipeline.CaptureSource（host 与 share 共用）。"""

    def setUp(self):
        # 这些用例不真的等待恢复退避；只验证状态机与阈值
        self._sleep = mock.patch.object(pipeline.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def test_error_path_rebuilds_after_threshold(self):
        """连续 5 次 grab 异常 → 重建采集器，旧句柄必须关闭（否则句柄泄漏）。"""
        bad = _FakeCap([RuntimeError("dxgi lost")] * 10)
        good = _FakeCap([_frame()])
        built = [bad, good]
        with mock.patch.object(pipeline, "CaptureManager",
                               side_effect=lambda *a: built.pop(0)):
            src = pipeline.CaptureSource("dxgi", 1, None)
            self.assertIs(src.cap, bad)
            for _ in range(4):
                self.assertIsNone(src.grab())      # 阈值内只计数
            self.assertIs(src.cap, bad, "未达阈值就重建了采集器")
            self.assertIsNone(src.grab())          # 第 5 次触发重建
            self.assertIs(src.cap, good, "达到阈值后未重建采集器")
            self.assertIsNotNone(src.grab())       # 新句柄可正常抓帧
        self.assertTrue(bad.closed, "旧采集句柄未关闭（句柄泄漏）")

    def test_injected_capture_is_never_rebuilt(self):
        """owned=False（注入的伪采集器）时永不重建——重建会把替身换成真实后端。"""
        bad = _FakeCap([RuntimeError("boom")] * 20)
        with mock.patch.object(pipeline, "CaptureManager",
                               side_effect=AssertionError("不应重建注入的采集器")):
            src = pipeline.CaptureSource(capture=bad, owned=False)
            for _ in range(10):
                self.assertIsNone(src.grab())
        self.assertIs(src.cap, bad)
        self.assertFalse(bad.closed)

    def test_none_rebuild_by_frame_threshold(self):
        """share 策略：连续 N 帧无画面即重建（不要求后端自报失效）。"""
        cap = _FakeCap([None] * 3)
        new = _FakeCap([_frame()])
        built = [cap, new]
        with mock.patch.object(pipeline, "CaptureManager",
                               side_effect=lambda *a: built.pop(0)):
            src = pipeline.CaptureSource("dxgi", 1, None, none_rebuild_frames=3)
            self.assertIs(src.cap, cap)
            self.assertIsNone(src.grab())
            self.assertIsNone(src.grab())
            self.assertIs(src.cap, cap, "未达帧数阈值就重建了")
            self.assertIsNone(src.grab())          # 第 3 帧触发重建
            self.assertIs(src.cap, new)

    def test_none_rebuild_requires_dead_when_configured(self):
        """host 策略：后端自报可用时长时间空帧也不重建（静止/独占全屏属正常语义）。"""
        alive = _FakeCap([None] * 20, working=True)
        with mock.patch.object(pipeline, "CaptureManager",
                               side_effect=AssertionError("后端可用时不应重建")):
            src = pipeline.CaptureSource(capture=alive, owned=True, none_rebuild_frames=3,
                                         none_rebuild_requires_dead=True)
            for _ in range(10):
                self.assertIsNone(src.grab())
        self.assertIs(src.cap, alive)

    def test_dead_backend_is_rebuilt_and_reported(self):
        """后端 working=False 且空帧超时 → 重建并上报（否则画面永久冻结且日志无痕）。"""
        dead = _FakeCap([None] * 10, working=False)
        new = _FakeCap([_frame()])
        built = [new]      # 注入句柄不走 CaptureManager 构造，这里只排"重建"那一次
        clock = [0.0]
        with mock.patch.object(pipeline, "CaptureManager",
                               side_effect=lambda *a: built.pop(0)), \
                mock.patch.object(pipeline.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(pipeline.log, "error") as err:
            src = pipeline.CaptureSource(capture=dead, owned=True, none_rebuild_s=5.0,
                                         none_rebuild_requires_dead=True)
            self.assertIsNone(src.grab())          # t=0，空帧起始
            self.assertIs(src.cap, dead)
            clock[0] = 4.9                         # 未满 5 秒
            self.assertIsNone(src.grab())
            self.assertIs(src.cap, dead)
            clock[0] = 5.1                         # 满 5 秒且后端已死 → 重建
            self.assertIsNone(src.grab())
            self.assertIs(src.cap, new)
        self.assertTrue(any("重建采集器" in " ".join(str(a) for a in c.args)
                            for c in err.call_args_list),
                        "后端已死的重建未上报（error 级）")

    def test_long_none_warns_only_once(self):
        """长期空帧：超过 none_warn_s 只告警一次（不重建、不刷屏）。"""
        cap = _FakeCap([None] * 20, working=True)
        clock = [0.0]
        with mock.patch.object(pipeline.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(pipeline.log, "warning") as warn:
            src = pipeline.CaptureSource(capture=cap, owned=False, none_warn_s=30.0)
            for i in range(6):
                clock[0] = i * 10.0
                src.grab()
        hits = [c for c in warn.call_args_list
                if "采集不到新帧" in str(c.args[0])]
        self.assertEqual(len(hits), 1, "长期空帧告警应只发一次（每帧刷屏会淹没日志）")

    def test_init_failure_reports_unusable(self):
        """采集器构造抛异常 → working() 为 False（share 据此报错停止，不再静默假活）。"""
        with mock.patch.object(pipeline, "CaptureManager",
                               side_effect=RuntimeError("两个后端都挂了")):
            src = pipeline.CaptureSource("dxgi", 1, None)
        self.assertIsNone(src.cap)
        self.assertFalse(src.working())

    def test_missing_handle_retries_then_rebuilds_without_attributeerror(self):
        """句柄缺失（初始化失败）时给出明确日志，累计到阈值后重建，不抛 AttributeError。"""
        built = [_FakeCap(), _FakeCap([_frame()])]
        with mock.patch.object(pipeline, "CaptureManager",
                               side_effect=lambda *a: built.pop(0)), \
                mock.patch.object(pipeline.log, "warning") as warn, \
                mock.patch.object(pipeline.log, "error") as err:
            src = pipeline.CaptureSource("dxgi", 1, None)
            src.cap = None                      # 模拟构造后句柄仍缺失
            for _ in range(4):
                self.assertIsNone(src.grab())
            self.assertIsNone(src.cap, "未达阈值就重建了")
            self.assertIsNone(src.grab())       # 第 5 次触发重建（消耗 built[1]）
            self.assertIsNotNone(src.cap)
        msgs = " ".join(str(a) for c in warn.call_args_list + err.call_args_list
                        for a in c.args)
        self.assertIn("采集句柄缺失", msgs)
        self.assertNotIn("AttributeError", msgs)

    def test_working_reflects_handle_state(self):
        self.assertTrue(pipeline.CaptureSource(capture=_FakeCap(), owned=False).working())
        self.assertFalse(
            pipeline.CaptureSource(capture=_FakeCap(working=False), owned=False).working())

    def test_set_params_forwards_and_counts_failure(self):
        cap = _FakeCap()
        src = pipeline.CaptureSource(capture=cap, owned=False)
        self.assertTrue(src.set_params("mss", 2, None))
        self.assertEqual(cap.set_params_calls, [("mss", 2, None)])

        class _NoParams:
            def set_params(self, *a):
                raise RuntimeError("参数不合法")

        bad = pipeline.CaptureSource(capture=_NoParams(), owned=False)
        self.assertFalse(bad.set_params("dxgi", 9, None))

    def test_close_closes_injected_handle_and_is_idempotent(self):
        cap = _FakeCap()
        src = pipeline.CaptureSource(capture=cap, owned=False)
        src.close()
        src.close()
        self.assertTrue(cap.closed)
        self.assertIsNone(src.cap)


class TestStillGate(unittest.TestCase):
    """第 111 条：静止门控状态机统一到 pipeline.StillGate（host 与 share 共用）。"""

    def _static(self):
        img = np.full((90, 160, 3), 100, np.uint8)
        img[::4, :, :] = 200
        return img

    def test_static_then_motion_edges(self):
        clock = [0.0]
        edges = []
        with mock.patch.object(pipeline.time, "perf_counter", lambda: clock[0]):
            gate = pipeline.StillGate(10, 0.005, 0.05, on_edge=edges.append)
            self.assertTrue(gate.check(self._static()), "首帧无参考图应视为有变化")
            gate.check(self._static())                 # t=0：无变化，计时开始
            self.assertFalse(gate.in_still)
            clock[0] = 0.02
            gate.check(self._static())
            self.assertFalse(gate.in_still, "未满 quiet_s 不应判定静止")
            clock[0] = 0.06
            gate.check(self._static())
            self.assertTrue(gate.in_still, "连续无变化满 quiet_s 应判定静止")
            self.assertEqual(edges, [True], "静止边沿只应上报一次")
            clock[0] = 0.20
            gate.check(self._static())
            self.assertEqual(edges, [True], "静止态持续期间不应重复上报边沿")
            moving = self._static()
            moving[::4, :, :] = 5                      # 明显变化
            self.assertTrue(gate.check(moving))
            self.assertFalse(gate.in_still, "有变化应立即恢复发送")
            self.assertEqual(edges, [True, False])

    def test_probe_exception_counts_as_motion(self):
        """探测异常按"有变化"处理：宁可多发一帧，不可画面冻结。"""
        gate = pipeline.StillGate(10, 0.005, 0.05)
        with mock.patch.object(pipeline, "motion_changed",
                               side_effect=RuntimeError("cv2 炸了")):
            self.assertTrue(gate.check(self._static()))
        self.assertFalse(gate.in_still)


class TestSharedCaptureWiring(unittest.TestCase):
    """host 与 share 都只经 pipeline.CaptureSource/StillGate 采集（不再各自实现）。"""

    def test_host_uses_pipeline_capture(self):
        src = inspect.getsource(host)
        self.assertIn("pipeline.CaptureSource", src)
        self.assertIn("pipeline.StillGate", src)
        self.assertNotIn("_grab_with_recovery", src)

    def test_share_uses_pipeline_capture(self):
        src = inspect.getsource(share)
        self.assertIn("pipeline.CaptureSource", src)
        self.assertIn("pipeline.StillGate", src)
        self.assertNotIn("CaptureManager(", src, "share 不再自行构造采集器")

    def test_share_run_uses_capture_source_with_injected_capture(self):
        """注入伪采集器的会话：_run 用 owned=False 的 CaptureSource 包它，结束时释放。"""
        cap = _FakeCap()
        sess = share.ScreenShareSession(
            None, None, {"host": {"fps": 5, "jpeg_quality": 50,
                                  "codec": {"encoder": "jpeg", "target_width": 0}}},
            "t", capture=cap)
        self.assertFalse(sess._cap_owned)
        with mock.patch.object(pipeline, "CaptureManager",
                               side_effect=AssertionError("不应构造真实采集器")):
            sess._stop.set()          # 置停止位：主循环立即退出，直达 finally
            sess._run()
        self.assertTrue(cap.closed, "_run 结束时必须释放注入的采集句柄")
        self.assertIsNone(sess._src, "已释放的 CaptureSource 不应继续被持有")
        self.assertIsNone(sess._cap)


class _StubRuntime:
    """ABR 相关函数的最小运行环境（slot/lock/stats/clients 都齐但都是替身）。"""

    def __init__(self, cur_bitrate=6_000_000):
        self.slot_lock = threading.Lock()
        self.stats_lock = threading.Lock()
        self.codec_lock = threading.Lock()
        self.clients_lock = threading.Lock()
        self.slot = {"quality": 80, "scale": 1.0, "fps": 30, "bitrate": cur_bitrate,
                     "out_w": 854, "out_h": 480}
        self.stats = {"avg_send_ms": 0.0, "abr_changes": 0}
        self.clients = {}
        self.video_encoder = None
        self.target_bitrate = cur_bitrate
        self.force_key = False
        self.cfg = {"host": {}}


class _StubParams:
    """PipelineParams 的替身：只带 ABR 用到的边界值。"""

    min_bitrate = 400_000
    max_bitrate = 6_000_000
    fps_min = 10
    quality_min = 40
    scale_min = 0.25


class _StubClient:
    """ClientInfo 替身：只提供 _perf_log_and_bandwidth 需要的方法。"""

    def __init__(self, kbps=0.0, send_ms=0.0, drop=False):
        self.addr = "stub"
        self.username = ""
        self.bytes_sent = 0
        self.dropped_frames = 0
        self._lock = threading.Lock()
        self._kbps = kbps
        self._send_ms = send_ms
        self._drop = drop

    def rate_kbps(self, now=None, window=1.0):
        return self._kbps

    def recent_send_ms(self, now=None, window=2.0):
        return self._send_ms

    def has_recent_drop(self, now=None):
        return self._drop


class TestAbrClosedLoop(unittest.TestCase):
    """第 112 条（C1）：按实测吞吐定码率 + 只在真重建时打日志。"""

    def setUp(self):
        patcher = mock.patch.object(host, "_set_encoder_bitrate")
        self.set_bitrate = patcher.start()
        self.addCleanup(patcher.stop)

    def _stats(self, **over):
        cur = {"quality": 80, "scale": 1.0, "fps_now": 30, "video_mode": True,
               "cur_bitrate": 3_000_000, "egress_bps": 0}
        cur.update(over)
        return cur

    def test_degrade_uses_measured_throughput(self):
        """链路实测只有 800 Kbps 时直接降到 ~720 Kbps，而不是从 6000 连乘 0.7。"""
        runtime = _StubRuntime()
        self.set_bitrate.return_value = True
        host._abr_degrade(runtime, _StubParams(), self._stats(egress_bps=800_000),
                          effective_send_ms=120.0, recent_drop=False)
        bps = self.set_bitrate.call_args.args[1]
        self.assertEqual(bps, int(800_000 * 0.9))
        # 实测吞吐高于 ×0.7 档位时仍按 ×0.7 小步降级
        self.set_bitrate.reset_mock()
        host._abr_degrade(runtime, _StubParams(),
                          self._stats(egress_bps=5_000_000, cur_bitrate=6_000_000),
                          effective_send_ms=120.0, recent_drop=False)
        self.assertEqual(self.set_bitrate.call_args.args[1], int(6_000_000 * 0.7))

    def test_degrade_respects_floor(self):
        """实测吞吐低于下限时不得越过 min_bitrate。"""
        runtime = _StubRuntime()
        self.set_bitrate.return_value = True
        host._abr_degrade(runtime, _StubParams(),
                          self._stats(cur_bitrate=420_000, egress_bps=10_000),
                          effective_send_ms=100.0, recent_drop=False)
        self.assertEqual(self.set_bitrate.call_args.args[1], _StubParams.min_bitrate)

    def test_degrade_prints_level_only_when_rebuilt(self):
        """未真正重建（滞后带内/冷却中）时不打印"降级"日志，也不谎报码率。"""
        runtime = _StubRuntime()
        self.set_bitrate.return_value = False
        with mock.patch.object(host.log, "warning") as warn:
            host._abr_degrade(runtime, _StubParams(), self._stats(egress_bps=800_000),
                              effective_send_ms=120.0, recent_drop=False)
        self.assertEqual([c for c in warn.call_args_list if "自适应降级" in str(c.args[0])],
                         [], "码率档未重建却打印了降级日志")

    def test_recover_prints_level_only_when_rebuilt(self):
        """回升：码率未真正重建时既不打印也不改动其它参数（旧实现每秒一条假日志）。"""
        runtime = _StubRuntime()
        self.set_bitrate.return_value = False
        with mock.patch.object(host.log, "info") as info:
            host._abr_recover(runtime, _StubParams(), self._stats(), 30, 80, 1.0)
        self.assertEqual([c for c in info.call_args_list if "自适应回升" in str(c.args[0])],
                         [])
        self.set_bitrate.return_value = True
        with mock.patch.object(host.log, "info") as info:
            host._abr_recover(runtime, _StubParams(), self._stats(), 30, 80, 1.0)
        self.assertTrue(any("自适应回升" in str(c.args[0]) and "码率" in str(c.args)
                            for c in info.call_args_list),
                        "真正重建后应打印回升日志（含码率档）")

    def test_recover_skips_bitrate_branch_when_no_rebuild(self):
        """码率分支未重建时应直接返回，不再往下走质量/缩放分支。"""
        runtime = _StubRuntime()
        self.set_bitrate.return_value = False
        with mock.patch.object(host, "_apply_slot_params") as apply_slot:
            host._abr_recover(runtime, _StubParams(), self._stats(), 30, 80, 1.0)
        apply_slot.assert_not_called()

    def test_egress_aggregated_from_client_windows(self):
        """stats["egress_bps"] 来自各客户端 1 秒窗口实测字节数（KB/s → bit/s）。"""
        runtime = _StubRuntime()
        runtime.clients = {("a", 1): _StubClient(kbps=500.0),
                           ("b", 2): _StubClient(kbps=300.0)}
        runtime.stats.update({"capture_fps": 30, "encode_fps": 30, "send_fps": 30,
                              "encode_ms": 1.0, "still": False})
        stats = {"capture_fps": 30, "encode_fps": 30, "send_fps": 30, "encode_ms": 1.0,
                 "avg_send_ms": 0.0, "still": False, "fps_now": 30, "quality": 80,
                 "scale": 1.0, "out_w": 854, "out_h": 480}
        host._perf_log_and_bandwidth(runtime, stats)
        self.assertEqual(stats["egress_bps"], int(800.0 * 8192))
        self.assertEqual(runtime.stats["egress_bps"], int(800.0 * 8192))

    def test_abr_uses_gop_length_window(self):
        """ABR 的实测吞吐必须按 3 秒（≥一个 GOP）取平均，而不是 1 秒瞬时值。"""
        windows = []

        class _WindowSpy(_StubClient):
            def rate_kbps(self, now=None, window=1.0):
                windows.append(window)
                return 100.0

        runtime = _StubRuntime()
        runtime.clients = {("a", 1): _WindowSpy()}
        runtime.stats.update({"capture_fps": 30, "encode_fps": 30, "send_fps": 30,
                              "encode_ms": 1.0, "still": False})
        stats = {"capture_fps": 30, "encode_fps": 30, "send_fps": 30, "encode_ms": 1.0,
                 "avg_send_ms": 0.0, "still": False, "fps_now": 30, "quality": 80,
                 "scale": 1.0, "out_w": 854, "out_h": 480}
        host._perf_log_and_bandwidth(runtime, stats)
        self.assertIn(host._ABR_EGRESS_WINDOW_S, windows,
                      "ABR 未使用 GOP 长度的窗口（关键帧突变成 10 倍跳变会砸穿码率）")
        self.assertIn(1.0, windows, "带宽日志仍应保留 1 秒窗口")


class TestClientRateWindow(unittest.TestCase):
    """第 112 条（C1）：ClientInfo.rate_kbps 的窗口参数与保留时长。"""

    def test_window_selects_and_normalizes(self):
        info = host.ClientInfo(("127.0.0.1", 1))
        info.record_sent(102400, now=0.0)          # 100 KB 全部在 t=0
        self.assertAlmostEqual(info.rate_kbps(now=0.5, window=1.0), 100.0, places=3)
        self.assertAlmostEqual(info.rate_kbps(now=2.5, window=1.0), 0.0, places=3)
        # 3 秒窗口要按 3 秒折算（不是按 1 秒），否则会把突发当成持续速率
        self.assertAlmostEqual(info.rate_kbps(now=2.5, window=3.0), 100.0 / 3.0, places=3)

    def test_samples_are_kept_long_enough_for_abr(self):
        info = host.ClientInfo(("127.0.0.1", 1))
        info.record_sent(1024, now=0.0)
        for t in (1.0, 2.0, 2.5):
            info.record_sent(1024, now=t)
        # 保留窗口必须 ≥ ABR 窗口，否则 3 秒平均永远算不全
        self.assertGreaterEqual(host._RATE_KEEP_S, host._ABR_EGRESS_WINDOW_S)
        self.assertGreater(info.rate_kbps(now=2.5, window=3.0), 0.0)


class _FakeCtx:
    """伪编码器上下文：只提供 _install_ctx/_retire_ctx 用到的属性。"""

    def __init__(self, w=64, h=48):
        self.width = w
        self.height = h
        self.closed = False

    def close(self):
        self.closed = True


class TestCodecBitrateRebuild(unittest.TestCase):
    """第 112 条（C1）：调码率时新上下文在锁外预建，不阻塞编码线程；失败保留旧上下文。"""

    def _patch(self, create):
        """在类上打补丁：`_pick_encoder` 固定返回替身编码器名，`_create_ctx` 用给定函数。

        用 `new=`（而非 side_effect）是为了保留描述符协议——替身函数需要拿到 self 才能
        断言"构造发生在锁外"。
        """
        for target, new in (("_pick_encoder", lambda self: ["fake_enc"]),
                            ("_create_ctx", create)):
            p = mock.patch.object(codec.VideoEncoder, target, new=new)
            p.start()
            self.addCleanup(p.stop)

    def _encoder(self, bitrate=1_000_000):
        """建一个不碰真实编码器的 VideoEncoder，并返回"构造时是否持锁"的记录。"""
        lock_owned = []

        def _create(self, name, bps):
            lock_owned.append(self._lock._is_owned())
            return _FakeCtx()

        self._patch(_create)
        enc = codec.VideoEncoder(64, 48, 30, bitrate, 30, "x264", "")
        self.assertTrue(enc.available, "替身上下文未装上，测试自身失效")
        self.assertEqual(lock_owned, [False], "初始构造不应持锁")
        return enc, lock_owned

    def test_set_bitrate_builds_outside_lock_and_swaps(self):
        made = []
        lock_owned = []

        def _create(self, name, bps):
            lock_owned.append(self._lock._is_owned())
            made.append((name, bps))
            return _FakeCtx()

        self._patch(_create)
        enc = codec.VideoEncoder(64, 48, 30, 1_000_000, 30, "x264", "")
        made.clear()          # 丢掉构造时那一次，只看调码率的构造
        lock_owned.clear()
        old = enc._ctx
        self.assertTrue(enc.set_bitrate(2_000_000))
        self.assertEqual(made, [("fake_enc", 2_000_000)])
        self.assertEqual(lock_owned, [False], "新上下文在锁内构造：编码线程会停等约 100ms")
        self.assertIsNot(enc._ctx, old, "新上下文未安装")
        self.assertTrue(old.closed, "旧上下文未释放（NVENC 会话泄漏）")
        self.assertEqual(enc.bitrate, 2_000_000)

    def test_set_bitrate_within_hysteresis_is_noop(self):
        made = []

        def _create(self, name, bps):
            made.append((name, bps))
            return _FakeCtx()

        self._patch(_create)
        enc = codec.VideoEncoder(64, 48, 30, 1_000_000, 30, "x264", "")
        made.clear()
        self.assertFalse(enc.set_bitrate(1_100_000), "±15% 内不应重建")
        self.assertEqual(made, [])

    def test_failed_rebuild_keeps_working_encoder(self):
        """新码率建不出来时保留旧上下文（旧实现先关后建，会把自己拆掉回退 JPEG）。"""
        self._patch(lambda self, name, bps: _FakeCtx())
        enc = codec.VideoEncoder(64, 48, 30, 1_000_000, 30, "x264", "")
        old = enc._ctx

        def _boom(self, name, bps):
            raise RuntimeError("显存不足")

        with mock.patch.object(codec.VideoEncoder, "_create_ctx", new=_boom):
            self.assertFalse(enc.set_bitrate(3_000_000))
        self.assertIs(enc._ctx, old, "重建失败却丢弃了原本可用的上下文")
        self.assertTrue(enc.available)
        self.assertFalse(old.closed)

    def test_recreate_size_change_drops_old_ctx_on_failure(self):
        """尺寸变化场景仍必须丢弃旧上下文，否则会按旧尺寸继续出帧。"""
        self._patch(lambda self, name, bps: _FakeCtx())
        enc = codec.VideoEncoder(64, 48, 30, 1_000_000, 30, "x264", "")
        old = enc._ctx

        def _boom(self, name, bps):
            raise RuntimeError("尺寸不支持")

        with mock.patch.object(codec.VideoEncoder, "_create_ctx", new=_boom):
            self.assertFalse(enc._recreate(1_000_000))
        self.assertIsNone(enc._ctx)
        self.assertTrue(old.closed)

    def test_encode_does_not_block_while_rebuild_prepares(self):
        """构造耗时（模拟 100ms NVENC 拆装）不应把 encode 挡在锁外。"""
        self._patch(lambda self, name, bps: _FakeCtx())
        enc = codec.VideoEncoder(64, 48, 30, 1_000_000, 30, "x264", "")
        real_ctx = enc._ctx

        def _slow_create(self, name, bps):
            time.sleep(0.15)
            return _FakeCtx()

        started = threading.Event()
        finished = threading.Event()

        def _enc_thread():
            started.set()
            with enc._lock:              # 模拟编码线程正在编码（持锁 50ms）
                time.sleep(0.05)
            finished.set()

        with mock.patch.object(codec.VideoEncoder, "_create_ctx", new=_slow_create):
            th = threading.Thread(target=_enc_thread)
            th.start()
            started.wait(1.0)
            time.sleep(0.01)             # 让编码线程先拿到锁
            t0 = time.perf_counter()
            enc.set_bitrate(4_000_000)   # 预建在锁外：不应等编码线程放锁
            build_ms = (time.perf_counter() - t0) * 1000.0
            th.join(2.0)
        self.assertTrue(finished.is_set())
        self.assertGreaterEqual(build_ms, 100.0, "构造没有被执行（测试自身失效）")
        self.assertIsNotNone(enc._ctx)
        self.assertIsNot(enc._ctx, real_ctx)


if __name__ == "__main__":
    unittest.main()
