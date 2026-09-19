# -*- coding: utf-8 -*-
"""第 134 条回归：诊断包脱敏导出 + 公网自检结论贯通（快照 → 面板/报告）。

hermetic：zip 落 tempfile 目录；host_ui/host 用 __new__ 构造，不建真实窗口、
不启动服务。messagebox 一律 patch 掉，避免测试卡在对话框后面。
"""
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

import diag
import host
import host_ui


class TestRedact(unittest.TestCase):

    def test_nested_token_and_password_masked(self):
        cfg = {"host": {"frp": {"token": "SEKRET-frp-token", "public_addr": "a:1"},
                        "auth": {"password": "sekret-pw"}}}
        out = diag.redact(cfg)
        self.assertEqual(out["host"]["frp"]["token"], "***")
        self.assertEqual(out["host"]["auth"]["password"], "***")
        self.assertEqual(out["host"]["frp"]["public_addr"], "a:1")
        # 原对象不能被改动（调用方还要把同一份 cfg 继续用下去）
        self.assertEqual(cfg["host"]["frp"]["token"], "SEKRET-frp-token")

    def test_token_in_list_masked(self):
        out = diag.redact([{"token": "x"}, {"other": "y"}])
        self.assertEqual(out[0]["token"], "***")
        self.assertEqual(out[1]["other"], "y")


class TestRecentLogFiles(unittest.TestCase):

    def test_only_today_and_yesterday_selected(self):
        now = time.time()
        today = time.strftime("%Y%m%d", time.localtime(now))
        yest = time.strftime("%Y%m%d", time.localtime(now - 86400))
        old = time.strftime("%Y%m%d", time.localtime(now - 8 * 86400))
        with tempfile.TemporaryDirectory() as d:
            for name in ("app_%s_pid1.log" % today, "app_%s_pid2.log" % yest,
                         "app_%s_pid3.log" % old, "other.log", "app_bad.log"):
                open(os.path.join(d, name), "w").close()
            picked = {os.path.basename(p) for p in diag.recent_log_files(d, now=now)}
        self.assertEqual(picked, {"app_%s_pid1.log" % today,
                                  "app_%s_pid2.log" % yest})

    def test_missing_dir_returns_empty(self):
        self.assertEqual(diag.recent_log_files(
            os.path.join(tempfile.gettempdir(), "no-such-logs-dir-134")), [])


class TestBuildReportZip(unittest.TestCase):

    def _cfg(self):
        return {"host": {"listen_host": "127.0.0.1", "port": 8000,
                         "frp": {"token": "TOPSECRET-xyz",
                                 "public_addr": "t.example:20000"},
                         "auth": {"enabled": True}}}

    def test_zip_created_and_token_never_plaintext(self):
        with tempfile.TemporaryDirectory() as d:
            path = diag.build_report_zip(d, config=self._cfg(),
                                         logs_dir=os.path.join(d, "missing-logs"),
                                         extra_text="在线观众数: 2")
            self.assertTrue(os.path.isfile(path))
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
                self.assertIn("report.txt", names)
                self.assertIn("config.json", names)
                # 负向：任何成员都不允许出现 token 明文
                for n in zf.namelist():
                    self.assertNotIn(b"TOPSECRET", zf.read(n),
                                     "诊断包成员 %s 泄漏了 token 明文" % n)
                cfg_json = json.loads(zf.read("config.json").decode("utf-8"))
                self.assertEqual(cfg_json["host"]["frp"]["token"], "***")
                report = zf.read("report.txt").decode("utf-8")
                self.assertIn("在线观众数: 2", report)
                self.assertIn("t.example:20000", report)

    def test_no_logs_dir_does_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            path = diag.build_report_zip(d, config={}, logs_dir=None)
            self.assertTrue(zipfile.is_zipfile(path))

    def test_logs_included(self):
        now = time.time()
        today = time.strftime("%Y%m%d", time.localtime(now))
        with tempfile.TemporaryDirectory() as d:
            logs = os.path.join(d, "logs")
            os.makedirs(logs)
            with open(os.path.join(logs, "app_%s_pid9.log" % today), "w") as f:
                f.write("hello\n")
            path = diag.build_report_zip(d, config={}, logs_dir=logs)
            with zipfile.ZipFile(path) as zf:
                self.assertIn("logs/app_%s_pid9.log" % today, zf.namelist())


