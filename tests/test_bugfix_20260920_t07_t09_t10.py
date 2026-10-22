# -*- coding: utf-8 -*-
"""第 6 轮审查 T-07 / T-09 / T-10：三条"测试反向不钉修复"的补测。

判定标准取自报告第十章：**把被修复的代码改回缺陷形态，测试会不会红**。
这三条报告原话（`docs/BUG审查报告-2026-09-20.md` 第十章表）：

  T-07｜`test_b3_account_manage.py:125-138`（auth-011）：改密后从 `challenge_for`
        取回 iter 再派生 dk——实现若仍**继承旧 iter**，两侧同值 → 改前改后都绿。
        缺"旧记录 iter=1000 → 改密后 iter==100000"的直接断言。
  T-09｜`test_b4_update_check.py:67-71, 105-124, 171-177`（dist-005/006/007）：全部
        用例显式传 `cache_path`，"临时名带 PID+线程"与 `data_dir() is None` 分支永不
        执行；无"`checked_at` 在未来"用例（`0 <= age` 可原样回退）；
        `test_network_failure_is_silent` 只 assertIsNone，未断言 INFO 留痕。
  T-10｜`docs/audit/codec.md`（codec-002~009）：`set_fps` 失败路径、`is_key` 门禁
        （codec-012 / codec-013）零回归。

每条用例下方注明"改回哪一行会转红"，并尽量加**负向对照**（把缺陷形态复刻成本地
函数，证明断言确实能区分两代行为——范式同 test_item97_stop_race.py）。
"""
import inspect
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import codec
import common
import host
import update_check

# ---------------------------------------------------------------- T-07

