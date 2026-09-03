# -*- coding: utf-8 -*-
"""E2E：观看组 3 人互看（hub + viewerA + viewerB）GUI 实测（fps-screen env 运行）。

单桌面限制：hub/A/B 三进程捕获同一合成桌面，各进程画面天然同色，"谁在看谁"
像素级不可区分。因此以 日志强断言（共享成员上线/注销、观看源切换、自动回落
local）+ UI 行为断言（共享按钮配色切换、源列表行变化、悬浮窗逐帧跟踪轮换的
全屏信号色）为主；悬浮窗区域采样仅作为"该流仍在逐帧更新"的活性检查
（信号整屏纯色 => 反馈/自捕获区域收敛后仍为纯色，分类鲁棒）。

驱动方式：真实物理鼠标（SetCursorPos 物理像素 + mouse_event 按下/抬起）。
交互前置条件：
  * 全屏信号先隐藏（否则吞掉点击）；
  * 目标面板 SetWindowPos 置顶（两 viewer 面板同位 (989,60)，靠 z 序切换目标；
    桌面左侧 x<989 被 IDE 占用，右侧条带无遮挡，面板同位放在右侧即可）；
  交互后还原 NOTOPMOST，让悬浮窗/信号重新盖在上面。

悬浮窗（root=Tk 顶层，overrideredirect + topmost）每帧会被 _position_top_right
吸回右上角（除非 user_moved=True）。因此：A 悬浮窗留在右上角不动；B 悬浮窗在
显示一帧后先单击一次（触发 Tk 拖拽锁存 user_moved=True），再 SetWindowPos 挪到
左下 (60,620)，此后不再回吸，两悬浮窗采样区互不重叠。

用法：
    F:/conda/envs/fps-screen/python.exe tests/e2e_multiview.py
"""
import ctypes
import ctypes.wintypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from PIL import ImageChops, ImageGrab, ImageStat

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.json")
# 第 78 条：PY/PORT/坐标原先全硬编码，换机器或端口被占即失败。改为可被环境变量覆盖，
# 默认值保持本机原配置不变（仍可用 fps-screen python 直接跑）。注意：坐标编码的是本机
# 显示布局假设（无遮挡条带/不与采样点重叠），仅覆盖坐标不能让脚本在任意分辨率下正确运行
# ——它本质是单机手动 GUI 验收工具（驱动真实鼠标、非 unittest discover 收集），并非 CI 用例。
PY = os.environ.get("E2E_PYTHON", r"F:/conda/envs/fps-screen/python.exe")
HOST = PY  # 调用方须用 fps-screen python 运行本脚本
PORT = int(os.environ.get("E2E_PORT", "5792"))
APP_TITLE = "SakuraVision"

HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
SW_HIDE, SW_SHOW = 0, 5

PANEL_POS = tuple(int(v) for v in os.environ.get("E2E_PANEL_POS", "989,60").split(","))   # 两 viewer 面板共用此屏位（右侧无遮挡条带），交互前置顶
B_FLOAT_POS = tuple(int(v) for v in os.environ.get("E2E_B_FLOAT_POS", "60,620").split(","))  # B 悬浮窗停靠位（左下，置顶可见，不与面板/采样点重叠）

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32


# ---------------- win32 基础 ----------------

def logical_screen():
    return (u32.GetSystemMetrics(0), u32.GetSystemMetrics(1))


def physical_scale():
    pw, ph = ImageGrab.grab().size
    lw, lh = logical_screen()
    return pw / lw, ph / lh


def to_phys(box):
    """逻辑 (l,t,r,b) → 物理像素盒（乘以屏幕缩放）。"""
    sx, sy = physical_scale()
    l, t, r, b = box
    return (int(l * sx), int(t * sy), int(r * sx), int(b * sy))


def enum_windows(top_only=True):
    out = []

    def cb(hwnd, _):
        out.append(hwnd)
        return True

    u32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                       ctypes.c_void_p)(cb), 0)
    return out


def enum_children(hwnd):
    out = []

    def cb(child, _):
        out.append(child)
        return True

    u32.EnumChildWindows(hwnd, ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(cb), 0)
    return out


