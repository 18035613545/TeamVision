# -*- coding: utf-8 -*-
"""B4 更新提示（实现方案 §6）：版本比对 + 24h 缓存 + 失败静默。全部走桩，CI 无网络。"""

import io
import json
import os
import tempfile
import unittest
from unittest import mock

import common
import update_check


class _Resp(io.BytesIO):
    """urlopen 返回值的最小替身：可读 + 上下文管理（BytesIO 已具备）。"""


def _opener(payload, calls=None):
    def op(url, timeout=None):
        if calls is not None:
            calls.append(url)
        if isinstance(payload, Exception):
            raise payload
        return _Resp(json.dumps(payload).encode("utf-8"))
    return op


class _TmpCache(unittest.TestCase):
    def setUp(self):
        fd, self.cache = tempfile.mkstemp(suffix="-update_check.json")
        os.close(fd)
        os.remove(self.cache)
        self.addCleanup(lambda: os.path.exists(self.cache) and os.remove(self.cache))


class TestVersionParsing(unittest.TestCase):

    def test_parse(self):
        self.assertEqual(update_check.parse_version("v1.5.0"), (1, 5, 0))
        self.assertEqual(update_check.parse_version(" 1.5 "), (1, 5))
        self.assertIsNone(update_check.parse_version("v1.5.0-beta"))
        self.assertIsNone(update_check.parse_version("latest"))
        self.assertIsNone(update_check.parse_version(""))
        self.assertIsNone(update_check.parse_version(None))
        self.assertIsNone(update_check.parse_version("1.5.0.0.0"))

    def test_is_newer_numeric_not_lexical(self):
        self.assertTrue(update_check.is_newer("v1.10.0", "1.9.0"))
        self.assertTrue(update_check.is_newer("1.4.1", "1.4.0"))
        self.assertFalse(update_check.is_newer("1.4.0", "1.4.0"))
        self.assertFalse(update_check.is_newer("v1.3", "1.4.0"))
        # 段数不同按补零对齐：1.5 == 1.5.0
        self.assertFalse(update_check.is_newer("1.5", "1.5.0"))
        # 任何一侧解析失败 → 绝不误报
        self.assertFalse(update_check.is_newer("garbage", "1.4.0"))
        self.assertFalse(update_check.is_newer("99.0.0", "not-a-version"))


class TestCheckUpdate(_TmpCache):

    NOW = 1_800_000_000.0

    #: dist-004（2026-09-18）后更新检查默认关闭——测试统一显式开启
    _ON = {"update_check": {"enabled": True}}

    def _check(self, payload, calls=None, cfg=None, role="host"):
        return update_check.check_update(cfg if cfg is not None else self._ON,
                                         now=self.NOW,
                                         opener=_opener(payload, calls),
                                         cache_path=self.cache,
                                         role=role)

    def test_newer_tag_shows_hint_and_caches(self):
        calls = []
        msg = self._check({"tag_name": "v99.0.0"}, calls)
        self.assertIn("有新版 v99.0.0", msg)
        self.assertIn("GitHub Releases", msg)
        self.assertEqual(len(calls), 1)
        with open(self.cache, encoding="utf-8") as f:
            cached = json.load(f)
        # 第 12 轮：缓存里存的是**原始发布列表**（判定按角色现算），
        # 不再是"判定结果"——两端共用一台机器时结果缓存会互相顶掉。
        self.assertEqual(cached["checked_at"], self.NOW)
        self.assertIsInstance(cached["releases"], list)
        self.assertEqual(cached["releases"][0]["tag_name"], "v99.0.0")

    def test_same_or_older_tag_no_hint(self):
        self.assertIsNone(self._check({"tag_name": "v" + common.APP_VERSION}))
        self.assertIsNone(self._check({"tag_name": "v0.1.0"}))

    def test_cache_fresh_for_24h_no_second_request(self):
        self._check({"tag_name": "v99.0.0"})
        boom = _opener(AssertionError("不该再打网络"))
        msg = update_check.check_update(self._ON, now=self.NOW + 3600,
                                        opener=boom, cache_path=self.cache)
        self.assertIn("v99.0.0", msg)

    def test_cache_expires_after_24h(self):
        self._check({"tag_name": "v0.1.0"})
        calls = []
        msg = update_check.check_update(self._ON, now=self.NOW + 25 * 3600,
                                        opener=_opener({"tag_name": "v99.0.0"},
                                                        calls),
                                        cache_path=self.cache)
        self.assertEqual(len(calls), 1)
        self.assertIn("v99.0.0", msg)

    def test_network_failure_is_silent(self):
        self.assertIsNone(self._check(OSError("getaddrinfo failed")))

    def test_failure_falls_back_to_stale_cache(self):
        self._check({"tag_name": "v99.0.0"})
        msg = update_check.check_update(self._ON, now=self.NOW + 100 * 86400,
                                        opener=_opener(OSError("timeout")),
                                        cache_path=self.cache)
        self.assertIn("v99.0.0", msg)  # 旧缓存继续顶，而不是报错或误判无更新

    def test_bad_payload_ignored(self):
        self.assertIsNone(self._check({"tag_name": 42}))
        self.assertIsNone(self._check([1, 2, 3]))

    def test_disabled_switch_never_touches_network(self):
        boom = _opener(AssertionError("不该再打网络"))
        msg = update_check.check_update({"update_check": {"enabled": False}},
                                        now=self.NOW, opener=boom,
                                        cache_path=self.cache)
        self.assertIsNone(msg)

    def test_corrupt_cache_file_recovered(self):
        with open(self.cache, "w", encoding="utf-8") as f:
            f.write("{broken")
        msg = self._check({"tag_name": "v99.0.0"})
        self.assertIn("v99.0.0", msg)


class TestEnabledAndWiring(unittest.TestCase):

    def test_enabled_defaults_false(self):
        # dist-004（2026-09-18）：默认关闭——开启会周期性向 api.github.com 暴露
        # 使用者 IP 与"在用本程序"的事实，按第 131 条口径应为 opt-in
        self.assertFalse(update_check.enabled({}))
        self.assertFalse(update_check.enabled({"update_check": {}}))
        self.assertFalse(update_check.enabled(None))
        self.assertFalse(update_check.enabled({"update_check": "坏值"}))
        self.assertTrue(update_check.enabled({"update_check": {"enabled": True}}))
        self.assertTrue(update_check.enabled({"update_check": {"enabled": 1}}))

    def test_default_config_ships_switch(self):
        self.assertEqual(
            common.DEFAULT_CONFIG["update_check"], {"enabled": False})

    def test_start_background_check_skips_when_disabled(self):
        self.assertIsNone(update_check.start_background_check(
            {"update_check": {"enabled": False}}, lambda m: None))

    def test_start_background_check_posts_message(self):
        import threading
        got = []
        done = threading.Event()

        def post(msg):
            got.append(msg)
            done.set()

        with mock.patch.object(update_check, "check_update",
                               return_value="有新版 v99.0.0 可下载（GitHub Releases）"):
            t = update_check.start_background_check(
                {"update_check": {"enabled": True}}, post)
        self.assertIsNotNone(t)
        self.assertTrue(done.wait(5))
        self.assertEqual(len(got), 1)
        self.assertIn("v99.0.0", got[0])

    def test_host_ui_and_viewer_hooks_present(self):
        import host_ui
        import viewer
        self.assertTrue(callable(getattr(host_ui.HostConsole,
                                         "_show_update_hint", None)))
        self.assertTrue(callable(getattr(viewer.ViewerApp,
                                         "_set_update_hint", None)))


if __name__ == "__main__":
    unittest.main()
