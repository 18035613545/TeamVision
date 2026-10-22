# -*- coding: utf-8 -*-
"""回归：控制台面板不得遮挡右上角悬浮窗（小窗里的对方屏幕必须完整可见）。

旧实现把面板定位为 `screenwidth - panel_reqwidth - 40`——只按屏幕右缘内缩 40px，
完全没算悬浮窗宽度（160~960px + 10px 边距）。而面板与悬浮窗同为 topmost，面板又
创建更晚（z 序在上），于是启动后面板把小窗压掉大半，用户只能看到露在面板上方的
一条画面（实测：2560 屏 / display_width=361 时，两窗相交 366x70 且面板在上）。

修复：面板改为按**悬浮窗实际左缘**让位——右边缘停在悬浮窗左侧 `_PANEL_GAP` 处；
悬浮窗尺寸一变（滑块/叠加行变宽/切文字占位）即重新让位；用户手动拖过面板后不再
自动挪动（重新显示面板即复位）。

T-14（2026-09-24）：不变量用例原先只取 2560/1920 宽屏（全有余量），刻意避开了
已知失效组合——1366 屏 + display_width=960 时 panel 0..694 vs float 从 396 起，
恒相交；`test_clamped_on_narrow_screen` 又只钉钳位值 `+0+60`、不查重叠。现在参数组
覆盖窄屏并把断言改为**矩形不相交**（收窄/下移都算合法兜底）。

hermetic：`ViewerApp.__new__` 绕开真实 Tk/接收线程，用记录型假 root/label/overlay/
panel，跑真实 `_position_top_right` + `_position_panel_left_of`。范式同 test_item98。
"""
import unittest

import viewer


class _FakeRoot:
    def __init__(self, screen_w=2560, x=0, y=10, screen_h=1440, height=203):
        self.screen_w = screen_w
        self.screen_h = screen_h
        self._x = x
        self._y = y
        self._height = height       # 悬浮窗实际高度（0 = 未 map，_float_rect 返回 None）
        self.geometries = []
        self.widths = []

    def update_idletasks(self):
        pass

    def winfo_width(self):
        return 0            # 真实 Tk 在 map 前返回 0：宽度须由 label/overlay 兜底

    def winfo_height(self):
        return self._height

    def winfo_screenwidth(self):
        return self.screen_w

    def winfo_screenheight(self):
        return self.screen_h

    def winfo_x(self):
        return self._x

    def winfo_y(self):
        return self._y

    def geometry(self, spec):
        self.geometries.append(spec)
        w, x, y = _parse_geometry(spec)
        if w is not None:
            self.widths.append(w)
        # Tk 语义："+x+y" 只挪位置
        self._x, self._y = x, y


def _parse_geometry(spec):
    """解析 "WxH+x+y" 或 "+x+y"，返回 (宽或 None, x, y)。"""
    size, rest = (spec.split("+", 1)[0], "+" + spec.split("+", 1)[1]) \
        if "+" in spec else (spec, "")
    if size and "x" in size:
        w = int(size.split("x")[0])
        y_part = rest.lstrip("+")
        x_s, _, y_s = y_part.partition("+")
        return w, int(x_s), int(y_s)
    x_s, _, y_s = spec.lstrip("+").partition("+")
    return None, int(x_s), int(y_s)


class _FakeLabel:
    def __init__(self, reqw):
        self._reqw = reqw

    def winfo_reqwidth(self):
        return self._reqw


class _FakeOverlay:
    def __init__(self, reqw=120):
        self._reqw = reqw

    def winfo_reqwidth(self):
        return self._reqw


class _FakePanel:
    def __init__(self, reqw=694, reqh=1010, x=0, y=0, state="normal"):
        self._reqw = reqw
        self._reqh = reqh
        self._x = x
        self._y = y
        self._state = state
        self.geometries = []

    def update_idletasks(self):
        pass

    def state(self):
        return self._state

    def winfo_x(self):
        return self._x

    def winfo_y(self):
        return self._y

    def winfo_reqwidth(self):
        return self._reqw

    def winfo_reqheight(self):
        return self._reqh

    def geometry(self, spec):
        self.geometries.append(spec)
        w, x, y = _parse_geometry(spec)
        if w is not None:
            self._reqw = w          # 收窄后 Tk 的请求宽度随之变化
        self._x, self._y = x, y


def _app(label_reqw=361, overlay_reqw=120, panel_reqw=694, screen_w=2560,
         screen_h=1440, panel_reqh=1010, float_h=203):
    app = viewer.ViewerApp.__new__(viewer.ViewerApp)
    app.root = _FakeRoot(screen_w=screen_w, screen_h=screen_h, height=float_h)
    app.label = _FakeLabel(label_reqw)
    app.status_overlay = _FakeOverlay(overlay_reqw)
    app.panel = _FakePanel(reqw=panel_reqw, reqh=panel_reqh)
    app._float_left = None
    app._panel_auto_xy = None
    app._panel_auto_spec = None
    app._panel_user_moved = False
    app.position = viewer.ViewerApp._position_top_right.__get__(app)
    return app