def win_pid(hwnd):
    pid = ctypes.c_ulong()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def win_class(hwnd):
    buf = ctypes.create_unicode_buffer(128)
    u32.GetClassNameW(hwnd, buf, 128)
    return buf.value


def win_title(hwnd):
    n = u32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    u32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def win_rect(hwnd):
    r = ctypes.wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def win_visible(hwnd):
    return bool(u32.IsWindowVisible(hwnd))


def top_windows_of(pid):
    return [h for h in enum_windows() if win_pid(h) == pid and win_visible(h)]


def set_win_pos(hwnd, x, y):
    u32.SetWindowPos(hwnd, 0, int(x), int(y), 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)


def raise_top(hwnd):
    """置顶（z 序顶部）。交互目标窗口在物理点击/像素采样前必须先置顶。"""
    u32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                     SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def unraise(hwnd):
    u32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                     SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def phys_click_at(lx, ly, double=False):
    """在逻辑屏幕坐标 (lx,ly) 处真实物理点击（SetCursorPos 用物理像素）。

    SendMessage 投递无法触发 ttk/Tk 回调，必须走真实输入；点击点所在窗口
    必须已置顶（命中测试取最上层窗口）。双击 = 两次 down/up（间隔 < 系统
    双击窗口即生成 <Double-1>）。
    """
    sx, sy = physical_scale()
    px, py = int(lx * sx), int(ly * sy)

    def one():
        u32.SetCursorPos(px, py)
        time.sleep(0.06)
        u32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.04)
        u32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.08)

    one()
    if double:
        time.sleep(0.07)
        one()


def show_win(hwnd, cmd):
    u32.ShowWindow(hwnd, cmd)


# ---------------- 子进程与日志 ----------------

class Proc:
    def __init__(self, name, args):
        self.name = name
        self.log_path = os.path.join(LOG_DIR, name + ".log")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        self.log = open(self.log_path, "wb")
        self.p = subprocess.Popen(
            args, cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT,
            env=env, creationflags=0x00000008)  # DETACHED_PROCESS 保留父进程可写句柄
        self.pid = self.p.pid

    def kill(self):
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.pid)],
                           capture_output=True, timeout=15)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass
        self.log.close()

    def text(self):
        try:
            with open(self.log_path, "rb") as f:
                return f.read().decode("utf-8", "replace")
        except Exception:
            return ""


PROCS = []


def launch(name, args):
    p = Proc(name, args)
    PROCS.append(p)
    return p


# ---------------- 截图与色域 ----------------

def grab(box_logical):
    return ImageGrab.grab(to_phys(box_logical))


def region_mean(box_logical):
    img = grab(box_logical).convert("RGB")
    st = ImageStat.Stat(img)
    return tuple(round(v, 1) for v in st.mean)


def classify(rgb):
    r, g, b = rgb
    if g - r > 14 and g - b > 14:
        return "green"
    if r - g > 14 and r - b > 14:
        return "red"
    if b - r > 14 and b - g > 14:
        return "blue"
    return "other"


def changed_px(img_a, img_b, thr=6):
    """两张同尺寸截图的变化像素数（>thr 的通道差计为变化）。"""
    a = img_a.convert("RGB")
    b = img_b.convert("RGB")
    diff = ImageChops.difference(a, b)
    hist = diff.histogram()
    n = 0
    for ch in range(3):
        base = ch * 256
        for i in range(thr + 1, 256):
            n += hist[base + i]
    return n // 3


# ---------------- 等待 ----------------

def wait_until(pred, timeout=10.0, desc="", poll=0.25):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = pred()
        if last:
            return last
        time.sleep(poll)
    raise AssertionError("等待超时(%.0fs): %s （最后采样: %r）" % (timeout, desc, last))


def wait_log(proc, pattern, timeout=10.0, desc=""):
    rx = re.compile(pattern)

    def chk():
        m = rx.search(proc.text())
        return m

    return wait_until(chk, timeout, desc or ("日志匹配 %s @ %s" % (pattern, proc.name)))


