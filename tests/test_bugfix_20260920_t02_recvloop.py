# -*- coding: utf-8 -*-
"""T-02 / net-015 + net-030 回归：真实 `host._client_recv_loop` 的计时与评估时机。

第 6 轮审查第十章 T-02（`docs/BUG审查报告-2026-09-20.md`）的判定：
  旧用例只锁了**泵侧**"on_buf 传消费条数"这条契约（test_protocol_single_point.py:156），
  真正的修复点 `host.py:2205-2226`（`_on_buf` 的记账口径）与 `host.py:2250-2262`
  （`on_tick=` 接线）零覆盖；"把 `elif n_msgs>0` 改回 `else` 无测试失败（net-030
  因此无人挡）"。本文件因此**不**再测 message_pump 本身，而是跑真实
  `_client_recv_loop`，配真实 `host.ClientInfo`、真实 `common.message_pump`、真实
  socketpair：

  net-030（评估时机）：滴字节但永不凑齐一条消息时，泵必须在**有数据**的轮次上
    评估超时并退出——旧实现只在 select 空转满 1 秒才调 `on_idle`，于是"每
    0.4~0.9 秒投 1 字节"的对端让判据一次都不执行（报告实测 12 秒内 on_idle 调用 0 次）。
  net-015（记账口径）：`info.last_rx_at`（半开检测）与 `partial_since`（单条消息
    总时限）只能在**真正消费掉完整消息**时刷新；任意字节刷新 = 滴字节无限续命。

改回缺陷形态会红在哪一行（逐行论证）：
  ① 删掉 `host.py:2262` 的 `on_tick=_on_idle,`（退回 net-030 之前）→
     `test_dribble_exits_on_data_path`：on_tick 从未被泵调用 → tick_calls==0 红；
     且退出改由 select 空转 1 秒后的 on_idle 给出 → idle_calls>0 红、
     "退出距最后一滴字节 < 0.9 秒" 红；`TestSourceWiring.test_pump_is_given_on_tick` 红。
  ② 把 `host.py:2216` 的 `elif n_msgs > 0:` 改回 `else:`（net-015 的缺陷形态）→
     `test_partial_bytes_do_not_renew_the_clocks` 的 assertIn("单条消息超时") 红、
     `test_dribble_exits_on_data_path` 的 joined 红（泵永不退出）。
  ③ 只把 `info.note_rx(now)` 挪出 `n_msgs > 0` 分支 →
     `test_partial_bytes_do_not_renew_the_clocks` 与
     `test_dribbling_does_not_renew_half_open_clock` 的 last_rx_at 断言红。
本项涉真实 socket/线程，纯函数变异体实测不适用，故除逐行论证外，另在
`TestOldAccountingWouldNotExit` 里复刻旧记账闭包，实证它对同一串滴字节"永不判超时"
——即本文件断言具备判定力，不是自我实现。
"""
import ast
import inspect
import os
import socket
import struct
import sys
import textwrap
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common
import host


def _partial_header(total=100000):
    """一条"声明 total 字节"的半截控制消息头（kind=MSG_CTRL，永不凑齐）。"""
    return bytes([common.MSG_CTRL]) + struct.pack(">I", total)


class _Runtime:
    """最小 runtime：`_client_recv_loop` 只读 `stop_event` 与 `msg_timeout`。

    刻意用真实类型（threading.Event / float）：`msg_timeout` 必须**真的存在**，
    否则命中的是 `getattr(runtime, "msg_timeout", 15.0)` 的缺省值，测试就变成在测
    15 秒（第 6 轮 T-04 批评的正是这类"替身太薄、生产代码的兜底分支替它过关"）。
    """

    def __init__(self, msg_timeout=1.0):
        self.stop_event = threading.Event()
        self.msg_timeout = msg_timeout


