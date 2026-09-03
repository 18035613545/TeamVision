# -*- coding: utf-8 -*-
"""第 76 条回归：config.json 里显式 null 不得覆盖默认值（否则采集线程崩溃）。

缺陷：_deep_merge 对非字典值无条件 `result[key] = override[key]`，于是 config.json 里
`"fps": null` 会让合并结果 host.fps=None；而 `host.get("fps", 60)` 因键存在返回 None
（不取默认）→ `slot["fps"]=None` → 采集线程 `1.0/fps_now` 抛 TypeError；`"backend": null`
同理构造 `CaptureManager(None, ...)`。

修复：_deep_merge 跳过 override 中为 None 的值（视为「未指定，沿用默认」）。唯一以 null
为合法值的键是 host.capture.region（默认即 None=全屏），保留 base 的 None 与 null 覆盖
等价，第 32 条 region=None 语义不变。

hermetic：直接调 _deep_merge；另用 monkeypatch _config_path 指向临时文件驱动真实
load_config，断言崩溃前置条件（host.get("fps", 60) 非 None）已消除。
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import common


class TestDeepMergeNullOverride(unittest.TestCase):

    def test_null_fps_keeps_default(self):
        out = common._deep_merge(common.DEFAULT_CONFIG, {"host": {"fps": None}})
        self.assertEqual(out["host"]["fps"], 30)        # 默认值，不是 None

    def test_null_backend_keeps_default(self):
        out = common._deep_merge(
            common.DEFAULT_CONFIG, {"host": {"capture": {"backend": None}}})
        self.assertEqual(out["host"]["capture"]["backend"], "dxgi")

    def test_null_region_stays_none(self):
        # region 默认即 None（全屏）：null 覆盖与保留默认等价，语义不变（第 32 条）
        out = common._deep_merge(
            common.DEFAULT_CONFIG, {"host": {"capture": {"region": None}}})
        self.assertIsNone(out["host"]["capture"]["region"])

    def test_non_null_override_still_applies(self):
        # 阳性对照：合法的非 null 覆盖照常生效
        out = common._deep_merge(common.DEFAULT_CONFIG, {"host": {"fps": 15}})
        self.assertEqual(out["host"]["fps"], 15)

    def test_nested_sibling_keys_preserved(self):
        out = common._deep_merge(
            common.DEFAULT_CONFIG, {"host": {"capture": {"monitor": 2}}})
        self.assertEqual(out["host"]["capture"]["monitor"], 2)
        self.assertEqual(out["host"]["capture"]["backend"], "dxgi")  # 兄弟键不被抹掉
        self.assertEqual(out["host"]["fps"], 30)                     # 上层兄弟键保留

    def test_null_whole_subdict_keeps_default_dict(self):
        # 整个子字典被写成 null：保留默认字典，而不是 None（否则 .get 链全断）
        out = common._deep_merge(common.DEFAULT_CONFIG, {"host": {"capture": None}})
        self.assertIsInstance(out["host"]["capture"], dict)
        self.assertEqual(out["host"]["capture"]["backend"], "dxgi")

    def test_crash_precondition_removed(self):
        # 直接复刻崩溃前置：合并后 host.get("fps", 60) 必须是数字而非 None
        out = common._deep_merge(common.DEFAULT_CONFIG, {"host": {"fps": None}})
        fps = out["host"].get("fps", 60)
        self.assertIsNotNone(fps)
        self.assertIsInstance(fps, (int, float))
        self.assertAlmostEqual(1.0 / fps, 1.0 / 30)   # 不再抛 TypeError


class TestLoadConfigEndToEnd(unittest.TestCase):

    def test_load_config_nulls_fall_back_to_defaults(self):
        cfg = {
            "host": {
                "fps": None,
                "capture": {"backend": None, "region": None, "monitor": None},
                "codec": {"bitrate_kbps": None},
            },
            "viewer": {"display_width": None, "alpha": None},
        }
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
            with mock.patch.object(common, "_config_path", return_value=path):
                out = common.load_config()
            self.assertEqual(out["host"]["fps"], 30)
            self.assertEqual(out["host"]["capture"]["backend"], "dxgi")
            self.assertIsNone(out["host"]["capture"]["region"])   # 合法 null 保留
            self.assertEqual(out["host"]["capture"]["monitor"], 1)
            self.assertEqual(out["host"]["codec"]["bitrate_kbps"], 2500)
            self.assertEqual(out["viewer"]["display_width"], 480)
            self.assertAlmostEqual(out["viewer"]["alpha"], 0.9)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
