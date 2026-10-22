# -*- coding: utf-8 -*-
"""T-05 / ui-002 + ui-029 回归：状态跃迁必须作废旧画面，poll 的早退簿记必须落账。

第 6 轮审查第十章 T-05 的判定：本轮对 `test_viewer_poll_backoff.py` 的唯一改动是
给桩补一行 `app._last_channel_status = status` 让旧断言继续通过；**`_invalidate_media`
与三处状态跃迁在 tests/ 中零调用**，`_show_text` 被空 lambda 吃掉，
"断线后不再定格最后一帧"无从验证。

本文件因此：
  · 用真实 `viewer.Channel`（真实 `__init__` / `_invalidate_media` / `_receiver_loop`），
    不用鸭子类型频道桩（避免第 6 轮 T-04 批评的"替身太薄、生产兜底分支替它过关"）；
  · 用真实 `ViewerApp.poll`，把 `_show_text`/`_show_frame` 换成**记录器**而不是空
    lambda——记录"被调了几次"才谈得上早退簿记。

钉住的缺陷形态：
  ui-002：poll 的"无变化"判据原来只看 frame_serial，状态跃迁既不递增 serial 也不清
    last_frame → 出一帧后"已断开，正在重连…"分支永不可达，用户面对静止最后一帧。
  ui-029：`_last_frame_serial/_last_channel_idx/_last_channel_status` 只在
    `_show_frame` 贴图成功时写，纯文字分支不写 → 断线态下"序列号未变"早退条件
    恒不成立，A4 §3.2 的 15→33→50ms 空闲退避在"host 离线/正在重连"这一常态下
    完全失效（每 15ms 一次整段 `_show_text` + 两次叠加层刷新 + 多次跨线程取锁）。

改回缺陷形态会红在哪一行（逐行论证）：
  ① 删掉 `viewer.py:3452-3455`（文字分支的三行簿记）→
     `test_second_poll_early_exits_after_text_branch` 收到第二次 `_show_text` → 红；
     `test_many_polls_draw_text_only_once`（20 轮 1 次）同红。
  ② 把 `viewer.py:3406-3408` 的判据改回只看 serial（去掉状态维度）→
     `test_status_change_alone_forces_redraw` 红：状态变了、serial 未变时 poll 早退，
     "已断开"文案永远上不了屏。
  ③ 把 `Channel._invalidate_media`（`viewer.py:357-367`）改成空实现或"只清帧不
     bump serial" → `test_invalidate_bumps_serial_and_clears_frame` 红、
     `TestRealReceiverLoopTransitions` 的两个跃迁计数红。
  ④ 删掉 `_receiver_loop` 三处跃迁里的任一 `_invalidate_media()`（`viewer.py:381
     /426/597`）→ 对应行为用例的"每跃迁一次"计数红 + 结构用例红。
  ⑤ 把 `finally` 里 `if self.status != "auth"` 的守卫去掉（auth 态被改写）→
     `test_auth_round_invalidates_and_keeps_auth_status` 的 status 断言红。
"""
import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import common
import viewer

# 依赖声明（task-9，2026-10-10）：本文件建真实 `viewer.Channel`，其 `__init__`
# （viewer.py:332）会取 `codec_mod.VideoDecoder()`——而 `viewer.codec_mod` 是**惰性
# 模块全局**（viewer.py:35 哨兵 None，只有 `viewer._load_heavy()` 才赋值）。
# 全量 discover 时 pipeline.py / share.py 的模块级 `from screen import …` 会在收集期
# 顺带触发加载，于是本文件"看起来不需要"；**单跑**（`python -m unittest
# tests.test_bugfix_20260920_t05_viewer`）则 codec_mod 仍是 None → 12/15 例报
# AttributeError。显式声明依赖后两种跑法一致。
# 不用 try/except 包加载：环境装不上重依赖时必须**明确报错**，静默跳过比顺序依赖更难发现。
viewer._load_heavy()