def _capture_closures(msg_timeout=2.0, idle_s=60.0):
    """用"只录不跑"的泵替身，把 `_client_recv_loop` 构造出的真实闭包取出来。

    被替换的只有泵体（common.message_pump 的骨架），计时闭包、ClientInfo、
    safe_float 解析全部是真的——这样能零时序依赖地检查记账口径。
    """
    box = {}

    def recorder(sock, **kw):
        box.update(kw)
        return "录制结束"

    rt = _Runtime(msg_timeout=msg_timeout)
    info = host.ClientInfo(("127.0.0.1", 54321))
    with mock.patch.object(host, "message_pump", recorder):
        reason = host._client_recv_loop(object(), info, rt, ("127.0.0.1", 54321),
                                        {"keepalive_idle_s": idle_s}, {})
    assert reason == "录制结束"
    return box, info


class _PumpSpy:
    """包一层真实 `common.message_pump`：只记录回调接线，泵体仍是真的。"""

    def __init__(self, capture_into=None):
        self.idle_calls = 0
        self.tick_calls = 0
        self.last_tick_at = None
        self._capture = capture_into

    def __call__(self, sock, **kw):
        if self._capture is not None:
            self._capture.update(kw)
        on_idle = kw.get("on_idle")
        on_tick = kw.get("on_tick")
        if on_idle is not None:
            def idle(now, buf, _f=on_idle, _s=self):
                _s.idle_calls += 1
                return _f(now, buf)
            kw["on_idle"] = idle
        if on_tick is not None:
            def tick(now, buf, _f=on_tick, _s=self):
                _s.tick_calls += 1
                _s.last_tick_at = now
                return _f(now, buf)
            kw["on_tick"] = tick
        return common.message_pump(sock, **kw)


class TestDribbleExitsOnDataPath(unittest.TestCase):
    """net-030 核心：滴字节且永不凑齐消息 → 泵必须在数据轮次上超时退出。"""

    def setUp(self):
        self.client, self.server = socket.socketpair()
        self.addCleanup(self.client.close)
        self.addCleanup(self.server.close)
        self.runtime = _Runtime(msg_timeout=1.0)
        self.info = host.ClientInfo(("127.0.0.1", 54321))
        self.feed_times = []
        self.stop_feed = threading.Event()
        self.addCleanup(self.stop_feed.set)

    def _feed(self):
        # 每 0.05 秒滴 1 字节：远小于泵内 select 的 1 秒超时 → select 永不空转
        while not self.stop_feed.is_set():
            try:
                self.client.sendall(b"\x00")
            except OSError:
                return
            self.feed_times.append(time.monotonic())
            time.sleep(0.05)

    def _run_loop(self, timeout=8.0):
        out = {}
        done = threading.Event()

        def run():
            spy = _PumpSpy()
            self.spy = spy
            with mock.patch.object(host, "message_pump", spy):
                out["reason"] = host._client_recv_loop(
                    self.server, self.info, self.runtime, ("127.0.0.1", 54321),
                    {"keepalive_idle_s": 60}, {})
            out["at"] = time.monotonic()
            done.set()

        th = threading.Thread(target=run, daemon=True)
        th.start()
        self.assertTrue(done.wait(timeout),
                        "泵未在 %s 秒内退出：滴字节即可永久占用接收线程/fd/连接名额（net-030）"
                        % timeout)
        th.join(2.0)
        return out

    def test_dribble_exits_on_data_path(self):
        self.client.sendall(_partial_header())
        started = time.monotonic()
        self.info.last_rx_at = started
        feeder = threading.Thread(target=self._feed, daemon=True)
        feeder.start()
        out = self._run_loop()
        self.stop_feed.set()
        feeder.join(2.0)
        reason = out["reason"] or ""
        self.assertIn("单条消息超时", reason,
                      "退出原因不是单条消息超时（%r）：计时口径或评估时机已回退" % reason)
        self.assertGreaterEqual(self.spy.tick_calls, 3,
                                "on_tick 没有在数据轮次上被调用（on_tick= 接线丢失）")
        self.assertEqual(self.spy.idle_calls, 0,
                         "超时判定仍只在 select 空转时评估（on_idle 被调 %d 次）"
                         % self.spy.idle_calls)
        self.assertGreaterEqual(self.spy.last_tick_at, started + 1.0,
                                "on_tick 提前判超时：合法链路还没给够时限就被切")
        self.assertLess(out["at"] - self.feed_times[-1], 0.9,
                        "退出发生在最后一滴字节 0.9 秒之后 → 判超时的是 select 空转"
                        "（on_idle）而不是数据路径上的 on_tick")
        self.assertLess(out["at"] - started, 4.0)

    def test_dribbling_does_not_renew_half_open_clock(self):
        """net-015 半边：滴字节不得刷新 `info.last_rx_at`（半开检测时钟）。"""
        self.client.sendall(_partial_header())
        started = time.monotonic()
        self.info.last_rx_at = started
        feeder = threading.Thread(target=self._feed, daemon=True)
        feeder.start()
        out = self._run_loop()
        self.stop_feed.set()
        feeder.join(2.0)
        self.assertIn("单条消息超时", out["reason"])
        self.assertEqual(self.info.last_rx_at, started,
                         "任意字节刷新了 last_rx_at：滴字节可无限绕过 60 秒半开检测")

    def test_complete_message_then_dribble_still_times_out(self):
        """真实端到端：先收一条完整 ping（续命一次），再滴字节 → 仍必须超时退出。"""
        self.client.sendall(common.pack_msg({"action": common.Act.PING, "t": 7}))
        self.client.sendall(_partial_header())
        self.info.last_rx_at = 0.0
        started = time.monotonic()
        feeder = threading.Thread(target=self._feed, daemon=True)
        feeder.start()
        out = self._run_loop()
        self.stop_feed.set()
        feeder.join(2.0)
        self.assertIn("单条消息超时", out["reason"])
        self.assertGreater(self.info.last_rx_at, 0.0,
                           "完整 ping 没被记为「客户端还活着」（note_rx 该刷未刷）")
        self.assertLess(self.info.last_rx_at - started, 2.0,
                        "last_rx_at 被滴字节持续刷新（net-015 回退）")
        # pong 真的回到了观看端（说明这条完整消息是被 _dispatch_ctrl 消费掉的）
        self.client.settimeout(2.0)
        got = common.recv_msg(self.client)
        self.assertEqual(got.get("action"), common.Act.PONG)
        self.assertEqual(got.get("t"), 7)