class TestAuth011IterResetOnPasswordChange(unittest.TestCase):
    """T-07：改密必须把 iter **重置为当前常量**，而不是继承旧记录里的值。

    转红的缺陷形态：`host.py` `AccountManager.set_password` 的 `_apply` 里
    `users[user] = dict(record, salt=salt, hash=pw_hash, iter=PBKDF2_ITERATIONS)`
    删掉 `iter=PBKDF2_ITERATIONS`（=旧实现，继承旧记录）→ 本用例红。
    旧用例（test_b3_account_manage.py:125-138）从 challenge_for 取回 iter 再派生，
    两侧同值，因此对"是否继承旧 iter"毫无判定力——这正是 T-07 判的假绿。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tv_t07_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "accounts.json")
        patcher = mock.patch.object(host, "accounts_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mgr = host.AccountManager()

    def _write_disk_with_legacy_iter(self, user, iter_value):
        """绕过 AccountManager 直接落盘一条"旧 iter"记录（模拟常量上调前的存量账户）。"""
        salt = os.urandom(16).hex()
        dk = common.derive_dk("OldPass123", salt, iter_value)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"users": {user: {
                "salt": salt, "hash": dk.hex(), "iter": iter_value,
                "created": "2026-01-01 00:00:00"}}}, f)
        self.mgr._users = self.mgr._read_disk()

    def test_password_change_resets_iter_to_current_constant(self):
        legacy = 1000          # 明显小于当前常量：旧口径会把它一路带进改密后的记录
        self.assertNotEqual(legacy, host.PBKDF2_ITERATIONS)
        self._write_disk_with_legacy_iter("carol", legacy)
        got = self.mgr.challenge_for("carol")
        self.assertEqual(got[1], legacy, "夹具失效：存量 iter 未被原样读出")

        ok, msg = self.mgr.set_password("carol", "NewPass456")
        self.assertTrue(ok, msg)
        rec = self.mgr._users["carol"]
        # 直接断言**落库字段**，而不是"取回来再派生"（那两侧同值，无判定力）
        self.assertEqual(rec.get("iter"), host.PBKDF2_ITERATIONS,
                         "改密后仍继承旧 iter=%r（auth-011 回归）" % rec.get("iter"))
        self.assertNotEqual(rec.get("iter"), legacy)

    def test_legacy_inherit_shape_differs(self):
        """负向对照：复刻"继承旧 iter"的缺陷形态，确认它与真实实现可区分。

        若某个改动让两者结果一致，说明上面的断言没有判定力（T 系列判的就是这个）。
        """
        legacy = 1000
        record = {"salt": "aa" * 16, "hash": "bb" * 32, "iter": legacy}

        def defective(users, user, salt, pw_hash):
            users[user] = dict(record, salt=salt, hash=pw_hash)      # 无 iter= → 继承
            return users[user]["iter"]

        def fixed(users, user, salt, pw_hash):
            users[user] = dict(record, salt=salt, hash=pw_hash,
                               iter=host.PBKDF2_ITERATIONS)          # 真实实现
            return users[user]["iter"]

        u, s, h = {}, "x", "cc" * 32
        self.assertEqual(defective(u, "x", s, h), legacy)
        self.assertEqual(fixed({}, "x", s, h), host.PBKDF2_ITERATIONS)
        self.assertNotEqual(defective({}, "x", s, h), fixed({}, "x", s, h))

    def test_source_writes_iter_explicitly(self):
        """结构侧证据：`set_password` 的落库字典里必须显式出现 iter。"""
        src = inspect.getsource(host.AccountManager.set_password)
        self.assertRegex(src, r"iter\s*=\s*PBKDF2_ITERATIONS",
                         "set_password 未显式写入 iter（auth-011 的修复点）")


# ---------------------------------------------------------------- T-09

class TestUpdateCheckUntestedBranches(unittest.TestCase):
    """T-09：补 dist-005/006/007 三条从未被执行的分支。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tv_t09_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = os.path.join(self.tmp, "update_check.json")

    def test_future_checked_at_is_not_fresh(self):
        """缓存时刻在**未来**（时钟回拨/手改）时不得算新鲜。

        转红的缺陷形态：`update_check.py` 把
        `fresh = (... and 0 <= age < CACHE_TTL_S)` 退回 `age < CACHE_TTL_S`
        —— (now - checked_at) 为负时旧判据恒真，该机器从此**永远不再检查更新**。
        """
        future = time.time() + 86400.0          # 明天才"检查过"
        with open(self.cache, "w", encoding="utf-8") as f:
            json.dump({"checked_at": future, "tag": "v0.0.1"}, f)
        calls = []

        def opener(req, *a, **k):
            calls.append(1)
            raise RuntimeError("模拟断网")

        update_check.check_update({"update_check": {"enabled": True}},
                                  now=time.time(), opener=opener,
                                  cache_path=self.cache)
        self.assertTrue(calls, "未来时刻的缓存被判成新鲜 → 永久不再检查（dist-006 回归）")

    def test_negative_age_guard_present_in_source(self):
        src = inspect.getsource(update_check.check_update)
        self.assertRegex(src, r"0\s*<=\s*age",
                         "总时限年龄判定缺 0 <= 下界（dist-006 的修复点）")

    def test_no_data_dir_returns_none_without_network(self):
        """`data_dir()` 为 None 时必须直接返回，且**绝不触网**。

        转红的缺陷形态：删掉 `if not base: return None` —— 旧实现会把 None 拼进
        os.path.join 抛 TypeError（dist-005 记的就是这个）。
        所有既有 b4 用例都显式传 cache_path，因此这条分支此前零执行。
        """
        def boom(*a, **k):
            raise AssertionError("无可写数据目录时不得发起网络请求")

        with mock.patch.object(update_check, "data_dir", return_value=None):
            self.assertIsNone(update_check.check_update(
                {"update_check": {"enabled": True}}, opener=boom, cache_path=None))

    def test_temp_name_carries_pid_and_thread(self):
        """dist-005：临时文件名的唯一性（host/viewer 同机运行不互踩）。"""
        src = inspect.getsource(update_check.check_update)
        self.assertRegex(src, r'"\.tmp"\s*%\s*\(.*os\.getpid\(\).*threading\.get_ident\(\)\)'
                         r"|\.tmp[^\n]*%[^\n]*os\.getpid\(\)",
                         "更新检查缓存临时名未带 PID")
        self.assertIn("threading.get_ident()", src, "更新检查缓存临时名未带线程号")

    def test_network_failure_leaves_info_trace_and_no_temp_residue(self):
        """dist-007 的"失败必须留痕"+ cfg-006 的"写失败不得留临时残渣"。

        旧用例只 assertIsNone（判不了有没有留痕）；而写失败路径此前不清理临时名，
        磁盘上会永久留下 `update_check.json.<pid>.<tid>.tmp`。
        转红的缺陷形态：`log.info("更新检查失败…")` 改回 `log.debug`，
        或删掉写失败后的 `os.remove(tmp)`。
        """
        seen = []
        real_info = update_check.log.info

        def spy(fmt, *a, **k):
            seen.append(fmt % a if a else fmt)
        update_check.log.info = spy
        self.addCleanup(setattr, update_check.log, "info", real_info)

        def opener(req, *a, **k):
            raise OSError("模拟 DNS 失败")

        out = update_check.check_update({"update_check": {"enabled": True}},
                                        now=time.time(), opener=opener,
                                        cache_path=self.cache)
        self.assertIsNone(out)
        self.assertTrue(any("更新检查失败" in m for m in seen),
                        "网络失败没有任何 INFO 留痕（dist-007 回归）")

        # 再触发一次"取到 tag 但缓存不可写"的路径，检查不留残渣
        os.chmod(self.tmp, 0o500)          # 只读目录：open(tmp,"w") 必失败
        self.addCleanup(os.chmod, self.tmp, 0o700)

        def opener2(req, *a, **k):
            return _FakeResp(json.dumps({"tag_name": "v99.0.0"}))

        with mock.patch.object(update_check, "urllib", update_check.urllib):
            update_check.check_update({"update_check": {"enabled": True}},
                                      now=time.time(), opener=opener2,
                                      cache_path=self.cache)
        residue = [f for f in os.listdir(self.tmp) if f.endswith(".tmp")]
        self.assertEqual(residue, [], "写失败后残留临时文件：%s（cfg-006 回归）" % residue)


