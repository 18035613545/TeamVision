# -*- coding: utf-8 -*-
"""优化方案-2026-09-08 阶段 0（P0 止血）回归测试。

覆盖三项 P0 + 一项同源缺陷：
  A1  host._accept_loop：瞬时 OSError（WSAEMFILE/WSAENOBUFS/ECONNABORTED…）只重试
      不退出；只有 stop_event 置位或监听 socket 已关闭才结束循环，且日志有节流。
  A2  common.data_dir / _config_path / accounts_path / save_config / load_config：
      安装目录不可写时配置、账户、日志统一落到可写数据目录；写失败只告警不抛。
  A3  viewer._panel_refresh_preview 清空分支：用 image="" 而非 image=None，
      避免 PhotoImage 被 GC 后 label 持有已删除图像名 → 每次 configure 抛 TclError。
  B9  viewer._show_text：同样用 image=""，否则 Label（compound 默认 none）只显示
      残留图像、占位文字永远不可见。

hermetic：A1 用假 server + 注入 spawn；A2 用 tmp 目录与模块级 _DATA_DIR 注入；
A3/B9 需要真实 Tk（Tcl 图像生命周期是 Tk 侧行为，无法用 mock 表达），无显示环境时 skip。
"""
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import common
import host
import viewer


# ---------------------------------------------------------------- A1：accept 循环

class _FakeRuntime:
    def __init__(self):
        self.stop_event = threading.Event()


class _FakeServer:
    """按脚本回放的假监听 socket；脚本元素为 "err" 或 (sock, addr)。"""

    def __init__(self, script, fileno_value=0):
        self.script = list(script)
        self.calls = 0
        self._fileno = fileno_value

    def accept(self):
        self.calls += 1
        if not self.script:
            raise OSError("脚本耗尽")
        item = self.script.pop(0)
        if item == "err":
            raise OSError("模拟瞬时错误 WSAEMFILE")
        return item

    def fileno(self):
        return self._fileno


class TestAcceptLoopResilience(unittest.TestCase):
    def test_transient_oserror_is_retried_not_fatal(self):
        # 连续 3 次瞬时错误后成功 accept 一次 → spawn 必须被调用（旧实现第一次就 break）
        server = _FakeServer(["err", "err", "err", ("sock-1", ("1.2.3.4", 5))])
        rt = _FakeRuntime()
        spawned = []

        def spawn(sock, addr, runtime):
            spawned.append((sock, addr))
            runtime.stop_event.set()   # 让循环结束

        with mock.patch.object(host.time, "sleep", lambda _s: None), \
                mock.patch.object(host, "log") as mlog:
            host._accept_loop(server, rt, spawn=spawn)

        self.assertEqual(server.calls, 4)              # 3 次失败都重试了
        self.assertEqual(spawned, [("sock-1", ("1.2.3.4", 5))])
        self.assertTrue(mlog.warning.called)           # 首次失败有告警

    def test_closed_server_socket_exits_without_spam(self):
        # 监听 socket 已关闭（fileno<0）→ 立刻退出，且不刷告警
        server = _FakeServer(["err"], fileno_value=-1)
        rt = _FakeRuntime()
        with mock.patch.object(host.time, "sleep", lambda _s: None), \
                mock.patch.object(host, "log") as mlog:
            host._accept_loop(server, rt, spawn=lambda *a: None)
        self.assertEqual(server.calls, 1)
        self.assertFalse(mlog.warning.called)

    def test_stop_event_prevents_accept(self):
        server = _FakeServer([("sock-1", ("1.2.3.4", 5))])
        rt = _FakeRuntime()
        rt.stop_event.set()
        host._accept_loop(server, rt, spawn=lambda *a: None)
        self.assertEqual(server.calls, 0)

    def test_warning_is_throttled(self):
        # 25 次连续失败 → 只在第 1、20 次记日志（旧行为是每帧一次刷屏）
        server = _FakeServer(["err"] * 25)
        rt = _FakeRuntime()
        original_accept = server.accept

        def accept():
            try:
                return original_accept()
            except OSError:
                if server.calls >= 25:
                    rt.stop_event.set()
                raise

        server.accept = accept
        with mock.patch.object(host.time, "sleep", lambda _s: None), \
                mock.patch.object(host, "log") as mlog:
            host._accept_loop(server, rt, spawn=lambda *a: None)
        self.assertEqual(mlog.warning.call_count, 2)

    def test_default_spawn_starts_daemon_thread(self):
        # 默认 spawn 必须起一个 daemon 线程调用 handle_client
        started = {}

        class _Recorder(threading.Thread):
            def __init__(self, target=None, args=(), daemon=None, **kw):
                super().__init__(target=target, args=args, daemon=daemon)
                started["target"] = target
                started["args"] = args
                started["daemon"] = daemon

            def start(self):
                started["started"] = True

        with mock.patch.object(host.threading, "Thread", _Recorder):
            host._spawn_client("s", ("h", 1), "rt")
        self.assertTrue(started["started"])
        self.assertTrue(started["daemon"])
        self.assertIs(started["target"], host.handle_client)
        self.assertEqual(started["args"], ("s", ("h", 1), "rt"))


