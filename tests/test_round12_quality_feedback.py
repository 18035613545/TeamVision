# -*- coding: utf-8 -*-
"""第 12 轮（协议 v2.5）：质量反馈闭环——观看端把"我这边看得卡"报给共享端。

背景：共享端的自适应（ABR）判据只有三个量——发送耗时、本机丢帧、帧龄，
**全都发生在字节离开本机之前**。共享端因此永远看不见"观看端解码跟不上、
下行带宽不够"——而那恰恰是用户抱怨"延迟高/画面卡"时最常见的原因。
观看端其实一直在算这些数（延迟 P50/P95、RTT、帧序号缺口），只是从不外发。

本轮补的闭环：
  ① 观看端周期上报（common.FEEDBACK 字段口径，两端共用一份定义）；
  ② 共享端按**自己的订阅账本**归源、聚合最差反馈、据此驱动 ABR；
  ③ 解码受限（CPU 跟不上）与链路受限（带宽不够）走**不同的降级杠杆**：
     前者降码率无效（解码量由像素数×帧数决定），该削画幅；
  ④ 队友源（peer:<n>）的反馈**转达**给真正持有编码器的那一端去调档。

兼容性：反馈走 CTRL 通道，**不动视频载荷布局**（VIDEO_SEQ_MINOR 仍是 4），
所以 (2,5) 观看端对 (2,4) 共享端只是反馈没人收，反之亦然——两端照常互通。
"""

import threading
import time
import unittest

import common
import host
import pipeline
import share
import viewer

# ---------------------------------------------------------------------------
# common：字段口径与判据
# ---------------------------------------------------------------------------

class TestParseFeedback(unittest.TestCase):
    """反馈字段的解析与夹紧——两端共用同一份定义（common.FEEDBACK_FIELDS）。"""

    def test_normal_fields(self):
        fb = common.parse_feedback({"action": common.Act.FEEDBACK,
                                    "fps": 29.5, "lat_ms": 120.0, "rtt_ms": 45.0,
                                    "backlog_ms": 320.0, "drop_pct": 1.5,
                                    "decode_ms": 8.25})
        self.assertIsNotNone(fb)
        self.assertAlmostEqual(fb["fps"], 29.5)
        self.assertAlmostEqual(fb["backlog_ms"], 320.0)
        self.assertAlmostEqual(fb["decode_ms"], 8.25)
        # action 不在字段表里，不应混进结果
        self.assertNotIn("action", fb)

    def test_out_of_range_is_clamped_not_dropped(self):
        """越界值夹住而不是丢掉：反馈是观感数据，丢掉等于"对端报了个离谱值"也无声无息。"""
        fb = common.parse_feedback({"backlog_ms": 10 ** 9, "drop_pct": 500.0,
                                    "fps": -5.0, "decode_ms": 10 ** 6})
        self.assertAlmostEqual(fb["backlog_ms"], 60_000.0)
        self.assertAlmostEqual(fb["drop_pct"], 100.0)
        self.assertAlmostEqual(fb["fps"], 0.0)
        self.assertAlmostEqual(fb["decode_ms"], 1_000.0)

    def test_non_numeric_is_dropped(self):
        """非数值（含 bool 与 NaN）必须丢掉——bool 是 int 的子类，不显式排除会当成 0/1。"""
        fb = common.parse_feedback({"backlog_ms": "120", "drop_pct": True,
                                    "decode_ms": float("nan"), "fps": 30})
        self.assertNotIn("backlog_ms", fb)
        self.assertNotIn("drop_pct", fb)
        self.assertNotIn("decode_ms", fb)
        self.assertIn("fps", fb)

    def test_empty_or_garbage_returns_none(self):
        self.assertIsNone(common.parse_feedback({}))
        self.assertIsNone(common.parse_feedback({"action": common.Act.FEEDBACK}))
        self.assertIsNone(common.parse_feedback(None))
        self.assertIsNone(common.parse_feedback("not a dict"))
        self.assertIsNone(common.parse_feedback([1, 2, 3]))


