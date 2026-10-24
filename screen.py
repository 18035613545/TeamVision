# -*- coding: utf-8 -*-
"""屏幕采集与静止检测公共模块（host 共享端与 viewer 上传端共用）。

从 host.py 平移而来，保持函数体与语义不变；host.py 顶部 re-export 同名符号，
既有 `import host; host.still_gate_update` 用法与单元测试不受影响。
"""

import os
import sys
import time

import cv2
import mss
import numpy as np

import logger

log = logger.get_logger()

#: capture-010（2026-09-24）：dxcam 输出匹配的告警必须"每个原因只报一次"。
#: 旧实现把标记挂在 CaptureManager 实例上（`self._pick_warned`），而 `_rebuild()`
#: 每次都新建实例 → 注释写着"只报一次"、实际每次重建都重报一条，静止/独占全屏下
#: 约每 1.5 秒一条把 5MB 滚动日志冲刷干净。模块级集合跨实例生效（同 codec 侧口径）。
_PICK_WARNED: set[str] = set()
_PICK_WARNED_MAX = 32


def _warn_pick_once(key, msg, *args):
    """同一 key 只记一条 WARNING（模块级，跨 CaptureManager 实例）；之后降为 debug。"""
    if key in _PICK_WARNED:
        log.debug(msg, *args)
        return
    if len(_PICK_WARNED) >= _PICK_WARNED_MAX:
        _PICK_WARNED.clear()      # 防御性上限：key 含 monitor 序号，理论上不会无限增长
    _PICK_WARNED.add(key)
    log.warning(msg, *args)


#: B7：vendor 目录是否已接入 sys.path（只做一次）
_VENDOR_ON_PATH = False


def ensure_vendor_on_path():
    """把项目自带的 `vendor/` 目录接入 `sys.path`（B7，2026-09-11）。

    项目把 dxcam 0.3.0 与 comtypes 以 vendored 形式放在 `vendor/`，但全仓没有任何
    把 `vendor/` 加进搜索路径的代码——`fps-host.spec` 只在**打包**时给
    `Analysis(pathex=['vendor'])`。于是源码运行（README 的"从源码构建"、setup.bat、
    `python host.py`）时 `import dxcam` 必然 ModuleNotFoundError，
    `capture.backend`（默认 dxgi）走进 except → **静默降级成 mss(GDI)**：
      - README/FAQ 里"独占全屏黑屏 → 把 capture.backend 设为 dxgi"这条官方排障建议
        在源码运行时完全无效；
      - GPU 采集、低延迟全部拿不到，"源码能跑、打包行为不同"难以解释。

    这里在延迟 `import dxcam` 之前把 vendor 目录接到搜索路径（打包时 `_MEIPASS`
    已含 dxcam，且该目录通常不存在，故无副作用）。
    """
    global _VENDOR_ON_PATH
    if _VENDOR_ON_PATH:
        return
    _VENDOR_ON_PATH = True
    candidates = []
    try:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
    except Exception:
        pass
    try:
        import common
        candidates.append(os.path.join(common.exe_dir(), "vendor"))
    except Exception:
        pass
    for candidate in candidates:
        try:
            if os.path.isdir(candidate) and candidate not in sys.path:
                sys.path.insert(0, candidate)
                log.info("已将 vendor 目录加入模块搜索路径: %s", candidate)
                return
        except Exception:
            continue


#: capture-012（2026-10-10）：几何漂移探测的调用间隔（秒）。
#:
#: 漂移检测的判据不是"画面是否可疑"而是"几何是否变了"，探针本身很便宜，但不必
#: 每帧都跑：mss 一帧 1920x1080 约 16ms，而探针约 2.4μs，1 秒一次的间隔相当于
#: 把开销压到每帧 1.5e-5 量级，同时把"检测到漂移"的最大延迟限定在 1 秒内。
GEOMETRY_PROBE_INTERVAL_S = 1.0

#: 静止检测抽稀步长（2K 下约 183x103 采样点，开销 <1ms）
STILL_STEP = 14


def downsample_frame(bgr, step=STILL_STEP):
    """把 BGR 帧抽稀为小参考图（拷贝，避免长期持有全帧内存）。"""
    if bgr is None:
        return None
    return bgr[::step, ::step].copy()


def motion_changed(ref, cur, point_thr=10, ratio_thr=0.005):
    """判定两帧抽稀图之间是否有“有效变化”，返回 (changed, 最新参考小图)。

    变点：逐通道绝对差 > point_thr 的「通道元素」计数；该计数占全部通道元素
    （H×W×3）的比例 > ratio_thr 判定有变化。第 87 条：实际按**通道元素**而非像素
    计数——单通道变化的像素只贡献 1/3，故 ratio_thr 比「按像素」语义严格约 3 倍
    （旧 docstring 误称“三通道平均绝对差”，与 count_nonzero(diff>thr)/diff.size 不符，已更正）。
    参考图仅在判定有变化（或形状不匹配）时更新：缓慢渐变会因累计帧差自动触发，
    无需定期刷新。异常按“有变化”处理（错误取向=宁可多发一帧，不可画面冻结）。
    """
    if ref is None or cur is None or ref.shape != cur.shape:
        return True, cur
    diff = cv2.absdiff(cur, ref)
    pts = int(np.count_nonzero(diff > point_thr))
    total = max(1, diff.size)
    if pts / total > ratio_thr:
        return True, cur
    return False, ref


