# -*- coding: utf-8 -*-
"""第 89 条回归：_perf_loop 的节拍体必须有异常兜底，配置非数字值不得静默杀死性能线程。

旧路径：_perf_loop 的 while 循环体（含 int(cfg["fps"])/float(cfg["scale"]) 等配置读取）
整体无 try/except。一个非数字配置值（手改或 GUI 写入，如 fps 被写成 "abc"）会让 int()/float()
抛 ValueError，异常沿 daemon 线程传播 → 线程静默死亡 → 此后无 1 秒统计日志、无自适应控制，
而采集/编码/发送线程照常，界面看着正常。

修复：把单次评估抽成 _perf_tick()，while 循环用 try/except 兜底调用——异常被 log.exception
记录、本次评估跳过、线程继续；下一拍重读配置，用户改回有效值即自动恢复。统计心跳（性能日志）
在配置读取之前，故 bad config 下心跳仍每秒输出。

hermetic：复刻两种节拍循环（裸调 vs try 兜底），用一个会因「坏配置」抛 ValueError 的 tick
确定性地证明——裸调线程在坏配置下当场死亡（心跳计数停在 1）；兜底线程存活（心跳持续增长、
错误被记录、自适应被跳过），且配置修复后自适应评估自动恢复。不绑定真实 socket / 不起真实
run_server。复刻范式与 test_item66_join_budget.py 一致。
"""
import threading
import time
import unittest


class _Cfg:
    """最小配置替身：fps 可为有效整数字符串或坏值（非数字），模拟手改 / GUI 实时写入。"""

    def __init__(self, fps="60"):
        self.fps = fps


def _tick_body(cfg, counters):
    """复刻 _perf_tick 关键路径：先记一次「统计心跳」，再读配置做自适应评估。

    心跳在配置读取之前（与真实 _perf_loop 一致：性能日志在 int(cfg) 之前），故即便配置坏掉，
    心跳仍应在每拍被记录——前提是循环体有兜底，否则线程在第一拍就死了。
    """
    counters["ticks"] += 1            # 每秒统计心跳（真实代码里是 log.info 性能行）
    base_fps = max(1, int(cfg.fps))   # 自适应评估：坏值在此抛 ValueError
    counters["adaptive_ok"] += 1      # 走到这里说明本拍自适应评估成功
    return base_fps


def _loop_bare(cfg, counters, stop):
    """旧路径：循环体无 try/except，tick 抛异常即沿线程传播、杀死线程。"""
    while not stop.is_set():
        _tick_body(cfg, counters)
        time.sleep(0.01)


def _loop_guarded(cfg, counters, stop):
    """修复后：try 兜底，异常被吞（真实代码里 log.exception），线程继续下一拍。"""
    while not stop.is_set():
        try:
            _tick_body(cfg, counters)
        except Exception:
            counters["errors"] += 1   # 真实代码里是 log.exception(...)
        time.sleep(0.01)


class TestPerfLoopGuard(unittest.TestCase):
    def _run(self, loop_fn, cfg):
        counters = {"ticks": 0, "adaptive_ok": 0, "errors": 0}
        stop = threading.Event()
        t = threading.Thread(target=loop_fn, args=(cfg, counters, stop), daemon=True)
        t.start()
        self.addCleanup(stop.set)
        return t, counters, stop

    def test_bare_loop_dies_on_bad_config(self):
        # 负向对照：坏配置（fps="abc"）下裸调循环第一拍即抛 ValueError、线程当场死亡，
        # 心跳停在 1（证明「静默杀死性能线程 → 此后不再有统计」确凿）。
        cfg = _Cfg(fps="abc")
        t, counters, _ = self._run(_loop_bare, cfg)
        t.join(timeout=1.0)             # 线程应已死，join 立即返回
        self.assertFalse(t.is_alive())  # 线程已死
        self.assertEqual(counters["ticks"], 1)        # 只跳了一拍就死
        self.assertEqual(counters["adaptive_ok"], 0)  # 自适应从未成功

    def test_guarded_loop_survives_bad_config(self):
        # 正向：坏配置下兜底循环存活，心跳持续增长（统计不丢），错误被记录，自适应被跳过。
        cfg = _Cfg(fps="abc")
        t, counters, _ = self._run(_loop_guarded, cfg)
        time.sleep(0.15)                # 约 15 拍（每拍 sleep 0.01）
        self.assertTrue(t.is_alive())   # 线程仍活（未被坏配置杀死）
        self.assertGreaterEqual(counters["ticks"], 5)    # 心跳持续输出
        self.assertGreaterEqual(counters["errors"], 5)   # 每拍异常被兜底记录
        self.assertEqual(counters["adaptive_ok"], 0)     # 坏配置期间自适应评估被跳过

    def test_guarded_loop_recovers_after_config_fixed(self):
        # 自动恢复：兜底线程在配置改回有效值后，无需重启即恢复自适应评估。
        cfg = _Cfg(fps="abc")
        t, counters, _ = self._run(_loop_guarded, cfg)
        time.sleep(0.08)
        self.assertEqual(counters["adaptive_ok"], 0)  # 坏配置期间从未成功
        cfg.fps = "30"                                 # 模拟用户改回有效值（GUI 实时写入）
        time.sleep(0.12)
        self.assertTrue(t.is_alive())                  # 全程存活
        self.assertGreaterEqual(counters["adaptive_ok"], 3)  # 恢复后自适应评估重新成功


if __name__ == "__main__":
    unittest.main()
