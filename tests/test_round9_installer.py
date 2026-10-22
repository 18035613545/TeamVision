# -*- coding: utf-8 -*-
"""第 9 轮（部署与更新）：安装包脚本里的「版本真源」与卸载口令不得漂移。

背景：第 67 条曾因"版本号多处漂移"发过陈旧产物，此后版本真源统一到
`common.APP_VERSION`。但 installer.iss 的 AppVersion / OutputBaseFilename / 文件头
注释仍是手抄，打 1.6.0 时漏改其中一个，产物名就会停在旧版本上——而这条件在 CI
之外，只有人工能发现。这里把 iss 纳入单测：三处都必须等于 APP_VERSION。

另钉两条安装体验（第 9 轮"部署与更新"）：
- 卸载必须让用户在删除前确认用户数据（UninstallablePathsDataFiles，Inno 6.3+）；
- 卸载不得整目录删除两端共用的 `%LOCALAPPDATA%\\TeamVision`。
"""

import os
import re
import unittest

import common

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISS = os.path.join(ROOT, "installer.iss")


def _read_iss():
    # Inno Setup 脚本按 ANSI 存（与 .bat 同一约定），这里按 utf-8 优先、失败退 gbk
    with open(ISS, "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("gbk")


class TestInstallerVersion(unittest.TestCase):

    def setUp(self):
        self.text = _read_iss()

    def test_app_version_matches(self):
        m = re.search(r"^AppVersion=(.+)$", self.text, re.M)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).strip(), common.APP_VERSION)

    def test_output_base_filename_matches(self):
        m = re.search(r"^OutputBaseFilename=(.+)$", self.text, re.M)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).strip(),
                         "TeamVision-Setup-%s" % common.APP_VERSION)

    def test_header_comment_version_matches(self):
        first = self.text.splitlines()[0]
        self.assertIn("v%s" % common.APP_VERSION, first)


class TestInstallerUninstall(unittest.TestCase):

    def setUp(self):
        self.text = _read_iss()

    def test_uninstall_can_ask_before_deleting_data(self):
        # Inno Setup 6.3+：列出"数据文件路径"后，卸载器会在删除它们之前逐项询问；
        # 没有这一条，卸载就只剩静默删除或完全保留两种选择。
        m = re.search(r"^UninstallablePathsDataFiles=(.+)$", self.text, re.M)
        self.assertIsNotNone(m)
        self.assertIn("{localappdata}\\TeamVision\\accounts.json", m.group(1))
        self.assertIn("{localappdata}\\TeamVision\\config.json", m.group(1))

    def test_never_wipes_shared_data_dir(self):
        # 只认两端共用的数据目录；{app} 下的清理仍允许（那是本安装目录自己的残渣）
        self.assertIsNone(
            re.search(r"filesandordirs[^;]*\{localappdata\}\\TeamVision", self.text),
            "卸载清单不应整目录删除两端共用的 %LOCALAPPDATA%\\TeamVision")


if __name__ == "__main__":
    unittest.main()