def still_gate_update(in_still, quiet_since, changed, now, quiet_s):
    """静止闸门状态机：连续无变化满 quiet_s（秒）判定静止；任何变化帧立即复位并恢复发送。

    返回 (in_still, quiet_since)。quiet_since = 最近一次变化帧之后的连续无变化起始
    时刻（None = 上一帧有变化）。仅“连续”无变化累计：打字/低频更新等间歇内容不会
    因零星停顿跨帧累计误入静止；时刻需与调用方同一单调时钟（time.perf_counter）。
    第 87 条：本参数单位是**秒**（与 perf_counter 同单位）；旧名 quiet_ms 名不副实，
    若按毫秒传值闸门会灵敏 1000 倍，故更名 quiet_s 消除歧义。
    """
    if changed:
        return False, None
    if quiet_since is None:
        quiet_since = now
    elif not in_still and now - quiet_since >= quiet_s:
        in_still = True
    return in_still, quiet_since


def calc_output_size(src_w, src_h, scale, target_width=0):
    """计算实际编码输出尺寸：先按 scale 缩放，再受 target_width 上限压制。

    target_width<=0 不压制；宽高向下对齐到偶数（H.264/HEVC 要求），且恒 >= 2，
    避免 target_width=1 等极小值产生宽度 0 导致 cv2.resize 抛异常（第 88 条）。"""
    w = max(2, int(round(src_w * scale)))
    h = max(2, int(round(src_h * scale)))
    if target_width and target_width > 0 and w > target_width:
        w = max(2, int(target_width))
        h = max(2, int(round(src_h * w / src_w)))
    w -= w % 2
    h -= h % 2
    return max(2, w), max(2, h)


def encode_bgr(bgr, scale, quality, target_width=0):
    """把一帧 BGR 图像按缩放比例编码为 JPEG 字节，返回 (jpeg_bytes, encode_ms)。

    参数由调用方传入动态值（自适应可能修改 scale/quality）；
    target_width>0 时输出宽度受其上限压制（分辨率档位）；
    encode_ms 为本次缩放+编码耗时（毫秒，用 time.perf_counter 测量）。
    """
    start = time.perf_counter()
    width, height = calc_output_size(
        bgr.shape[1], bgr.shape[0], scale, target_width)
    if (width, height) != (bgr.shape[1], bgr.shape[0]):
        bgr = cv2.resize(bgr, (width, height), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    encode_ms = (time.perf_counter() - start) * 1000.0
    return buf.tobytes(), encode_ms


#: capture-012：几何探针的模块级缓存与去重标记。
#: 缓存键是屏幕列表的元组形态（hashable），探针本身无状态，多实例共用是安全的。
_PROBE_STATE: dict = {"cached": None}


def probe_geometry(force=False, interval_s=GEOMETRY_PROBE_INTERVAL_S):
    """枚举当前显示器几何，返回 mss 语义的 monitors 列表；不可用时返回 None。

    capture-012（2026-10-10）：mss 的 `monitors` 是**每实例只算一次**的缓存
    （`mss/base.py:346-350`），上游明确把它当缓存用且从未提供刷新入口
    （https://python-mss.readthedocs.io/v10.2.0/api.html）。显示设置变更后旧矩形
    继续被传给 `grab()`，而 GDI 的 `BitBlt` 在源矩形完全离屏时**返回成功却
    根本不写目标 DIB**（`mss/windows/gdi.py:380`）——既有判据因此全部落空：
    不抛异常（不触发 pipeline.py:387 的异常重建）、不返回 None（不触发空帧重建）、
    后端一直 `working()`。实测下共享端会安静地重复发送分辨率变更前的最后一帧。

    本探针绕开 mss 的缓存直接问 Win32，构造口径与 `mss.windows.gdi.MSSImplGdi.
    monitors()` 完全一致（索引 0 = 虚拟桌面并集，取自 GetSystemMetrics 76/77/78/79；
    索引 1..N = EnumDisplayMonitors 逐块枚举），因此返回值可与配置里的显示器
    序号一一对应，不引入新的语义。

    非 Windows 平台或 user32 不可用时返回 None，调用方沿用 mss 的路径，不新增
    失败模式。
    """
    now = time.monotonic()
    cached = _PROBE_STATE.get("cached")
    if (not force and cached is not None
            and now - cached[0] < interval_s):
        return cached[1]
    mons = _probe_geometry_uncached()
    if mons is None:
        # 探测失败不做缓存：让下次调用立刻重试，而不是把失败钉住 interval_s
        return None
    _PROBE_STATE["cached"] = (now, mons)
    return mons


def _probe_geometry_uncached():
    """真正调用 Win32 枚举显示器；任何异常都归为"几何不可用"。"""
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetSystemMetrics.restype = ctypes.c_int

        gsm = user32.GetSystemMetrics
        virtual = {"left": int(gsm(76)),    # SM_XVIRTUALSCREEN
                   "top": int(gsm(77)),     # SM_YVIRTUALSCREEN
                   "width": int(gsm(78)),   # SM_CXVIRTUALSCREEN
                   "height": int(gsm(79))}  # SM_CYVIRTUALSCREEN
        monitors = [virtual]
        found = []

        proc_type = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wt.HMONITOR, wt.HDC,
            ctypes.POINTER(wt.RECT), ctypes.c_void_p)

        @proc_type
        def _cb(_hmon, _hdc, rect, _data):
            r = rect.contents
            found.append({"left": int(r.left), "top": int(r.top),
                          "width": int(r.right) - int(r.left),
                          "height": int(r.bottom) - int(r.top)})
            return True

        if not user32.EnumDisplayMonitors(0, None, _cb, 0):
            return None
        if not found:
            return None
        monitors.extend(found)
        return monitors
    except Exception as e:
        log.debug("几何探针不可用（%s），沿用 mss 的显示器几何", e)
        return None


