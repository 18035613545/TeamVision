# -*- coding: utf-8 -*-
"""第 62 条回归：frpc 不得因 _external_running latch 变成杀不掉的孤儿。

旧 bug：`_external_running` 一旦在早先某次 start() 检测到樱花服务时置真就永不复位。之后
樱花服务消失、start() 自己 spawn 了 frpc，但 latch 仍为真 → stop() 走「樱花启动器正在托管」
分支直接返回、永不 terminate 这个自spawn 的 frpc → host 退出后孤儿进程占着隧道与本地端口，
下次 start() 又起第二个 frpc（同 token 同隧道，樱花侧互踢）；is_running() 也永远报 True。

修复：start() 成功 spawn 自有 frpc 时把 `_external_running` 复位为 False（不变量：
_external_running 与自持有的 _proc 互斥）。

hermetic：mock 掉 `_sakura_service_running`（tasklist 探测）与 `subprocess.Popen`，
frpc_path 指向 sys.executable（绝对路径且存在，跳过搜索），不真正启动任何进程。
"""
import sys
import unittest
from unittest import mock

import host


class FakeProc:
    """伪 frpc 子进程：poll/terminate/wait + 空 stdout（_read_output 迭代它立即结束）。"""

    def __init__(self, alive=True):
        self._alive = alive
        self.terminated = False
        self.stdout = []

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0


def _mgr():
    cfg = {"host": {"frp": {
        "token": "tk", "tunnel_ids": "1", "frpc_path": sys.executable}}}
    return host.FrpManager(cfg)


def _start_spawning(mgr, fake):
    """让 start() 走自spawn 分支：外部服务检测为 False，Popen 返回 fake。"""
    with mock.patch.object(host.FrpManager, "_sakura_service_running",
                           return_value=False), \
            mock.patch.object(host.subprocess, "Popen", return_value=fake):
        return mgr.start()


class TestExternalLatchReset(unittest.TestCase):
    def test_spawn_own_proc_clears_external_latch(self):
        mgr = _mgr()
        mgr._external_running = True   # 模拟早先检测到樱花服务留下的 latch
        fake = FakeProc()
        ok, _msg = _start_spawning(mgr, fake)
        self.assertTrue(ok)
        self.assertIs(mgr._proc, fake)
        self.assertFalse(mgr._external_running)   # 第 62 条核心：latch 被复位

    def test_stop_terminates_own_proc_after_latch_cleared(self):
        mgr = _mgr()
        mgr._external_running = True
        fake = FakeProc()
        _start_spawning(mgr, fake)
        mgr.stop()
        self.assertTrue(fake.terminated)          # 自spawn 的 frpc 被终止，不再是孤儿
        self.assertIsNone(mgr._proc)
        self.assertFalse(mgr.is_running())

    def test_true_external_stop_terminates_nothing(self):
        # 正控：纯外部托管（无自spawn proc）时 stop() 不 terminate，is_running 仍报 True。
        # net-043（2026-09-24）：外部托管从"采信 latch"改为**实探服务进程**，本用例
        # 必须显式声明 SakuraFrpService 在跑（否则 stop() 会正确判定"未托管"）。
        mgr = _mgr()
        mgr._external_running = True
        mgr._proc = None
        with mock.patch.object(host.FrpManager, "_sakura_service_running",
                               staticmethod(lambda: True)):
            msg = mgr.stop()
            self.assertIn("樱花", msg)
            self.assertTrue(mgr.is_running())

    def test_latched_state_self_heals_when_service_gone(self):
        # 负控/不变量：latch 泄漏进 owned-proc 状态时，旧实现会跳过 terminate 留下孤儿。
        # net-043（2026-09-24）：外部托管改为实探服务进程——服务不在时 latch 立即复位，
        # stop() 走正常路径把自持有的 frpc 杀掉（不再需要重启 host 才能收拾）。
        mgr = _mgr()
        fake = FakeProc()
        mgr._proc = fake
        mgr._external_running = True   # 人为构造 bug 状态（fix #1 保证 start() 后不会这样）
        with mock.patch.object(host.FrpManager, "_sakura_service_running",
                               staticmethod(lambda: False)):
            msg = mgr.stop()
        self.assertTrue(fake.terminated, "服务已退出但 latch 未复位：frpc 成孤儿")
        self.assertFalse(mgr._external_running, "latch 未复位（面板恒显运行中）")
        self.assertIn("frpc 已终止", msg)

    def test_is_running_probes_external_service(self):
        """net-043：is_running() 必须实探——用户关掉 SakuraFrpService 后面板不能恒显运行中。"""
        mgr = _mgr()
        mgr._external_running = True
        with mock.patch.object(host.FrpManager, "_sakura_service_running",
                               staticmethod(lambda: False)):
            self.assertFalse(mgr.is_running())
        self.assertFalse(mgr._external_running)


if __name__ == "__main__":
    unittest.main()
