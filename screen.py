# -*- coding: utf-8 -*-
"""屏幕采集与静止检测公共模块（host 共享端与 viewer 上传端共用）。

从 host.py 平移而来，保持函数体与语义不变；host.py 顶部 re-export 同名符号，
既有 `import host; host.still_gate_update` 用法与单元测试不受影响。
"""

import time

import cv2
import numpy as np
import mss

import logger

log = logger.get_logger()


#: 静止检测抽稀步长（2K 下约 183x103 采样点，开销 <1ms）
STILL_STEP = 14


def downsample_frame(bgr, step=STILL_STEP):
    """把 BGR 帧抽稀为小参考图（拷贝，避免长期持有全帧内存）。"""
    if bgr is None:
        return None
    return bgr[::step, ::step].copy()


def motion_changed(ref, cur, point_thr=10, ratio_thr=0.005):
    """判定两帧抽稀图之间是否有“有效变化”，返回 (changed, 最新参考小图)。

    变点：采样点三通道平均绝对差 > point_thr；变点占比 > ratio_thr 判定有变化。
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


def still_gate_update(in_still, quiet_since, changed, now, quiet_ms):
    """静止闸门状态机：连续无变化满 quiet_ms 判定静止；任何变化帧立即复位并恢复发送。

    返回 (in_still, quiet_since)。quiet_since = 最近一次变化帧之后的连续无变化起始
    时刻（None = 上一帧有变化）。仅“连续”无变化累计：打字/低频更新等间歇内容不会
    因零星停顿跨帧累计误入静止；时刻需与调用方同一单调时钟（time.perf_counter）。
    """
    if changed:
        return False, None
    if quiet_since is None:
        quiet_since = now
    elif not in_still and now - quiet_since >= quiet_ms:
        in_still = True
    return in_still, quiet_since


def calc_output_size(src_w, src_h, scale, target_width=0):
    """计算实际编码输出尺寸：先按 scale 缩放，再受 target_width 上限压制。

    target_width<=0 不压制；宽高向下对齐到偶数（H.264/HEVC 要求）。"""
    w = max(2, int(round(src_w * scale)))
    h = max(2, int(round(src_h * scale)))
    if target_width and target_width > 0 and w > target_width:
        w = int(target_width)
        h = max(2, int(round(src_h * w / src_w)))
    return w - (w % 2), h - (h % 2)


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
        self._open()

    def _open(self):
        if self.backend == "dxgi":
            try:
                import dxcam  # 延迟导入：可选依赖，未安装时回退 mss
                self._camera = dxcam.create(output_idx=max(0, self.monitor - 1))
                self._effective = "dxgi"
                log.info("采集后端: dxgi（显示器 #%d）", self.monitor)
                return
            except Exception as e:
                log.warning("dxgi 后端不可用，回退 mss：%s", e)
                self._camera = None
        try:
            self._mss = mss.MSS()
            self._effective = "mss"
            if self.backend == "dxgi":
                log.info("已回退到 mss 采集后端")
        except Exception as e:
            log.error("mss 采集初始化失败: %s", e)
            self._mss = None

    def set_params(self, backend, monitor, region):
        """参数变更时重建后端；无变更时保持现有实例（避免频繁重建）。"""
        if (backend == self.backend and monitor == self.monitor and region == self.region):
            return
        self.backend, self.monitor, self.region = backend, monitor, region
        self.close()
        self._open()

    def effective_backend(self):
        return self._effective

    def grab(self):
        """采集一帧，返回 BGR numpy 数组；失败或无新帧返回 None。"""
        try:
            if self._effective == "dxgi" and self._camera is not None:
                if self.region:
                    l = int(self.region["left"]); t = int(self.region["top"])
                    r = l + int(self.region["width"]); b = t + int(self.region["height"])
                    frame = self._camera.grab(region=(l, t, r, b))
                else:
                    frame = self._camera.grab()
                return frame  # dxcam 返回 BGR ndarray 或 None（无新帧）
            if self._mss is not None:
                if self.region:
                    mon = {"left": int(self.region["left"]), "top": int(self.region["top"]),
                           "width": int(self.region["width"]), "height": int(self.region["height"])}
                else:
                    idx = self.monitor if 0 <= self.monitor < len(self._mss.monitors) else 1
                    mon = self._mss.monitors[idx]
                img = np.asarray(self._mss.grab(mon))  # BGRA
                return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        except Exception as e:
            log.warning("采集错误: %s", e)
        return None

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
