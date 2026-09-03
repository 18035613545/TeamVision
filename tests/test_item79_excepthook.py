# -*- coding: utf-8 -*-
"""第 79 条回归：import viewer 不得劫持 sys.excepthook（否则测试套件可能卡在模态弹窗后）。

缺陷：viewer.py 在**模块顶层**执行 `sys.excepthook = _handle_uncaught`，而该钩子会弹
**模态** Tk messagebox。任何 import viewer 的进程（每个单元测试都 import）都被全局换上
这个钩子；测试期间若有异常逃逸到解释器顶层，整套件就挂在一个 GUI 对话框后面（非交互
CI 上更是永久卡死）。host.py 早已把同类安装延迟到入口函数 _install_excepthooks()。

修复：把 `sys.excepthook = _handle_uncaught` 从模块顶层移入 main()，仅在作为 GUI 应用
运行时安装（仍覆盖 load_config / ViewerApp 构造等位于 app.run() try 之外的启动异常）。

验证：(1) AST 精确断言模块顶层不再有 sys.excepthook 赋值；(2) 干净子进程 import viewer
后 sys.excepthook 仍等于默认；(3) 安装动作确已移入 main() 函数体。
"""
import ast
import inspect
import subprocess
import sys
import unittest

import viewer


def _top_level_excepthook_assigns(module):
    """返回模块**顶层**语句里对 sys.excepthook 的赋值节点（嵌套在函数内的不算）。"""
    tree = ast.parse(inspect.getsource(module))
    hits = []
    for node in tree.body:                      # 只遍历模块顶层
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (isinstance(tgt, ast.Attribute) and tgt.attr == "excepthook"
                        and isinstance(tgt.value, ast.Name)
                        and tgt.value.id == "sys"):
                    hits.append(node)
    return hits


class TestNoImportTimeHijack(unittest.TestCase):

    def test_no_module_level_excepthook_assignment(self):
        # 核心：模块顶层不得再有 sys.excepthook = ...（import 即劫持的唯一途径）
        self.assertEqual(_top_level_excepthook_assigns(viewer), [],
                         "viewer 模块顶层仍存在 sys.excepthook 赋值，import 会劫持全局钩子")

    def test_handle_uncaught_still_defined(self):
        # 兜底处理函数本身保留（main 仍要用它）
        self.assertTrue(callable(viewer._handle_uncaught))

    def test_main_installs_excepthook(self):
        # 安装动作已移入 main() 函数体
        src = inspect.getsource(viewer.main)
        self.assertIn("sys.excepthook = _handle_uncaught", src)

    def test_clean_subprocess_import_leaves_excepthook_default(self):
        # 在干净解释器里 import viewer，断言 sys.excepthook 仍是默认（运行时确认）
        code = "import sys, viewer; print(sys.excepthook is sys.__excepthook__)"
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         "子进程 import viewer 失败：%s" % proc.stderr)
        last = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        self.assertEqual(last, "True",
                         "import viewer 后 sys.excepthook 被劫持（应仍为默认）；stdout=%r"
                         % proc.stdout)


if __name__ == "__main__":
    unittest.main()
