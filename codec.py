# -*- coding: utf-8 -*-
"""H.264/H.265(HEVC) 视频编解码封装（PyAV/FFmpeg）：NVENC 硬件编码优先，软件编码回退。

编码器（VideoEncoder）
- 支持 H.264（h264_nvenc / libx264）与 HEVC（hevc_nvenc / libx265）双编码；
  encoder_sel 决定候选链：auto 自动优先硬件（HEVC→H.264），无硬件回退软件，
  hevc 显式优先 HEVC，nvenc/x264 保持 H.264 语义
- 低延迟参数：无 B 帧、NVENC tune=ull/zerolatency + delay=0（每帧输入立即出包，
  消除恒定 2 帧编码缓冲）、CBR 码率
- 码率自适应（ABR）：set_bitrate() 变化超阈值时重建编码器，重建后首帧强制关键帧
  （顺带重置参考帧，避免解码端花屏）
- 强制关键帧（关键帧请求）：frame.pict_type = I
- 时间戳透传：以输入帧 pts（采集微秒时间戳）作为 packet.pts 输出，编码缓冲延迟如实
  计入端到端延迟测量；编码帧尺寸变化时自动重建
- 编码器不可用（无 PyAV / NVENC / x264 / x265）时 name 为空，调用方回退逐帧 JPEG
- codec_id 属性：指示当前实际编码（H.264/HEVC），供发送端写入协议 codec 字节

解码器（VideoDecoder）
- 从内存 Annex-B NAL 流解码（单帧独立 decode，配合“只解最新帧”低延迟策略）
- 支持 H.264/HEVC；set_codec() 在流中编码切换时重建解码器
- 缺参考帧（如首帧非关键帧）时静默返回空，由调用方等待/请求关键帧
- 解码异常自动 flush 恢复，避免异常数据长期卡死
"""

import threading
import time

from fractions import Fraction

import numpy as np

try:
    import av
    from av.video.frame import PictureType
    _AV_AVAILABLE = True
except Exception:  # PyAV 未安装（源码瘦身环境/打包缺依赖）
    av = None
    PictureType = None
    _AV_AVAILABLE = False

from common import CODEC_H264, CODEC_HEVC

#: NVENC 低延迟预设（FFmpeg >= 5.0 用 p1..p7，p1 最快，p7 最高画质）
_NVENC_PRESET = "p5"
#: x264/x265 低延迟预设
_X264_PRESET = "veryfast"
_X265_PRESET = "veryfast"

#: encoder_sel -> 候选编码器链（按优先级）；name 决定实际 codec_id
_ENCODER_CHAIN = {
    "auto": ["hevc_nvenc", "h264_nvenc", "libx265", "libx264"],
    "nvenc": ["h264_nvenc", "libx264"],            # H.264 硬件优先
    "x264": ["libx264"],                            # H.264 软件
    "hevc": ["hevc_nvenc", "libx265", "h264_nvenc", "libx264"],
}
#: 编码器名 -> 协议 codec 字节
_CODEC_BY_NAME = {
    "hevc_nvenc": CODEC_HEVC, "libx265": CODEC_HEVC,
    "h264_nvenc": CODEC_H264, "libx264": CODEC_H264,
}


def av_available():
    """PyAV 是否可用（提供 H.264/HEVC 编解码能力）。"""
    return _AV_AVAILABLE


