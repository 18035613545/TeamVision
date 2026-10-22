"""A3 §2（第 133 条）：冷启动 splash 前置的结构锁定。

锁三件事：
  1. viewer.py / host.py / splash.py 的 **AST 顶层 import** 不出现
     cv2 / numpy / PIL（重依赖只能经 _load_heavy / 函数级导入进入）；
  2. viewer._load_heavy / host._load_heavy 幂等，且加载后绑定真实模块；
  3. host 的 screen 转发契约名在加载后与原模块同对象（不改语义）。
"""
import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FORBIDDEN_TOP = {"cv2", "numpy", "PIL"}


def _top_level_import_modules(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    names = set()
    for node in tree.body:            # 只看模块顶层，函数体内的 import 不在范围
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return names


class TestNoHeavyTopLevelImports(unittest.TestCase):
    def test_viewer_host_splash_top_level_clean(self):
        for fname in ("viewer.py", "host.py", "splash.py"):
            hits = _top_level_import_modules(os.path.join(ROOT, fname)) \
                & FORBIDDEN_TOP
            self.assertEqual(
                hits, set(),
                "%s 顶层导入了重依赖 %s——会把 splash 上屏推迟到其加载完成"
                % (fname, sorted(hits)))


class TestLoadHeavy(unittest.TestCase):
    def test_viewer_load_heavy_idempotent_and_binds(self):
        import viewer
        viewer._load_heavy()
        cv2_1, np_1, img_1 = viewer.cv2, viewer.np, viewer.Image
        viewer._load_heavy()          # 幂等：第二次不得重载或置空
        self.assertIs(viewer.cv2, cv2_1)
        self.assertIs(viewer.np, np_1)
        self.assertIs(viewer.Image, img_1)
        import cv2
        self.assertIs(viewer.cv2, cv2)
        self.assertIsNotNone(viewer.ImageTk)
        self.assertIsNotNone(viewer.share_mod)   # share→pipeline 链一并就位

    def test_host_load_heavy_idempotent_and_screen_contract(self):
        import host
        host._load_heavy()
        pipe_1, _enc_1 = host.pipeline, host.encode_bgr
        host._load_heavy()
        self.assertIs(host.pipeline, pipe_1)
        import pipeline
        import screen
        self.assertIs(host.pipeline, pipeline)
        self.assertIs(host.encode_bgr, screen.encode_bgr)
        self.assertIs(host.CaptureManager, screen.CaptureManager)
        for name in ("STILL_STEP", "downsample_frame", "motion_changed",
                     "still_gate_update", "calc_output_size"):
            self.assertIs(getattr(host, name), getattr(screen, name),
                          "screen 转发契约名 %s 漂移" % name)


if __name__ == "__main__":
    unittest.main()