def _unreachable_addr():
    """一个在**解析期**就报错的地址：本轮接收循环必然走 except → finally 两次跃迁。

    刻意不选"能解析但连不上"的地址：Windows 上 connect 到一个刚释放的临时端口
    会花约 2 秒（SYN 重试）而非立刻拒绝，会把整份用例拖成计时依赖的慢用例。
    `:5700` 正是 net-027 修的那条口径（空主机必须抛 ValueError）。
    """
    return ":5700"


def _new_channel(addr=None, name="t05"):
    """真实构造 Channel，并抓住 `__init__` 自动起的接收线程以便精确停/起。

    Channel.__init__ 末尾会 start() 一条 channel-recv 线程。本文件要么自己同步
    驱动 `_receiver_loop`，要么保证那条线程已退出——绝不允许它在后台一边重连
    一边改 frame_serial（那会把断言变成计时竞态）。
    """
    before = {id(t) for t in threading.enumerate()}
    ch = viewer.Channel(name, addr or _unreachable_addr())
    new = [t for t in threading.enumerate() if id(t) not in before]
    ch._tv_thread = next((t for t in new if t.name.startswith("channel-recv")), None)
    ch.request_stop()                       # 让 __init__ 起的线程立刻退出
    if ch._tv_thread is not None:
        ch._tv_thread.join(5.0)
        ch._tv_thread = None
    return ch


def _run_one_receiver_iteration(ch):
    """同步跑**真实** `_receiver_loop` 的一整轮：进入一次、退避处 break。"""
    ch._running = True
    ch._stop_evt.set()                      # 第 108 条的可中断退避：立刻返回 → 退出循环
    ch._receiver_loop()


class _Root:
    def __init__(self):
        self.scheduled = []

    def after(self, ms, fn=None):
        self.scheduled.append(ms)
        return "after#%d" % len(self.scheduled)


class _Recorder:
    """记录器（不是空 lambda）：T-05 判的就是"到底被调了几次"。"""

    def __init__(self, name):
        self.name = name
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)

    def texts(self):
        return [a[0] for a in self.calls]


def _app(ch, status=None):
    """ViewerApp 的最小可 poll 形态（绕开真实 Tk），簿记初值与真实 __init__ 一致。"""
    app = viewer.ViewerApp.__new__(viewer.ViewerApp)
    if status is not None:
        ch.status = status
    app.running = True
    app.root = _Root()
    app.channels = [ch]
    app.active_idx = 0
    app.has_frame = False
    app.user_moved = False
    app._poll_idle_ms = 15
    # 与 viewer.py:1742-1744 的初值逐项一致（ui-002 新增了状态维度）
    app._last_frame_serial = -1
    app._last_channel_idx = -1
    app._last_channel_status = None
    app._drain_ui_queue = lambda: None
    app._sync_hotkey_notice_once = lambda: None
    app._process_cred_requests = lambda: None
    app._check_share_sessions = lambda: None
    app._update_status_overlay = _Recorder("overlay")
    app._show_text = _Recorder("text")
    app._frame_rec = _Recorder("frame")

    def _show_frame(frame, serial=None):
        app._frame_rec(frame, serial)
        # 真实 _show_frame 的簿记副作用（viewer.py:3538-3540）：poll 的早退判据依赖它
        if serial is not None:
            app._last_frame_serial = serial
            app._last_channel_idx = app.active_idx
        app.has_frame = True

    app._show_frame = _show_frame
    return app