class _FakeResp:
    def __init__(self, payload):
        self._p = payload.encode("utf-8")

    def read(self, *a):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------------------------------------------------------------- T-10

class TestCodec012SetFpsFailureRollback(unittest.TestCase):
    """T-10 / codec-012：set_fps 构造失败必须**回滚状态**并如实返回是否生效。

    转红的缺陷形态（原实现）：
      · 锁内先 `self._fps = fps` 再锁外构造；构造失败直接 `return` →
        同一 fps 从此命中开头的 `fps == self._fps` 提前返回，该帧率档**永不重试**，
        而源码注释写着"下次再试"（注释与实现相反）；
      · 调用方 `pipeline._ensure_encoder` 不看返回值、无条件 `self._enc_fps = fps`
        → 两侧各记一份"已生效"的假账。
    """

    def _encoder(self):
        enc = codec.VideoEncoder.__new__(codec.VideoEncoder)
        enc._lock = threading.RLock()
        enc._closed = False
        enc._permafailed = False
        enc._fps = 30
        enc._last_bitrate = 2500000
        return enc

    def test_failure_rolls_back_fps_and_reports_false(self):
        enc = self._encoder()
        with mock.patch.object(codec.VideoEncoder, "_pick_encoder",
                               return_value=["libx264"]), \
                mock.patch.object(codec.VideoEncoder, "_create_ctx_with",
                                  return_value=(None, None)):
            ok = enc.set_fps(60)
        self.assertFalse(ok, "构造失败却报告成功")
        self.assertEqual(enc._fps, 30,
                         "失败后仍保留新 fps → 同档永不重试（codec-012 回归）")

    def test_success_reports_true(self):
        enc = self._encoder()
        installed = []
        with mock.patch.object(codec.VideoEncoder, "_pick_encoder",
                               return_value=["libx264"]), \
                mock.patch.object(codec.VideoEncoder, "_create_ctx_with",
                                  return_value=("CTX", "libx264")), \
                mock.patch.object(codec.VideoEncoder, "_install_ctx",
                                  side_effect=lambda *a: installed.append(a)):
            ok = enc.set_fps(60)
        self.assertTrue(ok)
        self.assertEqual(enc._fps, 60)
        self.assertEqual(installed, [("CTX", "libx264", 2500000)])

    def test_retry_after_failure_is_attempted_again(self):
        """回滚的实际意义：同一帧率档下一次仍会**重试**（旧实现永远不再试）。"""
        enc = self._encoder()
        attempts = []
        with mock.patch.object(codec.VideoEncoder, "_pick_encoder",
                               return_value=["libx264"]), \
                mock.patch.object(codec.VideoEncoder, "_create_ctx_with",
                                  side_effect=lambda n, b: (attempts.append(1)
                                                            or (None, None))):
            enc.set_fps(60)
            enc.set_fps(60)
        self.assertEqual(len(attempts), 2,
                         "第二次同值调用被提前返回吞掉 → codec-012 未修")

    def test_pipeline_accounts_by_return_value(self):
        """pipeline 侧不得无条件把 `_enc_fps` 记成已生效（否则两侧各存一份假账）。"""
        src = inspect.getsource(__import__("pipeline").FrameEncoder._ensure_encoder)
        self.assertRegex(src, r"if\s+self\._encoder\.set_fps\(fps\)\s*:",
                         "pipeline 未按 set_fps 返回值落账（codec-012 的一半）")
        self.assertNotRegex(src, r"self\._encoder\.set_fps\(fps\)\n\s*self\._enc_fps\s*=\s*fps",
                            "仍是无条件记 _enc_fps")