# ---------------------------------------------------------------- A2：可写数据目录

class TestDataDirFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tv_stage0_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_falls_back_to_first_writable_candidate(self):
        blocker = os.path.join(self.tmp, "not_a_dir")       # 是文件 → 不可作为目录
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        good = os.path.join(self.tmp, "data")
        self.assertEqual(common.data_dir([blocker, good]), good)
        self.assertTrue(os.path.isdir(good))

    def test_returns_none_when_nothing_writable(self):
        blocker = os.path.join(self.tmp, "not_a_dir")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        self.assertIsNone(common.data_dir([blocker]))

    def test_config_path_uses_data_dir_and_migrates_legacy(self):
        exe_dir = os.path.join(self.tmp, "installed")
        data_dir = os.path.join(self.tmp, "userdata")
        os.makedirs(exe_dir)
        os.makedirs(data_dir)
        with open(os.path.join(exe_dir, "config.json"), "w", encoding="utf-8") as f:
            f.write('{"host": {"fps": 7}}')

        with mock.patch.object(common, "exe_dir", return_value=exe_dir), \
                mock.patch.object(common, "_DATA_DIR", data_dir):
            path = common._config_path()
            self.assertEqual(os.path.dirname(path), data_dir)
            self.assertTrue(os.path.exists(path))          # 旧配置已迁移
            cfg = common.load_config()
            self.assertEqual(cfg["host"]["fps"], 7)        # 用户设置没丢
            self.assertEqual(common.accounts_path(),
                             os.path.join(data_dir, "accounts.json"))

    def test_same_dir_when_exe_dir_writable_keeps_portable_behaviour(self):
        exe_dir = os.path.join(self.tmp, "portable")
        os.makedirs(exe_dir)
        with mock.patch.object(common, "exe_dir", return_value=exe_dir), \
                mock.patch.object(common, "_DATA_DIR", exe_dir):
            self.assertEqual(common._config_path(),
                             os.path.join(exe_dir, "config.json"))

    def test_load_config_returns_defaults_when_write_fails(self):
        blocker = os.path.join(self.tmp, "not_a_dir")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        bad = os.path.join(blocker, "config.json")         # 父路径是文件 → 写入必失败
        with mock.patch.object(common, "_config_path", return_value=bad):
            cfg = common.load_config()                     # 不得抛异常
        self.assertEqual(cfg["host"]["port"], common.DEFAULT_CONFIG["host"]["port"])

    def test_save_config_swallows_write_error(self):
        blocker = os.path.join(self.tmp, "not_a_dir")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        bad = os.path.join(blocker, "config.json")
        with mock.patch.object(common, "_config_path", return_value=bad):
            common.save_config(common.DEFAULT_CONFIG, "host")   # 不得抛异常


class TestAccountSaveFailure(unittest.TestCase):
    def test_register_rolls_back_when_save_fails(self):
        with mock.patch.object(host, "accounts_path",
                               return_value=os.path.join(tempfile.gettempdir(),
                                                         "no_such_dir_tv", "a.json")):
            mgr = host.AccountManager()
            mgr._save = lambda: False                  # 模拟磁盘不可写
            ok, msg = mgr.register("alice", "pw123456")
        self.assertFalse(ok)
        self.assertIn("注册未生效", msg)
        self.assertNotIn("alice", mgr._users)          # 内存已回滚

    def test_authenticate_succeeds_even_if_last_login_save_fails(self):
        tmp = tempfile.mkdtemp(prefix="tv_acct_")
        try:
            path = os.path.join(tmp, "accounts.json")
            with mock.patch.object(host, "accounts_path", return_value=path):
                mgr = host.AccountManager()
                ok, _ = mgr.register("bob", "pw123456")
                self.assertTrue(ok)
                mgr._save = lambda: False              # last_login 落盘失败
                ok2, msg2 = mgr.authenticate("bob", "pw123456")
            self.assertTrue(ok2)                       # 口令正确不得被拒
            self.assertEqual(msg2, "登录成功")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- A3 / B9：Tk 图像

