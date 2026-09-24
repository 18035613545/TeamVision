# -*- coding: utf-8 -*-
"""采集 → 静止门控 → 编码 的共享流水线（host 与 share 共用，第 110/111 条 · 阶段 2）。

host.py（本地共享端）与 share.py（观看端上传）原先各维护一套同构逻辑：采集句柄的
故障恢复、静止检测门控、编码器生命周期与 JPEG 回退。参数与判据几乎一致而实现细节
互有出入（host 缺编码器负缓存退避、share 缺静止边沿日志；重建阈值一个按秒一个按帧数），
任一侧修 bug 另一侧不会跟着好。这里收成三个可独立测试的小对象：

- `CaptureSource`（第 111 条）：采集句柄 + 故障恢复（连续异常重建、持续空帧重建、
  后端已死重建、长期空帧告警）——两侧策略差异做成参数。
- `StillGate`（第 111 条）：motion_changed + still_gate_update 状态机，可选边沿回调。
- `FrameEncoder`（第 110 条）：编码器选型/重建/负缓存退避 + 编码 + 关键帧 + JPEG 回退。
- `PipelineParams`（第 109 条，C1 归位）：host 四条流水线的派生参数对象（原在 host.py）。

两侧只负责"帧去哪"：host 写共享槽（发送线程广播），share 经 _tx_lock 直接发送。

第 90 条不变量：**本模块内所有 `encoder.encode(...)` 调用都在 try 之内**——编码器半死
（GPU 重置 / PyAV 错误）时异常不得沿调用线程传播，否则 host 编码线程死亡 → 观看端永久
定格，share 上传线程死亡 → 成员静默掉线。`tests/test_item90_encode_guard.py` 对此做
AST 结构断言。
"""

import time

import cv2

import logger
import codec as codec_mod
from common import safe_float, safe_int
from screen import (CaptureManager, calc_output_size, downsample_frame, encode_bgr,
                    motion_changed, still_gate_update)

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

    def __init__(self, *, fps, bitrate, keyint, encoder_sel,
                 preset="", target_width=0, quality=80, name="",
                 on_build=None, on_error=None):
        # ui-046（2026-09-24）：width/height 是死参数——两个生产调用点被迫各传一个
        # 无效的 0，而实际尺寸由 encode() 每帧按 scale/target_width 计算。删除。
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
                # codec-012（2026-09-20）：旧实现无条件 `self._enc_fps = fps`，而
                # set_fps 构造失败时会回滚自己的 _fps —— 两侧于是各存一份"已生效"的
                # 假账，这一档再也不被重试。改为按返回值落账。
                if self._encoder.set_fps(fps):
                    self._enc_fps = fps   # 帧率档变化重建
            # codec-006（2026-09-18）：码率参数不能静默忽略——旧实现把 bitrate 记进
            # self._bitrate 就完事，已存在的编码器只在"下次重建"时才采用，调用方
            # （配置热更等）以为已生效。带 ±15% 迟滞，重复同值调用只做一次比较。
            if bitrate is not None:
                try:
                    self._encoder.set_bitrate(int(bitrate))
                except Exception as e:
                    log.debug("%s码率下发失败（保持原码率）: %s", self._tag(), e)
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