class TestInvalidateMediaRealChannel(unittest.TestCase):
    """真实 `_invalidate_media`：bump serial + 清 last_frame + 作废旧统计。"""

    def test_invalidate_bumps_serial_and_clears_frame(self):
        ch = _new_channel()
        self.addCleanup(ch.stop)
        with ch.lock:
            base = ch.frame_serial
            ch.last_frame = np.zeros((2, 2, 3), dtype=np.uint8)
        ch._fps_times = [0.0, 0.0]
        ch._latency_samples = [1, 2, 3]
        ch.round_trip_ms = 42.0            # ui-027：RTT 也要随断线作废
        ch._lag_base_d = 5.0
        ch.fps = 25.0

        ch._invalidate_media()

        self.assertEqual(ch.frame_serial, base + 1,
                         "状态跃迁没 bump serial：poll 的「无变化」判据永远命中旧值")
        self.assertIsNone(ch.last_frame,
                          "没清 last_frame：断线后悬浮窗继续定格最后一帧（ui-002 主症状）")
        self.assertEqual(list(ch._fps_times), [], "ui-010：旧路径的帧率样本未作废")
        self.assertEqual(list(ch._latency_samples), [])
        self.assertEqual(ch.round_trip_ms, 0.0, "ui-027：断线后仍显示上一条连接的 RTT")
        self.assertIsNone(ch._lag_base_d)
        self.assertEqual(ch.fps, 0.0, "第 12 条：断流后 fps 未衰减（死频道仍报 30fps）")

    def test_repeated_invalidations_keep_bumping(self):
        ch = _new_channel()
        self.addCleanup(ch.stop)
        seen = []
        for _ in range(3):
            ch._invalidate_media()
            seen.append(ch.frame_serial)
        self.assertEqual(len(set(seen)), 3, "serial 不再单调递增：poll 判据失去区分度")

    def test_invalidate_media_is_called_from_all_three_transitions(self):
        """三处跃迁点（connecting / auth / disconnected）都必须经 `_invalidate_media`。

        行为用例见 TestRealReceiverLoopTransitions；这里锁"调用点存在"，一旦有人
        只补其中两处，结构断言先响（ui-002 的原始描述就是"三处跃迁统一收口"）。
        """
        import inspect
        import re
        src = inspect.getsource(viewer.Channel._receiver_loop)
        for lit in ("connecting", "auth", "disconnected"):
            pat = re.compile(r"self\.status\s*=\s*[\"']%s[\"']" % lit)
            hits = [m.start() for m in pat.finditer(src)]
            self.assertTrue(hits, "找不到 `self.status = \"%s\"` 跃迁点" % lit)
            for at in hits:
                tail = src[at:at + 420]
                self.assertIn("_invalidate_media()", tail,
                              "`self.status = \"%s\"` 之后没跟 _invalidate_media()"
                              "（该条跃迁再次定格旧帧）" % lit)


class TestRealReceiverLoopTransitions(unittest.TestCase):
    """真跑 `_receiver_loop`：断线一轮 = 两次作废；认证失败一轮 = 三次作废。"""

    def test_disconnected_round_invalidates_twice(self):
        ch = _new_channel()
        self.addCleanup(ch.stop)
        with ch.lock:
            ch.last_frame = np.zeros((2, 2, 3), dtype=np.uint8)
            base = ch.frame_serial
        _run_one_receiver_iteration(ch)
        self.assertEqual(ch.status, "disconnected")
        self.assertGreaterEqual(ch.frame_serial, base + 2,
                                "connecting 与 disconnected 两次跃迁没各作废一次（ui-002）")
        self.assertIsNone(ch.last_frame, "跑完一轮真实接收循环后仍缓存着旧帧")

    def test_auth_round_invalidates_and_keeps_auth_status(self):
        """认证失败：status=auth + 作废，且 finally 不得把它改写成 disconnected。"""
        # 先用"解析期即失败"的地址建频道（__init__ 的自动线程因此秒退），再把地址
        # 换成真监听端口——否则两条接收线程抢同一端口握手，会各自白等 10 秒超时。
        ch = _new_channel()
        self.addCleanup(ch.stop)
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        port = srv.getsockname()[1]
        conns = []

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                conns.append(conn)
                try:
                    common.server_handshake(conn)  # 真握手：校验 FS02、回 OK02
                except OSError:
                    return

        threading.Thread(target=serve, daemon=True).start()
        self.addCleanup(lambda: [c.close() for c in conns])

        ch.addr = "127.0.0.1:%d" % port
        ch._authenticate = lambda sock: False      # 只桩"认证未通过"这一个判定结果
        with ch.lock:
            base = ch.frame_serial
            ch.last_frame = np.zeros((2, 2, 3), dtype=np.uint8)
        _run_one_receiver_iteration(ch)
        self.assertEqual(ch.status, "auth",
                         "认证失败的 auth 态被 finally 改写成 disconnected（ui-026 同族）")
        self.assertGreaterEqual(ch.frame_serial, base + 3,
                                "connecting + auth + finally 三次跃迁应各作废一次")
        self.assertIsNone(ch.last_frame)
        self.assertTrue(ch.auth_msg, "auth_msg 未留具体原因，面板只能显示套话")