class VideoEncoder:
    """H.264/H.265(HEVC) 编码器封装：自动挑选编码器 + 低延迟参数 + 码率重建。

    encoder_sel: auto|nvenc|x264|hevc|jpeg（jpeg 表示明确禁用视频编码，name 恒为空）
    """

    def __init__(self, width, height, fps, bitrate, keyint=30,
                 encoder_sel="auto", preset=""):
        self._w = int(width)
        self._h = int(height)
        self._fps = max(1, int(fps))
        self._bitrate = int(bitrate)
        self._keyint = max(1, int(keyint))
        self._encoder_sel = encoder_sel
        self._preset = (preset or "").strip()
        self._ctx = None
        # 编码线程与 ABR 性能线程共享同一实例：用锁串行化 encode/set_bitrate/set_fps，
        # 避免并发重建上下文（set_bitrate 触发 _recreate）与编码操作互相踩踏
        self._lock = threading.RLock()
        self.name = ""           # 实际编码器名，如 hevc_nvenc / h264_nvenc / libx265 / libx264；空=不可用
        self.keyframe_sent = 0   # 累计输出的关键帧数
        self.frame_sent = 0      # 累计输出的帧数
        self._rebuilt_at_frame = 0  # 最近一次重建成功时的 frame_sent（用于判定关键帧是否需要重建）
        self._last_bitrate = 0
        self._recreate(self._bitrate)

    # ---------- 内部 ----------

    def _pick_encoder(self):
        """按 encoder_sel 返回候选编码器名列表；无候选或不可用时返回空列表。"""
        if not _AV_AVAILABLE:
            return []
        if self._encoder_sel == "jpeg":
            return []
        order = _ENCODER_CHAIN.get(self._encoder_sel,
                                   ["hevc_nvenc", "h264_nvenc", "libx265", "libx264"])
        usable = []
        for name in order:
            try:
                av.Codec(name, "w")
                usable.append(name)
            except Exception:
                continue
        return usable

    def _build_options(self, name, bitrate):
        """按编码器生成低延迟 + CBR 选项。"""
        b = max(64, int(bitrate))
        bufsize = max(128, int(b * 0.5))
        if name in ("h264_nvenc", "hevc_nvenc"):
            preset = self._preset or _NVENC_PRESET
            return {
                "preset": preset,
                "tune": "ull",
                "zerolatency": "1",
                "delay": "0",  # 关闭编码器帧缓冲：每帧输入立即出包，消除恒定 2 帧延迟
                "repeat_headers": "1",  # 每个关键帧前重复 SPS/PPS：观看端重连/重建解码器后可从任意关键帧起解
                "rc": "cbr",
                "b": str(b),
                "maxrate": str(b),
                "bufsize": str(bufsize),
            }
        if name == "libx265":
            preset = self._preset or _X265_PRESET
            return {
                "preset": preset,
                "b": str(b),
                "maxrate": str(b),
                "bufsize": str(bufsize),
                "x265-params": (
                    "keyint=%d:min-keyint=%d:scenecut=0:bframes=0:rc-lookahead=0"
                    % (self._keyint, self._keyint)),
            }
        preset = self._preset or _X264_PRESET
        return {
            "preset": preset,
            "tune": "zerolatency",
            "profile": "baseline",
            "b": str(b),
            "maxrate": str(b),
            "bufsize": str(bufsize),
            "x264-params": "keyint=%d:min-keyint=%d:scenecut=0:bframes=0" % (
                self._keyint, self._keyint),
        }

    def _create_ctx(self, name, bitrate):
        """创建并打开指定编码器上下文；失败抛出异常由调用方处理。"""
        ctx = av.CodecContext.create(name, "w")
        ctx.width, ctx.height = self._w, self._h
        ctx.time_base = Fraction(1, self._fps)
        ctx.framerate = Fraction(self._fps, 1)
        ctx.pix_fmt = "yuv420p"
        ctx.gop_size = self._keyint
        try:
            ctx.max_b_frames = 0  # 无 B 帧：降延迟、降解码缓冲
        except Exception:
            pass
        ctx.options = self._build_options(name, bitrate)
        ctx.open()
        return ctx

    def _recreate(self, bitrate):
        """关闭旧上下文并按新码率重建；返回是否成功。"""
        self._close_ctx()
        self._last_bitrate = int(bitrate)
        names = self._pick_encoder()
        for name in names:
            try:
                self._ctx = self._create_ctx(name, self._last_bitrate)
                self.name = name
                self._rebuilt_at_frame = self.frame_sent  # 重建后首帧必为关键帧（IDR）
                return True
            except Exception as e:
                self._ctx = None
                log_note("视频编码器 %s 初始化失败: %s" % (name, e))
        self.name = ""
        return False

    def _close_ctx(self):
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:
                pass
            self._ctx = None

    # ---------- 对外 ----------

    @property
    def available(self):
        """当前是否有可用的视频编码器。"""
        with self._lock:
            return self._ctx is not None

    @property
    def bitrate(self):
        with self._lock:
            return self._last_bitrate

    @property
    def codec_id(self):
        """当前实际编码对应的协议 codec 字节（H.264/HEVC）。"""
        with self._lock:
            return _CODEC_BY_NAME.get(self.name, CODEC_H264)

    def set_fps(self, fps):
        """自适应帧率变化时重建编码器（time_base/framerate 需随之更新）。"""
        fps = max(1, int(fps))
        with self._lock:
            if fps != self._fps:
                self._fps = fps
                self._recreate(self._last_bitrate)

    def encode(self, bgr, raw_ts=None, force_key=False):
        """编码一帧 BGR 图像（线程安全：与 set_bitrate/set_fps 串行执行）。

        返回 (annexb_bytes, is_keyframe, encode_ms, out_ts)；
        编码器不可用/尺寸不匹配重建失败时返回 None。
        - raw_ts：采集时刻微秒，透传为该帧 packet.pts（含编码缓冲延迟的精确时间戳）
        - force_key：强制本帧为关键帧（关键帧请求/重建后重置参考帧响应）。

        NVENC 等硬件编码器无法在既有上下文上用 pict_type 强制 IDR，故强制关键帧时
        若上下文已编码过帧则重建（重建后首帧必为 IDR 且带 SPS/PPS），保证观看端
        任何时刻重连/请求关键帧都能起解；软件编码器（x264/x265）仍用 pict_type 兜底。
        """
        with self._lock:
            return self._encode_locked(bgr, raw_ts, force_key)

    def _encode_locked(self, bgr, raw_ts=None, force_key=False):
        if self._ctx is None:
            return None
        if bgr is None or bgr.size == 0:
            return None
        h, w = bgr.shape[:2]
        # 帧尺寸变化（自适应缩放）时重建编码器：先更新目标尺寸再重建，
        # 否则重建仍沿用旧尺寸，导致编码分辨率不随缩放变化（PyAV 静默缩放回原尺寸）
        if (w, h) != (self._ctx.width, self._ctx.height):
            self._w, self._h = w, h
            self._recreate(self._last_bitrate)
            if self._ctx is None:
                return None
        if force_key and self.frame_sent > self._rebuilt_at_frame:
            # 旧上下文无法可靠强制 IDR：重建后首帧即关键帧（H.264/HEVC NVENC 通用）
            self._recreate(self._last_bitrate)
            if self._ctx is None:
                return None
        start = time.perf_counter()
        try:
            frame = av.VideoFrame.from_ndarray(bgr, format="bgr24")
            frame.pts = int(raw_ts) if raw_ts is not None else int(time.time() * 1_000_000)
            if force_key:
                frame.pict_type = PictureType.I
            pkts = list(self._ctx.encode(frame))
        except Exception:
            # 编码异常（如驱动错误）：尝试重建后放弃本帧，避免卡死
            self._recreate(self._last_bitrate)
            return None
        if not pkts:
            return None
        encode_ms = (time.perf_counter() - start) * 1000.0
        pkt = pkts[-1]  # 低延迟下每次 encode 至多输出一帧；取最新
        self.frame_sent += 1
        is_key = bool(getattr(pkt, "is_keyframe", False))
        if is_key:
            self.keyframe_sent += 1
        out_ts = getattr(pkt, "pts", None)
        if out_ts is None:
            out_ts = int(raw_ts) if raw_ts is not None else 0
        return bytes(pkt), is_key, encode_ms, int(out_ts)

    def set_bitrate(self, bps, hysteresis=0.15):
        """码率自适应：目标码率变化超过当前 ±hysteresis 时重建编码器。

        返回 True 表示已重建（调用方应让下一帧强制关键帧以重置解码端参考帧）。
        """
        bps = max(64, int(bps))
        with self._lock:
            if self._ctx is not None and abs(bps - self._last_bitrate) <= self._last_bitrate * hysteresis:
                return False
            return self._recreate(bps)

    def close(self):
        with self._lock:
            self._close_ctx()
            self.name = ""