def wait_float_color(v, expected, timeout=15.0, desc=""):
    """轮询悬浮窗中心色直到分类为 expected；超时给出最后采样 RGB 与分类。"""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = v.float_center_rgb()
        if classify(last) == expected:
            return last
        time.sleep(0.25)
    raise AssertionError(
        "等待超时(%.0fs): %s 期望中心色 %s，最后采样 RGB=%r（分类 %s）"
        % (timeout, desc, expected, last, classify(last)))


# ---------------- 信号窗口（整屏纯色 + 移动白块，独立进程） ----------------

class SignalWin:
    def __init__(self, name):
        self.name = name
        self.proc = launch("sig_" + name,
                           [PY, os.path.join(ROOT, "tests", "e2e_signalwin.py"), name])
        self.hwnd = None

    def wait_window(self, timeout=15):
        def chk():
            for h in top_windows_of(self.proc.pid):
                if win_class(h) == "TkTopLevel":
                    return h
            return None

        self.hwnd = wait_until(chk, timeout, "信号窗口 %s" % self.name)
        return self.hwnd

    def show(self):
        show_win(self.hwnd, SW_SHOW)
        # 置顶（HWND_TOPMOST）：全屏信号盖过普通窗口，作为被捕获的"桌面画面"
        u32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def hide(self):
        show_win(self.hwnd, SW_HIDE)


SIGNALS = {}
VIEWERS = []


