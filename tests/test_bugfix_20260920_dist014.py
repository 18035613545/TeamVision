# -*- coding: utf-8 -*-
"""dist-014 / T-08 回归：诊断包必须带 PID 收日志，且"本进程没日志"时要有兜底。

第 6 轮审查第十章 T-08 的判定（`docs/BUG审查报告-2026-09-20.md`）：
  `tests/test_diag.py:92-102` 的 `test_logs_included` **不带 pid** 调
  `build_report_zip`（即 auth-012 混包修复之前的原行为）并断言日志入包，
  **与修复方向相反**；`fake_zip(..., pid=None)` 又把新参数当可选吞掉，
  没有任何用例断言传 `os.getpid()` → dist-014 因此无人挡。

本文件补三面：
  ① 生产调用点真的传 `pid=os.getpid()`（host_ui._on_export_diag / viewer._export_diag_zip
     两条链路都**真跑一次**，用记录器接住 `build_report_zip` 的实参）；顺带钉住
     ui-008：压缩必须在名为 diag-export 的工作线程里跑，`_post_ui` 不得就地执行。
  ② `recent_log_files(pid=...)` 的兜底：本进程一条日志都没有（正是"崩了之后重启
     再导出"的场景）时，退化为按 mtime 取最近 N 份，而不是返回空列表把唯一能说明
     问题的东西过滤掉。
  ③ `build_report_zip` 在 report.txt 里**显式写明**本次到底收了谁的日志
     （含"可能包含同机另一端 host/viewer 的日志"这条风险告知）。

改回缺陷形态会红在哪一行（逐行论证）：
  ① 把 `host_ui.py:813` / `viewer.py:3250` 的 `pid=os.getpid()` 删掉 →
     `test_host_ui_call_site_passes_current_pid` / `test_viewer_call_site_passes_current_pid`
     的 `kwargs["pid"] == os.getpid()` 红（记录到 None）。
  ② 把 `diag.py:96-110` 的兜底段（`if out or pid is None: return out` 之后的
     跨进程收集）删掉 → `test_falls_back_to_recent_files_when_current_pid_absent`
     收到空列表红，`test_report_text_discloses_fallback` 也找不到"已改为收集最近"。
  ③ 把 `diag.py:74` 的 `_pid_matches` 过滤整个去掉（退回 auth-012 混包）→
     `test_current_pid_logs_present_excludes_other_pids` 红（混包防护不得被兜底顺带拆掉）。
  ④ 把 `_log_collection_note` 的分支改成静默 → 三条 report.txt 断言全红。
`TestFallbackShapeIsDiscriminating` 复刻"无兜底"的旧返回值，实证同一目录列表下
旧实现收不到任何日志——即 ② 的红不是编排出来的。
"""
import ast
import inspect
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diag
import host_ui
import viewer

_TODAY = time.strftime("%Y%m%d")
_YESTERDAY = time.strftime("%Y%m%d", time.localtime(time.time() - 86400))


def _logs_dir(files, mtime_spread=True):
    """在临时目录里造日志文件（绝不写进仓库工作树）。"""
    d = tempfile.mkdtemp(prefix="tv-diag-logs-")
    for i, name in enumerate(files):
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write("line for %s\n" % name)
        if mtime_spread:
            # 让第 i 个文件明显更"新"（兜底按 mtime 排序取前 N 份）
            ts = time.time() + i
            os.utime(p, (ts, ts))
    return d


class TestPidFilterStillGuardsMixing(unittest.TestCase):
    """auth-012 的那半边不能被 dist-014 的兜底顺手拆掉。"""

    def test_current_pid_logs_present_excludes_other_pids(self):
        mine = "app_%s_pid%d.log" % (_TODAY, os.getpid())
        other = "app_%s_pid%d.log" % (_TODAY, os.getpid() + 1)
        d = _logs_dir([mine, other])
        picked = [os.path.basename(p) for p in diag.recent_log_files(d, pid=os.getpid())]
        self.assertEqual(picked, [mine],
                         "本进程有日志时仍收了别的进程 → auth-012 混包回归")

    def test_no_pid_means_all_as_before(self):
        """`pid=None` 就是 test_diag 旧用例的形状：收两端全部日志。

        留这条是为了**说明**旧断言为什么没有判定力（它测的是混包原行为），
        不是为它背书——真正的生产路径必须传 pid，见下面的调用点用例。
        """
        mine = "app_%s_pid%d.log" % (_TODAY, os.getpid())
        other = "app_%s_pid%d.log" % (_TODAY, os.getpid() + 1)
        d = _logs_dir([mine, other])
        self.assertEqual(len(diag.recent_log_files(d)), 2)


