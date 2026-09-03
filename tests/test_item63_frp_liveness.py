# -*- coding: utf-8 -*-
"""第 63 条回归：frp 启动必须验活，秒退不得假报「已启动」。

旧 bug：start() 在 Popen 后不确认子进程是否活过第一秒就返回 True。token/隧道 ID 写错时
frpc 秒退，界面却显示「frpc 已启动（隧道: X）」，唯一线索是几行 info 日志。

修复：spawn 后出 _lock 轮询约 1 秒（每 50ms 一次 proc.poll()）；若期间退出，等读取线程
drain 完输出，把最后一行作为原因返回 (False, "frpc 启动后立即退出（rc=…）：…")，并清 _proc。

hermetic：mock `_sakura_service_running`（tasklist）与 `subprocess.Popen`，frpc_path 指向
sys.executable（绝对路径且存在，跳过搜索），不真正启动任何进程。FakeProc 用计时模拟秒退/存活。
"""
import sys
import time
import unittest
from unittest import mock

import host


class FakeProc:
    """伪 frpc：exit_after=None 表示存活；否则经过该秒数后 poll() 返回 rc。"""

    def __init__(self, exit_after=None, output=(), rc=1):
        self._t0 = time.monotonic()
        self._exit_after = exit_after
        self._rc = rc
        self.stdout = list(output)
        self.terminated = False

    def _exited(self):
        return self._exit_after is not None and \
            (time.monotonic() - self._t0) >= self._exit_after

    def poll(self):
        return self._rc if self._exited() else None

    @property
    def returncode(self):
        return self._rc if self._exited() else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return self._rc


def _mgr():
    cfg = {"host": {"frp": {
        "token": "tk", "tunnel_ids": "1", "frpc_path": sys.executable}}}
    return host.FrpManager(cfg)


def _start(mgr, fake):
    with mock.patch.object(host.FrpManager, "_sakura_service_running",
                           return_value=False), \
            mock.patch.object(host.subprocess, "Popen", return_value=fake):
        return mgr.start()


class TestFrpLiveness(unittest.TestCase):
    def test_immediate_exit_reports_failure_with_reason(self):
        mgr = _mgr()
        fake = FakeProc(exit_after=0.0,
                        output=["login to server failed: invalid token"], rc=1)
        t0 = time.monotonic()
        ok, msg = _start(mgr, fake)
        self.assertFalse(ok)
        self.assertIn("立即退出", msg)
        self.assertIn("invalid token", msg)   # 捕获了 frpc 最后一行输出作为原因
        self.assertIsNone(mgr._proc)          # 已清，不会留下"假活"句柄
        self.assertLess(time.monotonic() - t0, 0.6)  # 秒退被立即捕获，没有空等满 1 秒

    def test_mid_window_exit_is_caught(self):
        # frpc 撑过 0.3 秒才退（接近真实"连上服务器后被拒"）：仍应被 1 秒窗口捕获。
        mgr = _mgr()
        fake = FakeProc(exit_after=0.3, output=["tunnel not found"], rc=2)
        ok, msg = _start(mgr, fake)
        self.assertFalse(ok)
        self.assertIn("立即退出", msg)
        self.assertIn("tunnel not found", msg)
        self.assertIsNone(mgr._proc)

    def test_surviving_proc_reports_success(self):
        mgr = _mgr()
        fake = FakeProc(exit_after=None)      # 全程存活
        ok, msg = _start(mgr, fake)
        self.assertTrue(ok)
        self.assertIn("已启动", msg)
        self.assertIs(mgr._proc, fake)
        self.assertTrue(mgr.is_running())

    def test_exit_without_output_falls_back_to_hint(self):
        mgr = _mgr()
        fake = FakeProc(exit_after=0.0, output=[], rc=1)  # 无任何输出
        ok, msg = _start(mgr, fake)
        self.assertFalse(ok)
        self.assertIn("详见日志", msg)        # 回退到通用提示而非空字符串


if __name__ == "__main__":
    unittest.main()