class TestPollTextBranchBookkeeping(unittest.TestCase):
    """ui-029：纯文字分支也要落账 serial/idx/status，否则"无变化"早退永不命中。"""

    def test_second_poll_early_exits_after_text_branch(self):
        ch = _new_channel()
        self.addCleanup(ch.stop)
        app = _app(ch, status="disconnected")
        app.poll()
        self.assertEqual(len(app._show_text.calls), 1, "第一次 poll 未上屏状态文字")
        self.assertEqual(app._show_text.calls[0][0], "已断开，正在重连…")
        app.poll()
        self.assertEqual(len(app._show_text.calls), 1,
                         "第二次 poll 仍走完整绘制分支：文字分支没落账 "
                         "_last_frame_serial/_last_channel_idx/_last_channel_status"
                         "（ui-029 → 空闲退避在断线态完全失效）")
        self.assertEqual(app._last_channel_status, "disconnected")
        self.assertEqual(app._last_channel_idx, 0)
        with ch.lock:
            self.assertEqual(app._last_frame_serial, ch.frame_serial,
                             "簿记里的 serial 不是本轮真正看到的值")

    def test_many_polls_draw_text_only_once(self):
        ch = _new_channel()
        self.addCleanup(ch.stop)
        app = _app(ch, status="disconnected")
        for _ in range(20):
            app.poll()
        self.assertEqual(len(app._show_text.calls), 1,
                         "20 轮 poll 里 _show_text 被调 %d 次：每 15ms 一次整段重绘"
                         % len(app._show_text.calls))
        self.assertGreaterEqual(len(app._update_status_overlay.calls), 19,
                                "早退分支仍每轮刷新叠加层（这是设计内的轻量路径）")

    def test_invalidate_media_forces_exactly_one_redraw(self):
        """ui-002 + ui-029 合流：跃迁 bump serial → 下一轮必须重绘，再下一轮又早退。"""
        ch = _new_channel()
        self.addCleanup(ch.stop)
        app = _app(ch, status="disconnected")
        app.poll()
        app.poll()
        self.assertEqual(len(app._show_text.calls), 1)
        ch.status = "auth"
        ch.auth_msg = "口令错误"
        ch._invalidate_media()                     # 真实跃迁收口
        app.poll()
        self.assertEqual(len(app._show_text.calls), 2, "状态跃迁后没重绘（定格旧画面）")
        self.assertEqual(app._show_text.calls[-1][0], "口令错误")
        app.poll()
        self.assertEqual(len(app._show_text.calls), 2, "重绘后未重新落账：下一轮又走完整分支")
        app.poll()
        self.assertEqual(len(app._show_text.calls), 2)

    def test_status_change_alone_forces_redraw(self):
        """判据的状态维度：serial 未变、只改 status，也必须重绘（ui-002 原症状）。"""
        ch = _new_channel()
        self.addCleanup(ch.stop)
        app = _app(ch, status="connecting")
        app.poll()
        self.assertEqual(app._show_text.calls[-1][0], "连接中…")
        ch.status = "disconnected"                 # 只改状态，不 bump serial
        app.poll()
        self.assertEqual(len(app._show_text.calls), 2,
                         "poll 判据丢了状态维度：断线文案永不可达（ui-002）")
        self.assertEqual(app._show_text.calls[-1][0], "已断开，正在重连…")

    def test_stale_frame_is_not_shown_when_disconnected(self):
        """缓存里还有一帧，但状态是断线：必须显示文字，而不是"画面卡住"。"""
        ch = _new_channel()
        self.addCleanup(ch.stop)
        with ch.lock:
            ch.last_frame = np.zeros((4, 4, 3), dtype=np.uint8)
        app = _app(ch, status="disconnected")
        app.poll()
        self.assertEqual(app._frame_rec.calls, [], "断线态仍贴图（旧帧冒充活画面）")
        self.assertEqual(app._show_text.calls[-1][0], "已断开，正在重连…")
        self.assertFalse(app.has_frame)

    def test_receiving_with_frame_still_uses_frame_branch(self):
        """正向对照：收帧态仍走 _show_frame，簿记由 _show_frame 自己落账。"""
        ch = _new_channel()
        self.addCleanup(ch.stop)
        with ch.lock:
            ch.last_frame = np.zeros((4, 4, 3), dtype=np.uint8)
        app = _app(ch, status="receiving")
        app.poll()
        self.assertEqual(len(app._frame_rec.calls), 1)
        self.assertEqual(app._show_text.calls, [])
        self.assertEqual(app._last_channel_idx, 0)
        with ch.lock:
            self.assertEqual(app._last_frame_serial, ch.frame_serial)
        self.assertEqual(app._poll_idle_ms, 15, "刚贴图的一轮不该退避")
        app.poll()
        self.assertEqual(len(app._frame_rec.calls), 1, "无新帧仍重贴图")
        self.assertEqual(app._poll_idle_ms, 33, "A4 §3.2 空闲退避没生效")

    def test_no_channel_branch_stays_responsive(self):
        ch = _new_channel()
        self.addCleanup(ch.stop)
        app = _app(ch)
        app.channels = []
        app.poll()
        self.assertEqual(app._show_text.calls[-1][0], "无频道")
        self.assertEqual(app._poll_idle_ms, 15, "无频道时必须保持灵敏")

    def test_active_idx_change_forces_redraw(self):
        ch = _new_channel()
        self.addCleanup(ch.stop)
        app = _app(ch, status="disconnected")
        app.poll()
        app._last_channel_idx = 7                  # 模拟"曾停在别的频道"
        app.poll()
        self.assertEqual(len(app._show_text.calls), 2,
                         "频道切换未触发重绘（_last_channel_idx 维度失效）")


