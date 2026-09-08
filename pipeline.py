# -*- coding: utf-8 -*-
"""编码器生命周期 + 编码 + 关键帧 + JPEG 回退（host 与 share 共用，第 110 条 · 阶段 2）。

原先 host.py（本地共享端 `_encode_loop`）与 share.py（观看端上传 `_encode_send`）各自
维护一份同构的编码/回退逻辑，且已开始漂移：

- share 有第 53 条的"构造失败负缓存退避"，host 没有 —— 编码器不可用（NVENC/x264 都打
  不开、GPU 驱动异常）时 host 会**每帧**新建一次 VideoEncoder（4 次 av.Codec 查找 +
  4 次 open + 最多 4 条告警，30 帧/秒），把 CPU 与日志一起打满且永不恢复；
- host 有"编码器失效自动丢弃重建"，share 有等价逻辑但错误处理与日志各不相同。

本模块把这段收成 `FrameEncoder` 一个实现，两侧只提供"帧去哪"（host 写共享槽、share
经 _tx_lock 发送）与各自的动态参数（host 的 scale/quality/fps 来自共享槽，share 固定）。

第 90 条不变量：**本模块内所有 `encoder.encode(...)` 调用都在 try 之内**——编码器半死
（GPU 重置 / PyAV 错误）时异常不得沿调用线程传播，否则 host 编码线程死亡 → 观看端永久
定格，share 上传线程死亡 → 成员静默掉线。`tests/test_item90_encode_guard.py` 对此做
AST 结构断言。
"""

import time

import cv2

import logger
import codec as codec_mod
from screen import calc_output_size, encode_bgr

log = logger.get_logger()


class EncodedFrame:
    """一次成功编码的产物（视频 NAL 或 JPEG 字节 + 输出尺寸与统计）。"""

    __slots__ = ("data", "is_key", "is_video", "ts", "ms", "frame", "w", "h",
                 "codec_id", "bitrate", "keyframes")

    def __init__(self, data, is_key, is_video, ts, ms, frame, w, h,
                 codec_id=0, bitrate=0, keyframes=0):
        self.data = data          # 视频：NAL 字节；JPEG 回退：JPEG 字节
        self.is_key = is_key
        self.is_video = is_video
        self.ts = ts              # 输出时间戳（视频路径=编码输出时刻，JPEG=采集时刻）
        self.ms = ms              # 编码耗时（毫秒）
        self.frame = frame        # 缩放后的 BGR（静止期强制 IDR 复用）
        self.w = w
        self.h = h
        self.codec_id = codec_id
        self.bitrate = bitrate
        self.keyframes = keyframes