def _panel_rect(app):
    """按最后一次下发的几何求面板矩形（宽缺省用 reqwidth）。"""
    w, x, y = _parse_geometry(app.panel.geometries[-1])
    if w is None:
        w = app.panel._reqw
    return x, y, x + w, y + app.panel._reqh


def _float_rect(app):
    """按最后一次下发的几何求悬浮窗矩形（宽取 label/overlay 最宽者，高取 root 高度）。"""
    _, x, y = _parse_geometry(app.root.geometries[-1])
    w = max(app.label._reqw, app.status_overlay._reqw)
    return x, y, x + w, y + app.root._height


class TestPanelAvoidsFloat(unittest.TestCase):
    def test_panel_sits_left_of_float_with_gap(self):
        """默认 361 宽小窗：面板右缘停在悬浮窗左侧 20px（旧实现会相交 366x70）。"""
        app = _app()
        app.position()
        # 悬浮窗：w=max(0, 361, 120)=361 → x=2560-361-10=2189
        self.assertEqual(app.root.geometries, ["+2189+10"])
        self.assertEqual(app._float_left, 2189)
        # 面板：2189 - 20 - 694 = 1475
        self.assertEqual(app.panel.geometries, ["+1475+60"])
        self.assertEqual(app._panel_auto_xy, (1475, 60))

    def test_no_overlap_invariant(self):
        """核心不变量：面板矩形与悬浮窗矩形**不相交**（含窄屏组合）。

        T-14：参数组必须包含已知失效组合（1366 屏 + display_width=960、
        1080p@150% 等价形态），否则等于没有回归保护。
        """
        combos = (
            (361, 694, 2560),      # 宽屏默认
            (960, 694, 2560),      # 宽屏 + 大窗
            (380, 500, 1920),      # 宽屏中等
            (160, 694, 1920),      # 宽屏小窗
            (960, 694, 1366),      # T-14：1366 屏 + 960 小窗（旧实现恒相交）
            (600, 694, 1280),      # 窄屏（收窄兜底）
            (361, 694, 1024),      # 小屏
        )
        for label_w, panel_w, screen_w in combos:
            with self.subTest(label_w=label_w, panel_w=panel_w, screen_w=screen_w):
                app = _app(label_reqw=label_w, panel_reqw=panel_w, screen_w=screen_w)
                app.position()
                px1, py1, px2, py2 = _panel_rect(app)
                # T-14/R8-D-02：第 4 个值（悬浮窗**底缘**）此前被解成 `_`，而判据里
                # 又引用了 `fy2`——F821 未定义名。本机 7 个组合里项 1 `px2 <= fx1`
                # 恒为真（面板右缘按定义停在悬浮窗左侧 `_PANEL_GAP` 处），`or` 短路
                # 使第 4 项一次都没求值，于是"矩形不相交"实际只判了水平轴。
                # 接上真实底缘后纵向判据才真正生效，负向对照见
                # `test_overlap_judgement_is_not_masked_by_shortcircuit`。
                fx1, fy1, fx2, fy2 = _float_rect(app)
                overlap = not (px2 <= fx1 or px1 >= fx2 or py2 <= fy1 or py1 >= fy2)
                self.assertFalse(
                    overlap,
                    "面板与悬浮窗相交（小窗画面被盖）：panel=%s float=%s"
                    % ((px1, py1, px2, py2), (fx1, fy1, fx2, fy2)))

    def test_overlap_judgement_is_not_masked_by_shortcircuit(self):
        """R8-D-02 负向对照：不相交判据必须真的能判出「相交」。

        构造必然相交的一对矩形，断言 `overlap` 为 True。若将来又有人把某个判据项
        写回未定义名/改成永远为真，短路会把相交判成不相交，本用例即变红——上面
        `test_no_overlap_invariant` 自身的矩形在本机几何下永远由项 1 短路命中，
        单靠它区分不了"求值"与"短路"。
        """
        panel = (0, 600, 500, 1000)
        float_ = (450, 620, 900, 900)        # 纵向必然相交（[620,900] ⊂ [600,1000]）
        px1, py1, px2, py2 = panel
        fx1, fy1, fx2, fy2 = float_
        overlap = not (px2 <= fx1 or px1 >= fx2 or py2 <= fy1 or py1 >= fy2)
        self.assertTrue(overlap, "不相交判据把必然相交的矩形判成了不相交")
        # 只改纵向一项（px2 <= fx1 仍为真 → 仍然短路），横向分离时不得报相交
        px1, py1, px2, py2 = (0, 600, 400, 1000)
        overlap = not (px2 <= fx1 or px1 >= fx2 or py2 <= fy1 or py1 >= fy2)
        self.assertFalse(overlap, "横向已分离却判成相交")

    def test_overlay_wider_than_photo_counts(self):
        """叠加行/占位文字比画面宽时，悬浮窗实际更宽，面板须按最宽者让位（ui-014 同源）。"""
        app = _app(label_reqw=361, overlay_reqw=420)
        app.position()
        self.assertEqual(app.root.geometries, ["+%d+10" % (2560 - 420 - 10)])

    def test_float_resize_moves_panel_again(self):
        """滑块放大悬浮窗后重新让位（否则面板又压上来）。"""
        app = _app()
        app.position()
        first = app.panel.geometries[-1]
        app.label._reqw = 960
        app.position()
        self.assertEqual(app.root.geometries[-1], "+1590+10")
        self.assertEqual(app.panel.geometries[-1], "+876+60")
        self.assertNotEqual(first, app.panel.geometries[-1])

    def test_panel_missing_no_crash(self):
        """构建顺序：_build_ui 里 _position_top_right 先于 _build_panel 跑，面板还不存在。"""
        app = _app()
        app.panel = None
        app.position()          # 不应抛
        self.assertEqual(app.root.geometries, ["+2189+10"])