class TestFallbackWhenCurrentPidHasNoLogs(unittest.TestCase):
    """dist-014 核心：崩溃/重启后导出，本进程一条日志都没有 → 必须兜底。"""

    def test_falls_back_to_recent_files_when_current_pid_absent(self):
        foreign = ["app_%s_pid%d.log" % (_TODAY, 900000 + i) for i in range(3)]
        d = _logs_dir(foreign)
        picked = sorted(os.path.basename(p)
                        for p in diag.recent_log_files(d, pid=os.getpid()))
        self.assertEqual(picked, sorted(foreign),
                         "本进程无日志时返回空列表：诊断包把唯一能说明问题的东西过滤掉了")

    def test_fallback_prefers_newest_and_caps_count(self):
        names = ["app_%s_pid%d.log" % (_TODAY, 700000 + i) for i in range(10)]
        d = _logs_dir(names)
        picked = [os.path.basename(p) for p in diag.recent_log_files(d, pid=os.getpid())]
        self.assertEqual(len(picked), diag._FALLBACK_FILES,
                         "兜底份数不受控（应取 mtime 最近 %d 份）" % diag._FALLBACK_FILES)
        self.assertEqual(picked, sorted(names, reverse=True)[:diag._FALLBACK_FILES],
                         "兜底没按 mtime 从新到旧取（收了最旧的却丢了崩溃现场）")

    def test_fallback_ignores_out_of_window_files(self):
        old = "app_20200101_pid777.log"
        fresh = "app_%s_pid778.log" % _YESTERDAY
        d = _logs_dir([old, fresh])
        picked = [os.path.basename(p) for p in diag.recent_log_files(d, pid=os.getpid())]
        self.assertEqual(picked, [fresh], "兜底把时间窗外的老日志也塞进包里（隐私面扩大）")

    def test_truly_empty_dir_returns_empty(self):
        d = _logs_dir([])
        self.assertEqual(diag.recent_log_files(d, pid=os.getpid()), [])


class TestReportTextDisclosesCollection(unittest.TestCase):
    """report.txt 必须写明"收了谁的日志"，缺失也要写明缺失。"""

    def _report(self, logs_dir, pid):
        dest = tempfile.mkdtemp(prefix="tv-diag-out-")
        path = diag.build_report_zip(dest, config={"host": {}},
                                     logs_dir=logs_dir, pid=pid)
        with zipfile.ZipFile(path) as zf:
            return zf.read("report.txt").decode("utf-8"), zf.namelist(), path

    def test_report_text_discloses_fallback(self):
        foreign = ["app_%s_pid%d.log" % (_TODAY, 810001 + i) for i in range(2)]
        d = _logs_dir(foreign)
        report, names, _p = self._report(d, os.getpid())
        self.assertIn("诊断包日志", report, "report.txt 里没有日志收集情况一行（dist-014）")
        self.assertIn("本进程(pid=%s)尚无日志文件" % os.getpid(), report)
        self.assertIn("已改为收集最近", report)
        self.assertIn("host/viewer", report, "跨进程收集却没做混包风险告知")
        for name in foreign:
            self.assertIn("logs/" + name, names, "兜底收集的文件没真的入包")

    def test_report_text_says_current_pid_when_own_logs_exist(self):
        mine = "app_%s_pid%d.log" % (_TODAY, os.getpid())
        other = "app_%s_pid%d.log" % (_TODAY, os.getpid() + 5)
        d = _logs_dir([mine, other])
        report, names, _p = self._report(d, os.getpid())
        self.assertIn("收集本进程(pid=%s)日志 1 份" % os.getpid(), report)
        self.assertIn("logs/" + mine, names)
        self.assertNotIn("logs/" + other, names, "带 pid 的导出把另一端日志打进了包")

    def test_report_text_says_missing_when_nothing_to_collect(self):
        d = _logs_dir([])
        report, _names, _p = self._report(d, os.getpid())
        self.assertIn("没有可收集的日志文件", report)

    def test_report_text_handles_none_logs_dir(self):
        report, names, _p = self._report(None, os.getpid())
        self.assertIn("未指定日志目录", report)
        self.assertEqual([n for n in names if n.startswith("logs/")], [])

    def test_extra_text_and_note_both_present(self):
        d = _logs_dir(["app_%s_pid1.log" % _TODAY])
        dest = tempfile.mkdtemp(prefix="tv-diag-out-")
        path = diag.build_report_zip(dest, config={"host": {"auth": {"enabled": True}}},
                                     logs_dir=d, extra_text="在线观众数: 2",
                                     pid=os.getpid())
        with zipfile.ZipFile(path) as zf:
            report = zf.read("report.txt").decode("utf-8")
        self.assertIn("在线观众数: 2", report)
        self.assertIn("诊断包日志", report, "extra_text 传了就把日志收集说明挤掉了")