class TestFeedbackPredicates(unittest.TestCase):
    """「卡不卡」与「卡的原因」两个判据。"""

    def test_stuck_thresholds(self):
        self.assertFalse(common.feedback_is_stuck(None))
        self.assertFalse(common.feedback_is_stuck({}))
        self.assertFalse(common.feedback_is_stuck({"backlog_ms": 100.0,
                                                   "drop_pct": 1.0}))
        self.assertTrue(common.feedback_is_stuck(
            {"backlog_ms": common.FB_STUCK_BACKLOG_MS}))
        self.assertTrue(common.feedback_is_stuck(
            {"drop_pct": common.FB_STUCK_DROP_PCT}))

    def test_decode_bound_detection(self):
        """解码耗时占满帧预算 → 解码受限（该削画幅，不是削码率）。"""
        # 30fps → 帧预算 33.3ms；解码 30ms 已占 90%
        self.assertTrue(common.feedback_is_decode_bound(
            {"fps": 30.0, "decode_ms": 30.0}))
        # 解码 5ms（15%）→ 链路受限
        self.assertFalse(common.feedback_is_decode_bound(
            {"fps": 30.0, "decode_ms": 5.0}))
        # 缺 decode_ms → 判不了，按"非解码受限"处理（宁可少动一项）
        self.assertFalse(common.feedback_is_decode_bound({"fps": 30.0}))
        self.assertFalse(common.feedback_is_decode_bound(None))


# ---------------------------------------------------------------------------
# 观看端：统计与上报
# ---------------------------------------------------------------------------

def _channel():
    """造一个够用即可的 Channel（真锁真方法，只缺网络与解码的部分）。"""
    ch = viewer.Channel.__new__(viewer.Channel)
    ch.lock = threading.RLock()
    ch.name = "t"
    ch.fps = 30.0
    ch.latency_ms = 100.0
    ch.round_trip_ms = 60.0
    ch._fb_backlog_ms = 0.0
    ch._fb_decode_ms = 0.0
    ch._fb_decode_avg_ms = 0.0
    ch._fb_frames_seen = 0
    ch._fb_gaps = 0
    ch._fb_drop_pct = 0.0
    ch._fb_src_fps = 0.0
    ch._src_ts_samples = []
    ch._fb_sent_at = 0.0
    ch._resync = False
    ch._lag_base_d = None
    ch._lag_base_at = 0.0
    ch._last_video_seq = None
    ch._video_seq_gaps = 0
    ch._gap_warned = False
    ch._batch_first_at = 0.0
    return ch


class TestViewerBacklogAccounting(unittest.TestCase):

    def test_backlog_amount_recorded_below_threshold(self):
        """积压量在**未超阈值时也要记**——超阈值才知道的话，共享端只在已经卡住
        之后才收到第一个数，降档永远慢一拍。"""
        ch = _channel()
        now = 1000.0
        ch._lag_base_d = 0.5
        ch._lag_base_at = now
        ts = int((now - 0.7) * 1e6)      # 观测时延 0.7s，比基线多 0.2s
        lagged, _why = ch._backlog_exceeded(now, [(ts, 0, 0, b"")])
        self.assertFalse(lagged, "0.2 秒远不到 2.5 秒的积压阈值")
        self.assertAlmostEqual(ch._fb_backlog_ms, 200.0, places=1)

    def test_resync_floors_reported_backlog(self):
        """追新模式下即便观测延迟已回落也要报一个不小于阈值的量：
        那一刻这一端确实正在"卡住后恢复"，报 0 等于骗共享端"我这边没问题"。"""
        ch = _channel()
        ch._resync = True
        ch._note_backlog_ms(0.0)
        self.assertGreaterEqual(ch._fb_backlog_ms,
                                viewer.Channel._BACKLOG_LAG_US / 1000.0)

    def test_seq_gap_counted_per_window(self):
        ch = _channel()
        ch._note_video_seq(10)
        ch._note_video_seq(15)          # 中间缺 4 帧
        self.assertEqual(ch._fb_gaps, 4)
        self.assertEqual(ch._fb_frames_seen, 2)
        # 累计口径另算（告警用），两者互不干扰
        self.assertEqual(ch._video_seq_gaps, 4)


class TestViewerFeedbackPayload(unittest.TestCase):

    def test_payload_shape_and_drop_rate(self):
        ch = _channel()
        ch._fb_gaps = 3
        ch._fb_frames_seen = 97
        payload = ch._feedback_payload()
        self.assertEqual(payload["action"], common.Act.FEEDBACK)
        self.assertAlmostEqual(payload["drop_pct"], 3.0, places=1)
        self.assertAlmostEqual(payload["fps"], 30.0, places=1)
        # 延迟取 RTT/2（跨机时钟无关的那个口径）
        self.assertAlmostEqual(payload["lat_ms"], 30.0, places=1)
        self.assertAlmostEqual(payload["rtt_ms"], 60.0, places=1)

    def test_counters_reset_each_report(self):
        """丢帧率按**上报周期**统计：累计值只适合看趋势，判"现在卡不卡"不行。"""
        ch = _channel()
        ch._fb_gaps = 5
        ch._fb_frames_seen = 95
        ch._feedback_payload()
        self.assertEqual(ch._fb_gaps, 0)
        self.assertEqual(ch._fb_frames_seen, 0)
        # 下一周期没有新帧 → 丢帧率归零，而不是继续挂着上一周期的值
        self.assertAlmostEqual(ch._feedback_payload()["drop_pct"], 0.0)

    def test_payload_passes_common_parser(self):
        """观看端组装的报文必须能被共享端那一份解析器吃下（同一份字段口径）。"""
        ch = _channel()
        ch._fb_backlog_ms = 1234.5
        ch._fb_decode_avg_ms = 7.5
        fb = common.parse_feedback(ch._feedback_payload())
        self.assertIsNotNone(fb)
        self.assertAlmostEqual(fb["backlog_ms"], 1234.5)
        self.assertAlmostEqual(fb["decode_ms"], 7.5)

    def test_decode_ms_is_smoothed(self):
        ch = _channel()
        ch._note_decode_ms(30.0)
        self.assertAlmostEqual(ch._fb_decode_avg_ms, 30.0)
        ch._note_decode_ms(10.0)
        self.assertAlmostEqual(ch._fb_decode_avg_ms, 30.0 * 0.8 + 10.0 * 0.2)


