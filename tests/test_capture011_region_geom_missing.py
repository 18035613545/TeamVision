# -*- coding: utf-8 -*-
"""capture-011（2026-10-10）：`_dxgi_region` 在「显示器几何取不到」时的处理。

缺陷形态：旧实现对 `mon is None` 既不换算也不判越界，把虚拟桌面绝对坐标原样当成
输出内相对坐标递下去，同时报 `out_of_range=False` 阻止回退 mss。vendored dxcam
0.3.0 的 `_validate_region`（vendor/dxcam/dxcam.py:782-787）对这种坐标抛
`ValueError`，异常经 `CaptureSource._errors` 累加后触发每 5 次一轮的重建循环。

hermetic：真实 `screen.CaptureManager`（`__new__` 绕开 `_open()`，不开任何真实
后端句柄），配伪 mss 句柄与伪 dxcam camera。真实 dxcam 不可用时（CI 无 `vendor/`）
用等价校验替身；另有一条用例在 vendor 存在时交叉验证替身与真实 `_validate_region`
行为一致，避免"替身比真实实现宽松"导致假绿。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

import screen  # noqa: E402

# monitors 语义：索引 0 = 全部屏幕并集，索引 N = 第 N 块（mss/base.py:332-333）
MSS_MONITORS = [
    {"left": 0, "top": 0, "width": 3840, "height": 2160},
    {"left": 0, "top": 0, "width": 1920, "height": 1080},
    {"left": 1920, "top": 0, "width": 1920, "height": 1080},
]


def _validate_region_as_dxcam(width, height, region):
    """等价于 vendored dxcam 0.3.0 `DXCamera._validate_region`。

    保持与真实实现同样的判定式（含 `right > left` 的严格不等号），使 CI 无
    vendor/ 时也能复现"绝对坐标被拒绝"这一后果；`TestRealDxcamCrossCheck`
    在 vendor 存在时逐条比对两者行为。
    """
    left, top, right, bottom = region
    if not (width >= right > left >= 0 and height >= bottom > top >= 0):
        raise ValueError(
            "Invalid Region: Region should be in %dx%d" % (width, height))


class StrictCamera:
    """模拟 dxcam 0.3.0：`grab(region=...)` 会做边界校验，越界抛 ValueError。

    `capture()` 返回 True/False，供用例确认"到底有没有把 region 递进 dxcam"。
    """

    def __init__(self, width=1920, height=1080):
        self.width = width
        self.height = height
        self.calls = []

    def grab(self, region=None):
        self.calls.append(region)
        if region is not None:
            _validate_region_as_dxcam(self.width, self.height, region)
        return np.zeros((4, 6, 3), dtype=np.uint8)


class FakeMss:
    """伪 mss 句柄。`monitors=None` 模拟几何不可得（初始化失败或序号越界）。"""

    def __init__(self, monitors=None):
        self.monitors = monitors if monitors is not None else []
        self.grabbed = []

    def grab(self, mon):
        self.grabbed.append(dict(mon))
        return np.zeros((4, 6, 4), dtype=np.uint8)   # BGRA

    def close(self):
        pass


def _manager(mss, region, monitor=1, camera=None):
    """装配真实 CaptureManager（绕开 _open，不开任何真实后端句柄）。

    capture-012 起 `_monitor_rect` 会先问几何探针；本文件测的是 **mss 语义下的
    坐标换算**，几何一律由传入的伪 mss 决定。`IsolatedGeometry` 基类把探针钉成
    "不可用"，让它退回 mss 路径——否则在单屏机器上真实探针（只有一块
    1920x1080）会覆盖伪 mss 声明的双屏布局，副屏用例被误判为越界。那是环境
    依赖，不是缺陷。
    """
    mgr = screen.CaptureManager.__new__(screen.CaptureManager)
    mgr.backend = mgr._effective = "dxgi"
    mgr.monitor = monitor
    mgr.region = region
    mgr._mss = mss
    mgr._camera = camera
    mgr._errors = 0
    mgr._geometry = None
    return mgr


class IsolatedGeometry(unittest.TestCase):
    """把几何探针隔离在本文件的伪 mss 之外（见 `_manager` 的说明）。"""

    def setUp(self):
        _reset_warn_keys()
        self._patcher = mock.patch.object(screen, "probe_geometry", return_value=None)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        _reset_warn_keys()


def _reset_warn_keys():
    """`_warn_pick_once` 的集合是模块级的，跨用例会互相压制。"""
    screen._PICK_WARNED.discard("region-rect-missing-1")
    screen._PICK_WARNED.discard("region-rect-missing-3")


class TestRegionFallsBackWhenGeometryMissing(IsolatedGeometry):
    """几何取不到时必须按「越界」处理，交调用方回退 mss。"""


    def test_returns_out_of_range_flag(self):
        mgr = _manager(FakeMss(), {"left": 1920, "top": 100,
                                   "width": 800, "height": 600}, monitor=3)
        reg, out_of_range = mgr._dxgi_region()
        self.assertIsNone(reg,
                          "几何取不到却仍返回了 region：旧实现把虚拟桌面绝对坐标"
                          "(1920,100,2720,700) 当成输出内相对坐标递给 dxcam")
        self.assertTrue(out_of_range,
                        "几何取不到时必须报 out_of_range=True，否则 _grab_once "
                        "不会回退 mss")

    def test_never_leaks_absolute_coordinates(self):
        """几何取不到时**不得**把虚拟桌面绝对坐标当成输出内相对坐标交出去。

        R8-D-01（2026-10-10）：旧实现是 `if reg is not None: with assertRaises(...)`
        ——缺陷形态下 `reg = (1920, 100, 2720, 700)`（非 None）→ 进断言并通过；
        修复形态下 `reg is None` → 整段跳过、也通过。**两种形态都为绿**，这条以
        "不得泄露绝对坐标"命名的用例对被测行为零判定力。现在改成结论侧断言：
        既要 `reg is None`，也要越界标志置位（调用方靠它回退 mss）。
        """
        mgr = _manager(FakeMss(), {"left": 1920, "top": 100,
                                   "width": 800, "height": 600}, monitor=3)
        reg, out_of_range = mgr._dxgi_region()
        self.assertIsNone(
            reg,
            "几何取不到却仍返回了 region=%r：这就是把虚拟桌面绝对坐标当输出内"
            "相对坐标泄露给 dxcam 的缺陷形态（旧实现返回 (1920,100,2720,700)）"
            % (reg,))
        self.assertTrue(out_of_range,
                        "几何取不到时不得静默返回合法区域，必须报 out_of_range=True")

    def test_negative_control_stub_rejects_leaked_rectangle(self):
        """负向对照：证明上面那条断言真的能区分两种形态。

        直接把旧实现的返回值 `(1920, 100, 2720, 700)` 喂给替身校验器：它必须在
        `reg is not None` 的缺陷形态下抛 ValueError（若哪天 _dxgi_region 又返回
        绝对坐标，`test_never_leaks_absolute_coordinates` 的 assertIsNone 会红）。
        本用例不依赖被测行为，所以它自己不可能是假绿。
        """
        with self.assertRaises(ValueError):
            _validate_region_as_dxcam(1920, 1080, (1920, 100, 2720, 700))


    def test_warns_once_across_calls(self):
        """每帧都会走到这一支，告警必须只出一条（capture-010 的口径）。"""
        mgr = _manager(FakeMss(), {"left": 1920, "top": 100,
                                   "width": 800, "height": 600}, monitor=3)
        with self.assertLogs(screen.log, level="WARNING") as caught:
            for _ in range(5):
                mgr._dxgi_region()
        self.assertEqual(len(caught.records), 1,
                         "几何缺失每帧告警一次会刷爆 5MB 滚动日志")


class TestGrabEndToEnd(IsolatedGeometry):
    """端到端：几何缺失 + dxgi 后端 + 区域配置，grab 必须走 mss 而不是抛异常。"""

    def test_grab_falls_back_to_mss(self):
        cam = StrictCamera(1920, 1080)
        mss = FakeMss()
        mgr = _manager(mss, {"left": 1920, "top": 100,
                             "width": 800, "height": 600}, monitor=3, camera=cam)
        frame = mgr.grab()          # 缺陷形态在此抛 ValueError
        self.assertIsNotNone(frame, "回退 mss 后应抓到一帧")
        self.assertEqual(frame.shape, (4, 6, 3), "应返回 BGR 三通道")
        self.assertEqual(len(mss.grabbed), 1, "必须真的走 mss 抓帧")
        self.assertEqual(cam.calls, [],
                         "region 被递给了 dxcam：几何不可得时不该这么干")

    def test_grab_does_not_trip_error_counter(self):
        """回退成功不得计入连续异常——否则会误触发重建。"""
        cam = StrictCamera(1920, 1080)
        mgr = _manager(FakeMss(), {"left": 1920, "top": 100,
                                   "width": 800, "height": 600},
                       monitor=3, camera=cam)
        for _ in range(10):
            mgr.grab()
        self.assertEqual(mgr.consecutive_errors, 0,
                         "回退成功却仍在累加异常，5 次后照样进重建循环")

    def test_grab_returns_none_when_mss_also_unavailable(self):
        """mss 同时不可用时返回 None（交给 CaptureSource 的空帧判据），不抛异常。"""
        cam = StrictCamera(1920, 1080)
        mgr = _manager(None, {"left": 1920, "top": 100,
                              "width": 800, "height": 600}, monitor=3, camera=cam)
        self.assertIsNone(mgr.grab())
        self.assertEqual(mgr.consecutive_errors, 0,
                         "无句柄属既有语义（capture-002），不该被记成异常")
        self.assertEqual(cam.calls, [], "没有 mss 兜底时也不该把绝对坐标递给 dxcam")


class TestNormalPathsUnchanged(IsolatedGeometry):
    """过度修正防护：几何可用时的两条既有分支必须原样保留。"""

    def test_region_inside_monitor_is_translated(self):
        mgr = _manager(FakeMss(MSS_MONITORS),
                       {"left": 300, "top": 200, "width": 800, "height": 600},
                       monitor=1)
        reg, out_of_range = mgr._dxgi_region()
        self.assertFalse(out_of_range)
        self.assertEqual(reg, (300, 200, 1100, 800),
                         "屏内区域必须换算成输出内相对坐标")

    def test_region_inside_secondary_monitor_is_translated(self):
        """副屏：绝对坐标 1920 起 → 相对坐标 0 起（capture-003 的正例）。"""
        mgr = _manager(FakeMss(MSS_MONITORS),
                       {"left": 2000, "top": 100, "width": 800, "height": 600},
                       monitor=2)
        reg, out_of_range = mgr._dxgi_region()
        self.assertFalse(out_of_range)
        self.assertEqual(reg, (80, 100, 880, 700))

    def test_region_crossing_monitor_edge_falls_back(self):
        mgr = _manager(FakeMss(MSS_MONITORS),
                       {"left": 1800, "top": 100, "width": 800, "height": 600},
                       monitor=1)
        reg, out_of_range = mgr._dxgi_region()
        self.assertIsNone(reg)
        self.assertTrue(out_of_range)

    def test_secondary_monitor_origin_is_subtracted(self):
        """主屏在左、副屏起于 1920：副屏区域必须减掉 1920 而不是当成越界。"""
        mgr = _manager(FakeMss(MSS_MONITORS),
                       {"left": 1920, "top": 0, "width": 1920, "height": 1080},
                       monitor=2)
        reg, out_of_range = mgr._dxgi_region()
        self.assertFalse(out_of_range, "整块副屏不应被判越界")
        self.assertEqual(reg, (0, 0, 1920, 1080))


class TestRealDxcamCrossCheck(unittest.TestCase):
    """替身与真实 vendored dxcam 0.3.0 的校验行为必须一致（CI 无 vendor 时跳过）。"""

    def test_stub_matches_real_validate_region(self):
        screen.ensure_vendor_on_path()
        try:
            from dxcam.dxcam import DXCamera
        except Exception as e:                       # noqa: BLE001
            self.skipTest("vendored dxcam 不可用（CI 无 vendor/）：%s" % (e,))

        class _Self:
            width = 1920
            height = 1080

        cases = [
            (0, 0, 1920, 1080),          # 整屏：合法
            (300, 200, 1100, 800),       # 屏内：合法
            (1920, 100, 2720, 700),      # capture-011 缺陷坐标：非法
            (0, 0, 1920, 0),             # 零高：非法
            (-1, 0, 100, 100),           # 负起点：非法
            (0, 0, 2000, 1080),          # 超出宽度：非法
        ]
        for region in cases:
            with self.subTest(region=region):
                real_raised = stub_raised = False
                try:
                    DXCamera._validate_region(_Self(), region)
                except ValueError:
                    real_raised = True
                try:
                    _validate_region_as_dxcam(1920, 1080, region)
                except ValueError:
                    stub_raised = True
                self.assertEqual(real_raised, stub_raised,
                                 "替身与真实 _validate_region 判定不一致，"
                                 "本文件的替身用例会失去意义")


if __name__ == "__main__":
    unittest.main()