def geometry_changed(cached, current):
    """比对两份 mss 语义 monitors 列表是否等价（只看位置与尺寸）。

    capture-012：只比 `left/top/width/height`，忽略 mss 10.2 新增的 `is_primary`/
    `name`/`unique_id` 等元数据——那些字段不参与采集，且本探针不枚举设备名。
    任一侧不可用（None/空）时返回 False，避免把“探测失败”当成“几何变了”而触发
    无谓重建。

    长度不等直接判 True：显示器被插拔时 mss 的索引表长度必然变化，这是真实的
    拓扑变化，不是损坏。逐条比对只处理长度相等的情形。
    """
    if not cached or not current:
        return False
    if len(cached) != len(current):
        return True
    keys = ("left", "top", "width", "height")
    for a, b in zip(cached, current, strict=True):
        try:
            if any(int(a[k]) != int(b[k]) for k in keys):
                return True
        except Exception:
            # 单条记录不可解析时交给调用方走“几何不可用”路径，不误判为漂移
            return False
    return False


def reset_geometry_probe():
    """清空几何探针缓存（测试与显式强制刷新用）。"""
    _PROBE_STATE["cached"] = None


def _geometry_summary(monitors):
    """把几何列表压成一句人话，供一次性告警使用。"""
    try:
        parts = ["全部屏幕 %dx%d@(%d,%d)" % (m["width"], m["height"],
                                            m["left"], m["top"])
                 for m in monitors[:1]]
        parts += ["#%d %dx%d@(%d,%d)" % (i, m["width"], m["height"],
                                         m["left"], m["top"])
                  for i, m in enumerate(monitors[1:], 1)]
        return "，".join(parts)
    except Exception:
        return "几何不可用"