class CaptureSource:
    """采集句柄 + 故障恢复（第 111 条：host 与 share 共用同一份机制）。

    两侧策略确有差异，因此差异全部做成参数，机制只写一份：

    | 场景 | host（本地共享） | share（观看端上传） |
    |---|---|---|
    | 连续异常 | 5 次后重建 | 5 次后重建（仅自有句柄） |
    | 持续空帧 | 后端 `working=False` 且持续 5 秒才重建 | 连续 `max(10, fps*5)` 帧无画面即重建 |
    | 长期空帧 | 30 秒告警一次（画面静止/独占全屏属正常语义） | 无 |

    `owned=False`（调用方注入的伪采集器，测试用）时**永不重建**：重建会换回真实后端，
    把注入的替身顶掉。
    """

    def __init__(self, backend=None, monitor=1, region=None, *, capture=None,
                 owned=True, name="", rebuild_on_errors=5,
                 none_rebuild_frames=None, none_rebuild_s=None,
                 none_rebuild_requires_dead=False, none_warn_s=None,
                 sleep_on_none=0.01, sleep_on_error=0.5):
        self._backend = backend
        self._monitor = monitor
        self._region = region
        self._owned = owned
        self._name = name
        self._rebuild_on_errors = max(1, int(rebuild_on_errors))
        self._none_rebuild_frames = none_rebuild_frames
        self._none_rebuild_s = none_rebuild_s
        self._none_rebuild_requires_dead = bool(none_rebuild_requires_dead)
        self._none_warn_s = none_warn_s
        self._sleep_on_none = sleep_on_none
        self._sleep_on_error = sleep_on_error
        self._errors = 0        # 连续 grab 异常次数
        self._nones = 0         # 连续空帧次数（成功抓到一帧即清零）
        self._none_since = None  # 空帧起始时刻（monotonic）
        self._none_warned = False
        # A2：句柄缺失类告警去重（10 秒一次，避免 2 条/秒刷满 5MB 滚动日志）
        self._warn_at = 0.0
        self.cap = capture
        if self.cap is None:
            self._open()

    # ---------- 状态 ----------

    def _tag(self):
        return ("频道[%s] 共享" % self._name) if self._name else ""

    def _warn_throttled(self, msg, *args, interval=10.0):
        """A2：同一类"采集不可用"告警最多每 interval 秒记一条（其余降级 debug）。"""
        now = time.monotonic()
        if self._warn_at <= 0.0 or (now - self._warn_at) >= interval:
            self._warn_at = now
            log.warning(msg, *args)
        else:
            log.debug(msg, *args)

    def working(self):
        """采集后端是否可用：句柄存在、未自报失效、且未连续抛错。

        capture-002（2026-09-18）：旧判据只看句柄对象是否存在——真实后端句柄活着
        但每帧抛错（RDP 断连/锁屏唤醒/GPU 重置后的 BitBlt/COMError）时恒 True，
        空帧兜底 `none_rebuild_requires_dead` 被挡死、永不重建。`consecutive_errors`
        由真实 CaptureManager 提供；伪采集器（测试注入，无此属性）保持原语义。
        """
        cap = self.cap
        if cap is None:
            return False
        if not bool(getattr(cap, "working", True)):
            return False
        return getattr(cap, "consecutive_errors", 0) < self._rebuild_on_errors

    # ---------- 生命周期 ----------

    def _open(self):
        try:
            self.cap = CaptureManager(self._backend, self._monitor, self._region)
        except Exception as e:
            log.error("%s采集器初始化失败: %s", self._tag(), e)
            self.cap = None

    def _rebuild(self, reason, level="warning"):
        """关闭并重建采集句柄；owned=False 时不做任何事（保护注入的伪采集器）。"""
        if not self._owned:
            return
        getattr(log, level)("%s%s", self._tag(), reason)
        old, self.cap = self.cap, None
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        self._open()
        if self.working():
            log.info("%s采集器已重建", self._tag())
        else:
            log.error("%s重建后的采集后端仍不可用，请检查采集源设置", self._tag())

    def set_params(self, backend, monitor, region):
        """下发采集参数（host 允许在线切换采集源）；失败返回 False（调用方应退避重试）。

        A2（2026-09-11）：句柄缺失（两个后端初始化都失败 → `cap is None`）时必须走
        `_rebuild` 而不是 `self.cap.set_params`：
          - 旧实现抛 `AttributeError: 'NoneType' object has no attribute 'set_params'`，
            被这里吞成"采集参数重建失败"每 0.5 秒刷一条 WARNING；
          - 更致命的是 `host._capture_loop` 拿到 False 就 `continue`，**永远走不到
            `grab()`**——而 grab() 里本来就有"句柄缺失 → 计数 → 达阈值重建"的自愈逻辑。
            于是采集后端启动即失败时，采集线程永久无法自愈，只有重启进程才能恢复。
        现在缺句柄时直接复用 grab() 同一套重建机制（返回重建是否成功），让调用方既能
        看到失败、又能触发重建。
        """
        self._backend, self._monitor, self._region = backend, monitor, region
        if self.cap is None:
            self._errors += 1
            self._warn_throttled("%s采集句柄缺失（后端初始化失败），尝试重建采集器（连续 %d 次）",
                                 self._tag(), self._errors)
            self._rebuild("采集句柄缺失，尝试重建采集器", level="error")
            self._errors = 0
            time.sleep(self._sleep_on_error)
            return self.working()
        try:
            self.cap.set_params(backend, monitor, region)
            return True
        except Exception as e:
            self._errors += 1
            log.warning("%s采集参数重建失败: %s（连续 %d 次）",
                        self._tag(), e, self._errors)
            time.sleep(self._sleep_on_error)
            return False

    # ---------- 抓帧 ----------

    def _should_rebuild_none(self, now):
        if not self._owned:
            return False
        if self._none_rebuild_requires_dead and self.working():
            # 后端自报可用：空帧属正常语义（画面静止 / 独占全屏），重建无意义
            return False
        if (self._none_rebuild_frames is not None
                and self._nones >= self._none_rebuild_frames):
            return True
        return (self._none_rebuild_s is not None and self._none_since is not None
                and now - self._none_since >= self._none_rebuild_s)

    def grab(self):
        """抓一帧；返回 BGR，或 None 表示本轮无帧（内部已做恢复/告警/等待）。"""
        if self.cap is None:
            # 构造阶段就没拿到句柄（两个后端都不可用）：按错误路径计数，累计到阈值后
            # 再尝试重建。直接调 self.cap.grab() 只会抛 AttributeError 刷无意义日志。
            self._errors += 1
            self._warn_throttled("%s采集句柄缺失（后端初始化失败），采集不可用（连续 %d 次）",
                                 self._tag(), self._errors)
            if self._errors >= self._rebuild_on_errors:
                self._rebuild("采集句柄缺失，尝试重建采集器", level="error")
                self._errors = 0
            time.sleep(self._sleep_on_error)
            return None
        try:
            bgr = self.cap.grab()
        except Exception as e:
            self._errors += 1
            # capture-002：逐条告警加 10 秒冷却（故障持续时最高每帧一条 →
            # 重建周期内最多 5 条 + 1 条重建日志），避免刷爆滚动日志
            self._warn_throttled("%s采集错误: %s（连续 %d 次）",
                                 self._tag(), e, self._errors)
            if self._errors >= self._rebuild_on_errors:
                self._rebuild("采集后端持续异常，尝试重建采集器（故障恢复）")
                self._errors = 0
            time.sleep(self._sleep_on_error)
            return None
        if bgr is None:
            if self._errors > 0:
                self._errors = 0   # 抓取本身没抛异常，只是没有新帧
            self._nones += 1
            now = time.monotonic()
            if self._none_since is None:
                self._none_since = now
            if self._should_rebuild_none(now):
                # 后端已死（working=False）：必须重建并上报，否则画面永久冻结且日志无痕
                dead = not self.working()
                self._rebuild(
                    "采集后端已失效（dxgi/mss 均无句柄），重建采集器" if dead
                    else "采集连续无画面，尝试重建采集器",
                    level="error" if dead else "warning")
                self._nones = 0
                self._none_since = None
                self._none_warned = False
                time.sleep(self._sleep_on_error)
                return None
            if (self._none_warn_s is not None and not self._none_warned
                    and now - self._none_since >= self._none_warn_s):
                self._none_warned = True
                log.warning("%s已连续 %.0f 秒采集不到新帧（画面静止或独占全屏），"
                            "观看端将看不到更新；可切换采集后端为 mss 试试",
                            self._tag(), now - self._none_since)
            time.sleep(self._sleep_on_none)
            return None
        self._nones = 0
        self._none_since = None
        self._none_warned = False
        self._errors = 0
        return bgr

    def close(self):
        """释放采集句柄（幂等；注入的伪采集器同样关闭——本会话持有的资源归本会话）。"""
        cap, self.cap = self.cap, None
        if cap is not None:
            try:
                cap.close()
            except Exception:
                pass


