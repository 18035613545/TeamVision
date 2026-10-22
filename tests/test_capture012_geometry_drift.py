# -*- coding: utf-8 -*-
"""capture-012（2026-10-10）：显示几何漂移时 mss 采集必须跟进新分辨率。

缺陷形态（实测，见 docs/audit/capture.md capture-012）：

mss 的 `monitors` 是**每实例只算一次**的缓存（上游 mss/base.py:346-350，官方
文档明确说它是缓存且无刷新入口）。用户改显示分辨率/插拔显示器后，旧矩形继续
被传给 `grab()`；而 GDI 的 `BitBlt` 在源矩形完全离屏时**返回成功却根本不写目标
DIB**（mss/windows/gdi.py:380）。于是一条异常都不抛、一个 None 都不返回，
pipeline.py 的三条判据（异常重建 / 空帧重建 / 后端存活）全部落空，共享端安静地
重复发送分辨率变更前的最后一帧。

本文件钉住修复后的行为：
  - `screen.probe_geometry` / `screen.geometry_changed` —— 判据本身的语义；
  - `CaptureManager._monitor_rect` —— 几何优先取自探针；
  - `CaptureManager._grab_mss` —— 整屏分支把新矩形传给 grab（修复的落点）；
  - 探针不可用时必须退回 mss 既有语义，不新增失败模式。

所有用例都用伪 mss 与伪探针，**不碰真实屏幕、不触 DPI 感知副作用**，CI headless
可跑。真实 `EnumDisplayMonitors` 的构造口径由 TestProbeMatchesRealMss 逐项比对。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import screen  # noqa: E402

# mss 语义：索引 0 = 全部屏幕并集，索引 1..N = 第 1..N 块
GEOM_1920 = [
    {"left": 0, "top": 0, "width": 1920, "height": 1080},
    {"left": 0, "top": 0, "width": 1920, "height": 1080},
]
GEOM_2560 = [
    {"left": 0, "top": 0, "width": 2560, "height": 1440},
    {"left": 0, "top": 0, "width": 2560, "height": 1440},
]
GEOM_DUAL = [
    {"left": 0, "top": 0, "width": 3840, "height": 1080},
    {"left": 0, "top": 0, "width": 1920, "height": 1080},
    {"left": 1920, "top": 0, "width": 1920, "height": 1080},
]


class _Shot:
    """mss.ScreenShot 替身：暴露 BGRA 的 __array_interface__ 供 np.asarray。"""

    def __init__(self, width, height):
        self._arr = np.full((height, width, 4), 60, dtype=np.uint8)

    @property
    def __array_interface__(self):
        return self._arr.__array_interface__


class FakeMss:
    """伪 mss 句柄，忠实复刻上游 mss/base.py:346-350 的缓存语义。

    上游只有当 `self._monitors is None` 时才重新调用 `_impl.monitors()`。这里把
    `_monitors` 做成真正的开关：置空 → 下次访问 `monitors` 触发重枚举 → 拿到
    当前真实几何。不置空则永远返回陈旧值，这正是 capture-012 的缺陷前提。
    """

    def __init__(self, monitors=None):
        self._current_geom = [dict(m) for m in (monitors or GEOM_1920)]
        self._monitors = [dict(m) for m in self._current_geom]
        self.grabbed = []
        self.reenumerations = 0
        self.closed = False

    @property
    def monitors(self):
        if self._monitors is None:          # 上游 base.py:347 的开关
            self.reenumerations += 1
            self._monitors = [dict(m) for m in self._current_geom]
        return self._monitors

    @monitors.setter
    def monitors(self, value):
        self._monitors = value

    def set_real_geometry(self, geom):
        """模拟用户改显示设置：真实几何变了，但缓存仍需显式作废才会更新。"""
        self._current_geom = [dict(m) for m in geom]

    def grab(self, mon):
        self.grabbed.append(dict(mon))
        return _Shot(int(mon["width"]), int(mon["height"]))

    def close(self):
        self.closed = True
        self._monitors = None


def make_manager(mss, monitor=0, region=None, backend="mss"):
    """构造绕过 `_open()` 的 CaptureManager（不碰真实屏幕/真实句柄）。"""
    cap = screen.CaptureManager.__new__(screen.CaptureManager)
    cap.backend = backend
    cap.monitor = monitor
    cap.region = region
    cap._mss = mss
    cap._camera = None
    cap._effective = backend
    cap._errors = 0
    cap._geometry = None
    return cap


class TestGeometryChanged(unittest.TestCase):
    """`geometry_changed` 的比对语义。"""

    def test_identical_is_not_changed(self):
        self.assertFalse(screen.geometry_changed(GEOM_1920, [dict(m) for m in GEOM_1920]))

    def test_size_change_detected(self):
        self.assertTrue(screen.geometry_changed(GEOM_1920, GEOM_2560))

    def test_origin_change_detected(self):
        before = [{"left": 0, "top": 0, "width": 1920, "height": 1080}]
        after = [{"left": 1920, "top": 0, "width": 1920, "height": 1080}]
        self.assertTrue(screen.geometry_changed(before, after))

    def test_monitor_count_change_detected(self):
        """副屏被拔除：列表长度变化。"""
        self.assertTrue(screen.geometry_changed(GEOM_DUAL, GEOM_1920))

    def test_ignores_mss10_extra_metadata(self):
        """mss 10.2 新增的 is_primary/name/unique_id 不参与判定。"""
        before = [dict(m) for m in GEOM_1920]
        after = [dict(m, is_primary=True, name="Generic PnP Monitor", unique_id="x\\y")
                 for m in GEOM_1920]
        self.assertFalse(screen.geometry_changed(before, after))

    def test_unavailable_side_is_not_drift(self):
        """探测失败（None/空）不得被当成漂移，否则会触发无谓重建。"""
        self.assertFalse(screen.geometry_changed(None, GEOM_1920))
        self.assertFalse(screen.geometry_changed(GEOM_1920, None))
        self.assertFalse(screen.geometry_changed([], []))
        self.assertFalse(screen.geometry_changed(GEOM_1920, []))

    def test_malformed_entry_is_not_drift(self):
        """长度相等但单条记录缺字段时退回"几何不可用"，不误判为漂移。"""
        before = [{"left": 0, "top": 0, "width": 1920, "height": 1080},
                  {"left": 0, "top": 0, "width": 1920, "height": 1080}]
        after = [{"left": 0, "top": 0, "width": 1920, "height": 1080},
                 {"left": 0}]
        self.assertFalse(screen.geometry_changed(before, after))


class TestProbeGeometry(unittest.TestCase):
    """`probe_geometry` 的缓存与降级行为。"""

    def test_caches_within_interval(self):
        calls = []

        def _fake():
            calls.append(1)
            return [dict(m) for m in GEOM_1920]

        with mock.patch.object(screen, "_probe_geometry_uncached", _fake):
            screen.reset_geometry_probe()
            a = screen.probe_geometry(interval_s=60.0)
            b = screen.probe_geometry(interval_s=60.0)
        self.assertEqual(len(calls), 1)
        self.assertIs(a, b)

    def test_force_bypasses_cache(self):
        calls = []

        def _fake():
            calls.append(1)
            return [dict(m) for m in GEOM_1920]

        with mock.patch.object(screen, "_probe_geometry_uncached", _fake):
            screen.reset_geometry_probe()
            screen.probe_geometry(interval_s=60.0)
            screen.probe_geometry(interval_s=60.0, force=True)
        self.assertEqual(len(calls), 2)

    def test_failure_is_not_cached(self):
        """探测失败必须立刻可重试，不能把失败钉住一个 interval。"""
        state = {"n": 0}

        def _fake():
            state["n"] += 1
            return None if state["n"] == 1 else [dict(m) for m in GEOM_1920]

        with mock.patch.object(screen, "_probe_geometry_uncached", _fake):
            screen.reset_geometry_probe()
            self.assertIsNone(screen.probe_geometry(interval_s=60.0))
            self.assertIsNotNone(screen.probe_geometry(interval_s=60.0))
        self.assertEqual(state["n"], 2)

    def test_reset_clears_cache(self):
        with mock.patch.object(screen, "_probe_geometry_uncached",
                               lambda: [dict(m) for m in GEOM_1920]):
            screen.reset_geometry_probe()
            screen.probe_geometry(interval_s=60.0)
            screen.reset_geometry_probe()
            self.assertIsNone(screen._PROBE_STATE["cached"])

    def test_win32_error_degrades_to_none(self):
        """EnumDisplayMonitors 返回 0 时不得抛异常（几何不可用）。"""
        if not hasattr(screen, "_probe_geometry_uncached"):
            self.skipTest("探针不存在（非本次修复引入）")
        self.assertIsNone(self._run_real_probe_sandboxed())

    def _run_real_probe_sandboxed(self):
        """跑一次真实 Win32 调用，但强制让它失败以验证降级路径。"""
        import ctypes

        with mock.patch.object(ctypes, "WinDLL", return_value=mock.Mock()):
            return screen._probe_geometry_uncached()

    def test_probe_shape_matches_mss_semantics(self):
        """真实探针（本机单屏）至少给出并集 + 每块两块。"""
        screen.reset_geometry_probe()
        mons = screen.probe_geometry(interval_s=0.0)
        if mons is None:
            self.skipTest("本机无显示环境")
        self.assertGreaterEqual(len(mons), 2)
        for m in mons:
            self.assertEqual({"left", "top", "width", "height"}, set(m.keys()))
            self.assertGreater(int(m["width"]), 0)
            self.assertGreater(int(m["height"]), 0)


class TestMonitorRectUsesProbe(unittest.TestCase):
    """`_monitor_rect` 应优先取自探针，探针不可用才退回 mss。"""

    def test_uses_probe_geometry(self):
        mss = FakeMss(GEOM_1920)
        cap = make_manager(mss, monitor=0)
        with mock.patch.object(screen, "_probe_geometry_uncached",
                               lambda: [dict(m) for m in GEOM_2560]), \
                mock.patch.object(screen, "probe_geometry",
                                  side_effect=lambda **kw: [dict(m) for m in GEOM_2560]):
            rect = cap._monitor_rect(0)
        self.assertEqual(rect, (0, 0, 2560, 1440))

    def test_falls_back_to_mss_when_probe_unavailable(self):
        mss = FakeMss(GEOM_1920)
        cap = make_manager(mss, monitor=1)
        with mock.patch.object(screen, "_probe_geometry_uncached", lambda: None), \
                mock.patch.object(screen, "probe_geometry", return_value=None):
            rect = cap._monitor_rect(1)
        self.assertEqual(rect, (0, 0, 1920, 1080))

    def test_out_of_range_index_returns_none(self):
        mss = FakeMss(GEOM_1920)
        cap = make_manager(mss, monitor=9)
        with mock.patch.object(screen, "_probe_geometry_uncached",
                               lambda: [dict(m) for m in GEOM_1920]), \
                mock.patch.object(screen, "probe_geometry",
                                  side_effect=lambda **kw: [dict(m) for m in GEOM_1920]):
            self.assertIsNone(cap._monitor_rect(9))


class TestGrabMssFollowsGeometry(unittest.TestCase):
    """修复落点：`_grab_mss` 整屏分支必须把新矩形传给 grab。"""

    def _capture(self, probe_geom, mss_geom, monitor=0, region=None, baseline=None):
        """跑一次 `_grab_mss`。`baseline` = 抓这一帧**之前**实例已知的几何。

        拔屏场景必须给 baseline：capture-012 之后"序号越界"有两种含义，靠的正是
        "这个序号上一次探测时是否有效"来区分——没有基准就没有判据。
        """
        mss = FakeMss(mss_geom)
        mss._current_geom = [dict(m) for m in probe_geom]
        cap = make_manager(mss, monitor=monitor, region=region)
        if baseline is not None:
            cap._geometry = [dict(m) for m in baseline]
        with mock.patch.object(screen, "_probe_geometry_uncached",
                               lambda: [dict(m) for m in probe_geom]), \
                mock.patch.object(screen, "probe_geometry",
                                  side_effect=lambda **kw: [dict(m) for m in probe_geom]):
            frame = cap._grab_mss()
        return mss, frame

    def test_resolution_change_follows_new_size(self):
        """核心用例：分辨率变大后，抓取矩形必须从旧尺寸跟进到新尺寸。"""
        mss, frame = self._capture(GEOM_2560, GEOM_1920)
        self.assertEqual(mss.grabbed[0]["width"], 2560)
        self.assertEqual(mss.grabbed[0]["height"], 1440)
        self.assertEqual(frame.shape, (1440, 2560, 3))

    def test_never_passes_stale_geometry(self):
        """mss 自身缓存停在旧尺寸时，传给 grab 的绝不能是旧矩形。"""
        mss, _ = self._capture(GEOM_2560, GEOM_1920)
        self.assertNotEqual(mss.grabbed[0], GEOM_1920[0])

    def test_dual_monitor_uses_correct_index(self):
        mss, _ = self._capture(GEOM_DUAL, GEOM_DUAL, monitor=2)
        self.assertEqual(mss.grabbed[0]["left"], 1920)

    def test_monitor_removed_yields_no_frame(self):
        """副屏被拔除：探针已看到只剩 1 块屏，monitor=2 越界 → 本帧不出图。

        绝不能退回 mss 的陈旧缓存去抓那块已拔除的显示器——`BitBlt` 在离屏源上
        会"成功且不写入"，画面永久冻结，正是 capture-012 要消灭的形态。

        `baseline=GEOM_DUAL` 是必需的：拔屏的判据是"这个序号上一次探测时有效"。
        没有基准就不是拔屏，而是配置写错（见 TestOutOfRangeMonitorConfig）。
        """
        mss, frame = self._capture(GEOM_1920, GEOM_DUAL, monitor=2,
                                   baseline=GEOM_DUAL)
        self.assertEqual(mss.grabbed, [],
                         "显示器已不存在，不得用陈旧缓存继续抓")
        self.assertIsNone(frame, "整帧交给 CaptureSource 的空帧判据")

    def test_probe_unavailable_degrades_without_raising(self):
        """探针不可用 → 退回 mss 既有路径，不新增失败模式。"""
        mss = FakeMss(GEOM_1920)
        cap = make_manager(mss, monitor=0)
        with mock.patch.object(screen, "probe_geometry", return_value=None):
            frame = cap._grab_mss()
        self.assertIsNotNone(frame)
        self.assertEqual(frame.shape, (1080, 1920, 3))

    def test_end_to_end_resolution_change(self):
        """闭环：模拟用户把分辨率从 1920x1080 改成 2560x1440 的完整过程。

        这是 capture-012 的主回归用例：不修复时第二帧仍按旧尺寸抓（画面冻结），
        修复后第二帧跟进新尺寸。
        """
        mss = FakeMss(GEOM_1920)
        cap = make_manager(mss, monitor=0)

        # 第一帧：几何稳定
        with mock.patch.object(screen, "_probe_geometry_uncached",
                               lambda: [dict(m) for m in GEOM_1920]):
            f0 = cap._grab_mss()
        self.assertEqual(f0.shape, (1080, 1920, 3))
        self.assertEqual(mss.grabbed[0]["width"], 1920)

        # 用户改分辨率：真实几何变了，mss 缓存仍停在旧值
        mss.set_real_geometry(GEOM_2560)
        self.assertEqual(mss.monitors[0]["width"], 1920, "缓存此时应当仍是旧值")

        screen.reset_geometry_probe()
        with mock.patch.object(screen, "_probe_geometry_uncached",
                               lambda: [dict(m) for m in GEOM_2560]):
            f1 = cap._grab_mss()

        self.assertEqual(f1.shape, (1440, 2560, 3),
                         "分辨率变更后必须抓到新尺寸，否则画面冻结在旧帧")
        self.assertEqual(mss.grabbed[1]["width"], 2560)

        # mss 自身缓存此刻仍是旧值——修复不依赖它刷新，矩形直接来自几何探针。
        # 探针同时把 `_monitors` 置空，让 mss 的其它调用路径（若绕过探针）也能自愈。
        self.assertIsNone(mss._monitors,
                          "漂移后 mss 的私有缓存应被作废，等下次访问时重枚举")

    def test_region_branch_ignores_geometry(self):
        """区域采集来自用户配置，不受显示设置变更影响，行为必须不变。"""
        region = {"left": 10, "top": 20, "width": 640, "height": 480}
        mss, frame = self._capture(GEOM_2560, GEOM_1920, region=region)
        self.assertEqual(mss.grabbed[0], region)
        self.assertEqual(frame.shape, (480, 640, 3))

    def test_no_mss_handle_returns_none(self):
        cap = make_manager(None, monitor=0)
        self.assertIsNone(cap._grab_mss())


class TestRefreshGeometryInvalidatesCache(unittest.TestCase):
    """漂移时作废 mss 的几何缓存，让下一次访问触发重枚举。"""

    def test_drift_invalidates_mss_cache(self):
        """作废的是上游的私有缓存 `_monitors`——那正是 base.py:347 的重枚举开关。"""
        mss = FakeMss(GEOM_1920)
        cap = make_manager(mss, monitor=0)
        cap._geometry = [dict(m) for m in GEOM_1920]   # 基准：漂移前
        with mock.patch.object(screen, "probe_geometry",
                               side_effect=lambda **kw: [dict(m) for m in GEOM_2560]):
            changed = cap._refresh_geometry(force=True)
        self.assertTrue(changed)
        self.assertIsNone(mss._monitors,
                          "mss 的私有缓存 _monitors 应被置空以触发重枚举")

    def test_no_drift_leaves_cache_intact(self):
        mss = FakeMss(GEOM_1920)
        cap = make_manager(mss, monitor=0)
        cap._geometry = [dict(m) for m in GEOM_1920]
        with mock.patch.object(screen, "probe_geometry",
                               side_effect=lambda **kw: [dict(m) for m in GEOM_1920]):
            changed = cap._refresh_geometry(force=True)
        self.assertFalse(changed)
        self.assertEqual(mss.monitors[0]["width"], 1920)

    def test_probe_failure_is_not_drift(self):
        mss = FakeMss(GEOM_1920)
        cap = make_manager(mss, monitor=0)
        cap._geometry = [dict(m) for m in GEOM_1920]
        with mock.patch.object(screen, "probe_geometry", return_value=None):
            self.assertFalse(cap._refresh_geometry(force=True))
        self.assertEqual(mss.monitors[0]["width"], 1920)

    def test_private_field_failure_is_not_a_rebuild_signal(self):
        """mss 私有字段写不动时不是"重建信号"：该 except 的成因重建也无效。

        R8-capture-02（2026-10-10）：旧用例名 `..._degrades_to_rebuild_signal` 与
        docstring"交由调用方走整套重建"是**假承诺**——走这条 except 时函数已在
        `self._geometry = current` 之后，即"漂移已检出、告警已发、基准已刷新"；
        而重建出来的 mss 实例同样没有 `_monitors` 字段，对本条成因 100% 无效，
        全仓也没有第二个调用点能消费它。`True` 的真实语义只是"本帧检出了漂移"。

        这里改钉真实可观测后果，而不是那条兑现不了的信号。
        """
        class HostileMss(FakeMss):
            @property
            def monitors(self):
                raise RuntimeError("上游改了内部结构")

            @monitors.setter
            def monitors(self, _v):
                raise RuntimeError("上游改了内部结构")

        mss = HostileMss(GEOM_1920)
        cap = make_manager(mss, monitor=0)
        cap._geometry = [dict(m) for m in GEOM_1920]
        reopened = []
        cap._open = lambda: reopened.append(1)

        with mock.patch.object(screen, "probe_geometry",
                               side_effect=lambda **kw: [dict(m) for m in GEOM_2560]):
            changed = cap._refresh_geometry(force=True)   # (c) 不得抛异常
            rect = cap._monitor_rect(0)                   # (d) 之后仍取到新几何

        self.assertTrue(changed,
                        "走到这条 except 时漂移已经检出——True 表示「检出漂移」，"
                        "不是「需要重建」")
        self.assertEqual(reopened, [], "私有字段写不动不得被当成重建入口")
        self.assertEqual(cap._geometry, GEOM_2560, "漂移已检出，基准必须已刷新")
        self.assertIs(cap._mss, mss, "不应换掉 mss 句柄")
        self.assertEqual(rect, (0, 0, 2560, 1440),
                         "陈旧 mss 缓存（1920x1080）不得再被用作几何")

    def test_first_probe_establishes_baseline_without_warning(self):
        """首帧 `_geometry` 为 None，属建立基准而非漂移——不得报漂移、不得清缓存。"""
        mss = FakeMss(GEOM_1920)
        cap = make_manager(mss, monitor=0)
        self.assertIsNone(cap._geometry)
        with mock.patch.object(screen, "probe_geometry",
                               side_effect=lambda **kw: [dict(m) for m in GEOM_1920]):
            self.assertFalse(cap._refresh_geometry(force=True))
        self.assertEqual(mss.monitors[0]["width"], 1920)


class TestReviewFixes(unittest.TestCase):
    """审查发现的三处缺陷的回归钉子。"""

    def setUp(self):
        screen._PICK_WARNED.clear()

    def tearDown(self):
        screen._PICK_WARNED.clear()

    def test_removed_monitor_never_uses_stale_cache(self):
        """缺陷一：拔掉副屏后不得回落到 mss 陈旧缓存去抓那块已不存在的屏。

        基准 `_geometry = GEOM_DUAL` 表示"上一帧还在正常采第 2 块屏"，所以这次
        越界只能是拔屏，必须放弃本帧。
        """
        mss = FakeMss(GEOM_DUAL)          # mss 缓存仍是三屏
        cap = make_manager(mss, monitor=2)  # 但采的是第 2 块（副屏）
        cap._geometry = [dict(m) for m in GEOM_DUAL]
        with mock.patch.object(screen, "probe_geometry",
                               side_effect=lambda **kw: [dict(m) for m in GEOM_1920]):
            frame = cap._grab_mss()
        self.assertIsNone(frame)
        self.assertEqual(mss.grabbed, [],
                         "已拔除的显示器不得用陈旧矩形继续抓（会冻结画面）")

    def test_probe_unavailable_may_still_fall_back_to_mss(self):
        """对照：探针不可用是'没测到'而非'屏没了'，此时退回 mss 是安全的。"""
        mss = FakeMss(GEOM_DUAL)
        cap = make_manager(mss, monitor=2)
        with mock.patch.object(screen, "probe_geometry", return_value=None):
            frame = cap._grab_mss()
        self.assertIsNotNone(frame)
        self.assertEqual(len(mss.grabbed), 1)
        self.assertEqual(mss.grabbed[0]["left"], 1920)

    def test_drift_warning_key_includes_monitor(self):
        """缺陷二：漂移告警的 key 必须区分显示器序号，多路采集互不压制。"""
        seen = []

        def _cap(monitor):
            c = make_manager(FakeMss(GEOM_1920), monitor=monitor)
            c._geometry = [dict(m) for m in GEOM_1920]
            return c

        for monitor in (1, 2):
            with mock.patch.object(screen, "probe_geometry",
                                   side_effect=lambda **kw: [dict(m) for m in GEOM_2560]), \
                    mock.patch.object(screen.log, "warning",
                                      side_effect=lambda *a: seen.append(a[0] % a[1:])):
                _cap(monitor)._refresh_geometry(force=True)
        self.assertEqual(len(seen), 2,
                         "两路采集各报一次；key 相同会让第二条被降级成 debug")

    def test_mss_handle_missing_is_reported(self):
        """缺陷三：mss 句柄缺失必须仍有告警，不因探针可用而被吞掉。"""
        cap = make_manager(None, monitor=1)
        with mock.patch.object(screen, "probe_geometry",
                               side_effect=lambda **kw: [dict(m) for m in GEOM_1920]):
            self.assertEqual(cap._pick_output_idx(), (0, 0))
        self.assertTrue(any("mss-handle-missing" in k for k in screen._PICK_WARNED),
                        "mss 初始化失败必须留有日志线索")


class TestOutOfRangeMonitorConfig(unittest.TestCase):
    """配置的显示器序号越界时：降级出图，绝不退化成"完全不出图且不自愈"。

    背景：`host.py:1283` 的 `safe_int(capture.get("monitor", 1), 1, 0)` **只有下界
    没有上界**，`host_ui.py:348` 的下拉框是 readonly 但 `config.json` 可以手改，
    所以 `monitor: 99` 是可达的。这条路径上的"放弃本帧"会造成：

        host.py:3259-3260  none_rebuild_requires_dead=True

    空帧只有在采集后端确认死亡时才触发重建，纯空帧会被跳过 —— 于是丢掉这一帧
    等于黑屏到底、永远不会自愈。比降级出图糟糕得多。

    与拔屏的区别只有一条：**这个序号上一次探测时是否有效**。
    """

    def setUp(self):
        screen._PICK_WARNED.clear()

    def tearDown(self):
        screen._PICK_WARNED.clear()

    def _capture(self, probe_geom, monitor, mss_geom=GEOM_1920, baseline=None):
        mss = FakeMss(mss_geom)
        mss._current_geom = [dict(m) for m in probe_geom]
        cap = make_manager(mss, monitor=monitor)
        if baseline is not None:
            cap._geometry = [dict(m) for m in baseline]
        with mock.patch.object(screen, "probe_geometry",
                               side_effect=lambda **kw: [dict(m) for m in probe_geom]):
            frame = cap._grab_mss()
        return mss, frame, cap

    def test_config_out_of_range_still_produces_frame(self):
        """核心用例：配置 monitor=99 而本机只有 1 块屏 —— 必须出图。"""
        mss, frame, _ = self._capture(GEOM_1920, monitor=99)
        self.assertIsNotNone(frame,
                             "配置序号越界不是硬件故障，丢帧会导致黑屏且不自愈")
        self.assertEqual(len(mss.grabbed), 1)

    def test_config_out_of_range_falls_back_to_a_real_monitor(self):
        """降级目标必须是**当前真实存在**的显示器矩形，不能是并集也不是陈旧缓存。"""
        mss, frame, _ = self._capture(GEOM_DUAL, monitor=99)
        self.assertIn(mss.grabbed[0], GEOM_DUAL,
                      "必须抓当前探针报告的某块真实显示器")
        self.assertEqual(mss.grabbed[0], GEOM_DUAL[1],
                         "优先回退到第 1 块屏（mss 索引 1），而不是 0 号并集")
        self.assertEqual(frame.shape, (1080, 1920, 3))

    def test_config_out_of_range_keeps_producing_frames(self):
        """连续多帧都得有画面——这是"不自愈"与"自愈"的分界。"""
        mss = FakeMss(GEOM_1920)
        cap = make_manager(mss, monitor=99)
        frames = []
        with mock.patch.object(screen, "probe_geometry",
                               side_effect=lambda **kw: [dict(m) for m in GEOM_1920]):
            for _ in range(10):
                frames.append(cap._grab_mss())
        self.assertEqual(len(mss.grabbed), 10)
        # 不能用 assertNotIn(None, frames)：None 与 ndarray 逐元素比较会抛
        # "truth value of an array is ambiguous"
        self.assertTrue(all(f is not None for f in frames),
                        "任一帧为空都会被 host 的 none_rebuild_requires_dead 吞掉")

    def test_still_out_of_range_after_first_probe_stays_degraded(self):
        """首帧建立基准之后，序号仍越界 —— 不得在第二帧翻脸改成放弃本帧。"""
        mss = FakeMss(GEOM_1920)
        cap = make_manager(mss, monitor=99)
        with mock.patch.object(screen, "probe_geometry",
                               side_effect=lambda **kw: [dict(m) for m in GEOM_1920]):
            first = cap._grab_mss()
            self.assertIsNotNone(first)
            self.assertIsNotNone(cap._geometry, "首帧应已建立几何基准")
            second = cap._grab_mss()
        self.assertIsNotNone(second,
                             "基准只说明'屏幕没变'，不说明'配置写对了'")
        self.assertEqual(mss.grabbed[0]["width"], mss.grabbed[1]["width"])

    def test_baseline_shorter_than_index_is_config_error(self):
        """基准存在但长度不够（双屏机器上配了 monitor=5）→ 仍是配置错误。"""
        mss, frame, _ = self._capture(GEOM_DUAL, monitor=5, baseline=GEOM_DUAL)
        self.assertIsNotNone(frame)
        self.assertEqual(mss.grabbed[0], GEOM_DUAL[1])

    def test_warning_key_distinguishes_config_error_from_unplug(self):
        """两类故障的告警键必须不同，否则用户只会看到含糊的"屏幕没了"。"""
        self._capture(GEOM_1920, monitor=99)
        self.assertIn("monitor-out-of-range-99", screen._PICK_WARNED)
        self.assertNotIn("monitor-gone-99", screen._PICK_WARNED)

    def test_unplug_warns_under_its_own_key(self):
        """对照：真有基准的拔屏走 monitor-gone。"""
        self._capture(GEOM_1920, monitor=2, mss_geom=GEOM_DUAL, baseline=GEOM_DUAL)
        self.assertIn("monitor-gone-2", screen._PICK_WARNED)
        self.assertNotIn("monitor-out-of-range-2", screen._PICK_WARNED)

    def test_warning_message_names_the_setting_to_fix(self):
        """告警必须告诉用户改哪个配置项，否则等于没报。"""
        msgs = []
        with mock.patch.object(screen.log, "warning",
                               side_effect=lambda *a: msgs.append(a[0] % a[1:])):
            self._capture(GEOM_1920, monitor=99)
        self.assertTrue(any("capture.monitor" in m for m in msgs),
                        "告警文案需指明 host.capture.monitor：%r" % msgs)

    def test_region_capture_ignores_monitor_index(self):
        """区域采集的矩形来自用户配置，monitor 序号越界不该影响它。"""
        region = {"left": 10, "top": 20, "width": 640, "height": 480}
        mss = FakeMss(GEOM_1920)
        cap = make_manager(mss, monitor=99, region=region)
        with mock.patch.object(screen, "probe_geometry",
                               side_effect=lambda **kw: [dict(m) for m in GEOM_1920]):
            frame = cap._grab_mss()
        self.assertEqual(mss.grabbed[0], region)
        self.assertEqual(frame.shape, (480, 640, 3))

    def test_unplug_then_replug_recovers(self):
        """拔掉的那一帧放弃，插回来后立刻恢复出图（整段链路自愈）。"""
        mss = FakeMss(GEOM_DUAL)
        cap = make_manager(mss, monitor=2)
        cap._geometry = [dict(m) for m in GEOM_DUAL]

        with mock.patch.object(screen, "probe_geometry",
                               side_effect=lambda **kw: [dict(m) for m in GEOM_1920]):
            self.assertIsNone(cap._grab_mss(), "拔屏期间必须放弃本帧")

        mss.set_real_geometry(GEOM_DUAL)
        with mock.patch.object(screen, "probe_geometry",
                               side_effect=lambda **kw: [dict(m) for m in GEOM_DUAL]):
            back = cap._grab_mss()
        self.assertIsNotNone(back, "屏幕插回来后必须自动恢复")
        self.assertEqual(mss.grabbed[-1]["left"], 1920)


class TestRegionFollowsDesktop(unittest.TestCase):
    """capture-014（2026-10-10）：区域采集也必须跟随桌面几何变化。

    capture-012 的整屏分支走了几何探针，区域分支当时被断言为"不受显示设置变更
    影响"——**这个断言是错的**。区域矩形的取值来自用户配置没错，但它在虚拟桌面里
    是否还有效完全由当前几何决定；`BitBlt` 对离屏源矩形同样是"成功且不写入"。

    本机实测（真实 `mss.MSS()`，非替身）：
      - 同尺寸离屏源连续抓两次 → 两次的数组字节散列**完全相同**，且等于上一次
        同尺寸有效抓帧的散列（冻结）；
      - 部分越界 → 屏内部分正确、越屏部分写入全零（黑边）。
    """

    def setUp(self):
        screen._PICK_WARNED.clear()

    def tearDown(self):
        screen._PICK_WARNED.clear()

    def _cap(self, probe_geom, mss_geom=GEOM_1920):
        mss = FakeMss(mss_geom)
        mss._current_geom = [dict(m) for m in probe_geom]
        return mss, make_manager(mss, monitor=1)

    def _grab_with(self, probe_geom, region, mss_geom=GEOM_1920):
        mss, cap = self._cap(probe_geom, mss_geom)
        cap.region = region
        with mock.patch.object(screen, "probe_geometry",
                               side_effect=lambda **kw: [dict(m) for m in probe_geom]):
            frame = cap._grab_mss()
        return mss, frame

    def test_region_inside_desktop_is_untouched(self):
        """区域完全在桌面内：行为必须与修复前逐字节一致（不产生额外约束）。"""
        region = {"left": 100, "top": 100, "width": 640, "height": 480}
        mss, frame = self._grab_with(GEOM_1920, region)
        self.assertEqual(mss.grabbed[0], region)
        self.assertEqual(frame.shape, (480, 640, 3))

    def test_region_wider_than_new_desktop_is_clamped(self):
        """分辨率从 1920x1080 改到 1280x720：旧整屏区域必须夹回可见范围。"""
        small = [
            {"left": 0, "top": 0, "width": 1280, "height": 720},
            {"left": 0, "top": 0, "width": 1280, "height": 720},
        ]
        region = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        mss, frame = self._grab_with(small, region)
        self.assertEqual(mss.grabbed[0],
                         {"left": 0, "top": 0, "width": 1280, "height": 720},
                         "越界区域必须夹到当前桌面内，而不是继续抓离屏矩形")
        self.assertEqual(frame.shape, (720, 1280, 3))

    def test_clamped_region_never_exceeds_desktop(self):
        """不管怎么配，抓取矩形必须完全落在探针报告的桌面内。"""
        for region in ({"left": -500, "top": -500, "width": 1920, "height": 1080},
                       {"left": 1500, "top": 900, "width": 900, "height": 600},
                       {"left": 0, "top": 0, "width": 5000, "height": 5000}):
            mss, _ = self._grab_with(GEOM_1920, region)
            g = mss.grabbed[0]
            self.assertGreaterEqual(g["left"], 0)
            self.assertGreaterEqual(g["top"], 0)
            self.assertLessEqual(g["left"] + g["width"], 1920)
            self.assertLessEqual(g["top"] + g["height"], 1080)

    def test_fully_offscreen_region_degrades_to_full_screen(self):
        """区域完全跑到桌面外：退回整屏，**不返回 None**。

        返回 None 会被 host 的 `none_rebuild_requires_dead=True` 吞掉，纯空帧
        不触发重建——画面永久黑且不自愈，比退回整屏糟糕得多。
        """
        region = {"left": 5000, "top": 5000, "width": 800, "height": 600}
        mss, frame = self._grab_with(GEOM_1920, region)
        self.assertIsNotNone(frame, "不得因区域完全离屏而丢帧（丢帧=永久黑屏）")
        self.assertEqual(mss.grabbed[0], GEOM_1920[0])
        self.assertEqual(frame.shape, (1080, 1920, 3))

    def test_clamp_warns_under_its_own_key(self):
        region = {"left": 0, "top": 0, "width": 2400, "height": 1400}
        self._grab_with(GEOM_1920, region)
        self.assertIn("region-clamped-1", screen._PICK_WARNED)

    def test_offscreen_warns_under_its_own_key(self):
        region = {"left": 5000, "top": 5000, "width": 800, "height": 600}
        self._grab_with(GEOM_1920, region)
        self.assertIn("region-offscreen-1", screen._PICK_WARNED)

    def test_probe_unavailable_keeps_configured_region(self):
        """探针不可用 = 本次没测到，不是"区域无效"：原样抓，不新增失败模式。"""
        region = {"left": 5000, "top": 5000, "width": 800, "height": 600}
        mss = FakeMss(GEOM_1920)
        cap = make_manager(mss, monitor=1, region=region)
        with mock.patch.object(screen, "probe_geometry", return_value=None):
            frame = cap._grab_mss()
        self.assertEqual(mss.grabbed[0], region)
        self.assertEqual(frame.shape, (600, 800, 3))

    def test_negative_origin_region_survives(self):
        """副屏在左侧（负原点）是常见布局，正常区域不得被误夹。"""
        dual = [
            {"left": -1920, "top": 0, "width": 1920, "height": 1080},
            {"left": -1920, "top": 0, "width": 1920, "height": 1080},
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
        ]
        region = {"left": -1900, "top": 10, "width": 1800, "height": 1000}
        mss, frame = self._grab_with(dual, region)
        self.assertEqual(mss.grabbed[0], region)
        self.assertEqual(frame.shape, (1000, 1800, 3))

    def test_malformed_region_is_left_alone(self):
        """探针给出的记录不可解析时按配置矩形抓，不抛。"""
        mss, cap = self._cap(GEOM_1920)
        cap.region = {"left": 0, "top": 0, "width": 640, "height": 480}
        broken = [{"left": 0, "top": 0, "width": 1920, "height": 1080}, {"oops": 1}]
        with mock.patch.object(screen, "probe_geometry",
                               side_effect=lambda **kw: broken):
            frame = cap._grab_mss()
        self.assertEqual(mss.grabbed[0], cap.region)
        self.assertEqual(frame.shape, (480, 640, 3))


class TestNoPixelBasedJudgement(unittest.TestCase):
    """判据必须是几何比对，不得引入基于画面内容的检测。"""

    def test_static_frame_is_not_treated_as_failure(self):
        """静止画面（每帧字节相同）必须照常产出，不触发任何失效路径。"""
        import hashlib

        class IdenticalMss(FakeMss):
            def grab(self, mon):
                self.grabbed.append(dict(mon))
                return _Shot(int(mon["width"]), int(mon["height"]))

        mss = IdenticalMss(GEOM_1920)
        cap = make_manager(mss, monitor=0)
        hashes = []
        for _ in range(5):
            with mock.patch.object(screen, "_probe_geometry_uncached",
                                   lambda: [dict(m) for m in GEOM_1920]), \
                    mock.patch.object(screen, "probe_geometry",
                                      side_effect=lambda **kw: [dict(m) for m in GEOM_1920]):
                frame = cap._grab_mss()
            hashes.append(hashlib.md5(frame.tobytes()).hexdigest())
        self.assertEqual(len(set(hashes)), 1, "本用例就是要静止帧")
        self.assertEqual(len(mss.grabbed), 5, "静止帧必须照常抓取 5 次")


class TestRealProbeMatchesMss(unittest.TestCase):
    """本机交叉核验：探针的构造口径必须与真实 mss 一致（防止替身比真实宽松）。"""

    def test_probe_matches_real_mss_monitors(self):
        try:
            import mss
        except ImportError:
            self.skipTest("本机无 mss")
        screen.reset_geometry_probe()
        probed = screen.probe_geometry(interval_s=0.0)
        if probed is None:
            self.skipTest("本机无显示环境")
        try:
            real = mss.MSS().monitors
        except Exception:
            self.skipTest("mss 初始化失败")
        self.assertEqual(len(probed), len(real))
        keys = ("left", "top", "width", "height")
        for a, b in zip(probed, real, strict=True):
            for k in keys:
                self.assertEqual(int(a[k]), int(b[k]),
                                 "探针与 mss 的 %s 不一致：%s vs %s" % (k, a, b))


class TestExistingPathsUnchanged(unittest.TestCase):
    """回归：本次改动不得影响既有正常路径。"""

    def test_dxgi_region_still_converts_correctly(self):
        cap = make_manager(FakeMss(GEOM_DUAL), monitor=1,
                           region={"left": 100, "top": 200, "width": 800, "height": 600})
        with mock.patch.object(screen, "_probe_geometry_uncached",
                               lambda: [dict(m) for m in GEOM_DUAL]), \
                mock.patch.object(screen, "probe_geometry",
                                  side_effect=lambda **kw: [dict(m) for m in GEOM_DUAL]):
            reg, oob = cap._dxgi_region()
        self.assertFalse(oob)
        self.assertEqual(reg, (100, 200, 900, 800))

    def test_dxgi_region_still_detects_out_of_range(self):
        cap = make_manager(FakeMss(GEOM_1920), monitor=1,
                           region={"left": 5000, "top": 0, "width": 100, "height": 100})
        with mock.patch.object(screen, "_probe_geometry_uncached",
                               lambda: [dict(m) for m in GEOM_1920]), \
                mock.patch.object(screen, "probe_geometry",
                                  side_effect=lambda **kw: [dict(m) for m in GEOM_1920]):
            reg, oob = cap._dxgi_region()
        self.assertTrue(oob)
        self.assertIsNone(reg)


if __name__ == "__main__":
    unittest.main()
