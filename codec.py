# -*- coding: utf-8 -*-
"""H.264/H.265(HEVC) 视频编解码封装（PyAV/FFmpeg）：NVENC 硬件编码优先，软件编码回退。

编码器（VideoEncoder）
- 支持 H.264（h264_nvenc / libx264）与 HEVC（hevc_nvenc / libx265）双编码；
  encoder_sel 决定候选链：auto 自动优先硬件（HEVC→H.264），无硬件回退软件，
  hevc 显式优先 HEVC，nvenc/x264 保持 H.264 语义
- 低延迟参数：无 B 帧、NVENC tune=ull/zerolatency + delay=0（每帧输入立即出包，
  消除恒定 2 帧编码缓冲）、码率控制为 **VBR**（峰值 1.5×bitrate）
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
from typing import Any, cast

# 注（C9）：不再 import numpy——本模块只处理字节/帧对象，无任何 np.* 调用。
try:
    import av
    import av.video.frame
    _AV_AVAILABLE = True
except Exception:  # PyAV 未安装（源码瘦身环境/打包缺依赖）
    # 可选导入的失败分支：把名字置空是既有写法（下面每一处使用前都先判
    # _AV_AVAILABLE），但 mypy 按"模块不能被赋 None"报错。显式 cast 成 Any
    # 不是放宽检查，是把"这里可能没有"讲清楚——真正的判据是 _AV_AVAILABLE。
    # PictureType 不再单独导入：它作为类型名被赋 None 会被 mypy 判成
    # "Cannot assign to a type"（misc），改从模块取（av.video.frame.PictureType）
    # 既避开这条，也让"它属于 av.video.frame"这件事显式起来。
    av = cast(Any, None)
    _AV_AVAILABLE = False

from common import CODEC_H264, CODEC_HEVC

#: NVENC 低延迟预设（FFmpeg >= 5.0 用 p1..p7，p1 最快，p7 最高画质）
_NVENC_PRESET = "p5"
#: x264/x265 低延迟预设
_X264_PRESET = "veryfast"
_X265_PRESET = "veryfast"

#: codec-016（2026-09-24）：preset 必须按编码器族校验——按 common.py 的注释填
#: x264 风格 preset（veryfast 等）时，NVENC 的 ctx.open() 必失败，静默落到 libx264
#: 软件编码（CPU 与延迟翻倍），用户按官方注释配置即永久失去硬件编码。
_NVENC_PRESETS = frozenset({
    "p1", "p2", "p3", "p4", "p5", "p6", "p7", "default", "lossless",
    "slow", "medium", "fast", "hq", "ll", "llhq", "llhp", "hp", "bd",
})
_SW_PRESETS = frozenset({
    "ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
    "slow", "slower", "veryslow", "placebo",
})

#: 一次性告警集（codec-019 等）：同一 key 只报一条
_WARNED_ONCE: set[str] = set()


def _warn_once(key, msg, *args):
    """模块级"只报一次"告警（跨 VideoEncoder 实例生效）。"""
    if key in _WARNED_ONCE:
        return
    if len(_WARNED_ONCE) >= 32:
        _WARNED_ONCE.clear()
    _WARNED_ONCE.add(key)
    log_note((msg % args) if args else msg)

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

#: 连续编码失败达到该次数后永久标记编码器不可用（available=False），
#: 调用方据此回退逐帧 JPEG，避免「无限重建 + 自报健康 + 观看端永久定格」（第 6 条）
_ENCODE_FAIL_LIMIT = 10


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
        self.keyframe_sent = 0   # 累计输出的关键帧数（pipeline 读它做统计）
        self._last_bitrate = 0
        self._reconfiguring = False  # codec-017：锁外构造期间的重建护栏
        self._encode_failures = 0   # 连续编码失败计数（第 6 条：超限则永久标记不可用）
        self._permafailed = False   # 编码器彻底失效，available 恒 False，调用方回退 JPEG
        self._closed = False        # close() 后禁止 set_bitrate/encode 复活（第 52 条）
        self._recreate(self._bitrate)

    # ---------- 内部 ----------

    def _pick_encoder(self):
        """按 encoder_sel 返回候选编码器名列表；无候选或不可用时返回空列表。"""
        if not _AV_AVAILABLE:
            # codec-019（2026-09-24）：PyAV 完全缺失（打包漏 collect_all('av')、
            # 依赖损坏）时旧实现静默返回 []，全程零日志地永久跑 JPEG——上行占用与
            # CPU 翻倍却无任何线索可查（codec-007 的"按名字记一次"只覆盖 av.Codec
            # 抛错分支，第 11 条只覆盖解码侧）。这里补一条明确的一次性告警。
            _warn_once("av-missing",
                       "PyAV 不可用：视频编码整体禁用，将以 JPEG 逐帧上传"
                       "（上行占用与 CPU 明显更高）。打包版请检查 PyInstaller 的 "
                       "collect_all('av') 是否生效；源码运行请 pip install av")
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
            except Exception as e:
                _pick_encoder_logged_unavailable(name, e)   # codec-007：留痕（一次）
                continue
        return usable

    def _build_options(self, name, bitrate):
        """按编码器生成低延迟 + 码率控制选项（**实际恒为 VBR**，见下）。

        codec-010（2026-09-20）：本文件与 common.py / host.py 的注释统一为
        "VBR、峰值 1.5×码率"，排障时按 VBR 估上行占用
        （默认 upstream_max_kbps = 2 × codec.bitrate_kbps）。
        ui-046（2026-09-24）：`rate_control` 形参及 cbr 分支从未接线（唯一构造点
        pipeline.FrameEncoder 与 DEFAULT_CONFIG 都没有对应入口），按"要么接线要么
        删除"删除——避免"可配 CBR"的假接口再次误导排障。
        """
        b = max(64, int(bitrate))
        bufsize = max(128, int(b * 0.5))
        if name in ("h264_nvenc", "hevc_nvenc"):
            preset = self._preset or _NVENC_PRESET
            if self._preset and self._preset not in _NVENC_PRESETS:
                # codec-016：x264 风格 preset 会让 NVENC open() 必失败 → 静默退软件编码
                _warn_once("preset-mismatch-nvenc-%s" % self._preset,
                           "codec.preset=%r 不是 NVENC 预设（应为 p1..p7 等），"
                           "已忽略并使用默认 %s——否则 NVENC 打开失败会静默退到软件编码（CPU 翻倍）",
                           self._preset, _NVENC_PRESET)
                preset = _NVENC_PRESET
            return {
                "preset": preset,
                "tune": "ull",
                "zerolatency": "1",
                "delay": "0",  # 关闭编码器帧缓冲：每帧输入立即出包，消除恒定 2 帧延迟
                "repeat_headers": "1",  # 每个关键帧前重复 SPS/PPS：观看端重连/重建解码器后可从任意关键帧起解
                "forced-idr": "1",  # 让 pict_type=I 在既有上下文上原地产生真 IDR，无需重建编码器（第 47 条）
                "rc": "vbr",
                "b": str(b),
                "maxrate": str(int(b * 1.5)),   # ui-046：VBR 峰值 1.5×码率
                "bufsize": str(b),
            }
        if name == "libx265":
            preset = self._preset or _X265_PRESET
            if self._preset and self._preset not in _SW_PRESETS:
                # codec-016 同族收口：反向（NVENC 式 "p5" 喂给软件编码器）同样会 open 失败
                _warn_once("preset-mismatch-sw-%s" % self._preset,
                           "codec.preset=%r 不是 x264/x265 预设，已忽略并使用默认 %s",
                           self._preset, _X265_PRESET)
                preset = _X265_PRESET
            return {
                "preset": preset,
                "b": str(b),
                "maxrate": str(b),
                "bufsize": str(bufsize),
                # frame-threads=1：消除 libx265 的 4 帧流水线延迟。实测未加时首包要到第 4
                # 次 encode() 输入才出现（≈133ms@30fps），且 _close_ctx 不 drain 会扔掉这 4 帧；
                # 加上后首包提前到第 0 次输入、每帧立即出包、重建时管线残留归零。
                "x265-params": (
                    "keyint=%d:min-keyint=%d:scenecut=0:bframes=0:rc-lookahead=0:frame-threads=1"
                    % (self._keyint, self._keyint)),
            }
        preset = self._preset or _X264_PRESET
        if self._preset and self._preset not in _SW_PRESETS:
            _warn_once("preset-mismatch-sw-%s" % self._preset,
                       "codec.preset=%r 不是 x264/x265 预设，已忽略并使用默认 %s",
                       self._preset, _X264_PRESET)
            preset = _X264_PRESET
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

    def _create_ctx_with(self, names, bitrate):
        """在**锁外**按候选顺序构造并打开新上下文；返回 (ctx, name)，全失败返回 (None, "")。

        第 112 条（C1）：NVENC 拆装一次约 93–110ms，原先整个重建过程都在 `self._lock`
        之内，编码线程要停等三帧（30fps 下 100ms 卡顿）。把"构造"移出锁、只在锁内做
        指针切换，编码线程即可用旧上下文继续出帧直到新上下文就绪。
        """
        for name in names:
            try:
                return self._create_ctx(name, bitrate), name
            except Exception as e:
                log_note("视频编码器 %s 初始化失败: %s" % (name, e))
        return None, ""

    def _install_ctx(self, ctx, name, bitrate):
        """切换上下文（调用方须持锁）：只做指针交换 + 旧上下文解引用，耗时微秒级。"""
        old = self._ctx
        self._ctx = ctx
        self.name = name
        self._last_bitrate = int(bitrate)
        self._retire_ctx(old)

    def _recreate(self, bitrate, keep_old_on_failure=False):
        """按新码率重建上下文；返回是否成功。

        第 112 条（C1）：改为**先建后切**——新上下文构造成功才替换旧的。旧实现在
        构造前就 `_close_ctx()`，一旦新码率建不出来（显存不足/驱动拒绝），原本可用的
        编码器也被自己拆掉，只能回退 JPEG；现在失败时旧上下文原样保留。
        `keep_old_on_failure=False` 用于旧上下文确已不可用的场景（帧尺寸变化、连续
        编码失败后的自愈重建），此时必须丢弃它，否则会继续按旧尺寸出帧。
        """
        if self._permafailed or self._closed:
            return False
        ctx, name = self._create_ctx_with(self._pick_encoder(), bitrate)
        if ctx is None:
            if not keep_old_on_failure:
                self._close_ctx()
                self.name = ""
            return False
        self._install_ctx(ctx, name, bitrate)
        return True

    @staticmethod
    def _retire_ctx(ctx):
        # PyAV 17 的 CodecContext 没有 close()：先解引用让 GC 释放 NVENC 会话，
        # 仅当底层确实提供 close 时才调用（兼容旧版 PyAV），不再假装关闭。
        if ctx is None:
            return
        close = getattr(ctx, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _close_ctx(self):
        ctx, self._ctx = self._ctx, None
        self._retire_ctx(ctx)

    # ---------- 对外 ----------

    @property
    def available(self):
        """当前是否有可用的视频编码器。"""
        with self._lock:
            return self._ctx is not None and not self._permafailed

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
        """自适应帧率变化时重建编码器（time_base/framerate 需随之更新）。

        codec-002（2026-09-18）：与 set_bitrate 同款"锁外构造、锁内切换"——旧实现
        在锁内 `_recreate`（`keep_old_on_failure=False`），编码线程要停等一次 NVENC
        拆装（约 100ms），且新上下文建不出来时连原本可用的旧编码器也一起拆掉
        （自毁后只能回退 JPEG，直到下次成功重建）。

        codec-012（2026-09-20）：codec-002 留的尾巴在失败路径——旧实现先把
        `self._fps = fps` 提交，再锁外构造；构造失败直接 return，而同一 fps 从此
        命中开头的 `fps == self._fps` 提前返回，于是 274 行"下次再试"的注释与实现
        相反：该帧率档**永不重试**。同时调用方 pipeline.py 不看返回值、无条件把
        新值记进 `_enc_fps`，两侧各自存一份"已生效"的假账。
        现在失败即回滚 `self._fps`，并返回是否生效供调用方落账。
        """
        fps = max(1, int(fps))
        with self._lock:
            if self._closed or self._permafailed or fps == self._fps:
                return fps == self._fps
            prev_fps = self._fps
            self._fps = fps
            bitrate = self._last_bitrate
            names = self._pick_encoder()
        ctx, name = self._create_ctx_with(names, bitrate)
        if ctx is None:
            with self._lock:
                self._fps = prev_fps    # codec-012：回滚，让下一帧仍会重试这一档
            return False                # 保留旧上下文继续出帧；帧率未生效
        with self._lock:
            if self._closed or self._permafailed:
                self._retire_ctx(ctx)
                return False
            self._install_ctx(ctx, name, bitrate)
            return True

    def set_size(self, width, height):
        """分辨率档位变化（显示分辨率/缩放变化）时重建编码器到新尺寸；返回是否生效。

        第 11 轮：`_encode_locked` 里原本"尺寸不符就地重建"是**锁内同步**的，且失败
        时会按新尺寸落账再自毁——一次 1080p↔4K 切换要每帧付一次完整拆装（93–110ms）。
        这里按 set_fps 的同一套契约重做：锁外构造、锁内切换、失败回滚尺寸并让调用方
        落负缓存退避。pipeline.FrameEncoder 按返回值记账，不再每帧重试。
        """
        width, height = int(width), int(height)
        with self._lock:
            if self._closed or self._permafailed or self._ctx is None:
                return False
            if (width, height) == (self._ctx.width, self._ctx.height):
                return True
            if self._reconfiguring:
                return False          # 另一次重建正在锁外构造（codec-017 同口径）
            self._reconfiguring = True
            prev = (self._w, self._h)
            self._w, self._h = width, height
            bitrate = self._last_bitrate
            names = self._pick_encoder()
        try:
            ctx, name = self._create_ctx_with(names, bitrate)
        finally:
            with self._lock:
                self._reconfiguring = False
        if ctx is None:
            with self._lock:
                self._w, self._h = prev     # 回滚：旧上下文仍按旧尺寸出帧
            return False
        with self._lock:
            if self._closed or self._permafailed:
                self._retire_ctx(ctx)
                return False
            self._install_ctx(ctx, name, bitrate)
            return True

    def encode(self, bgr, raw_ts=None, force_key=False):
        """编码一帧 BGR 图像（线程安全：与 set_bitrate/set_fps/set_size 串行执行）。

        返回 (annexb_bytes, is_keyframe, encode_ms, out_ts)；
        编码器不可用/尺寸不匹配重建失败时返回 None。
        - raw_ts：采集时刻微秒，透传为该帧 packet.pts（含编码缓冲延迟的精确时间戳）
        - force_key：强制本帧为关键帧（关键帧请求/重建后重置参考帧响应）。

        强制关键帧通过 NVENC 的 forced-idr=1 选项 + frame.pict_type=I 在既有上下文上
        原地产生真 IDR（实测支持），无需重建编码器；软件编码器（x264/x265）同样用
        pict_type=I 兜底。帧尺寸变化由 set_size 在锁外重建（见其 docstring）。
        """
        with self._lock:
            return self._encode_locked(bgr, raw_ts, force_key)

    def _encode_locked(self, bgr, raw_ts=None, force_key=False):
        if self._closed or self._permafailed or self._ctx is None:
            return None
        if bgr is None or bgr.size == 0:
            return None
        h, w = bgr.shape[:2]
        # 帧尺寸变化（自适应缩放）时重建编码器：通常已由 set_size 在锁外处理；
        # 这里只兜"调用方没先 set_size"的路径，仍保持锁内重建（失败即丢上下文，
        # 由 pipeline 的重建/退避接管），不会把旧尺寸的包发出去。
        if (w, h) != (self._ctx.width, self._ctx.height):
            self._w, self._h = w, h
            self._recreate(self._last_bitrate)
            if self._ctx is None:
                return None
        # 第 47 条：forced-idr=1 已让 pict_type=I 在既有上下文上原地产生真 IDR，
        # 无需为强制关键帧重建编码器（重建带来 93-110ms NVENC 拆装 + 码率尖峰自激振荡）
        start = time.perf_counter()
        try:
            frame = av.VideoFrame.from_ndarray(bgr, format="bgr24")
            frame.pts = int(raw_ts) if raw_ts is not None else int(time.time() * 1_000_000)
            if force_key:
                frame.pict_type = av.video.frame.PictureType.I
            pkts = list(self._ctx.encode(frame))
        except Exception as e:
            # 第 6 条：连续编码失败计数；超限则永久标记不可用并回退 JPEG，
            # 不再无限重建（原行为：每次异常都重建且 available 恒 True → 观看端永久定格）
            self._encode_failures += 1
            if self._encode_failures >= _ENCODE_FAIL_LIMIT:
                self._permafailed = True
                self._close_ctx()
                self.name = ""
                log_note("视频编码器连续 %d 次编码失败，永久标记不可用并回退 JPEG: %s"
                         % (self._encode_failures, e))
                return None
            self._recreate(self._last_bitrate)
            return None
        if not pkts:
            # pre-roll（如 libx265 流水线延迟）：本帧无输出，不计入失败
            return None
        # codec-003（2026-09-18）：一次 encode 可能出**多包**（repeat_headers / 编码器
        # 内部拆分）：旧实现只取 `pkts[-1]`，前序 NAL 静默丢弃——关键帧的 SPS/PPS 与
        # IDR 被拆成两包时直接丢掉 SPS/PPS，观看端后续帧无法起解；0 字节包照发还会
        # 让观看端把空 payload 当关键帧处理（关键帧风暴）。这里合并全部非空包、
        # 关键帧标志取任一包命中，时间戳取最后一包。
        parts = [bytes(p) for p in pkts]
        data = b"".join(p for p in parts if p)
        if not data:
            return None
        self._encode_failures = 0
        encode_ms = (time.perf_counter() - start) * 1000.0
        pkt = pkts[-1]
        is_key = any(bool(getattr(p, "is_keyframe", False)) for p in pkts)
        if is_key:
            self.keyframe_sent += 1
        out_ts = getattr(pkt, "pts", None)
        if out_ts is None:
            out_ts = int(raw_ts) if raw_ts is not None else 0
        return data, is_key, encode_ms, int(out_ts)

    def set_bitrate(self, bps, hysteresis=0.15):
        """码率自适应：目标码率变化超过当前 ±hysteresis 时重建编码器。

        返回 True 表示已重建（调用方应让下一帧强制关键帧以重置解码端参考帧）。

        第 112 条（C1）：构造新上下文在**锁外**完成，编码线程不再为一次调码率停等
        约 100ms；且新上下文建不出来时保持旧上下文可用（不再自毁编码器）。

        codec-017（2026-09-24）：`_last_bitrate` 要到 `_install_ctx` 才更新，而构造在
        锁外进行——性能线程与编码线程（host.py 每帧传 bitrate）可同时通过迟滞判据、
        为同一次调整各构造一套上下文（check-then-act）：编码线程白停等一次完整 NVENC
        初始化（约丢 3 帧），并发 NVENC 会话还多一路（有上限，可能直接 open 失败）。
        这里加"重建中"标记：构造期间的后到者直接返回，不重复造上下文。
        """
        bps = max(64, int(bps))
        with self._lock:
            if self._closed or self._permafailed:
                return False  # 第 52 条：已关闭/已失效，禁止复活
            if self._ctx is not None and abs(bps - self._last_bitrate) <= self._last_bitrate * hysteresis:
                return False
            if self._reconfiguring:
                return False  # codec-017：另一次重建正在锁外构造，避免重复构造
            self._reconfiguring = True
            names = self._pick_encoder()
        try:
            ctx, name = self._create_ctx_with(names, bps)
        finally:
            with self._lock:
                self._reconfiguring = False
        if ctx is None:
            return False
        with self._lock:
            if self._closed or self._permafailed:
                self._retire_ctx(ctx)
                return False
            self._install_ctx(ctx, name, bps)
            return True

    def close(self):
        with self._lock:
            self._closed = True
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
        # B6（2026-09-11）：内部复位标志。decode() 因缺参考帧/坏 NAL 抛异常时会
        # flush_buffers()，此时参考帧链已被清空，但调用方（viewer 的 _got_key）无从得知，
        # 后续某个 P 帧若被解出就会跳过"仅关键帧后显示"门控被直接贴到悬浮窗（随机花屏）。
        # 这里把"解码器已复位、需要等下一个关键帧"显式告诉调用方。
        self.needs_keyframe = False
        self._init_ctx()

    def _decoder_name(self):
        return {CODEC_H264: "h264", CODEC_HEVC: "hevc"}.get(self._codec, "h264")

    @property
    def available(self):
        """解码器上下文是否可用（缺 PyAV 或打开失败时为 False，第 11 条）。"""
        return self._ctx is not None

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
        """流中编码切换（H.264 <-> HEVC）时重建解码器。

        第 116 条（C6）：重建前先关旧上下文，避免 PyAV CodecContext 只靠 GC 释放
        （重连风暴下会堆积线程/显存）。
        """
        if codec == self._codec and self._ctx is not None:
            return
        self.close()
        self._codec = codec
        self._init_ctx()

    def reset(self):
        """清空解码参考状态（重连/切源），尽量复用已有上下文。

        flush_buffers 比新建 CodecContext 便宜一个数量级；flush 失败才整体重开。
        第 116 条（C6）：观看端原先每次重连/切源都 `VideoDecoder(...)` 新建上下文，
        长跑 RSS/线程随重连上涨。
        """
        if self._ctx is not None:
            try:
                self._ctx.flush_buffers()
                self.needs_keyframe = False
                return
            except Exception:
                pass
        self.close()
        self._init_ctx()
        self.needs_keyframe = False

    def clear_needs_keyframe(self):
        """B6：调用方（viewer）在按标志作废显示门控后清除标志。"""
        self.needs_keyframe = False

    def decode(self, annexb):
        """解码一段 Annex-B NAL，返回 list[BGR ndarray]；失败/缺参考帧返回 []。"""
        if self._ctx is None:
            return []
        try:
            frames = self._ctx.decode(av.Packet(annexb))
        except Exception:
            # 缺参考帧 / 非法 NAL：重置解码器，等待下一关键帧
            # B6：显式置位 needs_keyframe——调用方据此把"已有关键帧起点"的门控作废，
            # 否则后续解出的 P 帧会以脏参考被直接显示（随机花屏/拖影）。
            self.needs_keyframe = True
            try:
                self._ctx.flush_buffers()
            except Exception:
                # 第 116 条补丁：flush 失败必须先 close 旧上下文再重建，
                # 否则 _init_ctx 直接覆盖 self._ctx，旧 PyAV 对象泄漏（线程/显存堆积）。
                self.close()
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
        """释放解码上下文（幂等）。

        codec-005（2026-09-18）：PyAV 17 的 CodecContext 没有 close()——旧实现
        `ctx.close()` 恒抛 AttributeError 被吞掉，释放全交 GC（重连风暴下线程/显存
        堆积）。与编码侧 `_retire_ctx` 同一口径：解引用让 GC 释放，仅当底层确实
        提供 close 时才调用；顺带 flush_buffers 提前归还 PyAV 内部缓冲。
        """
        ctx = self._ctx
        self._ctx = None
        if ctx is None:
            return
        flush = getattr(ctx, "flush_buffers", None)
        if callable(flush):
            try:
                flush()
            except Exception:
                pass
        close = getattr(ctx, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        del ctx


#: codec-007：候选编码器"不可用"的原因只记一次（避免每次重建刷屏）
_UNAVAILABLE_LOGGED = set()


def _pick_encoder_logged_unavailable(name, exc):
    """codec-007（2026-09-18）：候选编码器不可用的原因按名字只记一次。

    旧实现 `except: continue` 全吞——"为什么没有硬件编码"在日志里可能一个字都不落。
    只记一次避免每次重建都刷屏（编码器重建在自适应期间可达每秒一次）。
    """
    if name in _UNAVAILABLE_LOGGED:
        return
    _UNAVAILABLE_LOGGED.add(name)
    log_note("视频编码器 %s 不可用（%s）；候选链中将跳过" % (name, exc))


def log_note(msg):
    """编码器初始化失败等低频率告警的兜底输出（避免 codec.py 依赖 logger 产生环）。"""
    try:
        from logger import get_logger
        get_logger().warning("%s", msg)
    except Exception:
        pass