class StillGate:
    """静止检测闸门（第 111 条：host 与 share 共用同一状态机）。

    连续 quiet_s 秒无有效变化才判定静止；探测异常按"有变化"处理——宁可多发一帧，
    不可画面冻结。on_edge(in_still) 回调用于 host 侧打边沿日志并更新 runtime.stats
    （性能线程据此暂停自适应评估，避免把停发误判为空闲）；share 侧不需要。
    """

    def __init__(self, point_thr, ratio_thr, quiet_s, on_edge=None):
        self._point_thr = point_thr
        self._ratio_thr = ratio_thr
        self._quiet_s = quiet_s
        self._on_edge = on_edge
        self._ref = None          # 最近一次发送帧的抽稀参考图
        self._quiet_since = None  # 连续无变化起始时刻（perf_counter）；None=上一帧有变化
        self.in_still = False     # 是否处于静止停发态

    def check(self, bgr):
        """返回本帧是否有有效变化（True=需发送）；内部维护参考图与静止态。"""
        try:
            changed, self._ref = motion_changed(
                self._ref, downsample_frame(bgr), self._point_thr, self._ratio_thr)
        except Exception:
            changed, self._ref = True, downsample_frame(bgr)
        was_still = self.in_still
        self.in_still, self._quiet_since = still_gate_update(
            self.in_still, self._quiet_since, changed, time.perf_counter(),
            self._quiet_s)
        if self._on_edge is not None and self.in_still != was_still:
            self._on_edge(self.in_still)
        return changed