def _make_root():
    """创建（隐藏的）Tk root；无显示环境返回 None。"""
    import tkinter as tk
    try:
        root = tk.Tk()
    except Exception:
        return None
    root.withdraw()
    return root


class TestTkImageLifetime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _make_root()
        if cls.root is None:
            raise unittest.SkipTest("无可用 Tk 显示环境")
        from PIL import Image
        cls.Image = Image

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "root", None) is not None:
            try:
                cls.root.destroy()
            except Exception:
                pass

    def _photo(self):
        from PIL import ImageTk
        return ImageTk.PhotoImage(self.Image.new("RGB", (8, 8), (1, 2, 3)))

    def test_show_text_clears_image_and_makes_text_visible(self):
        import tkinter as tk
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app.label = tk.Label(self.root, text="")
        app._update_status_overlay = lambda: None
        app.has_frame = True                 # 之前显示过画面 → 必须真正切到文字
        app._last_text = None
        app._photo = self._photo()
        app.label.configure(image=app._photo)
        app._last_pos_size = None
        app.user_moved = True                # 跳过窗口定位

        app._show_text("已断开，正在重连…")

        self.assertEqual(app.label.cget("image"), "")          # -image 真的被清空
        self.assertIn("已断开", app.label.cget("text"))
        self.assertIsNone(app._photo)                          # 引用可安全释放
        # 关键回归：清空后再 configure 不得抛 TclError（旧写法会抛）
        app.label.configure(text="再改一次")

    def test_panel_preview_clear_branch_does_not_raise(self):
        import tkinter as tk
        from PIL import ImageTk
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app.running = True
        app.panel = mock.MagicMock()          # after(...) 可接受
        app._panel_visible = lambda: True
        app.active_idx = 0
        app._panel_selected = None
        app._panel_last_idx = 0
        app._panel_last_serial = 3
        app.channels = []                     # 无频道 → 走 clear 分支
        app._preview_label = tk.Label(self.root, text="")
        app._preview_info = tk.Label(self.root, text="")
        photo = ImageTk.PhotoImage(self.Image.new("RGB", (8, 8), (4, 5, 6)))
        app._preview_label.configure(image=photo)
        app._panel_photo = photo

        app._panel_refresh_preview()          # 不得抛 TclError

        self.assertEqual(app._preview_label.cget("image"), "")
        self.assertIsNone(app._panel_photo)
        self.assertEqual(app._preview_label.cget("text"), "暂无画面")
        app._panel_refresh_preview()          # 第二次（旧写法在这里必炸）

    def test_preview_error_log_is_throttled(self):
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app.running = True
        app.panel = mock.MagicMock()
        app._panel_visible = lambda: True
        app.active_idx = 0
        app._panel_selected = None
        app._panel_last_idx = -1
        app._panel_last_serial = -1
        app._panel_photo = None
        app._preview_label = mock.MagicMock()
        app._preview_label.configure.side_effect = RuntimeError("boom")
        app._preview_info = mock.MagicMock()
        ch = mock.MagicMock()
        ch.lock = threading.RLock()
        ch.name = "alice"
        ch.status = "receiving"
        ch.frame_serial = 1
        ch.last_frame = mock.MagicMock()
        app.channels = [ch]

        with mock.patch.object(viewer.cv2, "cvtColor",
                               side_effect=RuntimeError("bad shape")), \
                mock.patch.object(viewer, "log") as mlog:
            app._panel_refresh_preview()
            app._panel_refresh_preview()
            app._panel_refresh_preview()
        self.assertEqual(mlog.warning.call_count, 1)   # 60 秒窗口内只记一次


if __name__ == "__main__":
    unittest.main()