def raise_floats():
    """把各 viewer 悬浮窗置到信号窗之上：悬浮窗区域截图取其自身画面内容。"""
    for v in VIEWERS:
        if v.float is not None:
            u32.SetWindowPos(v.float, HWND_TOPMOST, 0, 0, 0, 0,
                             SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def sig(name):
    return SIGNALS[name]


def show_only(name):
    for k, s in SIGNALS.items():
        if k == name:
            s.show()
        else:
            s.hide()
    raise_floats()
    time.sleep(0.8)  # 等合成桌面完成切换


def hide_all():
    for s in SIGNALS.values():
        s.hide()
    time.sleep(0.6)


# ---------------- 应用窗口定位与交互 ----------------

class Viewer:
    def __init__(self, tag, proc):
        self.tag = tag
        self.proc = proc
        self.panel = None
        self.float = None
        self.ch_tree = None
        self.src_tree = None
        self.share_btn = None
        self._floats_raised = False

    def find_windows(self, timeout=20):
        def chk():
            found = {}
            for h in top_windows_of(self.proc.pid):
                if win_class(h) != "TkTopLevel":
                    continue
                t = win_title(h)
                if "FPS 画面控制台" in t:
                    found["panel"] = h
                else:
                    found.setdefault("float", h)
            if "panel" in found and "float" in found:
                return found
            return None

        w = wait_until(chk, timeout, "viewer %s 窗口（面板+悬浮窗）" % self.tag)
        self.panel = w["panel"]
        self.float = w["float"]
        if self not in VIEWERS:
            VIEWERS.append(self)
        set_win_pos(self.panel, *PANEL_POS)
        time.sleep(0.3)

    def activate_panel(self, wait=0.4):
        """把面板放回固定屏位并置顶：物理点击/面板像素采样前的必备步骤。"""
        set_win_pos(self.panel, *PANEL_POS)
        raise_top(self.panel)
        time.sleep(wait)

    def deactivate_panel(self):
        unraise(self.panel)

    def find_trees(self):
        """在面板内定位频道树/源树（TkChild 全宽帧，高差 ≈3 行 = 72px）。

        实测布局：频道树 ~199 高（7 行）、源树 ~127 高（4 行），另有预览卡等
        全宽帧高 126/168/91；用「高差 60..84 且垂直间隙最小」配对，排除预览卡。
        两树顶距面板顶 ~136 / ~427，行高 24、列头 ~31。
        """
        kids = enum_children(self.panel)
        cands = []
        for h in kids:
            if win_class(h) != "TkChild":
                continue
            l, t, r, b = win_rect(h)
            w, hh = r - l, b - t
            if w > 350 and 100 < hh < 240:
                cands.append((h, t, b, hh))
        cands.sort(key=lambda x: (x[1], x[3]))
        best = None
        for i in range(len(cands)):
            h1, t1, b1, hh1 = cands[i]
            for j in range(i + 1, len(cands)):
                h2, t2, b2, hh2 = cands[j]
                if t2 <= b1:
                    continue
                if not (60 <= hh1 - hh2 <= 84):
                    continue
                gap = t2 - b1
                if best is None or gap < best[0]:
                    best = (gap, h1, h2)
        if best is None:
            raise AssertionError(
                "viewer %s 面板内未定位到频道树/源树；TkChild 候选: %r" % (
                    self.tag, [(c[3], c[1]) for c in cands]))
        self.ch_tree = best[1]
        self.src_tree = best[2]

    def find_share_btn(self):
        """共享按钮 = 频道树与源树之间操作行最右侧的叶子 TkChild（实测 ~107x31）。"""
        _, _, _, tb = win_rect(self.ch_tree)
        _, st, _, _ = win_rect(self.src_tree)
        cands = []
        for h in enum_children(self.panel):
            if win_class(h) != "TkChild":
                continue
            l, t, r, b = win_rect(h)
            w = r - l
            if w <= 40 or w > 220:
                continue  # 排除容器帧（全宽）与窄装饰
            cy = (t + b) / 2.0
            if tb < cy < st:
                cands.append((h, l, t, r, b))
        if not cands:
            raise AssertionError(
                "viewer %s 未找到操作行按钮（ch_bottom=%d src_top=%d）" % (
                    self.tag, tb, st))
        cands.sort(key=lambda x: x[3])
        self.share_btn = cands[-1][0]

    def float_video_box(self):
        l, t, r, b = win_rect(self.float)
        return (l + 6, t + 6, r - 6, b - 40)  # 底部留出状态叠加条

    def float_center_rgb(self):
        l, t, r, b = self.float_video_box()
        cx, cy = (l + r) // 2, (t + b) // 2
        return region_mean((cx - 30, cy - 30, cx + 30, cy + 30))

    def _dblclick_tree_row(self, tree_hwnd, row):
        """物理双击树行：行高 24、列头 ~31（实测 7/4 行树高 199/127 反推一致）。"""
        l, t, r, b = win_rect(tree_hwnd)
        x = l + 120
        y = t + 31 + row * 24 + 12
        phys_click_at(x, y, double=True)
        time.sleep(0.5)

    def click_channel_row(self, row=0):
        self.activate_panel()
        try:
            self._dblclick_tree_row(self.ch_tree, row)
        finally:
            self.deactivate_panel()

    def click_src_row(self, row):
        self.activate_panel()
        try:
            self._dblclick_tree_row(self.src_tree, row)
        finally:
            self.deactivate_panel()

    def click_share(self):
        self.activate_panel()
        try:
            l, t, r, b = win_rect(self.share_btn)
            phys_click_at((l + r) // 2, (t + b) // 2)
            time.sleep(0.6)  # 等 UI 响应（按钮文本/配色/日志）
        finally:
            self.deactivate_panel()

    def share_btn_rgb(self):
        self.activate_panel()
        try:
            l, t, r, b = win_rect(self.share_btn)
            return region_mean((l + 6, t + 4, r - 6, b - 4))
        finally:
            self.deactivate_panel()

    def src_tree_img(self):
        self.activate_panel()
        try:
            l, t, r, b = win_rect(self.src_tree)
            return grab((l + 4, t + 4, r - 4, b - 4))
        finally:
            self.deactivate_panel()

    def park_float(self, x=B_FLOAT_POS[0], y=B_FLOAT_POS[1]):
        """把悬浮窗停靠在固定位置：先单击一次（触发 Tk 拖拽锁存 user_moved，
        阻止 _position_top_right 每帧吸回右上角），再 SetWindowPos 移动。"""
        raise_top(self.float)
        time.sleep(0.3)
        l, t, r, b = win_rect(self.float)
        phys_click_at((l + r) // 2, (t + b) // 2)
        time.sleep(0.3)
        set_win_pos(self.float, x, y)
        time.sleep(0.5)


# ---------------- 测试驱动 ----------------

def dump_failure(e):
    """失败时保存现场：全屏截图 + 各进程日志尾部 + 顶层窗口清单。"""
    try:
        ImageGrab.grab().save(os.path.join(LOG_DIR, "fail_screen.png"))
    except Exception:
        pass
    with open(os.path.join(LOG_DIR, "fail_windows.txt"), "w", encoding="utf-8") as f:
        f.write("%s\n\n" % e)
        for h in enum_windows():
            if win_visible(h):
                f.write("hwnd=%08x pid=%d cls=%s title=%s rect=%r\n" % (
                    h, win_pid(h), win_class(h), win_title(h), win_rect(h)))
    for p in PROCS:
        txt = p.text()
        with open(os.path.join(LOG_DIR, p.name + "_tail.log"), "w",
                  encoding="utf-8") as f:
            f.write(txt[-4000:])
    print("现场已保存到 %s" % LOG_DIR, flush=True)


def main():
    print("== E2E multiview: 备份并改写 config.json ==", flush=True)
    backup = CONFIG + ".e2e_bak"
    # 第 77 条：原 shutil.copy2(CONFIG, backup) 不在 try 内，全新克隆（config.json
    # 未跟踪、不存在）会直接抛 FileNotFoundError 崩在这里（_run 读 CONFIG 同样会崩）。
    # 改为先判存在并给出可操作提示，避免丑陋 traceback。
    if not os.path.exists(CONFIG):
        print("config.json 不存在（全新克隆？）。本测试需基于既有配置运行；"
              "请先启动一次 fps-host.exe / fps-viewer.exe 生成默认 config.json 后重试。",
              flush=True)
        sys.exit(2)
    shutil.copy2(CONFIG, backup)
    code = 1
    try:
        code = 0 if _run() else 1
    except AssertionError as e:
        print("E2E FAILED: %s" % e, flush=True)
        dump_failure(e)
    except Exception as e:
        print("E2E 异常: %r" % e, flush=True)
        import traceback
        traceback.print_exc()
        dump_failure(e)
    finally:
        for p in PROCS:
            try:
                p.kill()
            except Exception:
                pass
        restored = False
        try:
            if os.path.exists(backup):
                shutil.copy2(backup, CONFIG)
                restored = True
                print("config.json 已恢复原状", flush=True)
        except Exception as e:
            print("config.json 恢复失败: %s" % e, flush=True)
        # 第 77 条：仅在确认恢复成功后删除备份——备份含明文 frp token，不应长期留在仓库目录；
        # 恢复失败时保留备份，作为手动还原的唯一副本。（SIGKILL/断电无法在进程内兜底，
        # 但此时 gitignored 的 .e2e_bak 仍在磁盘上，可手动还原。）
        if restored:
            try:
                os.remove(backup)
                print("已删除临时备份 config.json.e2e_bak", flush=True)
            except Exception as e:
                print("临时备份删除失败（可手动删除 config.json.e2e_bak）: %s" % e, flush=True)
    sys.exit(code)


def _run():
    global LOG_DIR
    LOG_DIR = tempfile.mkdtemp(prefix="e2e_multiview_")
    print("日志目录: %s" % LOG_DIR, flush=True)

    with open(CONFIG, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["viewer"]["channels"] = [{"name": "E2E频道", "addr": "127.0.0.1:%d" % PORT}]
    cfg["viewer"]["panel_topmost"] = False
    cfg["viewer"]["click_through"] = False
    cfg["viewer"]["alpha"] = 1.0
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    results = []

    def step(desc):
        print("[STEP] %s" % desc, flush=True)

    def ok(desc):
        results.append(("PASS", desc))
        print("  PASS: %s" % desc, flush=True)

    # ---- 0. 启动信号窗口 ----
    step("启动三个全屏信号窗口（绿=hub 本地 / 红=A / 蓝=B）")
    for nm in ("green", "red", "blue"):
        SIGNALS[nm] = SignalWin(nm)
        SIGNALS[nm].wait_window()
    hide_all()

    # ---- 1. 启动 hub + 两 viewer，观看 local ----
    step("启动 host(--port %d) + viewerA + viewerB" % PORT)
    host = launch("host", [PY, os.path.join(ROOT, "host.py"), "--port", str(PORT),
                           "--console"])
    va = launch("viewerA", [PY, os.path.join(ROOT, "viewer.py")])
    vb = launch("viewerB", [PY, os.path.join(ROOT, "viewer.py")])
    wait_log(host, "服务已启动", 20, "host 启动")
    ok("host 服务已启动")
    A, B = Viewer("A", va), Viewer("B", vb)
    A.find_windows()
    B.find_windows()
    A.find_trees()
    B.find_trees()
    A.find_share_btn()
    B.find_share_btn()
    ok("A/B 面板同位置放，树与共享按钮已识别")

    show_only("green")
    wait_log(va, "连接成功", 20, "A 连上 hub")
    wait_log(vb, "连接成功", 20, "B 连上 hub")
    # 双击频道行：确保活动观看 local（幂等）
    A.click_channel_row(0)
    B.click_channel_row(0)
    # B 悬浮窗停靠左下，避免与 A（右上自动吸回）重叠采样
    B.park_float()
    show_only("green")
    wait_float_color(A, "green", 20, "A 悬浮窗显示 hub 本地画面(绿)")
    wait_float_color(B, "green", 20, "B 悬浮窗（左下）显示 hub 本地画面(绿)")
    ok("A/B 均观看 local：双悬浮窗画面为 hub 信号(绿)且逐帧更新")

    # ---- 2. A/B 各自开启共享 ----
    step("A/B 开启共享本机屏幕")
    hide_all()
    a0 = A.share_btn_rgb()
    b0 = B.share_btn_rgb()
    A.click_share()
    wait_log(va, "已开始共享本机屏幕", 10, "A 开始共享")
    wait_log(host, r"上线：peer:1", 10, "host 登记 peer:1")
    a1 = A.share_btn_rgb()
    assert_changed(a0, a1, "A 共享按钮配色切换（进入共享态）", results)
    ok("A 已共享：host 日志 共享成员上线 peer:1")
    B.click_share()
    wait_log(vb, "已开始共享本机屏幕", 10, "B 开始共享")
    wait_log(host, r"上线：peer:2", 10, "host 登记 peer:2")
    b1 = B.share_btn_rgb()
    assert_changed(b0, b1, "B 共享按钮配色切换（进入共享态）", results)
    ok("B 已共享：host 日志 共享成员上线 peer:2")
    time.sleep(1.0)  # 等 roster 广播驱动两端源列表重建，再按行号点源

    # ---- 3. B 切换到观看 A（红屏阶段） ----
    step("B 双击源列表[队友 A]行 → 观看 peer:1")
    hide_all()
    B.click_src_row(1)
    wait_log(vb, r"观看源切换为 peer:1", 10, "B 切到 peer:1")
    show_only("red")
    wait_float_color(B, "red", 15, "B 悬浮窗画面为红色（peer:1 即 A 画面语义）")
    wait_float_color(A, "red", 15, "A 悬浮窗画面为红色（本机捕获）")
    ok("B 观看源 = peer:1（A）；双悬浮窗画面活跃为红")

    # ---- 4. A 切换到观看 B（蓝屏阶段） ----
    step("A 双击源列表[队友 B]行 → 观看 peer:2")
    hide_all()
    A.click_src_row(2)
    wait_log(va, r"观看源切换为 peer:2", 10, "A 切到 peer:2")
    show_only("blue")
    wait_float_color(A, "blue", 15, "A 悬浮窗画面为蓝色（peer:2 即 B 画面语义）")
    wait_float_color(B, "blue", 15, "B 悬浮窗画面为蓝色")
    ok("A 观看源 = peer:2（B）；双悬浮窗画面活跃为蓝")

    # ---- 5. B 停止共享 → hub 注销 → A 自动回落 local ----
    step("B 停止共享（连接保留）：host 注销 peer:2，A 回落本地")
    hide_all()  # 截图源列表前先隐藏信号：面板需置顶可读
    a_src_before = A.src_tree_img()  # 3 行：本地/A/B
    B.click_share()
    wait_log(vb, "共享已关闭", 10, "B 关闭共享")
    wait_log(host, r"注销：peer:2", 10, "host 注销 peer:2")
    ok("host 日志出现注销 peer:2（B 连接保留）")
    wait_log(va, r"自动切回本地画面", 15, "A 自动回落 local")
    ok("A 日志：peer:2 已离线自动切回本地画面")
    a_src_after = A.src_tree_img()
    changed = changed_px(a_src_before, a_src_after)
    if changed < 30:
        raise AssertionError("A 源列表区域应因 B 行消失而变化，实际变化像素 %d" % changed)
    ok("A 画面源列表行已变化（B 行消失），UI 与 roster 同步")
    # B 仍看 A：A 保持共享；红阶段验证双悬浮窗仍活跃
    show_only("red")
    wait_float_color(B, "red", 15, "B 继续观看 A（红）不受 B 自身停共享影响")
    wait_float_color(A, "red", 15, "A 回落 local 后悬浮窗仍活跃（红）")

    # ---- 6. 5 分钟稳定性：每 15s 轮换信号色，双悬浮窗须逐帧跟踪 ----
    step("5 分钟稳定性：每 15s 轮换绿/红/蓝，双悬浮窗颜色须跟随（流活性）")
    colors = ["green", "red", "blue"]
    bad = []
    tick = 0
    end = time.time() + 300
    while time.time() < end:
        tick += 1
        col = colors[tick % len(colors)]
        show_only(col)
        try:
            wait_float_color(A, col, 8, "稳定期 A 跟随 %s" % col)
            wait_float_color(B, col, 8, "稳定期 B 跟随 %s" % col)
        except AssertionError as e:
            bad.append("tick%d %s" % (tick, e))
            break
        for p in (host, va, vb):
            t = p.text()
            for pat in ("Traceback", "连接断开", "重新连接", "服务端关闭"):
                if re.search(pat, t):
                    bad.append("tick%d %s 日志含 %s" % (tick, p.name, pat))
                    break
        print("  stability tick %d: %s 跟随 OK" % (tick, col), flush=True)
    if bad:
        raise AssertionError("稳定性巡检失败 %d 项，如: %s" % (len(bad), bad[:3]))
    ok("5 分钟稳定性：悬浮窗逐帧跟踪信号轮换，三进程日志无断开/异常")

    # ---- 收尾 ----
    hide_all()
    for s in SIGNALS.values():
        s.proc.kill()
    evidence = []
    for tag, pat in (("host", r"上线：peer:1"), ("host", r"上线：peer:2"),
                     ("host", r"注销：peer:2"),
                     ("A", r"已开始共享本机屏幕"), ("B", r"已开始共享本机屏幕"),
                     ("A", r"观看源切换为 peer:2"), ("B", r"观看源切换为 peer:1"),
                     ("A", r"自动切回本地画面")):
        p = {"host": host, "A": va, "B": vb}[tag]
        m = re.search(pat, p.text())
        evidence.append("%s:%s -> %s" % (tag, pat, bool(m)))
    print("证据行:", "；".join(evidence), flush=True)

    # 第 78 条：本脚本是「快速失败」设计——任一步骤不达标即 raise AssertionError，由 main()
    # 捕获并以退出码 1 表达失败；results 只会被 append "PASS"，从无 "FAIL"。故能执行到此处即
    # 代表全部步骤通过。原先的 fails 过滤 / RESULT:FAIL 分支 / return False 永不可达，是死代码，已移除。
    print("=" * 60, flush=True)
    print("RESULT: PASS（%d 项） 日志目录 %s" % (len(results), LOG_DIR), flush=True)
    return True


def assert_changed(c0, c1, desc, results):
    d = max(abs(a - b) for a, b in zip(c0, c1))
    if d < 15:
        raise AssertionError("%s：按钮中心色几乎不变 %r -> %r" % (desc, c0, c1))
    results.append(("PASS", desc))
    print("  PASS: %s（%r -> %r）" % (desc, c0, c1), flush=True)


if __name__ == "__main__":
    main()
