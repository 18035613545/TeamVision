# -*- coding: utf-8 -*-
"""第 99 条回归：品牌资源脚本不得静默产出豆腐块、不得静默覆盖 git 跟踪资源。

两处缺陷（gen_assets.py）：
A. 字体加载硬编码 `C:/Windows/Fonts` 且只 catch OSError，缺字体时静默回退
   `ImageFont.load_default()`（仅拉丁字形）→「队友视野」渲染成方框（豆腐块），脚本却
   照报「品牌资源生成完成」。修复=动态系统字体目录（SystemRoot/windir）+ 多中文字体
   候选 + 全失败回退时向 stderr 打印明确告警。
B. `gen_icon`/`gen_splash` 无提示直接覆盖两个 git 跟踪资源（assets/app.ico、
   assets/splash.png）。修复=覆盖已存在文件前打印提示（不静默 clobber）。刻意不加
   --force/脏检查/备份：资源确定性可再生且已纳入 git，git diff/checkout 即脏检查与还原，
   加这些会阻断既定再生成工作流（过度设计）。

hermetic：mock ImageFont.truetype/load_default 与 os.path.exists，捕获 stderr，
不真正渲染/写盘（避免 clobber 跟踪资源）。范式同 test_item96/97/98。
"""
import contextlib
import inspect
import io
import os
import unittest
from unittest import mock

import gen_assets


class TestSystemFontDir(unittest.TestCase):
    def test_uses_systemroot_env(self):
        """字体目录取自 SystemRoot 环境变量，而非硬编码 C: 盘。"""
        with mock.patch.dict(os.environ, {"SystemRoot": "D:\\FakeWin", "windir": ""}):
            d = gen_assets._system_font_dir()
        self.assertTrue(d.endswith("Fonts"))
        self.assertIn("FakeWin", d)
        self.assertNotIn("C:", d, "仍回退到硬编码 C:（未用 SystemRoot）")

    def test_falls_back_when_env_absent(self):
        """SystemRoot/windir 均缺失/空时回退 C:/Windows（保持可用）。"""
        with mock.patch.dict(os.environ, {"SystemRoot": "", "windir": ""}):
            d = gen_assets._system_font_dir()
        self.assertEqual(d, os.path.join("C:/Windows", "Fonts"))


class TestLoadBrandFont(unittest.TestCase):
    def test_returns_truetype_without_warning_when_found(self):
        """首选字体命中：返回 truetype 字体，不告警、不回退。"""
        with mock.patch.object(gen_assets.ImageFont, "truetype",
                               return_value="FONT") as tt:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                f = gen_assets._load_brand_font(48)
        self.assertEqual(f, "FONT")
        self.assertEqual(err.getvalue(), "", "命中字体却告警")
        tt.assert_called_once()

    def test_tries_next_candidate_when_first_missing(self):
        """首选缺失（OSError）→ 尝试下一候选；命中即不告警。"""
        calls = []

        def fake_tt(path, size):
            calls.append(path)
            if len(calls) == 1:
                raise OSError("missing")
            return "FONT2"

        with mock.patch.object(gen_assets.ImageFont, "truetype", side_effect=fake_tt):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                f = gen_assets._load_brand_font(48)
        self.assertEqual(f, "FONT2")
        self.assertEqual(len(calls), 2, "未在首选失败后尝试下一候选")
        self.assertEqual(err.getvalue(), "", "第二候选命中却告警")

    def test_falls_back_and_warns_when_all_missing(self):
        """全部候选缺失 → 回退 load_default() 且向 stderr 告警（豆腐块风险）。"""
        with mock.patch.object(gen_assets.ImageFont, "truetype",
                               side_effect=OSError("missing")) as tt:
            with mock.patch.object(gen_assets.ImageFont, "load_default",
                                   return_value="DEFAULT") as ld:
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    f = gen_assets._load_brand_font(48)
        self.assertEqual(f, "DEFAULT")
        ld.assert_called_once()
        self.assertEqual(tt.call_count, len(gen_assets._FONT_CANDIDATES_BOLD),
                         "未尝试全部粗体候选")
        self.assertIn("豆腐", err.getvalue(), "回退时未告警豆腐块风险（仍是静默坏输出）")

    def test_bold_and_regular_prefer_different_candidates(self):
        """粗体首选 msyhbd，常规首选 msyh（候选顺序按 bold 区分）。"""
        with mock.patch.object(gen_assets.ImageFont, "truetype", return_value="F") as tt:
            gen_assets._load_brand_font(48, bold=True)
            self.assertIn("msyhbd", tt.call_args_list[0][0][0])
        with mock.patch.object(gen_assets.ImageFont, "truetype", return_value="F") as tt:
            gen_assets._load_brand_font(20, bold=False)
            self.assertIn("msyh.ttc", tt.call_args_list[0][0][0])


class TestNoteOverwrite(unittest.TestCase):
    def test_notice_when_file_exists(self):
        """目标已存在 → 打印覆盖提示（含文件名），不静默 clobber。"""
        with mock.patch.object(gen_assets.os.path, "exists", return_value=True):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                gen_assets._note_overwrite("assets/splash.png")
        out = err.getvalue()
        self.assertIn("覆盖", out)
        self.assertIn("splash.png", out)

    def test_silent_when_file_absent(self):
        """目标不存在（首次生成）→ 不打印提示。"""
        with mock.patch.object(gen_assets.os.path, "exists", return_value=False):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                gen_assets._note_overwrite("assets/brand_new.png")
        self.assertEqual(err.getvalue(), "")


class TestOldLogicFellBackSilently(unittest.TestCase):
    def test_old_font_helper_warned_nothing(self):
        """负向对照：旧 _font（硬编码 + 只 catch OSError + 无告警）静默回退默认字体。"""
        def _font_old(size, bold=True):
            try:
                name = "msyhbd.ttc" if bold else "msyh.ttc"
                return gen_assets.ImageFont.truetype(
                    os.path.join("C:/Windows/Fonts", name), size)
            except OSError:
                return gen_assets.ImageFont.load_default()

        with mock.patch.object(gen_assets.ImageFont, "truetype",
                               side_effect=OSError("missing")):
            with mock.patch.object(gen_assets.ImageFont, "load_default",
                                   return_value="DEFAULT"):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    f = _font_old(48)
        self.assertEqual(f, "DEFAULT")
        self.assertEqual(err.getvalue(), "",
                         "对照失效：旧逻辑本应静默回退（无任何告警）")


class TestRealSourceFixed(unittest.TestCase):
    def test_no_hardcoded_windows_fonts_join(self):
        """真实源码不再硬编码 os.path.join("C:/Windows/Fonts", ...)，改用 _system_font_dir。"""
        src = inspect.getsource(gen_assets)
        self.assertNotIn('os.path.join("C:/Windows/Fonts"', src,
                         "仍硬编码 C:/Windows/Fonts 字体路径")
        self.assertIn("_system_font_dir", src)
        self.assertIn("SystemRoot", src)

    def test_gen_helpers_wired(self):
        """gen_icon/gen_splash 接入 _note_overwrite，gen_splash 用 _load_brand_font。"""
        icon_src = inspect.getsource(gen_assets.gen_icon)
        splash_src = inspect.getsource(gen_assets.gen_splash)
        self.assertIn("_note_overwrite", icon_src)
        self.assertIn("_note_overwrite", splash_src)
        self.assertIn("_load_brand_font", splash_src)
        self.assertNotIn("def _font(", splash_src, "gen_splash 仍含旧嵌套 _font")


if __name__ == "__main__":
    unittest.main()
