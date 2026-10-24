# -*- coding: utf-8 -*-
"""第 10 轮（部署与更新）：多份安装包并存时，自动更新检查不会**把旧版认成新版**。

起因（第 9 轮实测发现）：单包时代 `releases/latest` 只有一个 tag，语义明确；一旦按
"共享端 / 观看端"拆成两个安装包分别发版，最新那次发布只会是其中一端的新版本——
另一端的 `latest` 会长期停在**比本机更旧**的 tag 上。此时按标签版本的逻辑永远不提示，
该端用户从此收不到更新。判据必须换成"有没有本机版本的安装包"，而不是"标签是不是最新"。

本组用例全部走桩（CI 无网络），钉住 check_release 的判定矩阵与降级路径。
"""

import io
import json
import os
import tempfile
import unittest

import update_check


class _Resp(io.BytesIO):
    pass


def _opener(releases):
    """releases: 可直接 dict（单包形）或 list（多包形）。"""
    def op(url, timeout=None):
        return _Resp(json.dumps(releases).encode("utf-8"))
    return op


def _broken_opener(url, timeout=None):
    raise OSError("no network")


#: 产物名与 installer.iss 的命名同族：TeamVision-<Host|Viewer>-<版本>-Setup.exe
EXE = "TeamVision-%s-%s-Setup.exe"


class _Base(unittest.TestCase):
    def setUp(self):
        fd, self.cache = tempfile.mkstemp(suffix="-release.json")
        os.close(fd)
        os.remove(self.cache)
        self.addCleanup(lambda: os.path.exists(self.cache) and os.remove(self.cache))
        self.addCleanup(lambda: [os.remove(os.path.join(os.path.dirname(self.cache), n))
                                 for n in os.listdir(os.path.dirname(self.cache))
                                 if n.startswith(os.path.basename(self.cache))])

    def run_check(self, releases, role="host", opener=None, now=1e9):
        return update_check.check_release(
            role, "1.5.0", now=now, opener=opener or _opener(releases),
            cache_path=self.cache)


def _assets(role, version, file_id=1):
    return [{"name": EXE % (role.capitalize(), version),
             "browser_download_url": "https://h.example/dl/%s" % version,
             "id": file_id}]


class TestNewerReleaseFollowingLatest(_Base):

    def test_single_package_newer(self):
        info = self.run_check({"tag_name": "v1.6.0", "html_url": "https://h.example/r/1.6",
                               "assets": []})
        self.assertTrue(info["update"])
        self.assertEqual(info["version"], "1.6.0")
        self.assertEqual(info["url"], "https://h.example/r/1.6")

    def test_same_version_no_update(self):
        info = self.run_check({"tag_name": "v1.5.0", "html_url": "https://h.example/r",
                               "assets": []})
        self.assertFalse(info["update"])

    def test_canary_tag_ignored(self):
        # 预发布标签不是给用户的版本号：解析失败按"无更新"，绝不误报
        info = self.run_check({"tag_name": "canary-2026", "html_url": "https://h.example/r",
                               "assets": []})
        self.assertFalse(info["update"])


class TestSplitPackages(_Base):
    """多包发布：/latest 落在对端的新版本上，本机仍须收到更新。"""

    def _list(self, latest_tag, older_tag, latest_role="viewer", older_role="host"):
        """两条发布：最新的那条只有 latest_role 的包，上一条只有 older_role 的包。"""
        return [
            {"tag_name": latest_tag, "html_url": "https://h.example/r/" + latest_tag,
             "assets": (_assets(latest_role, latest_tag.lstrip("v"), 11)
                        if latest_role else [])},
            {"tag_name": older_tag, "html_url": "https://h.example/r/" + older_tag,
             "assets": (_assets(older_role, older_tag.lstrip("v"), 12)
                        if older_role else [])},
        ]

    def test_latest_older_than_mine_but_my_point_release_exists(self):
        info = self.run_check(self._list("v1.7.0", "v1.6.0"), role="host")
        self.assertTrue(info["update"])
        self.assertEqual(info["version"], "1.6.0")
        self.assertEqual(info["download_url"], "https://h.example/dl/1.6.0")
        self.assertEqual(info["url"], "https://h.example/r/v1.6.0")

    def test_my_role_has_no_newer_package(self):
        info = self.run_check(self._list("v1.7.0", "v1.5.0"), role="host")
        self.assertFalse(info["update"])

    def test_unknown_role_takes_newest(self):
        # 一条资产都认不出（单包时代的发布）→ 退回"最高标签"口径
        info = self.run_check(self._list("v1.7.0", "v1.5.0", latest_role=None, older_role=None),
                              role="host")
        self.assertTrue(info["update"])
        self.assertEqual(info["version"], "1.7.0")

    def test_tag_without_fitting_asset_is_skipped(self):
        # v2.0.0 只有 viewer 包（正常），v1.6.0 只有 host 包。问 viewer 时应拿到 2.0.0；
        # 问 host 时 2.0.0 不该被"最高标签"兜底拿走——没有本端的包就不算本端的更新
        payload = self._list("v2.0.0", "v1.6.0")
        self.assertEqual(self.run_check(payload, role="viewer")["version"], "2.0.0")
        host = self.run_check(payload, role="host")
        self.assertTrue(host["update"])
        self.assertEqual(host["version"], "1.6.0")