class TestViewerControlThreadResilience(unittest.TestCase):
    """控制线程（心跳 + 反馈）不许被反馈组装的意外杀死。

    这条线程同时管着心跳与反馈：它一死，整条连接的 RTT 统计与"重连后自动
    补发"都没了，而症状是"延迟一直显示 0、连着重连"——极难往反馈功能上想。
    """

    def _channel(self):
        ch = _channel()
        ch._running = True
        ch._active_sock = object()
        ch._ping_interval = 0.01
        ch._fb_interval = 0.01
        ch._stop_evt = threading.Event()
        ch.sent = []

        def _send(sock, obj, timeout=5.0):
            ch.sent.append(obj)
        ch._send_locked = _send
        return ch

    def test_feedback_error_does_not_kill_the_thread(self):
        ch = self._channel()
        calls = {"n": 0}
        real = ch._feedback_payload

        def _boom():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("模拟组装异常")
            return real()
        ch._feedback_payload = _boom
        # 跑一小会儿：线程必须还活着并继续发心跳
        th = threading.Thread(target=ch._control_ping_loop, args=(ch._active_sock,),
                              daemon=True)
        th.start()
        time.sleep(0.3)
        ch._running = False
        ch._stop_evt.set()
        th.join(timeout=2.0)
        self.assertFalse(th.is_alive(), "控制线程没有退出")
        self.assertGreaterEqual(calls["n"], 2,
                                "反馈组装抛异常后线程不再继续上报")
        self.assertTrue(any(o.get("action") == common.Act.PING for o in ch.sent),
                        "心跳也该继续发（线程没被反馈的异常带走）")

    def test_thread_stops_promptly_on_stop_event(self):
        ch = self._channel()
        th = threading.Thread(target=ch._control_ping_loop, args=(ch._active_sock,),
                              daemon=True)
        th.start()
        time.sleep(0.05)
        ch._running = False
        ch._stop_evt.set()
        th.join(timeout=1.0)
        self.assertFalse(th.is_alive(), "退出时控制线程应当立刻醒来")


class TestViewerSourceFps(unittest.TestCase):
    """源帧率由**帧的采集时间戳**估出——不能用观看端的显示帧率。

    解码受限时显示帧率恰恰是掉下来的那个数（源 30fps、每帧解码 30ms →
    只能显示 15fps）。拿它去算"帧预算"会把解码受限判反，于是共享端去降
    一个对解码受限毫无作用的码率。
    """

    def test_src_fps_from_capture_timestamps(self):
        ch = _channel()
        ch._src_ts_samples = []
        base_ts = 1_000_000
        # 30fps：相邻帧的采集时刻差 33.33ms
        for i in range(10):
            ch._note_src_fps(base_ts + int(i * 1_000_000 / 30))
        self.assertAlmostEqual(ch._fb_src_fps, 30.0, delta=0.5)

    def test_src_fps_independent_of_display_fps(self):
        """观看端显示帧率再低也不影响源帧率的估法。"""
        ch = _channel()
        ch._src_ts_samples = []
        ch.fps = 5.0                       # 显示只有 5fps（解码跟不上）
        base_ts = 1_000_000
        for i in range(10):
            ch._note_src_fps(base_ts + int(i * 1_000_000 / 30))
        self.assertAlmostEqual(ch._fb_src_fps, 30.0, delta=0.5)

    def test_payload_carries_src_fps(self):
        ch = _channel()
        ch._src_ts_samples = []
        base_ts = 1_000_000
        for i in range(10):
            ch._note_src_fps(base_ts + int(i * 1_000_000 / 30))
        payload = ch._feedback_payload()
        self.assertAlmostEqual(payload["src_fps"], 30.0, delta=0.5)
        fb = common.parse_feedback(payload)
        self.assertIn("src_fps", fb)