class TestProductionCallSitesPassPid(unittest.TestCase):
    """ui-008 / dist-014 的接线：两条生产链路都必须传 `pid=os.getpid()`。"""

    @staticmethod
    def _fail(msg):
        raise AssertionError(msg)

    def _pid_kwarg_of(self, func):
        """从源码里找 `build_report_zip(...)` 调用上的 pid 实参形态。"""
        tree = ast.parse(textwrap.dedent(inspect_source(func)))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) != "build_report_zip":
                continue
            kw = {k.arg: k.value for k in node.keywords}
            self.assertIn("pid", kw, "调用 build_report_zip 时没传 pid（dist-014 回退）")
            value = kw["pid"]
            self.assertIsInstance(value, ast.Call, "pid 实参不是 os.getpid() 调用")
            self.assertEqual(getattr(value.func, "attr", None), "getpid")
            self.assertEqual(getattr(value.func, "value", None).id
                             if isinstance(getattr(value.func, "value", None), ast.Name)
                             else None, "os", "pid 取的不是 os 模块的 getpid")
            return
        self.fail("源码里找不到 build_report_zip 调用")

    def test_host_ui_call_site_passes_current_pid(self):
        self._pid_kwarg_of(host_ui.HostConsole._on_export_diag)

    def test_viewer_call_site_passes_current_pid(self):
        self._pid_kwarg_of(viewer.ViewerApp._export_diag_zip)

    def test_host_ui_actually_passes_pid_at_runtime(self):
        seen = {}
        gate = threading.Event()

        def recorder(dest_dir, **kwargs):
            seen["kwargs"] = kwargs
            seen["thread"] = threading.current_thread().name
            gate.set()
            return os.path.join(dest_dir, "nothing.zip")

        posted = []
        console = host_ui.HostConsole.__new__(host_ui.HostConsole)
        console._diag_exporting = False
        console.root = None
        console.runtime = _SnapshotRuntime()
        console._set_status = lambda *a: None
        console._post_ui = lambda fn: posted.append(fn)   # 只登记，不就地执行（ui-008）
        tmp = tempfile.mkdtemp(prefix="tv-diag-dest-")
        with mock.patch.object(host_ui.diag, "build_report_zip", recorder), \
                mock.patch.object(host_ui, "data_dir", lambda: tmp):
            console._on_export_diag()
            self.assertTrue(gate.wait(5.0), "诊断包导出从未执行（工作线程没起来）")
        self.assertEqual(seen["kwargs"].get("pid"), os.getpid(),
                         "共享端导出没传当前 PID：崩溃后重启再导出等于空包")
        self.assertEqual(seen["thread"], "diag-export",
                         "压缩不在 diag-export 工作线程里跑（ui-008 回退成主线程同步压缩）")
        self.assertEqual(len(posted), 1, "_post_ui 没被调用：结果回不到主线程")
        # ui-008 的重入守卫：导出进行中再点一次不得起第二条导出线程
        seen.clear()
        gate.clear()
        console._diag_exporting = True
        with mock.patch.object(host_ui, "messagebox", mock.Mock()) as mb,                 mock.patch.object(host_ui.diag, "build_report_zip", recorder),                 mock.patch.object(host_ui, "data_dir", lambda: tmp):
            console._on_export_diag()
        self.assertTrue(mb.showinfo.called, "连点时没有「正在导出」提示")
        self.assertFalse(gate.wait(0.2), "重入守卫失效：并发了第二次导出")

    def test_viewer_actually_passes_pid_at_runtime(self):
        seen = {}
        gate = threading.Event()

        def recorder(dest_dir, **kwargs):
            seen["kwargs"] = kwargs
            seen["thread"] = threading.current_thread().name
            gate.set()
            return os.path.join(dest_dir, "nothing.zip")

        posted = []
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app._diag_exporting = False
        app.panel = None
        app.cfg = {"host": {}}
        app._post_ui = lambda fn: posted.append(fn)
        tmp = tempfile.mkdtemp(prefix="tv-diag-dest-")
        with mock.patch.object(viewer.diag, "build_report_zip", recorder), \
                mock.patch.object(viewer, "data_dir", lambda: tmp), \
                mock.patch.object(viewer, "messagebox", mock.Mock()):
            app._export_diag_zip("report text")
            self.assertTrue(gate.wait(5.0), "观看端诊断包导出从未执行")
        self.assertEqual(seen["kwargs"].get("pid"), os.getpid(),
                         "观看端导出没传当前 PID（与共享端不同口径）")
        self.assertEqual(seen["thread"], "diag-export")


