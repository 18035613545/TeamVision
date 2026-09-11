# -*- coding: utf-8 -*-
"""第 131 条（反作弊/安全加固）回归测试。

覆盖四组改动：
1) common.DEFAULT_CONFIG 的安全默认值（只监听本机 + 准入开启）与自助注册策略归一；
2) 1.x 遗留配置的一次性迁移（0.0.0.0 + 准入关闭 → 安全默认，写回并带 config_version）；
3) 自助注册策略 open / bootstrap / closed 的服务端判定；
4) frpc 检测不再 spawn tasklist（改为 Toolhelp32 快照）与 onedir 目录搜索范围。
"""

import copy
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

import common
import host
import viewer


class _FakeAccounts:
    def __init__(self, count=0):
        self._count = count

    def count(self):
        return self._count


class _FakeRuntime:
    """只提供注册策略判定需要的 accounts.count()。"""

    def __init__(self, count=0):
        self.accounts = _FakeAccounts(count)


class TestSecureDefaults(unittest.TestCase):
    """新装默认值：只监听本机 + 需要登录，且注册策略取值合法。"""

    def test_listen_host_defaults_to_loopback(self):
        self.assertEqual(common.DEFAULT_CONFIG["host"]["listen_host"], "127.0.0.1")

    def test_auth_enabled_by_default(self):
        self.assertTrue(common.DEFAULT_CONFIG["host"]["auth"]["enabled"])

    def test_default_register_policy_is_valid(self):
        policy = common.DEFAULT_CONFIG["host"]["auth"]["allow_register"]
        self.assertIn(policy, common.REGISTER_POLICIES)

    def test_loopback_default_is_not_bind_all(self):
        cfg = copy.deepcopy(common.DEFAULT_CONFIG)
        listen_host, port, bind_all = host._listen_target(cfg)
        self.assertEqual(listen_host, "127.0.0.1")
        self.assertFalse(bind_all, "默认配置不得绑定全网卡")
        self.assertEqual(port, 5700)

    def test_config_version_present(self):
        self.assertEqual(common.DEFAULT_CONFIG["config_version"], common.CONFIG_VERSION)


class TestRegisterPolicyNormalization(unittest.TestCase):

    def test_known_values_pass_through(self):
        for policy in common.REGISTER_POLICIES:
            self.assertEqual(common.register_policy({"allow_register": policy}), policy)

    def test_booleans_map_to_open_closed(self):
        self.assertEqual(common.register_policy({"allow_register": True}), "open")
        self.assertEqual(common.register_policy({"allow_register": False}), "closed")

    def test_missing_or_bogus_falls_back_to_open(self):
        self.assertEqual(common.register_policy({}), "open")
        self.assertEqual(common.register_policy({"allow_register": "nonsense"}), "open")
        self.assertEqual(common.register_policy({"allow_register": None}), "open")
        self.assertEqual(common.register_policy("not-a-dict"), "open")

    def test_case_and_whitespace_tolerated(self):
        self.assertEqual(common.register_policy({"allow_register": " Closed "}), "closed")


class TestRegisterAllowed(unittest.TestCase):

    def test_open_always_allows(self):
        rt = _FakeRuntime(count=5)
        allowed, why = host._register_allowed(rt, {"allow_register": "open"}, "1.2.3.4:1")
        self.assertTrue(allowed)
        self.assertEqual(why, "")

    def test_closed_always_rejects(self):
        rt = _FakeRuntime(count=0)
        allowed, why = host._register_allowed(rt, {"allow_register": "closed"}, "1.2.3.4:1")
        self.assertFalse(allowed)
        self.assertIn("关闭", why)

    def test_bootstrap_allows_first_account_only(self):
        empty = _FakeRuntime(count=0)
        allowed, _ = host._register_allowed(empty, {"allow_register": "bootstrap"}, "a:1")
        self.assertTrue(allowed)
        used = _FakeRuntime(count=1)
        allowed, why = host._register_allowed(used, {"allow_register": "bootstrap"}, "a:1")
        self.assertFalse(allowed)
        self.assertIn("已有账户", why)

    def test_account_manager_count_reflects_registrations(self):
        tmp = tempfile.mkdtemp(prefix="tv_count_")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "accounts.json")
        with mock.patch.object(host, "accounts_path", return_value=path):
            mgr = host.AccountManager()
            self.assertEqual(mgr.count(), 0)
            self.assertTrue(mgr.register("alice", "pw123456")[0])
            self.assertEqual(mgr.count(), 1)


