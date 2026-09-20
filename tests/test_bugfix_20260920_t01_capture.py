# -*- coding: utf-8 -*-
"""T-01 / capture-002 回归：**真实** `screen.CaptureManager.grab` 必须将采集异常上抛。

第 6 轮审查第十章 T-01 的判定（`docs/BUG审查报告-2026-09-20.md`）：
  旧用例（test_optimize_stage2.py:231-302）把 `pipeline.CaptureManager` 整体替换成
  替身，于是 `screen.py:344-350` 的 `except Exception: self._errors += 1; raise`
  **从未被执行**——把该 except 改回"吞成 None"，套件仍全绿。全仓无测试调用真实
  `CaptureManager.grab`（复核为真）。

本文件的用例不走 `pipeline.CaptureManager` 这个别名，而是直接 import `screen` 并
构造真实 `screen.CaptureManager` 实例（`__new__` 绕开 `_open()`，不碰 mss/dxcam 句柄、
不抓真实屏幕），让 `grab`/`_grab_once`/`_grab_mss` 三个真实方法全跑一遍，再把异常
一路传到真实 `pipeline.CaptureSource`，钉住 capture-002 的后果链：

    后端句柄"活着但每帧抛错"（RDP 断连 / 锁屏唤醒 / GPU 重置 / 分辨率变化）
      → CaptureManager 上抛且 `_errors` 递增
      → CaptureSource.working() 变 False（`consecutive_errors >= rebuild_on_errors`）
      → `_rebuild` 的触发条件可达 → 对方不再永久黑屏

改回缺陷会红的两处理由（逐行）：
  ① 把 `screen.py:344-350` 改回 `except Exception: return None`（吞异常、不计数）：
     `test_grab_reraises_and_counts` 的 assertRaises(RuntimeError) 直接失败；
     `test_working_turns_false_after_repeated_errors` 里 `mgr.consecutive_errors`
     恒为 0 → `src.working()` 恒 True → assertTrue(not working) 失败。
  ② 只保留上抛、漏掉计数（`except: raise` 而不 `self._errors += 1`）：
     ①的用例仍绿，但 `consecutive_errors == 0` → 第二个用例红——这正是
     capture-002 修的两件事必须**分别**有断言的原因。
  ③ 成功分支漏掉 `self._errors = 0`（screen.py:349）→
     `test_success_resets_error_count` 红。

mutation 实测口径：本项不是纯函数（要走真实类），故按上面逐行论证；
`pipeline.CaptureSource(owned=False)` 保证 `_rebuild` 不真的去开后端句柄，
用例离线、确定性、不触真实屏幕。
"""
import ast
import inspect
import os
import sys
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import pipeline
import screen


class _RaisingMss:
    """伪 mss 句柄：monitors 表齐备，但 grab() 必抛——正是"句柄活着、每帧抛错"形态。"""

    def __init__(self, exc=None):
        self.monitors = [{"left": 0, "top": 0, "width": 8, "height": 8},
                         {"left": 0, "top": 0, "width": 8, "height": 8}]
        self.calls = 0
        self._exc = exc or RuntimeError("BitBlt failed（模拟 RDP 断连）")

    def grab(self, mon):
        self.calls += 1
        raise self._exc

    def close(self):
        pass


class _GoodMss:
    """伪 mss 句柄：返回一小帧 BGRA，走真实 `_grab_mss` 的 cvtColor 链。"""

    def __init__(self):
        self.monitors = [{"left": 0, "top": 0, "width": 8, "height": 8},
                         {"left": 0, "top": 0, "width": 8, "height": 8}]
        self.calls = 0

    def grab(self, mon):
        self.calls += 1
        return np.zeros((4, 6, 4), dtype=np.uint8)   # BGRA

    def close(self):
        pass


def _manager(mss):
    """装配真实 CaptureManager（绕开 _open，不开任何后端句柄）。"""
    mgr = screen.CaptureManager.__new__(screen.CaptureManager)
    mgr.backend = mgr._effective = "mss"
    mgr.monitor = 1
    mgr.region = None
    mgr._mss = mss
    mgr._camera = None
    mgr._errors = 0
    return mgr