class TestPollUsesRealBookkeeping(unittest.TestCase):
    """结构侧证据：文字分支的三行簿记确实写在真实源码里（范式同 item97 第三段）。"""

    def test_text_branch_records_all_three_fields(self):
        import inspect
        import re
        src = inspect.getsource(viewer.ViewerApp.poll)
        self.assertRegex(src, r"self\._last_frame_serial\s*=\s*serial",
                         "poll 里找不到文字分支的 serial 落账（ui-029 回退）")
        self.assertRegex(src, r"self\._last_channel_idx\s*=\s*self\.active_idx",
                         "poll 里找不到 active_idx 落账")
        self.assertRegex(src, r"self\._last_channel_status\s*=\s*status",
                         "poll 里找不到 status 落账（判据的状态维度会永久失配）")
        # 三行必须落在"贴图之外"的文字分支里：即出现在 `_show_frame(` 之后
        tail = src[src.rfind("_show_frame("):]
        for name in ("_last_frame_serial", "_last_channel_idx", "_last_channel_status"):
            self.assertRegex(tail, r"self\.%s\s*=" % name,
                             "%s 的落账不在贴图之后的文字分支里（退避仍会失效）" % name)
        self.assertEqual(len(re.findall(r"self\._last_frame_serial\s*=\s*serial", src)), 1,
                         "serial 落账出现多次：分支归属已不可辨")

    def test_poll_judgement_includes_status(self):
        import inspect
        src = inspect.getsource(viewer.ViewerApp.poll)
        self.assertIn("_last_channel_status", src.split("_update_status_overlay")[0],
                      "poll 的早退判据里没有状态维度（ui-002 回退）")


if __name__ == "__main__":
    unittest.main()