class TestPanelNarrowScreen(unittest.TestCase):
    """T-14/G1/G2：窄屏与短屏的兜底（收窄面板 / 纵向钳制 / 下移）。"""

    def test_narrow_screen_shrinks_panel_no_overlap(self):
        """1366 屏 + 960 小窗：面板收窄到可用宽度（396-20=376）且不相交。"""
        app = _app(label_reqw=960, panel_reqw=694, screen_w=1366)
        app.position()
        spec = app.panel.geometries[-1]
        self.assertTrue(spec.startswith("376x"), "窄屏没有收窄面板：%s" % spec)
        self.assertEqual(app._panel_auto_xy[0], 0)
        px1, py1, px2, py2 = _panel_rect(app)
        self.assertLessEqual(px2 + 20, 396, "收窄后仍与小窗相交：%s" % spec)

    def test_no_negative_coords(self):
        """任何组合都不得产生负坐标（Tk 会原样保留越界位置）。"""
        for label_w, panel_w, screen_w in ((960, 694, 1366), (700, 900, 1024)):
            with self.subTest(screen_w=screen_w):
                app = _app(label_reqw=label_w, panel_reqw=panel_w, screen_w=screen_w)
                app.position()
                _, x, y = _parse_geometry(app.panel.geometries[-1])
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)

    def test_dropped_below_float_when_no_horizontal_room(self):
        """小窗几乎占满屏宽（可用宽度 < 下限）→ 纵向躲到小窗下方。"""
        app = _app(label_reqw=1000, panel_reqw=694, screen_w=1024,
                   screen_h=1400, float_h=203)
        app.position()
        _, x, y = _parse_geometry(app.panel.geometries[-1])
        self.assertEqual(x, 0)
        self.assertEqual(y, 10 + 203 + viewer.ViewerApp._PANEL_GAP)

    def test_panel_y_clamped_on_short_screen(self):
        """1080p 以下屏高 + 高面板：y 钳进屏内（G2；面板比屏高时只能贴顶，尽力而为）。"""
        app = _app(panel_reqh=1010, screen_h=800)
        app.position()
        _, x, y = _parse_geometry(app.panel.geometries[-1])
        self.assertEqual(x, 1475)
        self.assertEqual(y, viewer.ViewerApp._PANEL_MARGIN)

    def test_panel_y_raised_when_footer_would_overflow(self):
        """面板高 1050 + 屏高 1080：y 由 60 上抬到 1080-1050-10=20，footer 完整可见。"""
        app = _app(panel_reqh=1050, screen_h=1080)
        app.position()
        _, x, y = _parse_geometry(app.panel.geometries[-1])
        self.assertEqual(y, 20)