class TestNetStatusPlumbing(unittest.TestCase):
    """自检结论 → HostRuntime 快照 → 面板文字/诊断报告 的贯通锁。"""

    def _mk_runtime(self):
        rt = host.HostRuntime.__new__(host.HostRuntime)
        rt.clients_lock = threading.Lock()
        rt.clients = {}
        rt.stats_lock = threading.Lock()
        rt.stats = {}
        rt.slot_lock = threading.Lock()
        rt.slot = {"quality": 80, "scale": 1.0, "fps": 30, "bitrate": 0,
                   "monitor": 1, "region": None, "backend": "mss"}
        rt.cfg = {"host": {"port": 8000, "frp": {}, "auth": {}}}
        rt.frp = mock.Mock()
        rt.frp.is_running.return_value = False
        rt._server = None
        rt.bound_addr = "127.0.0.1"
        rt.startup_error = None
        rt._net_status = {"state": "unknown", "detail": "未运行自检", "ts": 0.0}
        return rt

    def test_set_net_status_visible_in_snapshot(self):
        rt = self._mk_runtime()
        rt.set_net_status("fail", "隧道未就绪")
        snap = rt.get_snapshot()
        self.assertEqual(snap["net_status"]["state"], "fail")
        self.assertEqual(snap["net_status"]["detail"], "隧道未就绪")
        self.assertGreater(snap["net_status"]["ts"], 0)

    def test_host_ui_export_uses_snapshot_net_status(self):
        console = host_ui.HostConsole.__new__(host_ui.HostConsole)
        console.runtime = mock.Mock()
        console.runtime.get_snapshot.return_value = {
            "clients": [{"addr": "1.2.3.4:1"}],
            "net_status": {"state": "fail", "detail": "隧道本地地址未指向监听端口",
                           "ts": 1.0},
        }
        console.runtime.cfg = {}
        console.root = None
        captured = {}

        def fake_zip(dest, *, config, logs_dir=None, extra_text="", pid=None):
            captured["extra_text"] = extra_text
            return os.path.join(str(dest), "fake.zip")

        # ui-008 后导出在工作线程执行：_post_ui 直通 + 轮询等 showinfo
        console._post_ui = lambda fn: fn()
        with mock.patch.object(host_ui.diag, "build_report_zip", fake_zip), \
                mock.patch.object(host_ui, "data_dir",
                                  return_value=tempfile.gettempdir()), \
                mock.patch.object(host_ui.messagebox, "showinfo") as info:
            console._on_export_diag()
            for _ in range(200):
                if info.called:
                    break
                time.sleep(0.02)
        self.assertIn("在线观众数: 1", captured["extra_text"])
        self.assertIn("失败（隧道本地地址未指向监听端口）", captured["extra_text"])
        info.assert_called_once()

    def test_host_ui_export_unknown_state_reports_not_run(self):
        console = host_ui.HostConsole.__new__(host_ui.HostConsole)
        console.runtime = mock.Mock()
        console.runtime.get_snapshot.return_value = {
            "clients": [], "net_status": {"state": "unknown", "ts": 0.0}}
        console.runtime.cfg = {}
        console.root = None
        captured = {}

        def fake_zip(dest, *, config, logs_dir=None, extra_text="", pid=None):
            captured["extra_text"] = extra_text
            return os.path.join(str(dest), "fake.zip")

        console._post_ui = lambda fn: fn()
        with mock.patch.object(host_ui.diag, "build_report_zip", fake_zip), \
                mock.patch.object(host_ui, "data_dir",
                                  return_value=tempfile.gettempdir()), \
                mock.patch.object(host_ui.messagebox, "showinfo") as info:
            console._on_export_diag()
            for _ in range(200):
                if captured:
                    break
                time.sleep(0.02)
        self.assertIn("最近公网自检: 未运行", captured["extra_text"])


if __name__ == "__main__":
    unittest.main()