# ---------------------------------------------------------------------------
# 共享端：接收、归源、聚合、驱动 ABR
# ---------------------------------------------------------------------------

class _InfoStub:
    """ClientInfo 的最小替身：只实现反馈路径真正用到的接口。

    反馈用 `_fb_pair` + `note_feedback` / `latest_feedback`——与生产的
    ClientInfo 同款（反馈与它的时刻成对读写，见 host.ClientInfo.note_feedback）。
    """

    def __init__(self, addr="1.2.3.4:5700", watch_source="local", share_id=None):
        self.addr = addr
        self.username = "u"
        self.watch_source = watch_source
        self.share_id = share_id
        self._fb_pair = (None, 0.0)
        self.sent = []
        self.sock = object()

    def note_feedback(self, fb, now=None):
        self._fb_pair = (fb, now if now is not None else time.monotonic())

    def latest_feedback(self):
        return self._fb_pair[0], self._fb_pair[1]

    def send_ctrl(self, sock, obj, send_timeout=2.0):
        self.sent.append(obj)

    # ABR 统计路径要用的几个
    def recent_send_ms(self, now=None):
        return 0.0

    def rate_kbps(self, now=None, window=1.0):
        return 0.0

    def has_recent_drop(self, now=None):
        return False

    bytes_sent = 0
    dropped_frames = 0


class _EncStub:
    available = True

    def __init__(self):
        self.calls = []

    def set_bitrate(self, bps, hysteresis=0.15):
        self.calls.append(bps)
        return True


class _RuntimeStub:
    """HostRuntime 的最小替身：只提供反馈与 ABR 路径真正读到的字段。"""

    def __init__(self, clients=None):
        self.clients_lock = threading.Lock()
        self.clients = clients if clients is not None else {}
        self.stats_lock = threading.Lock()
        self.stats = {"capture_fps": 30, "encode_fps": 30, "send_fps": 30,
                      "encode_ms": 5.0, "avg_send_ms": 1.0, "still": False,
                      "egress_bps": 0, "abr_changes": 0, "last_abr_at": 0.0}
        self.slot_lock = threading.Lock()
        self.slot = {"quality": 80, "scale": 1.0, "fps": 30, "out_w": 854,
                     "out_h": 480, "video_ts": 0, "ts": 0, "bitrate": 2_500_000,
                     "monitor": 1, "region": None, "backend": "mss", "video": None,
                     "jpeg": None, "is_key": False, "video_codec": 0, "encode_ms": 0.0}
        self.codec_lock = threading.Lock()
        self.video_encoder = _EncStub()
        self.target_bitrate = 2_500_000
        self.cfg = {
            "host": {"fps": 30, "jpeg_quality": 80, "scale": 1.0,
                     "perf": {"adaptive": True, "quality_min": 40,
                              "scale_min": 0.25, "fps_min": 10},
                     "codec": {"min_bitrate_kbps": 400, "max_bitrate_kbps": 6000}},
        }
        self.force_key = False
        self.raw_evt = threading.Event()
        self.stop_event = threading.Event()
        self._peer_fb_lock = threading.Lock()
        self._peer_fb_at = {}
        self._peer_key_lock = threading.Lock()
        self._peer_key_at = {}


class TestHostFeedbackDispatch(unittest.TestCase):

    def test_feedback_is_recorded_on_client(self):
        rt = _RuntimeStub()
        info = _InfoStub()
        rt.clients[info.sock] = info
        host._dispatch_ctrl(info.sock, info, rt,
                            {"action": common.Act.FEEDBACK, "backlog_ms": 1200,
                             "rtt_ms": 80}, info.addr, {})
        fb, fb_at = info.latest_feedback()
        self.assertIsNotNone(fb)
        self.assertAlmostEqual(fb["backlog_ms"], 1200.0)
        self.assertGreater(fb_at, 0.0)

    def test_garbage_feedback_is_ignored(self):
        rt = _RuntimeStub()
        info = _InfoStub()
        rt.clients[info.sock] = info
        host._dispatch_ctrl(info.sock, info, rt,
                            {"action": common.Act.FEEDBACK}, info.addr, {})
        fb, _at = info.latest_feedback()
        self.assertIsNone(fb)

    def test_source_is_taken_from_server_side_ledger(self):
        """归源以**服务端的订阅账本**为准，不采信报文里的任何"我在看哪一路"字段——
        客户端可以谎报，谎报只会让反馈落到错误的源上。"""
        rt = _RuntimeStub()
        info = _InfoStub(watch_source="local")
        rt.clients[info.sock] = info
        host._dispatch_ctrl(info.sock, info, rt,
                            {"action": common.Act.FEEDBACK, "src": "peer:9",
                             "backlog_ms": 100}, info.addr, {})
        # 谎报 src 不会改变账本上的订阅关系
        self.assertEqual(info.watch_source, "local")