class TestAccountingCallbacks(unittest.TestCase):
    """net-015/net-030 的闭包本体：直接驱动真实 `_on_buf`/`_on_idle`（零时序依赖）。"""

    def test_on_tick_is_the_same_predicate_as_on_idle(self):
        """`on_tick` 必须接线，且与 `on_idle` 同一判据（别接个空函数糊弄）。"""
        box, _info = _capture_closures()
        self.assertIn("on_tick", box, "_client_recv_loop 没传 on_tick（net-030 回退）")
        self.assertIsNotNone(box["on_tick"], "on_tick 传了 None（等于没接）")
        self.assertIs(box["on_tick"], box["on_idle"],
                      "on_tick 与 on_idle 不是同一判据：数据轮次与空转轮次的超时口径会漂移")

    def test_partial_bytes_do_not_renew_the_clocks(self):
        box, info = _capture_closures(msg_timeout=2.0)
        on_buf, on_idle = box["on_buf"], box["on_idle"]
        t0 = 1000.0
        info.last_rx_at = t0 - 1.0
        buf = bytearray(_partial_header())
        on_buf("pre_empty", buf, t0, 0)              # 半截消息的第一段
        for k in range(3):                           # 之后每 0.5 秒滴一段、永不凑齐
            on_buf("post", buf, t0 + 0.5 * (k + 1), 0)
        self.assertEqual(info.last_rx_at, t0 - 1.0,
                         "n_msgs==0 仍刷新了 last_rx_at（net-015 的 else 分支回退）")
        self.assertIsNone(on_idle(t0 + 1.9, buf),
                          "时限未到即判超时：把慢速合法链路当攻击")
        self.assertIn("单条消息超时", on_idle(t0 + 2.1, buf))

    def test_complete_message_renews_both_clocks(self):
        box, info = _capture_closures(msg_timeout=2.0)
        on_buf, on_idle = box["on_buf"], box["on_idle"]
        t0 = 1000.0
        info.last_rx_at = t0 - 100.0
        buf = bytearray(_partial_header())
        on_buf("pre_empty", buf, t0, 0)
        on_buf("post", buf, t0 + 1.9, 0)             # 滴了 1.9 秒仍未凑齐
        empty = bytearray()
        on_buf("post", empty, t0 + 2.0, 1)           # 这一轮凑齐并消费 1 条，缓冲区清空
        self.assertEqual(info.last_rx_at, t0 + 2.0,
                         "收到完整消息却没刷新半开检测时钟（会把活连接判死）")
        self.assertIsNone(on_idle(t0 + 2.1, empty),
                          "n_msgs>0 没重置 partial_since：合法链路被判超时")
        # 没有半截消息在途 → 单条消息时限此后不再参与判定（只剩 60 秒半开检测）
        self.assertIsNone(on_idle(t0 + 30.0, empty))
        self.assertIn("空闲超时", on_idle(t0 + 62.5, empty))

    def test_partial_message_after_a_complete_one_is_timed_from_now(self):
        """缓冲区还留着半截消息时，起点重置为"现在"（buf 非空 → partial_since=now）。"""
        box, info = _capture_closures(msg_timeout=2.0)
        on_buf, on_idle = box["on_buf"], box["on_idle"]
        t0 = 1000.0
        info.last_rx_at = t0
        buf = bytearray(_partial_header())
        on_buf("post", buf, t0, 1)                   # 消费掉 1 条，同时又来了半截
        self.assertIsNone(on_idle(t0 + 1.9, buf))
        self.assertIn("单条消息超时", on_idle(t0 + 2.1, buf))

    def test_half_open_clock_still_fires(self):
        """空闲超时（半开连接）分支不能被 net-030 的加密评估弄丢。"""
        box, info = _capture_closures(msg_timeout=30.0, idle_s=5.0)
        on_idle = box["on_idle"]
        t0 = 1000.0
        info.last_rx_at = t0
        self.assertIsNone(on_idle(t0 + 4.0, bytearray()))
        self.assertIn("空闲超时", on_idle(t0 + 6.0, bytearray()))

    def test_msg_timeout_floor_is_honoured(self):
        """runtime.msg_timeout 越界时按 safe_float 钳到下限 1.0（不是回退 15 秒）。"""
        box, _info = _capture_closures(msg_timeout=0.1)   # 低于下限 1.0 → 钳到 1.0
        on_buf, on_idle = box["on_buf"], box["on_idle"]
        buf = bytearray(_partial_header())
        on_buf("pre_empty", buf, 1000.0, 0)
        self.assertIsNone(on_idle(1000.9, buf))
        self.assertIn("单条消息超时", on_idle(1001.1, buf))