class TestRealGrabPropagates(unittest.TestCase):
    """核心：真实 `CaptureManager.grab` 不得把采集异常吞成 None。"""

    def test_grab_reraises_and_counts(self):
        mgr = _manager(_RaisingMss())
        with self.assertRaises(RuntimeError):
            mgr.grab()                       # ← 缺陷形态在此返回 None，本行即红
        self.assertEqual(mgr._errors, 1,
                         "上抛了但没计数：CaptureSource 的判活/重建看不到异常")
        self.assertEqual(mgr.consecutive_errors, 1)
        with self.assertRaises(RuntimeError):
            mgr.grab()
        self.assertEqual(mgr.consecutive_errors, 2, "计数必须逐帧累积（重建阈值靠它）")

    def test_grab_still_raises_when_backend_has_no_mss(self):
        """`_mss is None` 的旧语义（返回 None=无新帧）不得被误改成抛异常。

        这一条同时挡住"把整个 grab 包成无条件 raise"的过度修正：
        capture-002 只要求**异常上抛**，无句柄时仍应安静返回 None。
        """
        mgr = _manager(None)
        self.assertIsNone(mgr.grab())
        self.assertEqual(mgr.consecutive_errors, 0)

    def test_success_resets_error_count(self):
        """抓到一帧即清零（screen.py:349）——否则恢复后仍被判为死采集器。"""
        mgr = _manager(_RaisingMss())
        with self.assertRaises(RuntimeError):
            mgr.grab()
        self.assertEqual(mgr.consecutive_errors, 1)
        mgr._mss = _GoodMss()
        frame = mgr.grab()
        self.assertIsNotNone(frame, "正常路径被改坏：抓不到帧")
        self.assertEqual(frame.shape, (4, 6, 3), "应返回 BGR 三通道")
        self.assertEqual(mgr.consecutive_errors, 0,
                         "成功一帧未清零（重建判据会长期误活）")

    def test_dxgi_path_also_propagates(self):
        """dxgi 分支同样受保护：`self._camera.grab()` 抛错不得被吞。"""
        class Cam:
            def grab(self):
                raise OSError("DXGI device removed（模拟 GPU 重置）")

        mgr = _manager(None)
        mgr._effective = "dxgi"
        mgr._camera = Cam()
        with self.assertRaises(OSError):
            mgr.grab()
        self.assertEqual(mgr.consecutive_errors, 1)


class TestErrorCountReachesSelfHeal(unittest.TestCase):
    """后果链：真实 manager 的计数必须让真实 CaptureSource 判定"采集器已死"。

    capture-002 的原始后果是"永久黑屏"，其机制在 `pipeline.CaptureSource.working()`：
    旧 CaptureManager 吞异常 → `consecutive_errors` 恒 0 → `working()` 恒 True →
    空帧兜底 `none_rebuild_requires_dead` 被挡死 → 永不重建。
    """

    def _source(self, mgr):
        # owned=False：_rebuild 直接 no-op，绝不去开真实后端/真实屏幕
        src = pipeline.CaptureSource(capture=mgr, owned=False,
                                     rebuild_on_errors=3, sleep_on_error=0.0,
                                     sleep_on_none=0.0, name="t01")
        src._rebuild_calls = []
        src._rebuild = lambda reason, level="warning": src._rebuild_calls.append(reason)
        return src

    def test_working_turns_false_after_repeated_errors(self):
        mgr = _manager(_RaisingMss())
        src = self._source(mgr)
        self.assertTrue(src.working(), "初始即判死，重建逻辑永不触发")
        src.grab()
        src.grab()
        self.assertEqual(src._errors, 2,
                         "CaptureSource 看不到上抛的异常（旧吞成 None 的形态下恒为 0）")
        self.assertEqual(mgr.consecutive_errors, 2)
        self.assertTrue(src.working(), "未达阈值即判死：正常抖动会误触发重建")
        self.assertIsNone(src.grab(),
                          "CaptureSource 未把上抛的异常收敛为 None（会打死采集线程）")
        self.assertEqual(mgr.consecutive_errors, 3)
        self.assertEqual(len(src._rebuild_calls), 1,
                         "计数没到阈值 → 重建从未触发（capture-002 的自愈仍不可达）")
        self.assertFalse(src.working(),
                         "句柄每帧抛错仍判活：capture-002 的自愈链再次成为死代码")

    def test_working_true_again_after_a_good_frame(self):
        mgr = _manager(_RaisingMss())
        src = self._source(mgr)
        for _ in range(2):
            src.grab()
        mgr._mss = _GoodMss()
        self.assertIsNotNone(src.grab())
        self.assertEqual(src._errors, 0, "抓到帧未清 pipeline 侧计数")
        self.assertEqual(mgr.consecutive_errors, 0)
        self.assertEqual(src._rebuild_calls, [], "正常路径不应触发重建")
        self.assertTrue(src.working(), "恢复后仍判死：会触发无谓重建/长期空转")