class TestWorstLocalFeedback(unittest.TestCase):

    def test_feedback_and_its_timestamp_are_read_as_a_pair(self):
        """反馈与它的上报时刻必须**成对**读出。

        分开存、分开读的话，读侧可能拿到"新反馈配旧时刻"——那条刚到的反馈会被
        当成过期丢掉（而此时它恰恰最该被采纳）。
        """
        info = _InfoStub()
        info.note_feedback({"backlog_ms": 100.0}, now=1000.0)
        fb, at = info.latest_feedback()
        self.assertAlmostEqual(fb["backlog_ms"], 100.0)
        self.assertAlmostEqual(at, 1000.0)
        # 再写一条：旧的必须整条被换掉，不留半新半旧的状态
        info.note_feedback({"backlog_ms": 200.0}, now=2000.0)
        fb, at = info.latest_feedback()
        self.assertAlmostEqual(fb["backlog_ms"], 200.0)
        self.assertAlmostEqual(at, 2000.0)

    def test_real_client_info_exposes_the_pair(self):
        """生产用的 ClientInfo 也有这两个方法（别让测试替身与生产分家）。"""
        info = host.ClientInfo("1.2.3.4:5700")
        self.assertEqual(info.latest_feedback(), (None, 0.0))
        info.note_feedback({"backlog_ms": 5.0})
        fb, at = info.latest_feedback()
        self.assertAlmostEqual(fb["backlog_ms"], 5.0)
        self.assertGreater(at, 0.0)

    def test_only_local_source_counted(self):
        """只采信本机源订阅者：订阅 peer:X 的观众归队友的编码器管（net-016 同口径）。"""
        a = _InfoStub(watch_source="local")
        a.note_feedback({"backlog_ms": 100.0}, now=time.monotonic())
        b = _InfoStub(watch_source="peer:1")
        b.note_feedback({"backlog_ms": 5000.0}, now=time.monotonic())
        rt = _RuntimeStub({a.sock: a, b.sock: b})
        worst, count = host._worst_local_feedback(rt)
        self.assertEqual(count, 1)
        self.assertAlmostEqual(worst["backlog_ms"], 100.0)

    def test_expired_feedback_ignored(self):
        """过期反馈不算数：观看端可能已掉线或自己卡到发不出，拿旧反馈降码率
        等于按幽灵观众的抱怨调整所有人的画质。"""
        a = _InfoStub()
        a.note_feedback({"backlog_ms": 5000.0}, now=time.monotonic() - (common.FB_MAX_AGE_S + 1.0))
        rt = _RuntimeStub({a.sock: a})
        worst, count = host._worst_local_feedback(rt)
        self.assertIsNone(worst)
        self.assertEqual(count, 0)

    def test_stuck_wins_over_merely_large(self):
        a = _InfoStub()
        a.note_feedback({"backlog_ms": 500.0, "drop_pct": 0.0},
                        now=time.monotonic())   # 没到阈值，不算卡
        b = _InfoStub()
        b.note_feedback({"backlog_ms": 100.0, "drop_pct": 20.0},
                        now=time.monotonic())   # 丢帧率超阈值，算卡
        rt = _RuntimeStub({a.sock: a, b.sock: b})
        worst, count = host._worst_local_feedback(rt)
        self.assertEqual(count, 2)
        # "卡"的那份优先于"积压大但没到阈值"的那份
        self.assertAlmostEqual(worst["drop_pct"], 20.0)