class TestOldAccountingWouldNotExit(unittest.TestCase):
    """负向对照：复刻 net-015 之前的记账闭包，实证它对同一串滴字节"永不判超时"。"""

    @staticmethod
    def _old_closures(info, idle_s=60.0, msg_timeout=2.0):
        partial_since = [None]

        def on_idle(now, buf):
            if now - info.last_rx_at > idle_s:
                return "空闲超时（半开连接）"
            if partial_since[0] is not None and now - partial_since[0] > msg_timeout:
                return "单条消息超时"
            return None

        def on_buf(event, buf, now, n_msgs=0):
            # 旧形态：pre_empty 与 post 都无条件重置起点，且任意字节刷新半开时钟
            partial_since[0] = now
            info.note_rx(now)

        return on_idle, on_buf

    def test_old_shape_survives_the_same_dribbling(self):
        info = host.ClientInfo(("127.0.0.1", 54321))
        info.last_rx_at = 1000.0
        on_idle, on_buf = self._old_closures(info)
        buf = bytearray(_partial_header())
        for k in range(40):                       # 滴 20 秒（时限只有 2 秒）
            t = 1000.0 + 0.5 * k
            on_buf("post", buf, t, 0)
            self.assertIsNone(on_idle(t, buf), "对照失效：旧形态本应永不判超时")
        self.assertEqual(info.last_rx_at, 1019.5,
                         "对照失效：旧形态本应由任意字节刷新 last_rx_at")
        # 同样的输入喂给真实闭包 → 越过 2 秒时限那一轮就必然判超时
        box, info2 = _capture_closures(msg_timeout=2.0)
        info2.last_rx_at = 1000.0
        real_on_buf, real_on_idle = box["on_buf"], box["on_idle"]
        real_on_buf("pre_empty", buf, 1000.0, 0)
        first_hit = None
        for k in range(40):
            t = 1000.0 + 0.5 * k
            real_on_buf("post", buf, t, 0)
            if real_on_idle(t, buf) is not None:
                first_hit = t
                break
        self.assertIsNotNone(first_hit,
                             "真实闭包对 20 秒滴字节从不判超时（net-015 回退）")
        self.assertAlmostEqual(first_hit, 1002.5, delta=0.5,
                               msg="判超时的时点没落在 2 秒时限上（口径漂移）")
        self.assertEqual(info2.last_rx_at, 1000.0,
                         "真实闭包被任意字节续命了（net-015 回退）")


