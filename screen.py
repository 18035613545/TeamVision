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
        相对坐标：副屏上的区域直接透传给 dxcam 时越界 → 永久黑屏（dxcam 返回 None）。
        返回 (region 或 None, 是否越界)：越界（区域跨出所选显示器）时交由调用方回退 mss。
        """
        mon = self._monitor_rect(self.monitor)
        left = int(self.region["left"])
        top = int(self.region["top"])
        w = int(self.region["width"])
        h = int(self.region["height"])
        if mon is None:
            # 拿不到显示器几何：不做换算（保持旧行为，避免误截）
            return (left, top, left + w, top + h), False
        ml, mt, mw, mh = mon
        if (left < ml or top < mt or left + w > ml + mw or top + h > mt + mh):
            return None, True
        x0 = left - ml
        y0 = top - mt
        return (x0, y0, x0 + w, y0 + h), False

    def _monitor_rect(self, idx):
        """mss 语义的显示器矩形 (left, top, width, height)；不可用时 None。"""
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
        """
        if self.monitor <= 0:
            return None      # 0 = 全部屏幕（虚拟桌面）：单输出无法表达，回退 mss
        rect = self._monitor_rect(self.monitor)
        if rect is not None:
            try:
                import dxcam
                factory = getattr(dxcam, "_DXFactory__factory", None) or \
                    getattr(dxcam, "__factory", None)
                outputs = getattr(factory, "outputs", None) if factory else None
                if outputs:
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
            except Exception as e:
                # capture-006（2026-09-20）：旧口径是 log.debug——默认日志级别下等于
                # 静默，"按几何匹配显示器"整条路径失效却查不到线索。首次失败提为
                # WARNING（一次性的，避免每次重建都刷一条）。
                if not getattr(self, "_pick_warned", False):
                    self._pick_warned = True
                    log.warning("dxcam 输出枚举失败，按序号回退（capture-006，只报一次）: %s", e)
                else:
                    log.debug("dxcam 输出枚举失败，按序号回退: %s", e)
        # 几何匹配不可用（dxcam 内部结构变化等）：退回"序号-1"的旧口径，
        # 设备取默认 0（不改变历史行为）
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
                # capture-003：mss 语义的绝对坐标 → dxcam 需要的显示器内相对坐标；
                # 区域跨出所选显示器时本帧回退 mss（dxcam 会返回永久黑屏）
                reg, out_of_range = self._dxgi_region()
                if out_of_range:
                    return self._grab_mss()
                return self._camera.grab(region=reg)
            return self._camera.grab()  # 返回 BGR ndarray 或 None（无新帧）
        return self._grab_mss()

    def _grab_mss(self):
        """mss 抓帧（区域为虚拟桌面绝对坐标；也是 dxgi 区域越界时的回退路径）。"""
        if self._mss is None:
            return None
        if self.region:
            mon = {"left": int(self.region["left"]), "top": int(self.region["top"]),
                   "width": int(self.region["width"]), "height": int(self.region["height"])}
        else:
            idx = self.monitor if 0 <= self.monitor < len(self._mss.monitors) else 1
            mon = self._mss.monitors[idx]
        img = np.asarray(self._mss.grab(mon))  # BGRA
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    def close(self):
        if self._mss is not None:
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