class PipelineParams:
    """采集/编码/发送/性能四条流水线的派生参数（第 109 条 · 阶段 2 重构）。

    这些值原先散落在 run_server 开头的 60 余行局部变量里，被 4 个嵌套闭包共享。
    抽成对象后四条流水线成为模块级函数（可单独构造参数测试），run_server 只做编排。
    所有数值读取仍走 common.safe_int/safe_float（第 100 条，C1 下沉），手改配置不会打死线程。
    """

    __slots__ = ("base_fps", "base_quality", "base_scale", "quality_min", "scale_min",
                 "fps_min", "still_enabled", "still_probe_interval", "still_quiet_s",
                 "point_thr", "ratio_thr", "encoder_sel", "min_bitrate", "max_bitrate",
                 "keyint", "codec_preset", "target_width")

    def __init__(self, cfg):
        cfg = cfg if isinstance(cfg, dict) else {}
        host_cfg = cfg.get("host") if isinstance(cfg.get("host"), dict) else {}
        perf_cfg = host_cfg.get("perf") if isinstance(host_cfg.get("perf"), dict) else {}
        codec_cfg = host_cfg.get("codec") if isinstance(host_cfg.get("codec"), dict) else {}
        self.base_fps = safe_int(host_cfg.get("fps", 30), 30, 1, 240)
        self.base_quality = safe_int(host_cfg.get("jpeg_quality", 80), 80, 1, 100)
        self.base_scale = safe_float(host_cfg.get("scale", 1.0), 1.0, 0.05, 4.0)
        self.quality_min = safe_int(perf_cfg.get("quality_min", 40), 40, 1, 100)
        self.scale_min = safe_float(perf_cfg.get("scale_min", 0.25), 0.25, 0.05, 1.0)
        self.fps_min = safe_int(perf_cfg.get("fps_min", 10), 10, 1, 240)
        # 静止检测（perf.still，全部可配；默认关闭——开启后小的锐利局部变化
        # 会被整屏面积比判据误判为静止，帧率被压到 probe_fps）
        still_cfg = perf_cfg.get("still") if isinstance(perf_cfg.get("still"), dict) else {}
        self.still_enabled = bool(still_cfg.get("enabled", False))
        self.still_probe_interval = 1.0 / safe_int(
            still_cfg.get("probe_fps", 5), 5, 1, 240)
        still_frames = safe_int(still_cfg.get("still_frames", 3), 3, 1, 3600)
        self.point_thr = safe_int(still_cfg.get("point_thr", 10), 10, 0, 255)
        self.ratio_thr = safe_float(still_cfg.get("ratio_thr", 0.005), 0.005, 0.0, 1.0)
        # 连续无变化满该时长才判定静止（默认 3 次探测 × 0.2s = 0.6s，仅连续累计，
        # 间歇内容不会因零星停顿跨帧累计误入静止）；单位**秒**（第 87 条）
        self.still_quiet_s = still_frames * self.still_probe_interval
        self.encoder_sel = codec_cfg.get("encoder", "auto")
        # 注：原 run_server 里的 base_bitrate 局部量自始未被使用（死代码），
        # 目标码率由 HostRuntime.__init__ 写入 runtime.target_bitrate，故不再保留。
        self.min_bitrate = safe_int(codec_cfg.get("min_bitrate_kbps", 400), 400, 64) * 1000
        self.max_bitrate = safe_int(codec_cfg.get("max_bitrate_kbps", 6000), 6000, 64) * 1000
        if self.max_bitrate < self.min_bitrate:
            self.min_bitrate, self.max_bitrate = self.max_bitrate, self.min_bitrate
        self.keyint = safe_int(codec_cfg.get("keyint", 60), 60, 1, 3600)
        self.codec_preset = codec_cfg.get("preset", "") or ""
        self.target_width = safe_int(codec_cfg.get("target_width", 854) or 0, 854, 0)
