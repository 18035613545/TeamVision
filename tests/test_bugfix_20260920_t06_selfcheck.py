# -*- coding: utf-8 -*-
"""T-06 / net-032 + net-033 回归：公网隧道自检必须真的跑起来，也必须真的停得下来。

第 6 轮审查（`docs/BUG审查报告-2026-09-20.md` 第 23/40/64/212/232 行）：
  net-032｜高｜`host.py` 的 `_probe` 循环里写 `delay = 600.0` 却没声明 `nonlocal`，
    Python 编译期就把 `delay` 判成 `_probe` 的局部名 → **首次迭代**读它即
    `UnboundLocalError`，daemon 线程一启动就死、异常无人接。自检从"启动跑一次"
    退化为"从不跑"，第 5 轮为 net-023 做的三项改动全部成为死代码。
  net-033｜中｜`is_running()` 只在 `_start_tunnel_selfcheck` 入口判一次，循环内不复核
    → net-032 一修好就变成"用户停掉 frpc 后仍每 10 分钟对外拨号直到进程退出"。
  T-06 的判定：`tests/` 里**没有任何** `_start_tunnel_selfcheck` 的用例（只有
    test_diag 的替身），所以 595 全绿没拦住它。

本文件按第十四章第 7 条的要求"**真起线程**跑一次"：
  · 起一条真实回环监听模拟公网隧道（真 connect、真 FS02/OK02 握手）；
  · 通过线程名 `frp-selfcheck` 拿到那条真线程并 join，判其生死；
  · 结论走真实 `HostRuntime.set_net_status`（面板与诊断包消费的就是它）。

改回缺陷形态会红在哪一行（逐行论证）：
  ① 把 `host.py:2810/2818/2820-2821` 的 `next_delay` 改回 `delay`（net-032 原形态，
     且不加 `nonlocal`）→ 线程首次迭代 `UnboundLocalError`：
     `test_selfcheck_really_probes_and_records_ok` 等不到任何隧道连接（assertTrue(连接)
     红）、net_status 仍是 unknown 红；结构用例 (3) 红。
  ② 删掉循环内的 `if not runtime.frp.is_running(): return`（`host.py:2814-2816`）→
     `test_selfcheck_stops_when_frp_stops`：线程在第二轮 `_stop_event.wait(600)` 上
     睡死 → `join(5)` 后 `is_alive()` 仍 True → 红。
  ③ 把 `except Exception: log.exception(...)` 兜底去掉 → 结构用例 (4) 红。
  ④ 删掉入口的两条前置判定（无隧道配置 / frpc 未运行）→
     `test_no_thread_without_tunnel_or_frp` 红（会白起线程、白做外连）。
`TestOldThreadBodyShape` 另把 net-032 的线程体复刻成本地函数，实证它必抛
UnboundLocalError——即 (1) 的红不是编排出来的。
"""
import ast
import inspect
import os
import socket
import sys
import viewer
import textwrap
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common
import host


def _runtime(public_addr=None, frp_running=True):
    """真实 HostRuntime 实例（绕开 __init__ 的建线程/开监听），但 set_net_status 是真的。"""
    rt = host.HostRuntime.__new__(host.HostRuntime)
    rt.stop_event = threading.Event()
    rt.stats_lock = threading.Lock()
    rt._net_status = {"state": "unknown", "detail": "", "ts": 0.0}
    rt.cfg = {"host": {"port": 5700, "frp": {"public_addr": public_addr or ""},
                       "auth": {}}}

    class _Frp:
        def __init__(self, running):
            self._running = running
            self.calls = 0

        def is_running(self):
            self.calls += 1
            return self._running

    rt.frp = _Frp(frp_running)
    return rt