class TestCodec013HostIsKeyGate(unittest.TestCase):
    """T-10 / codec-013：host 侧静止期强制 IDR 也必须校验 is_key。

    codec-004 当时只改了 share.py，《修复记录》却写"host.py、share.py 两处都改"。
    转红的缺陷形态：`host._encode_static_keyframe` 里删掉
    `if not is_key: … runtime.force_key = True; return` 这道门。
    """

    def _runtime(self):
        rt = mock.MagicMock()
        rt.slot_lock = threading.Lock()
        rt.force_key = True
        rt.slot = {"video": None, "video_ts": 0, "is_key": False,
                   "video_codec": 0, "encode_ms": 0, "bitrate": 0}
        return rt

    def test_p_frame_result_is_not_consumed_as_keyframe(self):
        rt = self._runtime()
        fe = mock.MagicMock()
        fe.force_idr.return_value = ((b"nal", False, 1.0, 12345), None)
        before = dict(rt.slot)
        host._encode_static_keyframe(rt, fe, object(), 0.0)
        self.assertIs(rt.force_key, True,
                      "编码器只回了 P 帧却消费了 force_key 请求（codec-013 回归）")
        self.assertEqual(rt.slot["video"], before["video"],
                         "P 帧被当作关键帧写进 slot")

    def test_real_keyframe_is_relayed_and_request_consumed(self):
        rt = self._runtime()
        fe = mock.MagicMock()
        fe.force_idr.return_value = ((b"nal", True, 1.0, 12345), None)
        fe.encoder.codec_id = common.CODEC_H264
        fe.encoder.bitrate = 2500000
        out = host._encode_static_keyframe(rt, fe, object(), 0.0)
        self.assertIs(rt.force_key, False, "真出 IDR 后应消费请求")
        self.assertEqual(rt.slot["video"], b"nal")
        self.assertTrue(rt.slot["is_key"])
        self.assertEqual(out, 0.0)


class TestAuditIndexRegistrationSane(unittest.TestCase):
    """cfg-009 的机械面：审计索引写"关联测试"时必须指向真实存在且真含该用例的文件。

    报告点破的三条失真（auth-009 落空 / capture-002 假绿未标注 / codec-009 有有效
    用例却记 —）已更正；本用例守住"登记的文件路径必须存在"这一最低机械要求，
    避免索引继续与代码漂移。
    """
    AUDIT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "docs", "audit")
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_registered_test_paths_exist(self):
        bad = []
        for fn in sorted(os.listdir(self.AUDIT_DIR)):
            if not fn.endswith(".md") or fn == "README.md":
                continue
            path = os.path.join(self.AUDIT_DIR, fn)
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            for line in lines:
                for m in __import__("re").finditer(r"(tests/[\w.]+\.py)", line):
                    if not os.path.isfile(os.path.join(self.ROOT, m.group(1))):
                        bad.append("%s → %s" % (fn, m.group(1)))
        self.assertEqual(bad, [], "审计索引指向不存在的测试文件: %s" % bad)


if __name__ == "__main__":
    unittest.main()