class TestAbrDrivenByFeedback(unittest.TestCase):

    def _params(self):
        return host.PipelineParams(self._runtime().cfg)

    def _runtime(self):
        return _RuntimeStub()

    def test_stuck_feedback_triggers_degrade_even_when_link_looks_healthy(self):
        """发送侧一切正常（发送耗时 1ms、无丢帧）但观看端报"卡" → 仍要降档。
        这正是本轮要补的盲区：旧判据在这条链路上什么都看不见。"""
        rt = _RuntimeStub()
        info = _InfoStub()
        info.note_feedback({"backlog_ms": 2000.0, "drop_pct": 0.0, "fps": 30.0,
                   "decode_ms": 2.0}, now=time.monotonic())
        rt.clients[info.sock] = info
        p = pipeline.PipelineParams(rt.cfg)
        host._perf_tick(rt, p)
        # 链路受限（解码很快）→ 先动码率
        self.assertTrue(rt.video_encoder.calls, "观看端报卡却没有触发任何降档")
        self.assertLess(rt.video_encoder.calls[0], 2_500_000)

    def test_decode_bound_prefers_scale_over_bitrate(self):
        """解码受限（单帧解码占满帧预算）时降码率无效——该削画幅。

        `src_fps` 是**源**帧率：解码受限时观看端的显示帧率恰恰是掉下来的那个数
        （源 30fps、每帧解码 30ms → 只能显示 15fps），用它算预算会把"解码受限"
        判反，于是去降一个对解码受限毫无作用的码率。
        """
        rt = _RuntimeStub()
        info = _InfoStub()
        info.note_feedback({"backlog_ms": 2000.0, "drop_pct": 0.0, "fps": 15.0,
                   "src_fps": 30.0, "decode_ms": 30.0}, now=time.monotonic())
        rt.clients[info.sock] = info
        p = pipeline.PipelineParams(rt.cfg)
        host._perf_tick(rt, p)
        self.assertEqual(rt.video_encoder.calls, [], "解码受限不该先动码率")
        self.assertLess(rt.slot["scale"], 1.0, "解码受限应先削画幅")

    def test_decode_bound_not_inferred_from_display_fps(self):
        """反例钉住：只看显示帧率会把解码受限判成"不是解码受限"。

        源 30fps / 每帧解码 30ms（预算 33ms，占 90%）→ 观看端只能显示 15fps。
        若用显示帧率算预算（1000/15 = 66ms），30ms 只占 45% → 判"链路受限" →
        去降码率 → 对解码受限**完全无效**（解码量由像素数×帧数决定）。
        """
        fb = {"fps": 15.0, "src_fps": 30.0, "decode_ms": 30.0}
        self.assertTrue(common.feedback_is_decode_bound(fb, fps=fb["src_fps"]))
        # 用显示帧率算就判反了——这正是必须显式传 src_fps 的原因
        self.assertFalse(common.feedback_is_decode_bound(fb, fps=fb["fps"]))

    def test_degrade_never_raises_bitrate(self):
        """降级**绝不能把码率调高**。

        用户把 `codec.bitrate_kbps` 配到低于 `min_bitrate_kbps`（如 100 vs 默认
        下限 400）时，"降到下限"这个兜底值反而**高于**当前码率——于是一个正在
        为省带宽而降档的会话被反向加码。旧实现在调用点用
        `cur_bitrate > min_bitrate` 挡住了这一路；抽成 `_degrade_bitrate` 后
        这道门必须留在函数里（调用方有多处，挡在调用点会漏）。
        """
        rt = _RuntimeStub()
        rt.slot["bitrate"] = 100_000            # 100 Kbps，低于下限 400
        rt.slot["scale"] = 0.25                # 画幅已到底
        rt.slot["fps"] = 10                    # 帧率已到底
        p = pipeline.PipelineParams(rt.cfg)
        cur = {"quality": 80, "scale": 0.25, "fps_now": 10, "video_mode": True,
               "cur_bitrate": 100_000, "egress_bps": 0}
        host._abr_degrade(rt, p, cur, 1.0, False, prefer="scale")
        self.assertEqual(rt.video_encoder.calls, [],
                         "降级把码率从 100 Kbps 抬到了 400 Kbps")

    def test_degrade_bitrate_still_lowers_when_above_floor(self):
        """正常情形下码率降级照旧生效（别把上一条修成"永不降码率"）。"""
        rt = _RuntimeStub()
        p = pipeline.PipelineParams(rt.cfg)
        cur = {"quality": 80, "scale": 1.0, "fps_now": 30, "video_mode": True,
               "cur_bitrate": 2_500_000, "egress_bps": 0}
        host._abr_degrade(rt, p, cur, 1.0, False, prefer="bitrate")
        self.assertEqual(rt.video_encoder.calls, [1_750_000])

    def test_scale_first_then_bitrate_when_both_at_floor(self):
        """画幅与帧率都到底后，码率才是最后一招——且此时才动它。"""
        rt = _RuntimeStub()
        rt.slot["scale"] = 0.25
        rt.slot["fps"] = 10
        p = pipeline.PipelineParams(rt.cfg)
        cur = {"quality": 80, "scale": 0.25, "fps_now": 10, "video_mode": True,
               "cur_bitrate": 2_500_000, "egress_bps": 0}
        host._abr_degrade(rt, p, cur, 1.0, False, prefer="scale")
        self.assertEqual(rt.video_encoder.calls, [1_750_000])

    def test_jpeg_path_unchanged_by_scale_prefer(self):
        """JPEG 模式（video_mode=False）走质量分支，prefer 不影响它。"""
        rt = _RuntimeStub()
        p = pipeline.PipelineParams(rt.cfg)
        cur = {"quality": 80, "scale": 1.0, "fps_now": 30, "video_mode": False,
               "cur_bitrate": 0, "egress_bps": 0}
        host._abr_degrade(rt, p, cur, 1.0, False, prefer="scale")
        self.assertEqual(rt.slot["quality"], 70)
        self.assertEqual(rt.video_encoder.calls, [])

    def test_no_recovery_while_someone_reports_stuck(self):
        """有人报"卡"时不回升——回升会让链路刚缓过来又立刻顶回去。"""
        rt = _RuntimeStub()
        # 造一个"已经降过档"的状态：码率低于上限、画幅低于基准
        rt.slot["bitrate"] = 1_000_000
        rt.slot["scale"] = 0.5
        info = _InfoStub()
        info.note_feedback({"backlog_ms": 3000.0, "drop_pct": 0.0, "fps": 30.0,
                   "decode_ms": 2.0}, now=time.monotonic())
        rt.clients[info.sock] = info
        p = pipeline.PipelineParams(rt.cfg)
        host._perf_tick(rt, p)
        # 降级方向，不该出现回升（画幅不会变大、码率不会涨回上限）
        self.assertLessEqual(rt.slot["scale"], 0.5)
        for call in rt.video_encoder.calls:
            self.assertLessEqual(call, 1_000_000)

    def test_healthy_feedback_does_not_degrade(self):
        rt = _RuntimeStub()
        info = _InfoStub()
        info.note_feedback({"backlog_ms": 10.0, "drop_pct": 0.0, "fps": 30.0,
                   "decode_ms": 2.0}, now=time.monotonic())
        rt.clients[info.sock] = info
        p = pipeline.PipelineParams(rt.cfg)
        host._perf_tick(rt, p)
        # 反馈健康 → 不得触发降级（画幅不变、码率不降）。
        # 注意"回升"分支可能仍然动码率（链路空闲时的既有行为），所以只断言不降级。
        self.assertEqual(rt.slot["scale"], 1.0)
        for call in rt.video_encoder.calls:
            self.assertGreaterEqual(call, 2_500_000)


