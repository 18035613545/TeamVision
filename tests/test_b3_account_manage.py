# -*- coding: utf-8 -*-
"""B3 账户管理面板（实现方案 §5）：AccountManager 本地运维接口的行为锁定。

面板本身是纯本地 UI（host_ui._open_accounts），逻辑全部落在 list_users/remove/
set_password 三个方法上；这里按验收标准逐条锁：closed 策略可绕开自助注册限制、
写盘失败回滚、损坏文件不崩、改密后旧口令立即失效且与 D1 挑战-应答兼容。
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import host
import common


class _MgrCase(unittest.TestCase):
    """每个用例一个独立的 accounts.json（accounts_path 桩到临时文件）。"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix="-accounts.json")
        os.close(fd)
        os.remove(self.path)  # 从"文件不存在"起步，AccountManager 建表时自动落盘
        self._patcher = mock.patch.object(host, "accounts_path",
                                          return_value=self.path)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for p in (self.path, self.path + ".tmp"):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        for name in os.listdir(os.path.dirname(self.path) or "."):
            if name.startswith(os.path.basename(self.path) + ".corrupt-"):
                try:
                    os.remove(os.path.join(os.path.dirname(self.path), name))
                except OSError:
                    pass

    def _mgr(self):
        return host.AccountManager()

    def _reload(self):
        """新建实例 = 重新从磁盘读，模拟共享端重启。"""
        return host.AccountManager()


class TestListUsers(_MgrCase):

    def test_rows_have_no_credential_material(self):
        m = self._mgr()
        self.assertTrue(m.register("alice", "pw-a")[0])
        self.assertTrue(m.register("bob", "pw-b")[0])
        self.assertTrue(m.authenticate("alice", "pw-a")[0])
        rows = m.list_users()
        self.assertEqual(len(rows), 2)
        users = [r[0] for r in rows]
        self.assertIn("alice", users)
        self.assertIn("bob", users)
        for user, created, last in rows:
            self.assertIsInstance(created, str)
            self.assertTrue(created)  # created 一定存在
            if user == "alice":
                self.assertTrue(last)  # 登录过 → last_login 非空
            else:
                self.assertEqual(last, "")
        # 绝不泄漏 salt/hash 字段（返回三元组的扁平结构本身就是防线）
        self.assertNotIn("salt", json.dumps(rows))
        self.assertNotIn("hash", json.dumps(rows))

    def test_corrupt_file_gives_empty_list_not_crash(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json at all")
        m = self._mgr()
        self.assertEqual(m.list_users(), [])
        # 第 27 条留存文件在位，面板据空列表给提示即可
        backups = [n for n in os.listdir(os.path.dirname(self.path) or ".")
                   if ".corrupt-" in n]
        self.assertEqual(len(backups), 1)


class TestRemove(_MgrCase):

    def test_delete_persists_across_reload(self):
        m = self._mgr()
        m.register("alice", "pw")[1]
        m.register("bob", "pw")[1]
        ok, msg = m.remove("alice")
        self.assertTrue(ok, msg)
        self.assertEqual([r[0] for r in m.list_users()], ["bob"])
        self.assertEqual([r[0] for r in self._reload().list_users()], ["bob"])

    def test_remove_unknown_user(self):
        m = self._mgr()
        self.assertEqual(m.remove("ghost"), (False, "账号不存在"))

    def test_save_failure_rolls_back(self):
        m = self._mgr()
        m.register("alice", "pw")
        with mock.patch.object(m, "_save", return_value=False):
            ok, msg = m.remove("alice")
        self.assertFalse(ok)
        self.assertIn("未生效", msg)
        self.assertEqual([r[0] for r in m.list_users()], ["alice"])


class TestSetPassword(_MgrCase):

    def test_old_password_dies_new_password_works(self):
        m = self._mgr()
        m.register("alice", "old-pw")
        ok, msg = m.set_password("alice", "new-pw")
        self.assertTrue(ok, msg)
        self.assertFalse(m.authenticate("alice", "old-pw")[0])
        self.assertTrue(m.authenticate("alice", "new-pw")[0])
        # 持久化：重启后仍是新口令
        self.assertTrue(self._reload().authenticate("alice", "new-pw")[0])

    def test_challenge_path_uses_new_salt_after_change(self):
        """B3 与 D1 的衔接：改密重写 salt 后，挑战-应答按新 salt/iter 仍能登录。"""
        m = self._mgr()
        m.register_derived("alice", "a" * 32, "b" * 64)
        self.assertTrue(m.set_password("alice", "new-pw")[0])
        salt, iter_ = m.challenge_for("alice")
        self.assertNotEqual(salt, "a" * 32)  # 盐已更换
        dk = common.derive_dk("new-pw", salt, iter_)
        nonce = os.urandom(16)
        self.assertTrue(m.verify_login_proof("alice", nonce,
                                             common.challenge_mac(dk, nonce, "alice"))[0])
        old_dk = common.derive_dk("new-pw-wrong", salt, iter_)
        self.assertFalse(m.verify_login_proof("alice", nonce,
                                              common.challenge_mac(old_dk, nonce, "alice"))[0])

    def test_validation_and_rollback(self):
        m = self._mgr()
        self.assertEqual(m.set_password("", "pw"), (False, "用户名与密码不能为空"))
        self.assertEqual(m.set_password("alice", ""), (False, "用户名与密码不能为空"))
        self.assertEqual(m.set_password("ghost", "pw"), (False, "账号不存在"))
        m.register("alice", "old")
        with mock.patch.object(m, "_save", return_value=False):
            ok, msg = m.set_password("alice", "new")
        self.assertFalse(ok)
        self.assertIn("未生效", msg)
        self.assertTrue(m.authenticate("alice", "old")[0])  # 回滚后旧口令仍有效


class TestClosedPolicyDecoupled(_MgrCase):
    """验收核心：注册策略只约束观看端自助注册，本地面板增删不受影响。"""

    def test_local_add_works_under_closed_policy(self):
        m = self._mgr()
        auth_cfg = {"enabled": True, "allow_register": "closed"}
        # _register_allowed 是观看端路径的闸——本地面板 register 不经它
        allowed, _reason = host._register_allowed(mock.Mock(), auth_cfg, "1.2.3.4:5")
        self.assertFalse(allowed)
        self.assertTrue(m.register("carol", "pw")[0])
        self.assertTrue(m.authenticate("carol", "pw")[0])


class TestPanelWiring(unittest.TestCase):
    """不实例化 Tk 的最小接线检查：入口方法存在且读 runtime.accounts。"""

    def test_host_console_has_accounts_entry(self):
        import host_ui
        self.assertTrue(callable(getattr(host_ui.HostConsole, "_open_accounts",
                                         None)))


if __name__ == "__main__":
    unittest.main()