class CaptureManager:
    """采集后端封装：mss（GDI）与 dxgi（dxcam）双后端，支持动态切换显示器/区域/后端。

    dxgi 不可用（缺依赖/无 DXGI 环境/初始化异常）时自动回退 mss 并记录日志。
    """

    def __init__(self, backend, monitor, region):
        self.backend = backend
        self.monitor = monitor
        self.region = region
        self._mss = None
        self._camera = None
        self._effective = backend  # 回退后的实际后端
        self._errors = 0           # 连续采集异常次数（成功一帧即清零，capture-002）
        self._geometry = None      # 上次探测到的几何（capture-012，用于漂移比对）
        self._open()

    def _open(self):
        # capture-006（2026-09-20）：vendor/ 接入 sys.path 必须**早于**任何 dxcam 导入。
        # 旧实现把 ensure_vendor_on_path() 放在下面的 dxcam.create 之前（182 行），
        # 而 _pick_output_idx()（174 行）内部就有 `import dxcam`——源码运行环境下
        # dxcam 只存在于 vendor/，那次导入抛 ModuleNotFoundError 被 314 行的
        # `except Exception → log.debug` 静默吞掉，capture-004 的"按几何匹配 DXGI 输出"
        # 在第一个采集会话内等于不存在，直接退回 `(0, monitor-1)` 的旧口径；
        # 直到发生一次自愈重建（再次 _open，此时 sys.path 已被改过）才生效——
        # 同一份配置在故障恢复前后采到不同显示器。打包形态不受影响（dxcam 在 _MEIPASS 可直接导入）。
        ensure_vendor_on_path()   # B7：源码运行时 vendor/ 需显式接入 sys.path
        # capture-003/004（2026-09-18）：mss 句柄在两种后端下都先打开——dxgi 的区域
        # 换算（虚拟桌面绝对坐标 → 显示器内相对坐标）与"全部屏幕/几何不匹配"回退
        # 都需要 mss 的显示器几何；mss 初始化失败只是失去回退能力，不影响 dxgi。
        self._mss = None
        try:
            self._mss = mss.MSS()
        except Exception as e:
            log.error("mss 采集初始化失败: %s", e)
            self._mss = None
        if self.backend == "dxgi":
            out = self._pick_output_idx()
            if out is None:
                self._effective = "mss"
                log.warning("dxgi 无法映射显示器 #%d（全部屏幕），改用 mss 采集",
                            self.monitor)
                return
            device_idx, output_idx = out
            try:
                import dxcam  # 延迟导入：可选依赖，未安装时回退 mss
                # 第 100 条：dxcam 的 output_color 默认是 "RGB"，其处理器做的是
                # COLOR_BGRA2RGB，即 grab() 返回 RGB 数组；而本程序全链路（JPEG
                # imencode、av.VideoFrame(format="bgr24")、观看端 BGR2RGB）都按 BGR
                # 处理，用默认值会让整路画面红蓝互换。必须显式要 BGR。
                used_bgr = True
                # capture-004：device_idx 仅在非 0 时显式传入（保持默认设备的调用形态不变）
                extra = {"device_idx": device_idx} if device_idx else {}
                try:
                    self._camera = dxcam.create(
                        output_idx=output_idx, output_color="BGR", **extra)
                except TypeError as e:
                    # C16（2026-09-11）：只把"未知关键字参数 output_color"当成版本不支持。
                    # dxcam.create 内部是一整条 DXGI/comtypes/winrt 第三方栈，任何位置抛出
                    # 的 TypeError 原来都会被误判为"不支持 output_color"，然后去掉参数重试
                    # 成功——可重试成功意味着拿到的是默认 RGB 帧，而全链路按 BGR 处理，
                    # 结果是红蓝互换且只留一条容易被忽略的 WARNING。
                    if "output_color" not in str(e):
                        raise
                    log.warning("dxcam 不支持 output_color 参数（%s）；已回退为 RGB 帧，"
                                "画面红蓝可能互换，请升级 dxcam", e)
                    self._camera = dxcam.create(output_idx=output_idx, **extra)
                    used_bgr = False
                self._effective = "dxgi"
                # C16：日志必须与实际拿到的颜色格式一致，不再无条件宣称 BGR
                if used_bgr:
                    log.info("采集后端: dxgi（显示器 #%d，BGR）", self.monitor)
                else:
                    log.info("采集后端: dxgi（显示器 #%d，RGB 未交换，红蓝可能互换）",
                             self.monitor)
                return
            except Exception as e:
                log.warning("dxgi 后端不可用，回退 mss：%s", e)
                self._camera = None
        self._effective = "mss" if self._mss is not None else self._effective
        if self.backend == "dxgi" and self._mss is not None:
            log.info("已回退到 mss 采集后端")

    def set_params(self, backend, monitor, region):
        """参数变更时重建后端；无变更时保持现有实例（避免频繁重建）。"""
        if (backend == self.backend and monitor == self.monitor and region == self.region):
            return
        self.backend, self.monitor, self.region = backend, monitor, region
        self.close()
        self._open()

    @property
    def working(self):
        """是否至少有一个可用采集后端；dxgi 与 mss 均失败时为 False（第 5 条）。"""
        return self._camera is not None or self._mss is not None

    @property
    def consecutive_errors(self):
        """连续采集异常次数（成功一帧即清零）。

        capture-002（2026-09-18）：供 CaptureSource 判活——句柄存在但每帧抛错时
        `working` 恒 True，自愈判据必须能看到这个计数才能触发重建。
        """
        return self._errors

    def _dxgi_region(self):
        """把配置区域换算成 dxcam 的"显示器内相对坐标"（capture-003，2026-09-18）。

        mss 的 region 是**虚拟桌面绝对坐标**，dxcam 的 region 是**所选输出内**的
        相对坐标：副屏上的区域直接透传给 dxcam 会越界。
        返回 (region 或 None, 是否越界)：越界（区域跨出所选显示器，或所选显示器的
        几何本身取不到）时交由调用方回退 mss。

        capture-011（2026-10-10）：旧实现对"显示器几何取不到"这一支既不换算也不判
        越界，把虚拟桌面绝对坐标原样当成输出内相对坐标递下去，同时报
        `out_of_range=False` 阻止回退。vendored dxcam 0.3.0 的 `_validate_region`
        （vendor/dxcam/dxcam.py:782-787）对这种坐标抛 `ValueError`，而不是本
        docstring 旧版描述的"返回 None"（那是更早版本 dxcam 的行为）。异常经
        `grab` 上抛 → `CaptureSource._errors` 累加 → 连续 5 次触发 `_rebuild` →
        `_open()` 重建 mss/dxcam；几何若依旧取不到则整轮重来（每轮 `dxcam.create`
        内含一次 `time.sleep(0.1)`，vendor/dxcam/__init__.py:163），形成重建循环。

        几何取不到时唯一安全的动作是回退 mss：换算的前提不成立，"照常递下去"的
        任何猜测都不可能对。若 mss 同时不可用，`_grab_mss()` 返回 None，由
        `CaptureSource` 的空帧判据接管（不会退化成上面的重建循环）。
        """
        mon = self._monitor_rect(self.monitor)
        left = int(self.region["left"])
        top = int(self.region["top"])
        w = int(self.region["width"])
        h = int(self.region["height"])
        if mon is None:
            _warn_pick_once("region-rect-missing-%d" % self.monitor,
                            "显示器 #%d 的矩形不可用（mss 初始化失败，或配置序号超出"
                            "枚举范围），区域采集改用 mss 采集", self.monitor)
            return None, True
        ml, mt, mw, mh = mon
        if (left < ml or top < mt or left + w > ml + mw or top + h > mt + mh):
            return None, True
        x0 = left - ml
        y0 = top - mt
        return (x0, y0, x0 + w, y0 + h), False

    def _refresh_geometry(self, force=False):
        """探测当前几何；相对上次结果发生变化时作废 mss 的显示器缓存。

        capture-012（2026-10-10）：显示设置变更后 mss 的 `monitors` 不会刷新，
        旧矩形会被继续传给 `grab()`。这里在检测到漂移时把 mss 的实例级缓存置空
        （`base.py:347` 的 `if self._monitors is None` 随即重新枚举），使下一帧
        拿到新矩形。

        只作废几何缓存而不重建整个 mss 实例：屏幕 DC（`gdi.py:207` 的
        `_srcdc = GetWindowDC(0)`）在模式变更后依然有效，`BitBlt` 只是不写入而非
        失效；而一次 `mss.MSS()` 构造在本机要 22.5–36.7ms（≈1.4–2.2 帧），周期
        重建会造成静止画面下的周期性卡顿。绑定到"几何确实变了"这个条件后，
        这笔开销只在真正需要时支付一次。

        返回是否检测到几何漂移：**True = 本帧检出了漂移**（两个出口同义），False =
        没漂移、或探测不可用。它**不是**"需要重建采集器"的信号——曾经有代码把它当
        重建信号用（R8-capture-02，2026-10-10 订正），但清空 mss 缓存失败时清空的
        其实是 `probe_geometry` 的模块级缓存（独立于 mss），而重新枚举的开关本来
        就在 mss 自己的 `_monitors`；重建 mss 实例对那条 except 的成因（上游改了
        私有字段名/结构）**无效**，所以这里既没有消费者、也不该有。
        """
        # `__new__` 构造的实例没有 __init__ 跑过（既有单测大量这么用），
        # getattr 兜底避免 AttributeError 打穿采集路径
        prev = getattr(self, "_geometry", None)
        current = probe_geometry(force=force)
        if current is None:
            return False
        if not geometry_changed(prev, current):
            self._geometry = current
            return False
        self._geometry = current
        # key 必须含 monitor 序号：同进程内可能有多路采集（host/share 各自持有
        # CaptureManager），只按 len(prev) 去重会让第二路的告警被第一路压制成
        # debug，用户只看到一条"屏幕变了"却不知道是哪一路在跟进。与同文件
        # `region-rect-missing-%d` 的口径保持一致。
        _warn_pick_once("geometry-drift-%d-%s" % (self.monitor, prev and len(prev)),
                        "显示器 #%d 的几何已变化（%s），采集区域自动跟进",
                        self.monitor, _geometry_summary(current))
        try:
            if getattr(self, "_mss", None) is not None:
                # mss 的 monitors 是每实例只算一次的缓存，置空后下次访问重新枚举
                getattr(self, "_mss", None)._monitors = None
        except Exception as e:
            # 私有字段不可用（上游改名/结构变化）：唯一有效的处置是**不再依赖它的
            # 缓存**——几何一律走 probe_geometry，而 `_monitor_rect` 本来就是探针
            # 优先（探针也不可用时 mss 陈旧缓存是既有的安全回退）。旧文案写"改用
            # 重建采集器"，但本模块没有那个消费者，且重建出的 mss 实例同样没有该
            # 字段、对这条成因无效（R8-capture-02）。只记 debug，不改行为。
            log.debug("作废 mss 显示器缓存失败（%s），几何改由探针提供", e)
        return True

    def _fit_region_to_desktop(self, region):
        """把采集区域夹回当前虚拟桌面；探针不可用时原样返回（不新增失败模式）。

        capture-014（2026-10-10）：区域矩形的取值来自用户配置，但它**在虚拟桌面里
        是否还有效**完全由当前几何决定。分辨率改小后旧区域会越出桌面，而 `BitBlt`
        对离屏源矩形是"成功且不写入"（`mss/windows/gdi.py:380`）——本机实测同尺寸
        离屏抓取返回上一次抓帧的逐字节副本，部分越界时越屏部分写入全零。

        三种处置：
          - 完全在桌面内 → 原样返回，不产生任何额外开销（探针是 1 秒节流的缓存读）；
          - 部分越界 → 夹到可见交集继续抓，画面缩小但**是真实内容**，不再冻结；
          - 完全在桌面外 → 已无任何可采内容，退回整屏而不是丢帧——丢帧会被 host 侧
            `none_rebuild_requires_dead=True` 吞掉，画面永久黑且不自愈。
        """
        probed = probe_geometry()
        if not probed:
            return region
        try:
            virtual = probed[0]
            vl, vt = int(virtual["left"]), int(virtual["top"])
            vr, vb = vl + int(virtual["width"]), vt + int(virtual["height"])
            rl, rt = int(region["left"]), int(region["top"])
            rr, rb = rl + int(region["width"]), rt + int(region["height"])
        except Exception as e:
            log.debug("采集区域与桌面对齐失败（%s），按配置矩形抓取", e)
            return region
        nl, nt = max(rl, vl), max(rt, vt)
        nr, nb = min(rr, vr), min(rb, vb)
        if (nl, nt, nr, nb) == (rl, rt, rr, rb):
            return region                      # 区域仍在桌面内，行为不变
        if nr > nl and nb > nt:
            _warn_pick_once("region-clamped-%d" % self.monitor,
                            "采集区域 (%d,%d %dx%d) 超出现有桌面 %dx%d，已夹到 %dx%d；"
                            "分辨率或显示器布局可能已变更",
                            rl, rt, rr - rl, rb - rt, vr - vl, vb - vt, nr - nl, nb - nt)
            return {"left": nl, "top": nt, "width": nr - nl, "height": nb - nt}
        _warn_pick_once("region-offscreen-%d" % self.monitor,
                        "采集区域 (%d,%d %dx%d) 完全在现有桌面 %dx%d 之外，"
                        "已退回整屏采集；请重新框选采集区域",
                        rl, rt, rr - rl, rb - rt, vr - vl, vb - vt)
        return {"left": vl, "top": vt,
                "width": int(virtual["width"]), "height": int(virtual["height"])}

    def _monitor_rect(self, idx, force=False, stale_ok=False):
        """mss 语义的显示器矩形 (left, top, width, height)；不可用时 None。

        capture-012：几何优先取自 `probe_geometry`（会检测漂移），探针不可用时
        退回 mss 的 `monitors`——保持既有语义与既有降级路径不变。

        `stale_ok=True` 时**只用探针**，探针不可用直接返回 None，不回退 mss 的
        陈旧缓存。调用方据此区分两种"拿不到矩形"：

        - 探针可用但 idx 越界 → 配置点的显示器**已经不存在**（被拔除）。此时退回
          mss 缓存等于把刚刚发现的拓扑变化又捡回来，继续抓一块离屏的显示器 ——
          `BitBlt` 会成功且不写入，画面永久冻结。这正是 capture-012 要消灭的形态。
        - 探针不可用 → 只是"这次没测到"，mss 缓存仍是当前有效几何，退回是安全的。

        默认 `stale_ok=False` 保持既有行为：仅在**探针不可用**时才回退 mss 缓存。
        `_dxgi_region` / `_pick_output_idx` 只需要知道"有没有矩形"，两种"拿不到"
        （探针坏了 / 配置的屏不存在）对它们是同一件事——都拿不到可用矩形。
        真正需要区分的是 `_grab_mss`：那里"屏没了"必须放弃本帧，"没测到"才退回。
        """
        self._refresh_geometry(force=force)
        mons = probe_geometry()
        if not mons and not stale_ok:
            try:
                mons = getattr(self._mss, "monitors", None)
            except Exception:
                mons = None
        if not mons or idx < 0 or idx >= len(mons):
            return None
        try:
            m = mons[idx]
            return int(m["left"]), int(m["top"]), int(m["width"]), int(m["height"])
        except Exception:
            return None

    def _pick_output_idx(self):
        """选择 dxcam 的 output_idx（capture-004，2026-09-18）。

        旧实现直接把配置里的显示器序号当 DXGI 输出序号：配置 0（mss 语义的"全部
        屏幕"）变成第一屏；混合显卡/多屏按序号枚举不一致时采错屏，越界则
        IndexError 被静默吞掉回退 mss。这里按**几何矩形**在 dxcam 输出列表里匹配
        配置的显示器；匹配不到或配置为"全部屏幕"时返回 None（调用方回退 mss）。

        capture-007（2026-09-24）："没匹配上"的路径旧实现全部落到末尾的"序号-1"回退且
        零日志（capture-006 的 WARNING 只在 except 分支）。现在按两类分开：
          - **几何枚举不可用**（mss 矩形取不到 / dxcam 未暴露 outputs / 枚举抛错）：
            dxcam 本身仍可用，保留"序号-1"旧回退，但**记一条一次性 WARNING**；
          - **枚举成功但没有与配置显示器几何一致的输出**：这是 capture-004 要根治的
            "采到错误显示器"，返回 None 交调用方回退 mss，同样记一条一次性 WARNING。
        告警标记在模块级（capture-010：旧实现挂在实例上，每次重建都会重报）。
        """
        if self.monitor <= 0:
            return None      # 0 = 全部屏幕（虚拟桌面）：单输出无法表达，回退 mss
        # capture-012：mss 句柄缺失直接判"几何枚举不可用"，不再依赖
        # `_monitor_rect()` 返回 None——几何探针独立于 mss 工作，mss 死掉时它
        # 照样能返回矩形，旧判据因此永远等不到触发，"mss 初始化失败"会从日志里
        # 彻底消失。矩形本身仍可能因序号越界而取不到（配置了不存在的屏），
        # 那属于"没有几何一致项"，由下面的 no-match 分支报。
        if getattr(self, "_mss", None) is None:
            _warn_pick_once("mss-handle-missing-%d" % self.monitor,
                            "显示器 #%d 的 mss 句柄缺失（初始化失败？），几何无基准，"
                            "按序号回退 dxgi", self.monitor)
            return (0, self.monitor - 1)
        rect = self._monitor_rect(self.monitor)
        if rect is None:
            _warn_pick_once("rect-missing-%d" % self.monitor,
                            "显示器 #%d 的矩形不可用（mss 初始化失败？），按序号回退 dxgi",
                            self.monitor)
            return (0, self.monitor - 1)
        try:
            import dxcam
            factory = getattr(dxcam, "_DXFactory__factory", None) or \
                getattr(dxcam, "__factory", None)
            outputs = getattr(factory, "outputs", None) if factory else None
            if not outputs:
                _warn_pick_once("no-outputs",
                                "dxcam 未暴露输出列表（内部结构变化？），按序号回退")
                return (0, self.monitor - 1)
            targets = []
            for didx, outs in enumerate(outputs):
                for oidx, output in enumerate(outs):
                    desc = getattr(output, "desc", None)
                    coords = getattr(desc, "DesktopCoordinates", None)
                    if coords is None:
                        continue
                    out_rect = (int(coords.left), int(coords.top),
                                int(coords.right - coords.left),
                                int(coords.bottom - coords.top))
                    if out_rect == rect:
                        targets.append((didx, oidx))
            if targets:
                if len(targets) > 1:
                    log.warning("多个 DXGI 输出与显示器 #%d 几何一致，取第一个",
                                self.monitor)
                return targets[0]
            _warn_pick_once("no-match-%d" % self.monitor,
                            "DXGI 输出中没有与显示器 #%d 几何一致的项（DPI 缩放/混合显卡"
                            "枚举顺序不一致？），改用 mss 后端采集", self.monitor)
            return None
        except Exception as e:
            # capture-006（2026-09-20）：旧口径是 log.debug——默认日志级别下等于
            # 静默，"按几何匹配显示器"整条路径失效却查不到线索。
            _warn_pick_once("enum-failed", "dxcam 输出枚举失败，按序号回退: %s", e)
            return (0, self.monitor - 1)

    def grab(self):
        """采集一帧，返回 BGR numpy 数组；失败或无新帧返回 None。

        capture-002（2026-09-18）：采集异常**上抛**，不再就地吞成 None——
        `CaptureSource.grab` 的"连续异常→重建采集器"自愈依赖异常计数，旧实现把
        所有异常吞掉后 `_errors` 恒 0，该恢复逻辑是死代码：RDP 断连/锁屏唤醒/
        GPU 重置/分辨率变化后对方永久黑屏，且每帧一条 WARNING（最高约 100 条/秒）
        刷爆 5MB 滚动日志、冲掉真实病因。
        """
        try:
            frame = self._grab_once()
        except Exception:
            self._errors += 1
            raise
        self._errors = 0
        return frame

    def _grab_once(self):
        if self._effective == "dxgi" and self._camera is not None:
            if self.region:
                # capture-003/capture-011：mss 语义的绝对坐标 → dxcam 需要的显示器内
                # 相对坐标；区域跨出所选显示器、或显示器几何取不到时本帧回退 mss
                reg, out_of_range = self._dxgi_region()
                if out_of_range:
                    return self._grab_mss()
                return self._camera.grab(region=reg)
            return self._camera.grab()  # 返回 BGR ndarray 或 None（无新帧）
        return self._grab_mss()

    def _grab_mss(self):
        """mss 抓帧（区域为虚拟桌面绝对坐标；也是 dxgi 区域越界时的回退路径）。

        capture-012（2026-10-10）：整屏分支原本直接读 `self._mss.monitors[idx]`，
        那是每实例只算一次的缓存；显示设置变更后旧矩形继续被传给 `grab()`，
        而 GDI 的 `BitBlt` 在源矩形完全离屏时返回成功却不写入目标 DIB
        （`mss/windows/gdi.py:380`）——于是既不抛异常也不返回 None，共享端安静地
        重复发送旧画面。这里改为先过一遍几何探针（`_monitor_rect` 内部已做漂移
        检测与 mss 缓存作废），探针不可用时按 mss 语义回落到 `self._mss.monitors`。

        区域分支原先也直接透传配置矩形，理由是"区域不是显示器枚举结果"。**这个理由
        是错的**：区域矩形的**取值**来自配置没错，但它在虚拟桌面里**是否还有效**完全
        取决于当前几何。用户把分辨率从 1920x1080 改到 1280x720，原来框住整屏的区域
        就有一部分跑到了桌面外——而 mss 对离屏源矩形同样是"成功且不写入"
        （`mss/windows/gdi.py:380`）。实测（`grab` 同尺寸离屏源）返回的是上一次抓帧
        的**逐字节副本**，部分越界时越屏部分是全零：同样不抛异常、不返回 None，画面
        冻结或出现黑边。判据与整屏分支同源，因此这里也过一遍探针把区域夹回桌面内。
        """
        if getattr(self, "_mss", None) is None:
            return None
        if self.region:
            mon = {"left": int(self.region["left"]), "top": int(self.region["top"]),
                   "width": int(self.region["width"]), "height": int(self.region["height"])}
            mon = self._fit_region_to_desktop(mon)
        else:
            # capture-012：探针可用时只信探针——它说"这块屏不存在了"就必须放弃，
            # 绝不能退回 mss 的陈旧缓存去抓一块已拔除的显示器：BitBlt 在离屏源上
            # 会"成功且不写入"，画面永久冻结，那正是本条要消灭的形态。探针不可用
            # 才退回（此时 mss 缓存仍是当前有效几何）。
            probed = probe_geometry()
            if probed:
                idx = self.monitor if self.monitor > 0 else 0
                # 必须在 `_monitor_rect` 之前取：它内部会跑 `_refresh_geometry`
                # 把 `_geometry` 刷成新值，之后再取就分不出"本来就越界"和
                # "刚被拔掉"了。
                prev = getattr(self, "_geometry", None)
                rect = self._monitor_rect(idx, stale_ok=True)
                if rect is None:
                    # 序号越界。要分清是"屏被拔了"还是"配置本来就写错了"——
                    # 两者都表现为取不到矩形，但后果相反：
                    #   - 上一次探测时这个序号**有效** → 屏是真被拔掉的。抓它会
                    #     让 BitBlt 在离屏源上"成功且不写入"，画面永久冻结。
                    #   - 上一次探测时就**已越界**（或首次探测，从未有效）→ 配置
                    #     错误，不是硬件变化。丢掉这一帧会黑屏且不会自愈
                    #     （host 侧 none_rebuild_requires_dead=True 会跳过空帧重建），
                    #     比降级出图糟糕得多。
                    was_valid = bool(prev) and idx < len(prev)
                    if was_valid:
                        _warn_pick_once("monitor-gone-%d" % self.monitor,
                                        "显示器 #%d 已不存在（被拔除或几何变更），"
                                        "本帧不出图", self.monitor)
                        return None
                    # 降级目标与告警里的块数必须同一口径：探针保证"并集 + ≥1 块"
                    # 至少两项（`_probe_geometry_uncached` 在 found 为空时返回 None），
                    # 这里的 else 只是防御。
                    n_monitors = max(len(probed) - 1, 1)
                    _warn_pick_once("monitor-out-of-range-%d" % self.monitor,
                                    "配置的显示器序号 #%d 超出当前范围（共 %d 块），"
                                    "已回退到第 1 块；请修正 host.capture.monitor",
                                    self.monitor, n_monitors)
                    mon = dict(probed[1] if len(probed) > 1 else probed[0])
                else:
                    mon = {"left": rect[0], "top": rect[1],
                           "width": rect[2], "height": rect[3]}
            else:
                idx = self.monitor if 0 <= self.monitor < len(getattr(self, "_mss", None).monitors) else 1
                mon = self._mss.monitors[idx]
        img = np.asarray(self._mss.grab(mon))  # BGRA
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    def close(self):
        if getattr(self, "_mss", None) is not None:
            try:
                self._mss.close()
            except Exception:
                pass
            self._mss = None
        if self._camera is not None:
            try:
                if hasattr(self._camera, "release"):
                    self._camera.release()  # 释放并移除 dxcam 实例缓存，允许下次按新参数重建
                else:
                    self._camera.stop()
            except Exception:
                pass
            self._camera = None