class TestSourceWiring(unittest.TestCase):
    """结构侧证据（范式同 test_item97_stop_race.py 第三段）。"""

    def setUp(self):
        self.src = textwrap.dedent(inspect.getsource(host._client_recv_loop))
        self.tree = ast.parse(self.src)

    def test_pump_is_given_on_tick(self):
        call = next(n for n in ast.walk(self.tree)
                    if isinstance(n, ast.Call)
                    and getattr(n.func, "id", None) == "message_pump")
        kw = {k.arg for k in call.keywords}
        self.assertIn("on_tick", kw,
                      "message_pump 未接 on_tick：滴字节的对端让 select 永不空转，"
                      "超时判定一次都不执行（net-030）")
        self.assertIn("on_buf", kw)
        self.assertIn("on_idle", kw)

    def test_accounting_gated_on_consumed_messages(self):
        """`_on_buf` 里"续命"必须挂在以 `n_msgs` 为条件的分支内，不得落在裸 else。

        缺陷形态（`elif n_msgs > 0:` → `else:`）会让 note_rx / partial_since 出现在
        没有 n_msgs 守卫的位置，本用例与行为用例同时红。
        """
        fn = self.tree.body[0]
        on_buf = next(n for n in ast.walk(fn)
                      if isinstance(n, ast.FunctionDef) and n.name == "_on_buf")

        def chain_tests(node):
            """一个 If 连同其 elif 链上的全部条件。"""
            tests = [node.test]
            orelse = node.orelse
            while orelse and isinstance(orelse[0], ast.If):
                tests.append(orelse[0].test)
                orelse = orelse[0].orelse
            return tests

        n_msgs_guarded = set()     # 位于"条件含 n_msgs"的分支子树内的节点
        pre_empty_guarded = set()
        for node in ast.walk(on_buf):
            if not isinstance(node, ast.If):
                continue
            for idx, test in enumerate(chain_tests(node)):
                bucket = (n_msgs_guarded if "n_msgs" in ast.dump(test)
                          else pre_empty_guarded if "pre_empty" in ast.dump(test)
                          else None)
                if bucket is None:
                    continue
                if idx == 0:
                    stmts = node.body
                else:                       # elif/else 挂在最外层 If 的 orelse 链上
                    walk = node
                    for _ in range(idx):
                        walk = walk.orelse[0]
                    stmts = walk.body if idx < len(chain_tests(node)) else []
                for stmt in stmts:
                    bucket.update(id(x) for x in ast.walk(stmt))
        self.assertTrue(n_msgs_guarded,
                        "_on_buf 里没有以 n_msgs 为条件的分支（net-015 回退成无条件续命）")
        touched = []
        for node in ast.walk(on_buf):
            if isinstance(node, ast.Call) and \
                    getattr(node.func, "attr", None) in ("note_rx",):
                touched.append(node)
            elif isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "partial_since"
                    for t in node.targets):
                touched.append(node)
        self.assertTrue(touched, "_on_buf 不再触碰 partial_since / note_rx（对照失效）")
        for node in touched:
            ok = (id(node) in n_msgs_guarded
                  or (isinstance(node, ast.Assign) and id(node) in pre_empty_guarded))
            self.assertTrue(ok, "%s 出现在没有 `n_msgs > 0` 守卫的分支里"
                            % type(node).__name__)


if __name__ == "__main__":
    unittest.main()