def _selfcheck_thread(before, timeout=3.0):
    """取 `_start_tunnel_selfcheck` 新起的那条 `frp-selfcheck` 线程（真线程，不 mock）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for t in threading.enumerate():
            if id(t) not in before and t.name == "frp-selfcheck":
                return t
        time.sleep(0.01)
    return None


class _Tunnel:
    """假公网隧道：真实回环监听 + 真实 FS02/OK02 握手，连接次数可查。"""

    def __init__(self, reply=True, hold=None):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.conns = []
        self.replies = []
        self._reply = reply
        self._hold = hold
        self.accepted = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(conn)
            self.accepted.set()
            try:
                magic = conn.recv(4)
                self.replies.append(magic)
                if self._hold is not None:
                    self._hold.wait(10.0)         # 由测试决定何时完成握手
                if self._reply and magic == common.MAGIC_CLIENT:
                    conn.sendall(common.MAGIC_SERVER)
            except OSError:
                pass

    def addr(self):
        return "127.0.0.1:%d" % self.port

    def stop(self):
        try:
            self.sock.close()
        except OSError:
            pass
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass


class TestSelfcheckReallyRuns(unittest.TestCase):
    """net-032：自检线程必须真的跑到第一次拨号，并把结论写进 _net_status。"""

    def test_selfcheck_really_probes_and_records_ok(self):
        tunnel = _Tunnel()
        self.addCleanup(tunnel.stop)
        rt = _runtime(tunnel.addr())
        before = {id(t) for t in threading.enumerate()}

        host._start_tunnel_selfcheck(rt, 5700, delay=0.05)

        th = _selfcheck_thread(before)
        self.assertIsNotNone(th, "自检线程根本没起来（_start_tunnel_selfcheck 未建线程）")
        self.addCleanup(th.join, 5.0)
        self.assertTrue(tunnel.accepted.wait(5.0),
                        "5 秒内隧道侧没有收到任何回连：自检线程首次迭代即死"
                        "（net-032 的 UnboundLocalError 形态）")
        # 注意：`accepted` 在服务端 accept() 返回的那一刻就置位，而客户端在内核完成
        # 三次握手后就已经连上并立刻发魔数——服务端要到 recv() 之后才把字节记进
        # replies。因此"accepted 一到就断言 replies"是**必输的竞态**（实测每次都红，
        # 与 net-032 无关）。这里改为等 replies 真的落数据。
        got = time.monotonic() + 5.0
        while time.monotonic() < got and not tunnel.replies:
            time.sleep(0.02)
        self.assertEqual(tunnel.replies[:1], [common.MAGIC_CLIENT],
                         "连上了但没发握手魔数（net-023 的 sendall 入 try 又回退了）")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and rt._net_status["state"] != "ok":
            time.sleep(0.02)
        snap = rt._net_status
        self.assertEqual(snap["state"], "ok",
                         "握手成功却没写结论：%r（面板/诊断包将永远显示自检未运行）" % (snap,))
        self.assertIn(str(tunnel.port), snap["detail"])
        self.assertGreater(snap["ts"], 0.0, "结论没带时间戳（_stale 判定无从计算）")
        rt.frp._running = False                    # 交棒给 TestSelfcheckStopsWithTunnel 的复核路径
        rt.stop_event.set()
        th.join(3.0)
        self.assertFalse(th.is_alive())

    def test_failure_path_records_actionable_conclusion(self):
        """隧道指向一个没人听的端口：必须落 fail 结论，而不是静默消失。"""
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()                              # 只留地址，不留监听 → 连接被拒
        rt = _runtime("127.0.0.1:%d" % port)
        self.addCleanup(rt.stop_event.set)         # 收尾：绝不让线程睡满 600 秒复检
        before = {id(t) for t in threading.enumerate()}
        host._start_tunnel_selfcheck(rt, 5700, delay=0.05)
        th = _selfcheck_thread(before)
        self.assertIsNotNone(th, "自检线程没起来")
        # 等线程已过第一次循环内复核（入口 1 次 + 循环 1 次），再停掉 frpc：
        # 翻转过早会让线程在第一轮就 return，测不到 fail 结论
        gate_deadline = time.monotonic() + 3.0
        while rt.frp.calls < 2 and time.monotonic() < gate_deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(rt.frp.calls, 2, "自检循环没做第一次复核（calls=%d）" % rt.frp.calls)
        rt.frp._running = False                   # 让线程在下一轮复核处退出，别外跑
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and rt._net_status["state"] != "fail":
            time.sleep(0.02)
        snap = rt._net_status
        self.assertEqual(snap["state"], "fail",
                         "隧道不可达却没有失败结论：%r（A0 那类故障再次无从归因）" % (snap,))
        self.assertIn(str(port), snap["detail"])
        rt.stop_event.set()
        th.join(3.0)
        self.assertFalse(th.is_alive(), "自检线程不随 stop_event 退出（无法被关停）")

    def test_no_thread_without_tunnel_or_frp(self):
        """入口两条前置判定：没配 public_addr / frpc 未运行时不得起线程、不得外连。"""
        tunnel = _Tunnel()
        self.addCleanup(tunnel.stop)
        for cfg_addr, running in (("", True), (tunnel.addr(), False)):
            with self.subTest(addr=cfg_addr, running=running):
                rt = _runtime(cfg_addr, frp_running=running)
                before = {id(t) for t in threading.enumerate()}
                host._start_tunnel_selfcheck(rt, 5700, delay=0.01)
                time.sleep(0.3)
                self.assertIsNone(_selfcheck_thread(before, timeout=0.05),
                                  "前置条件不满足仍起自检线程（无谓外连/无谓拨号）")
                self.assertEqual(tunnel.conns, [], "未满足前置条件就拨号了")


class TestSelfcheckStopsWithTunnel(unittest.TestCase):
    """net-033：frpc 被停掉后，自检循环必须在下一轮复核处退出，不再对外拨号。"""

    def test_selfcheck_stops_when_frp_stops(self):
        hold = threading.Event()
        gate = threading.Event()          # 隧道侧收到握手后置位，测试据此翻转 frpc 状态
        tunnel = _TunnelHold(hold, gate)
        self.addCleanup(tunnel.stop)
        rt = _runtime(tunnel.addr())
        before = {id(t) for t in threading.enumerate()}

        host._start_tunnel_selfcheck(rt, 5700, delay=0.05)
        th = _selfcheck_thread(before)
        self.assertIsNotNone(th, "自检线程没起来")
        self.addCleanup(th.join, 5.0)
        self.assertTrue(gate.wait(5.0), "自检没有完成第一次握手（net-032 形态）")
        rt.frp._running = False             # 用户停掉 frpc：此刻线程还阻塞在 recv 上
        hold.set()                          # 隧道把 OK02 回出去 → 第一次自检结束

        th.join(5.0)
        self.assertFalse(th.is_alive(),
                         "frpc 已停止，自检线程仍在跑（下一轮会等满 600 秒再对外拨号）"
                         "——net-033：修 net-032 时漏了循环内复核")
        self.assertFalse(rt.stop_event.is_set(),
                         "线程是被 stop_event 带走的：那不是本用例要证的复核路径")
        self.assertEqual(len(tunnel.conns), 1,
                         "frpc 停止后又发生了新的回连（%d 次）" % len(tunnel.conns))
        self.assertGreaterEqual(rt.frp.calls, 3,
                                "循环内根本没再查 is_running（calls=%d 说明只查过入口）"
                                % rt.frp.calls)


class _TunnelHold(_Tunnel):
    """握手要等测试放行才回 OK02 的隧道（用于在固定时点上翻转 frpc 状态）。"""

    def __init__(self, hold, gate):
        self._gate = gate
        super(_TunnelHold, self).__init__(reply=True, hold=hold)

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(conn)
            self.accepted.set()
            try:
                magic = conn.recv(4)
                self.replies.append(magic)
                self._gate.set()                # 通知测试：握手已到"等回包"这一步
                self._hold.wait(10.0)          # 由测试决定何时让自检完成
                if magic == common.MAGIC_CLIENT:
                    conn.sendall(common.MAGIC_SERVER)
            except OSError:
                return


class TestDpiDeclaredBeforeFirstWindow(unittest.TestCase):
    """报告 T-06 的另一半（第十四章第 7 条点名的"真实现"第四类）：
    `enable_dpi_awareness()` 必须**无条件**出现在两端 main() 里第一个建窗口调用之前。

    第 36 条修过一次、第 133 条又把 splash 的 `tk.Tk()` 提到声明之前 → 声明必然失败
    且返回值被丢弃（完全静默），这正是报告说的"复发点"。这里用真实 AST 的行号
    判序，不复制任何生产逻辑：把 `tk.Tk()` 挪到声明之前、或把声明塞回
    `if not args.console:` 里，本类立刻红。
    """

    @staticmethod
    def _calls(func, name):
        """真实源码里所有 `name(...)` 调用：[(lineno, 条件深度)]（深度 0 = main 顶层直挂）。"""
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        main = tree.body[0]
        parents = {}
        for node in ast.walk(main):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def dotted(node):
            parts = []
            while isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            if isinstance(node, ast.Name):
                parts.append(node.id)
            return ".".join(reversed(parts))

        def cond_depth(call):
            depth, node = 0, call
            while node in parents:
                par = parents[node]
                if isinstance(par, (ast.If, ast.While, ast.For)):
                    branches = list(par.body) + list(par.orelse or []) +                         list(getattr(par, "finalbody", None) or [])
                    if any(node is b for b in branches):
                        depth += 1          # 只在"分支体里"才算有条件
                node = par
            return depth

        out = []
        for node in ast.walk(main):
            if isinstance(node, ast.Call) and dotted(node.func).rsplit(".", 1)[-1] == name:
                out.append((node.lineno, cond_depth(node)))
        return sorted(out)

    def _first(self, func, name):
        calls = self._calls(func, name)
        self.assertTrue(calls, "main() 里找不到 %s 调用" % name)
        return calls[0]

    def test_host_order_and_uncondition(self):
        dpi = self._first(host.main, "enable_dpi_awareness")
        tk = self._first(host.main, "Tk")
        self.assertLess(dpi[0], tk[0],
                        "DPI 声明晚于首个 tk.Tk()（第 133 条的回退形态）：Windows 在进程"
                        "第一个 HWND 创建时就把 DPI 感知定死了")
        self.assertEqual(dpi[1], 0,
                         "DPI 声明被塞进 if 分支（capture-005：命令行模式也必须声明）")

    def test_host_checks_return_value_and_leaves_a_trace(self):
        src = textwrap.dedent(inspect.getsource(host.main))
        self.assertRegex(src, r"if not enable_dpi_awareness\(\):[\s\S]{0,200}log\.warning",
                         "声明失败完全静默：面板/日志无从知道（第 36 条的原症状）")

    def test_viewer_order(self):
        dpi = self._first(viewer.main, "enable_dpi_awareness")
        tk = self._first(viewer.main, "Tk")
        self.assertLess(dpi[0], tk[0],
                        "观看端 DPI 声明晚于 splash 的 tk.Tk()：高缩放下文字发虚"
                        "（与 host 侧顺序应当一致）")

    def test_heavy_imports_do_not_preempt_the_declaration(self):
        """声明必须早于任何会自己抬 DPI 等级的导入（dxcam 在模块级调 SetProcessDpiAwareness）。

        真实 main() 里 DPI 声明之前不得出现 `import dxcam` / `_load_heavy()`；
        这条挡住"把重依赖导入提到声明前"的回退（test_startup_order.py 管的是顶层导入）。
        """
        dpi_line = self._first(host.main, "enable_dpi_awareness")[0]
        src = textwrap.dedent(inspect.getsource(host.main)).splitlines()
        # 只扫真实代码行：注释里本来就写着 "import dxcam 自身会抬 DPI 等级" 这类说明
        code = [ln for ln in src[:dpi_line - 1] if not ln.strip().startswith("#")]
        before = os.linesep.join(code) + os.linesep
        for needle in ("import dxcam", "_load_heavy(", "import cv2", "import numpy"):
            self.assertNotIn(needle, before,
                             "DPI 声明之前出现 %r：进程感知等级可能已被别处定死" % needle)


class TestOldThreadBodyShape(unittest.TestCase):
    """负向对照：net-032 的线程体形态（循环内写 delay 却不 nonlocal）必然首次即抛。"""

    @staticmethod
    def _old_probe(stop_event):
        # 外层函数持形参 delay（对应 `_start_tunnel_selfcheck(runtime, port, delay=3.0)`），
        # 线程体是**闭包**：在闭包里给 delay 赋值而不声明 nonlocal，编译期就把 delay
        # 判成 _probe 的局部名 → 读它即 UnboundLocalError。
        delay = 3.0

        def _probe():
            waited = 0.0
            while not stop_event.is_set():
                stop_event.wait(max(0.0, delay - waited))   # ← 首次迭代在此抛
                waited = 0.0
                delay = 600.0                                # 缺 nonlocal
        _probe()

    def test_old_shape_dies_on_first_iteration(self):
        with self.assertRaises(UnboundLocalError):
            self._old_probe(threading.Event())

    def test_fixed_shape_survives_same_input(self):
        """同一形状改成"只写本函数局部 next_delay"后不再抛（= 真实实现的口径）。"""
        def _probe_new(delay, stop_event, ticks):
            next_delay = float(delay)
            waited = 0.0
            while ticks:
                stop_event.wait(min(0.001, max(0.0, next_delay - waited)))
                waited = 0.0
                next_delay = 600.0
                ticks.pop()
        ticks = [1, 2, 3]
        _probe_new(3.0, threading.Event(), ticks)     # 外层同名 delay 不参与
        self.assertEqual(ticks, [])


class TestProbeStructure(unittest.TestCase):
    """结构侧证据（三段式的第三段）：真实 `_probe` 必须同时含三项防护。"""

    def setUp(self):
        src = textwrap.dedent(inspect.getsource(host._start_tunnel_selfcheck))
        self.src = src
        self.tree = ast.parse(src)
        self.probe = next((n for n in ast.walk(self.tree)
                           if isinstance(n, ast.FunctionDef) and n.name == "_probe"), None)
        if self.probe is None:                     # 结构被改掉时给出可读的失败，而非 TypeError
            self.fail("_start_tunnel_selfcheck 里找不到 _probe 线程体")

    def test_probe_exists_with_while_loop(self):
        self.assertIsNotNone(self.probe, "_start_tunnel_selfcheck 里没有 _probe 线程体")
        self.assertTrue(any(isinstance(n, ast.While) for n in ast.walk(self.probe)),
                        "_probe 里没有周期循环")

    def test_delay_is_not_reassigned_in_thread_body(self):
        """net-032：线程体内**不得**给形参 `delay` 赋值（除非显式 nonlocal）。"""
        assigned = [n for n in ast.walk(self.probe)
                    if isinstance(n, (ast.Assign, ast.AugAssign))
                    and any(getattr(t, "id", None) == "delay"
                            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
        declared = [n for n in ast.walk(self.probe) if isinstance(n, ast.Nonlocal)
                    and any(x.arg == "delay" for x in n.names)]
        self.assertFalse(assigned and not declared,
                         "_probe 内给 delay 赋值却没 nonlocal 声明 → 首次迭代即 "
                         "UnboundLocalError（net-032 原形态复发）")

    def test_is_running_rechecked_inside_loop(self):
        """net-033：循环体内必须有 `if not ...is_running(): return`。"""
        found = False
        for node in ast.walk(self.probe):
            if not isinstance(node, ast.While):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.If):
                    continue
                t = sub.test
                if isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not) and \
                        isinstance(t.operand, ast.Call) and \
                        getattr(t.operand.func, "attr", None) == "is_running":
                    if any(isinstance(b, ast.Return) for b in sub.body):
                        found = True
        self.assertTrue(found, "循环内没有 `if not runtime.frp.is_running(): return`"
                              "（停掉 frpc 后仍会周期性对外拨号）")

    def test_thread_body_has_exception_guard(self):
        """net-032 的第二半：整段循环必须被**宽口径** try/except 包住。

        daemon 线程里抛出的异常只有 stderr（windowed 打包下没有 stderr），
        只 catch 具体小类的 handler 等于没兜底——所以要求 `except Exception`
        （或裸 `except:`）。把口径改成 `except ZeroDivisionError:` 本用例即红。
        """
        def broad(handler):
            return handler.type is None or (
                isinstance(handler.type, ast.Name) and handler.type.id == "Exception")
        guarded = any(isinstance(n, ast.Try)
                      and any(isinstance(w, ast.While) for b in n.body
                              for w in ast.walk(b))
                      and any(broad(h) for h in n.handlers)
                      for n in ast.walk(self.probe))
        self.assertTrue(guarded, "_probe 的循环没有被宽口径 try/except 包住"
                              "（自检异常会静默消失）")


if __name__ == "__main__":
    unittest.main()