# ---------------------------------------------------------------------------
# 队友源：反馈转达
# ---------------------------------------------------------------------------

class TestPeerFeedbackForwarding(unittest.TestCase):

    def test_stuck_feedback_forwarded_to_peer_owner(self):
        rt = _RuntimeStub()
        peer = _InfoStub(watch_source="local", share_id="peer:1")
        viewer = _InfoStub(watch_source="peer:1")
        rt.clients[peer.sock] = peer
        rt.clients[viewer.sock] = viewer
        host._dispatch_ctrl(viewer.sock, viewer, rt,
                            {"action": common.Act.FEEDBACK, "backlog_ms": 1500},
                            viewer.addr, {})
        self.assertEqual(len(peer.sent), 1)
        self.assertEqual(peer.sent[0]["action"], common.Act.FB_ADJUST)
        self.assertAlmostEqual(peer.sent[0]["backlog_ms"], 1500.0)

    def test_healthy_feedback_not_forwarded(self):
        """只转达"卡"的：同一路流上"一个卡一个不卡"时，不卡的那份会让持有端
        把码率又顶回去，两条反馈互相打架。"""
        rt = _RuntimeStub()
        peer = _InfoStub(watch_source="local", share_id="peer:1")
        viewer = _InfoStub(watch_source="peer:1")
        rt.clients[peer.sock] = peer
        rt.clients[viewer.sock] = viewer
        host._dispatch_ctrl(viewer.sock, viewer, rt,
                            {"action": common.Act.FEEDBACK, "backlog_ms": 10},
                            viewer.addr, {})
        self.assertEqual(peer.sent, [])

    def test_forwarding_is_cooldown_merged(self):
        rt = _RuntimeStub()
        peer = _InfoStub(watch_source="local", share_id="peer:1")
        v1 = _InfoStub(watch_source="peer:1")
        v2 = _InfoStub(watch_source="peer:1")
        rt.clients[peer.sock] = peer
        rt.clients[v1.sock] = v1
        rt.clients[v2.sock] = v2
        for v in (v1, v2):
            host._dispatch_ctrl(v.sock, v, rt,
                                {"action": common.Act.FEEDBACK, "backlog_ms": 1500},
                                v.addr, {})
        # 两个观看端各自的反馈合并成一条转达
        self.assertEqual(len(peer.sent), 1)


# ---------------------------------------------------------------------------
# 共享者侧（share）：按反馈调档
# ---------------------------------------------------------------------------