class _SnapshotRuntime:
    """host_ui._on_export_diag 要的两个面：get_snapshot() 与 cfg（都是真实读法）。"""

    def __init__(self):
        self.cfg = {"host": {"port": 5700, "frp": {}, "auth": {}}}

    def get_snapshot(self):
        return {"clients": [], "net_status": {"state": "ok", "detail": "127.0.0.1:1"}}


def inspect_source(func):
    return inspect.getsource(func)


class TestFallbackShapeIsDiscriminating(unittest.TestCase):
    """负向对照：无兜底的旧实现在同一目录列表下收不到任何日志。"""

    @staticmethod
    def _old_recent_log_files(logs_dir, days=2, now=None, pid=None):
        out = []
        if not logs_dir or not os.path.isdir(logs_dir):
            return out
        base = now if now is not None else time.time()
        wanted = {time.strftime("%Y%m%d", time.localtime(base - i * 86400))
                  for i in range(days)}
        for name in sorted(os.listdir(logs_dir)):
            if not diag._LOG_DATE_RE.match(name):
                continue
            if diag._LOG_DATE_RE.match(name).group(1) not in wanted:
                continue
            if pid is not None and not diag._pid_matches(name, pid):
                continue                     # 旧实现：到这里就没了，没有兜底
            p = os.path.join(logs_dir, name)
            if os.path.isfile(p):
                out.append(p)
        return out

    def test_old_shape_returns_nothing_after_a_crash(self):
        foreign = ["app_%s_pid%d.log" % (_TODAY, 880001 + i) for i in range(3)]
        d = _logs_dir(foreign)
        self.assertEqual(self._old_recent_log_files(d, pid=os.getpid()), [],
                         "对照失效：旧形态本应在崩溃重启后收不到任何日志")
        self.assertEqual(len(diag.recent_log_files(d, pid=os.getpid())), 3,
                         "真实实现没兜底（dist-014 复发）")

    def test_old_shape_still_mixes_packages_without_pid(self):
        """旧用例（不带 pid）在旧形态与新形态下都收全部日志 → 该断言无判定力。"""
        foreign = ["app_%s_pid%d.log" % (_TODAY, 890001 + i) for i in range(2)]
        d = _logs_dir(foreign)
        self.assertEqual(self._old_recent_log_files(d), diag.recent_log_files(d),
                         "对照失效：不带 pid 的调用两侧本应同值")


class TestZipFileNameCarriesPid(unittest.TestCase):
    """ui-021：两端同秒导出不得互相覆盖（文件名必须带 PID）。"""

    def test_file_name_contains_pid(self):
        d = _logs_dir([])
        dest = tempfile.mkdtemp(prefix="tv-diag-out-")
        path = str(diag.build_report_zip(dest, config={}, logs_dir=d, pid=os.getpid()))
        self.assertIn("pid%d" % os.getpid(), os.path.basename(path))
        self.assertFalse(os.path.exists(path + ".part"), ".part 临时文件未清理")
        self.assertTrue(zipfile.is_zipfile(path))


if __name__ == "__main__":
    unittest.main()