class TestLegacyConfigUpgrade(unittest.TestCase):
    """1.x 配置（0.0.0.0 + 准入关闭）一次性升级到安全默认。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tv_upgrade_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write(self, name, cfg):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        return path

    def test_upgrade_flips_legacy_insecure_values(self):
        raw = {"host": {"listen_host": "0.0.0.0", "auth": {"enabled": False}}}
        changes = common._upgrade_config(raw)
        self.assertEqual(len(changes), 2)
        self.assertEqual(raw["host"]["listen_host"], "127.0.0.1")
        self.assertTrue(raw["host"]["auth"]["enabled"])
        self.assertEqual(raw["config_version"], common.CONFIG_VERSION)

    def test_upgrade_is_idempotent(self):
        raw = {"host": {"listen_host": "0.0.0.0", "auth": {"enabled": False}}}
        self.assertTrue(common._upgrade_config(raw))
        self.assertEqual(common._upgrade_config(raw), [])       # 第二次不再改动

    def test_custom_listen_host_is_preserved(self):
        raw = {"host": {"listen_host": "192.168.1.5", "auth": {"enabled": True}}}
        self.assertEqual(common._upgrade_config(raw), [])
        self.assertEqual(raw["host"]["listen_host"], "192.168.1.5")
        self.assertTrue(raw["host"]["auth"]["enabled"])

    def test_already_enabled_auth_is_not_touched(self):
        raw = {"host": {"listen_host": "0.0.0.0", "auth": {"enabled": True}}}
        changes = common._upgrade_config(raw)
        self.assertEqual(len(changes), 1)                        # 只改监听地址
        self.assertTrue(raw["host"]["auth"]["enabled"])

    def test_load_config_migrates_and_persists(self):
        path = self._write("config.json", {
            "host": {"listen_host": "0.0.0.0", "fps": 20,
                     "auth": {"enabled": False, "auth_timeout": 150}},
            "viewer": {"display_width": 300},
        })
        with mock.patch.object(common, "_config_path", return_value=path):
            cfg = common.load_config()
        # 返回值已经是安全默认
        self.assertEqual(cfg["host"]["listen_host"], "127.0.0.1")
        self.assertTrue(cfg["host"]["auth"]["enabled"])
        # 用户自己的设置没有被顺手清掉
        self.assertEqual(cfg["host"]["fps"], 20)
        self.assertEqual(cfg["viewer"]["display_width"], 300)
        # 磁盘上也写回了，并打上版本号（只迁移一次）
        with open(path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["config_version"], common.CONFIG_VERSION)
        self.assertEqual(on_disk["host"]["listen_host"], "127.0.0.1")
        self.assertTrue(on_disk["host"]["auth"]["enabled"])
        self.assertEqual(on_disk["host"]["fps"], 20)

    def test_load_config_does_not_rewrite_secure_config(self):
        path = self._write("config.json", {
            "config_version": common.CONFIG_VERSION,
            "host": {"listen_host": "127.0.0.1", "auth": {"enabled": True}},
        })
        before = os.path.getmtime(path)
        with mock.patch.object(common, "_config_path", return_value=path):
            common.load_config()
        self.assertEqual(os.path.getmtime(path), before)         # 未发生写回


class TestNoTasklistSubprocess(unittest.TestCase):
    """frpc 检测不得再 spawn tasklist（进程发现不再拉起 cmd.exe）。"""

    def test_sakura_detection_does_not_use_subprocess(self):
        with mock.patch.object(host.subprocess, "run") as run, \
                mock.patch.object(host.subprocess, "Popen") as popen:
            host.FrpManager._sakura_service_running()
        run.assert_not_called()
        popen.assert_not_called()

    def test_iter_process_names_does_not_shell_out(self):
        # 真正的约束是「不拉起子进程」；方法注释里提到 tasklist 是为了记录这次改动
        import inspect
        src = inspect.getsource(host.FrpManager._iter_process_names)
        self.assertNotIn("subprocess", src)
        self.assertNotIn("Popen", src)

    @unittest.skipUnless(sys.platform == "win32", "Toolhelp32 仅 Windows 可用")
    def test_iter_process_names_finds_self(self):
        names = [n.lower() for n in host.FrpManager._iter_process_names()]
        self.assertIn(os.path.basename(sys.executable).lower(), names)

    def test_sakura_service_running_returns_bool(self):
        self.assertIsInstance(host.FrpManager._sakura_service_running(), bool)


class TestAppDirs(unittest.TestCase):
    """frpc 搜索范围：exe 目录 + （冻结运行时）上一级安装根。"""

    def test_source_run_searches_exe_dir(self):
        self.assertIn(common.exe_dir(), common.app_dirs())

    def test_frozen_run_searches_install_root(self):
        bundle = os.path.join(self._tmpdir(), "TeamVision", "fps-host")
        with mock.patch.object(common.sys, "frozen", True, create=True), \
                mock.patch.object(common, "exe_dir", return_value=bundle):
            dirs = common.app_dirs()
        self.assertEqual(dirs[0], bundle)
        self.assertEqual(dirs[1], os.path.dirname(bundle))

    def _tmpdir(self):
        tmp = tempfile.mkdtemp(prefix="tv_dirs_")
        self.addCleanup(shutil.rmtree, tmp, True)
        return tmp


class TestViewerRegisterRejected(unittest.TestCase):
    """服务端关闭自助注册时，观看端要显示原因并停止重试（而不是反复弹框）。"""

    def _channel(self):
        ch = viewer.Channel.__new__(viewer.Channel)
        ch.name = "测试频道"
        ch.username = ""
        ch._password = None
        ch.auth = {}
        ch.owner = None
        ch._anon_ok = False
        ch._auth_probed = True       # 跳过首轮 probe，让下一轮直接询问凭据
        ch._prompt_blocked_until = 0.0
        ch.auth_msg = ""
        return ch

    def test_register_closed_reason_is_surfaced(self):
        ch = self._channel()
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        seen = []

        def serve():
            try:
                req = common.recv_msg(b)
                seen.append(req)
                common.send_msg(b, {"action": "auth_result", "ok": False,
                                    "msg": "共享端已关闭自助注册，请向共享端索取账号"})
            except Exception:  # pragma: no cover - 出错时由主线程断言兜底
                pass

        with mock.patch.object(ch, "_ask_credentials",
                               return_value=("register", "alice", "pw123456")):
            th = threading.Thread(target=serve, daemon=True)
            th.start()
            ok = ch._authenticate(a)
            th.join(timeout=5.0)

        self.assertFalse(ok)
        self.assertEqual(seen and seen[0].get("action"), "register")
        self.assertIn("注册失败", ch.auth_msg)
        self.assertIn("关闭自助注册", ch.auth_msg)

    def test_login_failure_still_clears_credentials(self):
        # 对照组：登录失败路径保持原语义（清空缓存凭据，等下次重连重新询问）
        ch = self._channel()
        ch.username = "alice"
        ch._password = "wrong"
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)

        def serve():
            try:
                common.recv_msg(b)
                common.send_msg(b, {"action": "auth_result", "ok": False,
                                    "msg": "用户名或密码错误"})
            except Exception:  # pragma: no cover
                pass

        th = threading.Thread(target=serve, daemon=True)
        th.start()
        ok = ch._authenticate(a)
        th.join(timeout=5.0)
        self.assertFalse(ok)
        self.assertEqual(ch.username, "")
        self.assertIsNone(ch._password)
        self.assertIn("用户名或密码错误", ch.auth_msg)


class TestDataDirPreference(unittest.TestCase):

    def test_frozen_prefers_localappdata(self):
        local = os.path.join(tempfile.gettempdir(), "tv_localappdata")
        with mock.patch.object(common.sys, "frozen", True, create=True), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": local}):
            candidates = common._data_dir_candidates()
        self.assertEqual(candidates[0], os.path.join(local, "TeamVision"))

    def test_source_run_prefers_exe_dir(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:\\fake"}):
            candidates = common._data_dir_candidates()
        self.assertEqual(candidates[0], common.exe_dir())


if __name__ == "__main__":
    unittest.main()