class TestPanelUserMove(unittest.TestCase):
    def test_user_moved_panel_not_touched(self):
        """用户手动拖过面板（位置与上次自动定位不符）→ 不再自动挪动。"""
        app = _app()
        app.position()
        app.panel._x, app.panel._y = 300, 400     # 用户拖动
        app.label._reqw = 960                     # 悬浮窗变宽触发让位
        app.position()
        self.assertTrue(app._panel_user_moved)
        self.assertEqual(len(app.panel.geometries), 1, "手动挪动后仍被自动挪回")

    def test_user_move_detected_only_when_visible(self):
        """面板隐藏期间位置不可信（withdraw 后 winfo_x 不可靠）→ 不误判为用户挪动。"""
        app = _app()
        app.panel._state = "withdrawn"
        app.panel._x, app.panel._y = 0, 0
        app.position()
        self.assertFalse(app._panel_user_moved)
        self.assertEqual(app.panel.geometries, ["+1475+60"], "隐藏面板也应跟随")

    def test_own_move_not_mistaken_for_user(self):
        """自动挪动本身不得被误判为用户挪动（否则第二次就停止跟随）。"""
        app = _app()
        app.position()
        app.position()
        self.assertFalse(app._panel_user_moved)
        # L5/ui-042：几何未变时跳过冗余 Tcl 下发（只发一次），但标志位判定照常
        self.assertEqual(app.panel.geometries, ["+1475+60"])

    def test_panel_show_resets_user_moved(self):
        """G3/ui-034：重新显示面板是复位入口——清掉手动标记并按当前小窗让位。"""
        app = _app()
        app.position()
        app.panel._x, app.panel._y = 300, 400     # 用户拖动
        app.label._reqw = 960
        app.position()
        self.assertTrue(app._panel_user_moved)
        app.panel.geometries = []

        def deiconify():
            app.panel._state = "normal"

        app.panel.deiconify = deiconify
        app.panel.lift = lambda: None
        app.panel.attributes = lambda *a, **k: None
        app.panel_topmost = True
        viewer.ViewerApp._panel_show.__get__(app)()
        self.assertFalse(app._panel_user_moved, "重新显示后仍不复位（用户无恢复手段）")
        self.assertEqual(app.panel.geometries[-1], "+876+60")

    def test_drag_updates_float_left_and_panel(self):
        """G4/ui-035：拖动悬浮窗后 `_float_left` 必须同步，面板按新左缘让位。"""
        app = _app()
        app.position()
        self.assertEqual(app._float_left, 2189)
        app.drag_offset = (10, 10)
        app._drag_start_root = (2189 + 10, 10 + 10)
        app.user_moved = False
        app.panel.geometries = []

        class _Ev:
            x_root = 1000 + 10
            y_root = 300 + 10

        viewer.ViewerApp._on_drag_move.__get__(app)(_Ev())
        self.assertEqual(app._float_left, 1000, "拖动后 _float_left 未同步（面板按失效左缘让位）")
        self.assertEqual(app.panel.geometries[-1], "+%d+60" % (1000 - 20 - 694))


class _FakeEvent:
    def __init__(self, widget, width, height):
        self.widget = widget
        self.width = width
        self.height = height


class TestPanelConfigureReflow(unittest.TestCase):
    def _configure(self, app, widget=None, width=694, height=1010):
        real = viewer.ViewerApp._on_panel_configure.__get__(app)
        real(_FakeEvent(widget if widget is not None else app.panel, width, height))

    def test_size_growth_repositions(self):
        """内容撑宽面板后按新宽度重新让位，保住 20px 间隙。"""
        app = _app()
        app._panel_last_size = None
        app.position()                     # 首次定位 1475
        app.panel._reqw = 710              # 树/预览填充后变宽
        self._configure(app, width=710)    # Tk Configure：尺寸变化
        self.assertEqual(app.panel.geometries[-1], "+%d+60" % (2189 - 20 - 710))

    def test_position_only_change_ignored(self):
        """只改位置（用户拖动/自动挪动）不得触发重排，避免互相触发成环。"""
        app = _app()
        app._panel_last_size = (694, 1010)
        app.position()
        before = len(app.panel.geometries)
        self._configure(app, width=694, height=1010)   # 同尺寸、仅位置变
        self.assertEqual(len(app.panel.geometries), before, "同尺寸 Configure 触发了重排")

    def test_user_moved_ignored(self):
        app = _app()
        app._panel_user_moved = True
        self._configure(app, width=900, height=1010)
        self.assertEqual(app.panel.geometries, [])

    def test_other_widget_ignored(self):
        app = _app()
        app._panel_last_size = None
        self._configure(app, widget=object(), width=900, height=1010)
        self.assertEqual(app.panel.geometries, [])


class TestBuildUsesFloatLeft(unittest.TestCase):
    def test_build_panel_defers_to_helper(self):
        """防回归：_build_panel 不得再出现"屏幕右缘 - 40"的旧定位公式。"""
        import inspect
        src = inspect.getsource(viewer.ViewerApp._build_panel)
        self.assertIn("_position_panel_left_of", src)
        self.assertNotIn("winfo_screenwidth() - panel.winfo_reqwidth() - 40", src)

    def test_top_right_repositions_panel(self):
        import inspect
        src = inspect.getsource(viewer.ViewerApp._position_top_right)
        self.assertIn("_position_panel_left_of", src)
        self.assertIn("_float_left", src)


if __name__ == "__main__":
    unittest.main()
