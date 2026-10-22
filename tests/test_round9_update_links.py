# -*- coding: utf-8 -*-
"""第 9 轮（部署与更新）：更新提示从"只报版本号"升级为**可点开的可执行入口**。

背景：update_check 此前只回一句"有新版 x.y.z 可下载（GitHub Releases）"，用户拿不到
地址，得自己去搜仓库；'About' 窗口里的官网链接也是纯 Label（不可点击）。

这里钉住三件事：
1. 提示文案里带上 release 页面地址（`update_check.release_url`），且该地址可被
   `extract_url` 原样取回——两端按钮都靠它分流，取不回就等于没有入口；
2. 页面地址取自 API 的 `html_url`，缺失/畸形时回退到固定 Releases 页，绝不抛；
3. 缓存兼容：旧的"只有 tag 和 checked_at"缓存文件仍能被正常读取（不因新增字段失效）。
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import update_check


class _FakeResp:
    def __init__(self, payload):
        self._raw = payload if isinstance(payload, str) else json.dumps(payload)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._raw.encode("utf-8")


def _opener(payload):
    def _open(url, timeout=None):
        return _FakeResp(payload)
    return _open


class TestReleaseUrl(unittest.TestCase):
    def test_release_url_is_https_releases(self):
        self.assertTrue(update_check.release_url().startswith("https://"))
        self.assertTrue(update_check.release_url().endswith("/releases"))

    def test_extract_url_roundtrip(self):
        text = "有新版 v9.9.9 可下载（GitHub Releases） - %s" % update_check.release_url()
        self.assertEqual(update_check.extract_url(text), update_check.release_url())

    def test_extract_url_none_when_absent(self):
        self.assertIsNone(update_check.extract_url("有新版 v9.9.9 可下载"))
        self.assertIsNone(update_check.extract_url(""))
        self.assertIsNone(update_check.extract_url(None))

    def test_extract_url_strips_trailing_punct(self):
        self.assertEqual(update_check.extract_url("见 https://x.example/a。"),
                         "https://x.example/a")


class TestCheckUpdateMessage(unittest.TestCase):
    def _run(self, payload, cached=None):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: [os.remove(os.path.join(d, f)) for f in os.listdir(d)])
        cache = os.path.join(d, "uc.json")
        if cached is not None:
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(cached, f)
        cfg = {"update_check": {"enabled": True}}
        return update_check.check_update(cfg, now=1e9, opener=_opener(payload),
                                         cache_path=cache)

    def test_message_carries_page_from_api(self):
        msg = self._run({"tag_name": "v9.9.9",
                         "html_url": "https://github.com/o/r/releases/tag/v9.9.9"})
        self.assertIn("v9.9.9", msg)
        self.assertEqual(update_check.extract_url(msg),
                         "https://github.com/o/r/releases/tag/v9.9.9")

    def test_message_falls_back_when_html_url_bad(self):
        msg = self._run({"tag_name": "v9.9.9", "html_url": "javascript:alert(1)"})
        self.assertEqual(update_check.extract_url(msg), update_check.release_url())

    def test_old_cache_without_url_still_readable(self):
        msg = self._run({"tag_name": "v9.9.9"},
                        cached={"tag": "v9.9.9", "checked_at": 1e9})
        # 缓存新鲜 → 不请求 API；页面地址回退到固定 Releases 页
        self.assertIn("v9.9.9", msg)
        self.assertEqual(update_check.extract_url(msg), update_check.release_url())

    def test_cache_stores_page(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: [os.remove(os.path.join(d, f)) for f in os.listdir(d)])
        cache = os.path.join(d, "uc.json")
        update_check.check_update({"update_check": {"enabled": True}}, now=1e9,
                                  opener=_opener({"tag_name": "v9.9.9",
                                                  "html_url": "https://h.example/r"}),
                                  cache_path=cache)
        with open(cache, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved.get("page"), "https://h.example/r")

    def test_no_update_returns_none(self):
        self.assertIsNone(self._run({"tag_name": "v0.0.1",
                                     "html_url": "https://h.example/r"}))

    def test_headline_drops_url_but_keeps_sentence(self):
        # ' - ' 是分隔符；tag 里的连字符（不带空格）不属于分隔符，必须留在人话里
        msg = ("有新版 v9.9.9-rc.1 可下载（GitHub Releases） - https://h.example/r")
        head = update_check.hint_headline(msg)
        self.assertIn("v9.9.9-rc.1", head)
        self.assertNotIn("http", head)
        # 与 extract_url 互不干扰：一句一文一址
        self.assertEqual(update_check.extract_url(msg), "https://h.example/r")

    def test_headline_without_url_is_whole_text(self):
        self.assertEqual(update_check.hint_headline("有新版 v9.9.9 可下载"),
                         "有新版 v9.9.9 可下载")
        self.assertEqual(update_check.hint_headline(None), "")


class TestOnClickContract(unittest.TestCase):
    """两端的按钮都靠"有 URL → 打开浏览器；没有 → 回落旧行为"分流。"""

    def test_open_url_returns_false_on_failure(self):
        # 传入不可用的 opener：不得抛异常，返回 False 让调用方回落到旧提示
        with mock.patch("webbrowser.open", side_effect=OSError("no shell")):
            self.assertFalse(update_check.open_url("https://h.example/r"))

    def test_open_url_rejects_non_http(self):
        with mock.patch("webbrowser.open") as m:
            self.assertFalse(update_check.open_url("file:///C:/x.exe"))
        m.assert_not_called()


def _tk_root_or_skip(tc):
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as e:  # headless/CI：与仓库既有 Tk 用例同口径跳过
        raise unittest.SkipTest("无显示环境：%s" % e) from e
    root.withdraw()
    tc.addCleanup(root.destroy)
    return root


class TestHostConsoleWiring(unittest.TestCase):
    """host_ui 的接线：提示到达后按钮出现、点按钮调 open_url、取消勾选即收起。

    只锁这三条行为链，不构造整个 HostConsole（会拉起采集/网络）；用对象级夹具
    凑出用到的那几个属性，因此也顺带钉住"这些属性必须是 self 上的，不能是闭包"。
    """

    def test_apply_hint_shows_button_and_click_opens(self):
        import host_ui
        ui = object.__new__(host_ui.HostConsole)
        ui._update_btn = mock.Mock()
        ui._set_status = mock.Mock()
        ui._apply_update_hint("有新版 v9.9.9 可下载（GitHub Releases） - "
                              "https://h.example/r")
        ui._update_btn.pack.assert_called_once()
        self.assertEqual(ui._update_url, "https://h.example/r")
        ui._set_status.assert_called_once()
        self.assertIn("v9.9.9", ui._set_status.call_args[0][0])
        with mock.patch.object(update_check, "open_url", return_value=True) as m:
            ui._on_update_click()
        m.assert_called_once_with("https://h.example/r")

    def test_apply_none_hides_button(self):
        import host_ui
        ui = object.__new__(host_ui.HostConsole)
        ui._update_btn = mock.Mock()
        ui._set_status = mock.Mock()
        ui._apply_update_hint(None)
        ui._update_btn.pack_forget.assert_called_once()

    def test_click_falls_back_to_clipboard(self):
        import host_ui
        ui = object.__new__(host_ui.HostConsole)
        ui.root = mock.Mock()
        ui._set_status = mock.Mock()
        ui._update_url = "https://h.example/r"
        with mock.patch.object(update_check, "open_url", return_value=False):
            ui._on_update_click()
        ui.root.clipboard_append.assert_called_once_with("https://h.example/r")
        self.assertTrue(ui._set_status.call_args.kwargs.get("error"))


class TestViewerWiring(unittest.TestCase):
    """viewer 的面板：同一套"有地址→按钮出现"契约，状态栏只放人话部分。"""

    def setUp(self):
        import tkinter as tk

        import viewer
        self.viewer = viewer
        self.tk = tk
        self.root = _tk_root_or_skip(self)

    def test_set_hint_shows_button_and_headline(self):
        v = object.__new__(self.viewer.ViewerApp)
        v.root = self.root
        v._status_after_id = None
        v._update_hint = ""
        v._update_url = None
        v._update_btn = self.tk.Button(v.root, text="下载新版")
        v._status_label = self.tk.Label(v.root)
        v._set_update_hint("有新版 v9.9.9 可下载（GitHub Releases） - https://h.example/r")
        self.assertTrue(v._update_btn.winfo_manager())
        self.assertEqual(v._update_url, "https://h.example/r")
        text = v._status_label.cget("text")
        self.assertIn("就绪", text)
        self.assertIn("v9.9.9", text)
        self.assertNotIn("http", text)
        # 清空提示后独立校验默认状态文本（设置入口不会重绘状态栏，由下一个 tick 恢复）
        v._update_hint = ""
        self.assertEqual(v._panel_default_status_text(), "就绪")
        v._set_update_hint("")
        self.assertFalse(v._update_btn.winfo_manager())


if __name__ == "__main__":
    unittest.main()