class TestDegradation(_Base):

    def test_api_failure_falls_back_to_legacy_check(self):
        info = self.run_check(None, opener=_broken_opener)
        self.assertFalse(info["update"])
        self.assertEqual(info["source"], "none")

    def test_bad_payload_shape(self):
        info = self.run_check("not-a-release-list")
        self.assertFalse(info["update"])


class TestRoleFilename(unittest.TestCase):

    def test_role_from_asset_name(self):
        self.assertEqual(update_check.role_of_asset_name("TeamVision-Host-1.6.0-Setup.exe"),
                         "host")
        self.assertEqual(update_check.role_of_asset_name("TeamVision-Viewer-Setup.exe"),
                         "viewer")
        # 认不出角色时归为 None：宁可回到"按标签版本"的旧口径，也不替资产猜归属
        self.assertIsNone(update_check.role_of_asset_name("frpc-0.62.1.zip"))
        # 子串不算：ghosting.exe / previewer.exe 不是两端的安装包
        self.assertIsNone(update_check.role_of_asset_name("ghosting.exe"))
        self.assertIsNone(update_check.role_of_asset_name("previewer.exe"))
        self.assertIsNone(update_check.role_of_asset_name(None))


class TestCheckUpdateIsWired(unittest.TestCase):
    """第 12 轮：`check_update` 已按角色判定（不再是"最新标签比本机新"）。

    这组用例盯住"接线"这件事本身：两端各自判定、共享同一份缓存。
    """

    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: [os.remove(os.path.join(d, f))
                                 for f in os.listdir(d)])
        self.cache = os.path.join(d, "uc.json")
        self.cfg = {"update_check": {"enabled": True}}

    def _releases(self):
        return [{"tag_name": "v1.7.0",
                 "html_url": "https://h.example/r/v1.7.0",
                 "assets": _assets("viewer", "1.7.0", 11)}]

    def _check(self, role, calls=None):
        return update_check.check_update(self.cfg, now=1e9,
                                         opener=_opener(self._releases()),
                                         cache_path=self.cache, role=role)

    def test_viewer_is_prompted_for_viewer_package(self):
        msg = self._check("viewer")
        self.assertIsNotNone(msg)
        self.assertIn("v1.7.0", msg)

    def test_host_is_not_prompted_for_viewer_only_release(self):
        """最新那次发布只有观看端的包 → 共享端**不该**被提示去装它
        （装错包比"没提示"糟得多）。"""
        self.assertIsNone(self._check("host"))

    def test_both_roles_share_one_cache(self):
        """缓存里存的是原始发布列表，两端共用同一份：共享端先查过之后，
        观看端再查**不该**重新打网络（否则同机两端每次启动都各打一次）。"""
        calls = []

        def opener(url, timeout=None):
            calls.append(url)
            return _Resp(json.dumps(self._releases()).encode("utf-8"))

        update_check.check_update(self.cfg, now=1e9, opener=opener,
                                  cache_path=self.cache, role="host")
        update_check.check_update(self.cfg, now=1e9, opener=opener,
                                  cache_path=self.cache, role="viewer")
        self.assertEqual(len(calls), 1, "两端应共享同一份发布列表缓存")


class TestOfflineFallback(unittest.TestCase):
    """断网时旧缓存要能顶住——"仍然告诉用户有新版"比"装作没事"有用。

    第 12 轮把缓存格式从"判定结果"改成"原始发布列表"，旧格式缓存
    （只有 tag/page）也要照样能读、照样能在断网时顶上去。
    """

    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: [os.remove(os.path.join(d, f))
                                 for f in os.listdir(d)])
        self.cache = os.path.join(d, "uc.json")
        self.cfg = {"update_check": {"enabled": True}}

    def _broken(self, url, timeout=None):
        raise OSError("no network")

    def _write_cache(self, payload):
        with open(self.cache, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def test_old_format_cache_survives_outage(self):
        self._write_cache({"tag": "v99.0.0", "page": "https://h.example/r",
                           "checked_at": 1e9})
        msg = update_check.check_update(self.cfg, now=1e9, opener=self._broken,
                                        cache_path=self.cache)
        self.assertIsNotNone(msg)
        self.assertIn("v99.0.0", msg)

    def test_new_format_cache_survives_outage(self):
        self._write_cache({"checked_at": 1e9,
                           "releases": [{"tag_name": "v88.0.0",
                                         "html_url": "https://h.example/r2"}]})
        msg = update_check.check_update(self.cfg, now=1e9, opener=self._broken,
                                        cache_path=self.cache)
        self.assertIsNotNone(msg)
        self.assertIn("v88.0.0", msg)

    def test_no_cache_and_outage_returns_none(self):
        """既没缓存又断网 → 不提示（调用方保持旧文案不动），且不抛。"""
        msg = update_check.check_update(self.cfg, now=1e9, opener=self._broken,
                                        cache_path=self.cache)
        self.assertIsNone(msg)


if __name__ == "__main__":
    unittest.main()