def _share_session(bitrate_kbps=2500, min_kbps=400):
    s = share.ScreenShareSession.__new__(share.ScreenShareSession)
    s._name = "t"
    s._bitrate = bitrate_kbps * 1000
    s._cfg_bitrate = bitrate_kbps * 1000
    s._min_bitrate = min_kbps * 1000
    s._fb_stuck_at = 0.0
    s._fb_changed_at = 0.0
    return s


class TestShareApplyFeedback(unittest.TestCase):

    def test_stuck_feedback_lowers_bitrate(self):
        s = _share_session()
        now = time.monotonic()
        s._fb_changed_at = now - 60.0      # 冷却已过
        got = s.apply_feedback({"backlog_ms": 2000.0})
        self.assertIsNotNone(got)
        self.assertEqual(got, 1750)        # 2500 × 0.7
        self.assertEqual(s._bitrate, 1_750_000)

    def test_healthy_feedback_does_not_touch_bitrate(self):
        s = _share_session()
        self.assertIsNone(s.apply_feedback({"backlog_ms": 10.0}))
        self.assertEqual(s._bitrate, 2_500_000)

    def test_drop_is_cooldown_limited(self):
        """共享端每 2 秒转达一次"卡"，不设冷却的话码率十几秒就塌到下限。"""
        s = _share_session()
        s._fb_changed_at = time.monotonic()
        self.assertIsNone(s.apply_feedback({"backlog_ms": 2000.0}))
        self.assertEqual(s._bitrate, 2_500_000)

    def test_never_below_configured_floor(self):
        s = _share_session(bitrate_kbps=500, min_kbps=400)
        s._fb_changed_at = 0.0
        got = s.apply_feedback({"backlog_ms": 2000.0})
        self.assertEqual(got, 400)
        self.assertEqual(s._bitrate, 400_000)

    def test_restores_after_quiet_period(self):
        s = _share_session()
        s._fb_changed_at = 0.0
        s.apply_feedback({"backlog_ms": 2000.0})
        self.assertEqual(s._bitrate, 1_750_000)
        # 刚投诉过 → 还不回升
        self.assertIsNone(s._maybe_restore_bitrate())
        # 投诉已过去 30 秒 → 回升一档
        s._fb_stuck_at = time.monotonic() - (share._FB_RESTORE_S + 1.0)
        got = s._maybe_restore_bitrate()
        self.assertIsNotNone(got)
        self.assertGreater(got, 1750)
        self.assertLessEqual(got, 2500)

    def test_restore_stops_at_configured_value(self):
        s = _share_session()
        s._bitrate = 2_400_000
        s._fb_stuck_at = time.monotonic() - (share._FB_RESTORE_S + 1.0)
        s._maybe_restore_bitrate()
        self.assertEqual(s._bitrate, 2_500_000)
        # 又安静了一个周期：此时码率已在配置值上 → 清空投诉记录，不再反复计时
        s._fb_stuck_at = time.monotonic() - (share._FB_RESTORE_S + 1.0)
        s._maybe_restore_bitrate()
        self.assertEqual(s._fb_stuck_at, 0.0)


# ---------------------------------------------------------------------------
# 兼容性
# ---------------------------------------------------------------------------

class TestProtocolCompat(unittest.TestCase):

    def test_video_payload_layout_gate_unchanged(self):
        """2.5 只加控制消息，**视频载荷布局的门槛不得跟着抬**——
        抬了 VIDEO_SEQ_MINOR 就等于让所有 1.5.0 观看端在认证期被拒（net-047 先例）。"""
        self.assertEqual(common.VIDEO_SEQ_MINOR, 4)
        self.assertEqual(common.PROTO_CHAL_MIN_MINOR, 3)
        self.assertEqual(common.PROTOCOL_VERSION, (2, 5))

    def test_unknown_action_is_ignored_by_old_peer(self):
        """旧版观看端不认识 feedback：共享端按动作分发，末尾无 else → 静默忽略，
        连接不因此中断。用真实的分发函数验证（不是复刻它的几行）。"""
        rt = _RuntimeStub()
        info = _InfoStub()
        rt.clients[info.sock] = info
        # 模拟旧版对端：不认识的动作不产生任何回复，也不返回致命原因
        result = host._dispatch_ctrl(info.sock, info, rt,
                                     {"action": "some_future_action", "x": 1},
                                     info.addr, {})
        self.assertIsNone(result, "未知 action 不得返回致命原因（那会断连）")
        self.assertEqual(info.sent, [], "未知 action 不得回任何东西")

    def test_feedback_action_names_are_stable(self):
        self.assertEqual(common.Act.FEEDBACK, "feedback")
        self.assertEqual(common.Act.FB_ADJUST, "fb_adjust")


if __name__ == "__main__":
    unittest.main()