class VideoDecoder:
    """H.264/H.265(HEVC) 解码器封装：从内存 Annex-B NAL 单帧解码（PyAV）。

    用于观看端“只解最新帧”低延迟接收：每收到一帧独立 decode，
    缺参考帧时静默返回空，解码异常自动 flush 恢复。
    """

    def __init__(self, codec=CODEC_H264):
        self._codec = codec
        self._ctx = None
        self.decoded_frames = 0
        self._init_ctx()

    def _decoder_name(self):
        return {CODEC_H264: "h264", CODEC_HEVC: "hevc"}.get(self._codec, "h264")

    def _init_ctx(self):
        if not _AV_AVAILABLE:
            return
        try:
            ctx = av.CodecContext.create(self._decoder_name(), "r")
            ctx.open()
            self._ctx = ctx
        except Exception:
            self._ctx = None

    def set_codec(self, codec):
        """流中编码切换（H.264 <-> HEVC）时重建解码器。"""
        if codec == self._codec and self._ctx is not None:
            return
        self._codec = codec
        self._ctx = None
        self._init_ctx()

    def decode(self, annexb):
        """解码一段 Annex-B NAL，返回 list[BGR ndarray]；失败/缺参考帧返回 []。"""
        if self._ctx is None:
            return []
        try:
            frames = self._ctx.decode(av.Packet(annexb))
        except Exception:
            # 缺参考帧 / 非法 NAL：重置解码器，等待下一关键帧
            try:
                self._ctx.flush_buffers()
            except Exception:
                try:
                    self._init_ctx()
                except Exception:
                    pass
            return []
        out = []
        for f in frames:
            try:
                out.append(f.to_ndarray(format="bgr24"))
            except Exception:
                continue
        self.decoded_frames += len(out)
        return out

    def close(self):
        self._ctx = None


def log_note(msg):
    """编码器初始化失败等低频率告警的兜底输出（避免 codec.py 依赖 logger 产生环）。"""
    try:
        from logger import get_logger
        get_logger().warning("%s", msg)
    except Exception:
        pass