class TestOldShapeWouldBeGreen(unittest.TestCase):
    """负向对照：把旧实现（吞成 None）复刻成本地函数，确认它会让上面的断言失效。

    旧 `grab` 的返回值与计数恰好是"异常→None、_errors 恒 0"，即：
      · assertRaises 收不到异常 → 红；
      · 若只断言"返回 None"（旧用例的写法）则**无论修没修都绿** → 判定力为零。
    """

    @staticmethod
    def _grab_old(mgr):
        try:
            return mgr._grab_once()
        except Exception:
            return None                    # 旧形态：吞掉，_errors 永不递增

    def test_old_shape_hides_the_failure(self):
        mgr = _manager(_RaisingMss())
        self.assertIsNone(self._grab_old(mgr))
        self.assertEqual(mgr.consecutive_errors, 0,
                         "对照失效：旧形态本应看不到任何异常计数")
        # 同一 mgr 走真实实现则计数递增——两相对照即证明断言打在 screen.py 上
        with self.assertRaises(RuntimeError):
            mgr.grab()
        self.assertEqual(mgr.consecutive_errors, 1)

    def test_none_check_alone_is_not_a_regression_guard(self):
        """把"返回 None"当唯一判据的用例（旧写法）对修复前后同值 → 明确判为无效。"""
        mgr = _manager(None)               # 无句柄：修复前后都返回 None
        self.assertIsNone(mgr.grab())
        self.assertIsNone(self._grab_old(mgr))


class TestRealSourceStructure(unittest.TestCase):
    """结构侧证据：真实 `screen.CaptureManager.grab` 的 except 必须"计数 + 上抛"。"""

    def test_grab_except_handler_reraises(self):
        src = textwrap.dedent(inspect.getsource(screen.CaptureManager.grab))
        tree = ast.parse(src)
        fn = tree.body[0]
        handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
        self.assertTrue(handlers, "grab() 没有 try/except（capture-002 的计数点丢失）")
        for h in handlers:
            has_raise = any(isinstance(n, ast.Raise) for n in ast.walk(h))
            self.assertTrue(has_raise,
                            "except 分支未 raise：异常又被吞回 None（capture-002 回退）")
            counted = any(isinstance(n, ast.AugAssign)
                          and isinstance(n.target, ast.Attribute)
                          and n.target.attr == "_errors"
                          for n in ast.walk(h))
            self.assertTrue(counted, "except 分支未累加 _errors：working() 判活看不到异常")

    def test_success_branch_clears_counter(self):
        src = textwrap.dedent(inspect.getsource(screen.CaptureManager.grab))
        self.assertIn("_errors = 0", src, "成功路径未清零 _errors")

    def test_pipeline_does_not_shadow_the_real_manager(self):
        """本文件必须走真实类：`pipeline.CaptureManager` 只是 screen 的再导出别名。

        T-01 的成因就是用例把这个名字 patch 掉。此处显式钉住"同一对象"，
        一旦有人在 pipeline 里另写一份 CaptureManager，真实 screen 实现就再次脱测。
        """
        self.assertIs(pipeline.CaptureManager, screen.CaptureManager)


if __name__ == "__main__":
    unittest.main()