class FrameEncoder:
    """视频优先、JPEG 回退的编码器封装（线程内独占使用，非线程安全）。"""

    def __init__(self, *, width, height, fps, bitrate, keyint, encoder_sel,
                 preset="", target_width=0, quality=80, name="",
                 on_build=None, on_error=None):
        self._w = width
        self._h = height
        self._fps = max(1, int(fps))
        self._bitrate = int(bitrate)
        self._keyint = max(1, int(keyint))
        self._encoder_sel = encoder_sel or "auto"
        self._preset = preset or ""
        self._target_width = int(target_width or 0)
        self._quality = int(quality)
        self._name = name
        self._on_build = on_build
        self._on_error = on_error
        self._encoder = None
        self._enc_fps = 0
        self._retry_at = 0.0
        self._backoff = 1.0

    # ---------- 只读状态 ----------

    @property
    def encoder(self):
        return self._encoder

    @property
    def video_mode(self):
        return self._encoder is not None

    @property
    def backoff(self):
        """当前编码器构造退避秒数（测试与诊断用）。"""
        return self._backoff

    def _tag(self):
        return ("频道[%s] 共享" % self._name) if self._name else ""

    # ---------- 编码器生命周期 ----------

    def _drop_encoder(self):
        enc, self._encoder = self._encoder, None
        if enc is not None:
            try:
                enc.close()
            except Exception:
                pass

    def _ensure_encoder(self, w, h, fps, bitrate):
        """确保有可用的视频编码器；不可用（含退避期）时静默返回，由调用方走 JPEG。"""
        if self._encoder is not None and not self._encoder.available:
            self._drop_encoder()   # 编码器失效（如 GPU 驱动错误）：下次重建/回退
        if self._encoder is not None:
            if fps is not None and fps != self._enc_fps:
                self._encoder.set_fps(fps)   # 帧率档变化重建
                self._enc_fps = fps
            return
        if self._encoder_sel == "jpeg":
            return
        now = time.monotonic()
        if now < self._retry_at:
            return                     # 第 53 条：负缓存退避期，期间只走 JPEG 回退
        try:
            enc = codec_mod.VideoEncoder(w, h, fps or self._fps, bitrate, self._keyint,
                                         self._encoder_sel, self._preset)
        except Exception as e:
            # 构造本身抛异常（比 available=False 更糟）：同样进入退避，避免每帧重试
            log.warning("%s编码器构造异常: %s", self._tag(), e)
            enc = None
        if enc is None or not enc.available:
            # 构造失败：指数退避（1→2→4→…→30s 封顶），期间走 JPEG 回退
            self._retry_at = now + self._backoff
            self._backoff = min(30.0, self._backoff * 2)
            return
        self._encoder = enc
        self._enc_fps = fps or self._fps
        self._backoff = 1.0
        if self._on_build is not None:
            try:
                self._on_build(enc, w, h, bitrate)
            except Exception as e:
                log.warning("%s编码器回调异常: %s", self._tag(), e)

    # ---------- 编码 ----------

    def encode(self, bgr, ts_us, *, force_key=False, scale=1.0, quality=None,
               fps=None, bitrate=None):
        """缩放 → 编码（视频优先，不可用回退 JPEG）；返回 EncodedFrame 或 None。

        None 表示"本帧未产出"（编码器 pre-roll/编码失败/JPEG 编码异常），调用方不应
        把本帧记为已发送。所有 encode() 异常都在这里被吞掉并交给 on_error 上报。
        """
        try:
            if bitrate is not None:
                self._bitrate = int(bitrate)
            if quality is not None:
                self._quality = int(quality)
            w, h = calc_output_size(bgr.shape[1], bgr.shape[0], scale, self._target_width)
            self._ensure_encoder(w, h, fps, self._bitrate)
            frame = bgr
            if (w, h) != (bgr.shape[1], bgr.shape[0]):
                frame = cv2.resize(bgr, (w, h), interpolation=cv2.INTER_AREA)
            if self._encoder is not None:
                res = self._encoder.encode(frame, raw_ts=ts_us, force_key=force_key)
                if res is None:
                    return None
                nal, is_key, ms, out_ts = res
                return EncodedFrame(nal, is_key, True, out_ts, ms, frame, w, h,
                                    self._encoder.codec_id, self._encoder.bitrate,
                                    self._encoder.keyframe_sent)
            jpeg, ms = encode_bgr(bgr, scale, self._quality, self._target_width)
            return EncodedFrame(jpeg, False, False, ts_us, ms, frame, w, h)
        except Exception as e:
            if self._on_error is not None:
                try:
                    self._on_error(e)
                except Exception:
                    pass
            else:
                log.warning("%s编码失败（已跳过本帧）: %s", self._tag(), e)
            return None

    def force_idr(self, last_frame):
        """静止/空闲期强制一帧 IDR；返回 (encode 结果 或 None, 异常 或 None)。

        第 90 条：encode() 调用在 try 之内（异常上抛会杀死 host 编码线程 / share 上传
        线程）。异常交给调用方按各自策略上报（host 有 10 秒冷却告警，share 记一条
        warning）。无可用编码器或没有缓存帧时返回 (None, None)，调用方按 JPEG 路径
        重发缓存帧。
        """
        if (self._encoder is None or not self._encoder.available
                or last_frame is None):
            return None, None
        try:
            res = self._encoder.encode(last_frame,
                                       raw_ts=int(time.time() * 1_000_000),
                                       force_key=True)
        except Exception as e:
            return None, e
        return res, None

    def close(self):
        """释放编码器（幂等）。"""
        self._drop_encoder()
