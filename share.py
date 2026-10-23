# -*- coding: utf-8 -*-
"""观看端共享上传会话（观看组 MultiView 成员端）。

ScreenShareSession 在独立线程内完成 采集→静止门控→编码→发送：
- 采集恢复与静止门控复用 pipeline.CaptureSource / pipeline.StillGate（第 111 条，
  与 host 共享端同一实现，差异只在参数），参数读 cfg["host"]
- 视频编码与 JPEG 回退复用 pipeline.FrameEncoder（第 110 条，与 host 一致）
- 静止期收到 req_keyframe 时补一帧：视频路径强制 IDR，JPEG 路径重发缓存帧（自包含）；
  保证新观众订阅静止画面可立即起解
- 所有发送经调用方传入的 _tx_lock，与 ping/watch 等控制消息串行，防字节交错
- 发送失败即停止（记日志），由 Channel 层负责提示与重连后的自动重启
"""

import threading
import time

import logger
import pipeline

# B5：配置数值转换与区域归一复用公共实现，避免观看端上传路径继续用裸 int()/float()。
# C1（优化方案 2026-09-17）：这三个工具已从 host 下沉到 common——原先反向 import host
# 会把整个共享端巨模块拖进观看端进程，且 host/viewer/share 三套 clamp 互为副本。
from common import (
    SeqCounter,
    feedback_is_stuck,
    normalize_region,
    pack_frame,
    pack_video,
    safe_float,
    safe_int,
)

log = logger.get_logger()

#: net-050（v2.5）：两次降档之间的最小间隔（秒）。共享端每 2 秒转达一次"卡"的
#: 反馈，不设间隔的话 2500 Kbps 会在十几秒内一路塌到下限——用户还没来得及反应，
#: 画质已经掉到底了。
_FB_DROP_COOLDOWN_S = 10.0
#: 多久没再收到"卡"的反馈后，开始把码率往配置值回升（秒）。
_FB_RESTORE_S = 30.0
#: 回升一档的倍率（与 host 侧 ABR 的码率回升同款：1.3 倍一步，不跳回原值，
#: 免得链路刚好抽风一下就瞬间反弹）。
_FB_RESTORE_STEP = 1.3
#: 降一档的倍率。
_FB_DROP_STEP = 0.7


class ScreenShareSession:
    """上传器：构造后 start()/stop()。capture 可注入伪采集器（测试）。"""

    def __init__(self, sock, tx_lock, cfg, name, capture=None):
        self._sock = sock
        self._tx = tx_lock
        self._name = name
        self._stop = threading.Event()
        self._key_evt = threading.Event()
        host_cfg = ((cfg or {}).get("host", {})) if isinstance(cfg, dict) else {}
        host_cfg = host_cfg if isinstance(host_cfg, dict) else {}
        capture_cfg = host_cfg.get("capture")
        capture_cfg = capture_cfg if isinstance(capture_cfg, dict) else {}
        codec_cfg = host_cfg.get("codec")
        codec_cfg = codec_cfg if isinstance(codec_cfg, dict) else {}
        perf_cfg = host_cfg.get("perf")
        perf_cfg = perf_cfg if isinstance(perf_cfg, dict) else {}
        still_cfg = perf_cfg.get("still")
        still_cfg = still_cfg if isinstance(still_cfg, dict) else {}
        # 第 100 条 / B5（2026-09-11）：配置数值一律走安全转换。原实现用裸 int()/float()，
        # 配置里出现字符串数字（"fps": "30"、"monitor": "1"）就在 __init__ 抛
        # ValueError/TypeError，用户看到的是"共享启动失败: invalid literal for int()"这种
        # 与真实问题无关的提示。三端（host/viewer/share）现统一走 common.safe_int/
        # safe_float（C1 下沉），唯独观看端上传路径此前漏了。
        self._backend = capture_cfg.get("backend", "mss")
        self._monitor = safe_int(capture_cfg.get("monitor", 1), 1, 1)
        # region 必须归一（缺键/负值区域会直达 CaptureManager.grab，每帧 KeyError 被吞成
        # None → "共享开着但对方永远看不到画面"，且无任何提示）
        self._region = normalize_region(capture_cfg.get("region"))
        self._fps = safe_int(host_cfg.get("fps", 30), 30, 1, 240)
        self._quality = safe_int(host_cfg.get("jpeg_quality", 80), 80, 1, 100)
        self._target_width = safe_int(codec_cfg.get("target_width", 854) or 0, 854, 0)
        self._encoder_sel = codec_cfg.get("encoder", "auto")
        self._bitrate = safe_int(codec_cfg.get("bitrate_kbps", 2500), 2500, 64) * 1000
        # net-050（v2.5）：反馈调档的三个锚点——配置值（回升目标）、下限（不塌穿）、
        # 最近一次"卡"反馈的时刻（回升计时）。_bitrate 本身就是当前码率（bps）。
        self._cfg_bitrate = self._bitrate
        self._min_bitrate = safe_int(codec_cfg.get("min_bitrate_kbps", 400), 400, 64) * 1000
        if self._min_bitrate > self._cfg_bitrate:
            self._min_bitrate = self._cfg_bitrate
        self._fb_stuck_at = 0.0        # 最近一次"卡"反馈时刻（0=还没被投诉过）
        self._fb_changed_at = 0.0      # 最近一次调档时刻（降档冷却用）
        self._keyint = safe_int(codec_cfg.get("keyint", 60), 60, 1, 3600)
        self._preset = codec_cfg.get("preset", "") or ""
        self._still_enabled = bool(still_cfg.get("enabled", False))
        probe_fps = safe_int(still_cfg.get("probe_fps", 5), 5, 1, 240)
        self._probe_interval = 1.0 / probe_fps
        still_frames = safe_int(still_cfg.get("still_frames", 3), 3, 1, 600)
        self._quiet_s = still_frames * self._probe_interval  # 单位秒（第 87 条：旧名 _ms 名不副实）
        self._point_thr = safe_int(still_cfg.get("point_thr", 10), 10, 0, 255)
        self._ratio_thr = safe_float(still_cfg.get("ratio_thr", 0.005), 0.005, 0.0, 1.0)
        self._cap = capture
        self._cap_owned = capture is None
        # 第 110 条（阶段 2）：编码器生命周期与 JPEG 回退改由 pipeline.FrameEncoder
        # 统一提供——与 host.py 共用同一实现（含第 53 条构造失败负缓存退避）。
        self._fe = pipeline.FrameEncoder(
            fps=self._fps, bitrate=self._bitrate,
            keyint=self._keyint, encoder_sel=self._encoder_sel, preset=self._preset,
            target_width=self._target_width, quality=self._quality, name=name,
            on_build=self._on_encoder_built)
        self._thread = None
        # net-045（2026-10-09）：本路上行视频的单调递增帧序号（协议 v2.4）。
        # 每条流一个计数器——序号只在同一路流内比较，跨流共用会把缺口算错。
        self._seq = SeqCounter()
        # B8：会话自行终止的原因。Channel 检测到 not alive 时据此在状态栏说明原因，
        # 而不是让用户以为"还在共享"（原实现既不通知对端也不通知本端 UI）。
        self.exit_reason = None

    # ---------- 生命周期 ----------

    def start(self):
        """启动上传线程（幂等）。"""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self.exit_reason = None
        self._thread = threading.Thread(
            target=self._run, name="share-upload", daemon=True)
        self._thread.start()
        return self

    def signal_stop(self):
        """第 60 条：仅置停止标志、不 join（非阻塞）。

        退出时先对所有频道并发 signal_stop，让各上传线程同时开始收尾，再逐个 join；
        否则 stop() 的 join(2.0) 串行执行，N 个共享频道最多冻结主线程 2N 秒（「关不掉」）。
        """
        self._stop.set()

    def stop(self):
        """停止上传线程并释放采集/编码资源（幂等，最多等 2 秒）。

        codec-009（2026-09-18）：join 超时（线程仍活着）时**保留** `_thread` 引用——
        旧实现先抹引用再 join，提前返回后 `start()` 的"已有活线程直接复用"承诺失效：
        可再起一条上传线程与旧线程共用同一个非线程安全 FrameEncoder。
        """
        self.signal_stop()
        th = self._thread
        if th is not None:
            th.join(timeout=2.0)
            if th.is_alive():
                # 第 97 条：线程仍阻塞在 sendall（超时 5s > join 2s）时不在此处关闭
                # 采集/编码资源，避免在运行中的线程脚下抽掉 cap/encoder；
                # 资源由线程自身 finally（_run 末尾）释放。
                return
        self._thread = None
        self._close_resources()

    def request_keyframe(self):
        """线程安全：请求尽快补一帧可独立解码的帧（host 转达的 req_keyframe）。"""
        self._key_evt.set()

    def apply_feedback(self, fb):
        """按观看端反馈下调上行码率；返回调整后的码率（Kbps），未调整返回 None。

        net-050（v2.5）：这一端是**真正持有编码器**的那一方，共享端改不了它的码率，
        所以"某个观看端看这条流很卡"必须落到这里才有用。

        只在反馈判定为"卡"时下调（`common.feedback_is_stuck`）；降档有 10 秒冷却——
        共享端每 2 秒转达一次"卡"，不设冷却的话码率会在十几秒内一路塌到下限。
        下限取配置的 `codec.min_bitrate_kbps`。

        线程安全：`_bitrate` 是一个数字的读写（CPython 下不会撕裂），上传线程下一帧
        就会用上新的值；不为此加锁——加锁反而把"接收线程等上传线程"变成一种新的耦合。
        """
        if not feedback_is_stuck(fb):
            return None
        now = time.monotonic()
        self._fb_stuck_at = now
        if now - self._fb_changed_at < _FB_DROP_COOLDOWN_S:
            return None
        new_b = max(self._min_bitrate, int(self._bitrate * _FB_DROP_STEP))
        if new_b >= self._bitrate:
            return None          # 已在下限，无可再降
        self._bitrate = new_b
        self._fb_changed_at = now
        log.info("频道[%s] 观看端反馈画面卡，上行码率降至 %d Kbps",
                 self._name, new_b // 1000)
        return new_b // 1000

    def _maybe_restore_bitrate(self, now=None):
        """久未收到"卡"的反馈 → 把码率往配置值回升一档；返回新码率（Kbps）或 None。

        为什么由这一端自己计时、而不是让共享端下发"可以升了"：共享端只转达"卡"的
        反馈（不卡的不转——否则同一条流上"一个卡一个不卡"的两份反馈会互相打架），
        要它再发一条"可以升了"就得再引入一套跨端状态同步；而这里的判据是纯本地的
        （多久没被投诉），自己算最简单也最不容易不同步。
        """
        now = time.monotonic() if now is None else now
        if not self._fb_stuck_at:
            return None
        if now - self._fb_stuck_at < _FB_RESTORE_S:
            return None
        if self._bitrate >= self._cfg_bitrate:
            self._fb_stuck_at = 0.0    # 已回到配置值，投诉记录清空
            return None
        new_b = min(self._cfg_bitrate, int(self._bitrate * _FB_RESTORE_STEP))
        self._bitrate = new_b
        self._fb_stuck_at = now       # 继续计时：下一档再等一个周期
        log.info("频道[%s] 观看端反馈已恢复，上行码率回升至 %d Kbps",
                 self._name, new_b // 1000)
        return new_b // 1000

    @property
    def alive(self):
        return self._thread is not None and self._thread.is_alive()

    # ---------- 主循环 ----------

    def _run(self):
        """上传主循环：采集（含恢复）→ 静止门控 → 编码 → 发送。

        第 111 条（阶段 2）：采集句柄的故障恢复与静止检测闸门改由
        pipeline.CaptureSource / pipeline.StillGate 统一提供（与 host.py 共用同一实现）。
        差异只在参数：注入的伪采集器不重建（owned=False）。
        capture-009（2026-09-24）：空帧重建阈值原先按**帧数**计（`max(10, fps*5)`），
        而空帧轮询速率固定 ~100Hz、与 fps 解耦 → 静止屏幕下每约 1.5 秒重建一次
        DXGI 采集器（release+create 抖动、每 1.5s 一条 WARNING，且每次重建后首个
        grab() 必出一帧 → 静止画面仍持续编码上传）。host 侧用的是时间判据，现与之对齐。
        """
        src = pipeline.CaptureSource(
            self._backend, self._monitor, self._region, capture=self._cap,
            owned=self._cap_owned, name=self._name,
            none_rebuild_s=5.0,
            sleep_on_none=min(0.01, 1.0 / self._fps))
        self._src = src
        # 第 5 条：采集器构造可能"成功"但无任何可用后端（dxgi 与 mss 都挂时句柄置 None），
        # 此后 grab() 恒返回 None，会话空转（每秒约 100 次无效采集且什么都不上传）。
        # 直接报错停止，由 Channel 层提示用户，而不是静默假活。
        if not src.working():
            log.error("频道[%s] 共享采集器无可用后端（dxgi/mss 均失败），已停止", self._name)
            self.exit_reason = "采集后端不可用（dxgi/mss 均失败）"
            self._close_resources()
            return
        interval = 1.0 / self._fps
        gate = (pipeline.StillGate(self._point_thr, self._ratio_thr, self._quiet_s)
                if self._still_enabled else None)
        last_frame = None   # 最近一次成功发送的缩放后 BGR（静止补帧/强制 IDR 用）
        last_packed = None  # 最近一次发送的完整消息字节（JPEG 补帧用）
        last_is_video = False
        try:
            while not self._stop.is_set():
                t0 = time.perf_counter()
                # net-050（v2.5）：久未收到"卡"的反馈 → 码率往配置值回升一档。
                # 放在循环头部：恢复是"没有消息时"发生的事，不能等下一条反馈才想起来。
                self._maybe_restore_bitrate()
                ts_us = int(time.time() * 1_000_000)
                # 第 3 条：不在此处清除 key_evt。只有真正补出关键帧/发送成功后才清除，
                # 避免 host 转达的唯一一次请求在 bgr=None/采集异常/静止无缓存路径上被丢掉。
                key_wanted = self._key_evt.is_set()
                bgr = src.grab()   # 恢复/重建/等待已在 CaptureSource 内部完成
                if bgr is None:
                    # 采集空帧（dxgi 无新帧）：响应关键帧请求后短等再巡；成功才清事件
                    if key_wanted and last_packed is not None:
                        if self._send_key_response(last_frame, last_packed,
                                                   last_is_video):
                            self._key_evt.clear()   # codec-008：仅在真送出后消费
                    continue
                changed = gate.check(bgr) if gate is not None else True
                if changed:
                    res = self._encode_send(bgr, ts_us, force_key=key_wanted)
                    if res is not None:
                        last_frame, last_packed, last_is_video = res
                        if key_wanted:
                            self._key_evt.clear()  # 第 3 条：已成功补帧，消费请求
                    elapsed = time.perf_counter() - t0
                    if interval - elapsed > 0:
                        time.sleep(interval - elapsed)
                    continue
                # 无有效变化：静止时按探测间隔巡检；请求关键帧则补帧响应（成功才清事件）
                if key_wanted and last_packed is not None:
                    if self._send_key_response(last_frame, last_packed, last_is_video):
                        self._key_evt.clear()   # codec-008：仅在真送出后消费
                wait = self._probe_interval if gate.in_still else interval
                elapsed = time.perf_counter() - t0
                if wait - elapsed > 0:
                    time.sleep(wait - elapsed)
        finally:
            # B8：非主动停止（异常/采集链路失效）时留下原因，供 Channel 上报 UI 与对端
            if self.exit_reason is None and not self._stop.is_set():
                self.exit_reason = "上传线程意外结束（采集或编码链路失效）"
            self._close_resources()

    def _close_resources(self):
        """释放本会话持有的采集与编码资源（幂等；可由线程自身或 stop() 调用）。"""
        fe = getattr(self, "_fe", None)
        if fe is not None:
            fe.close()
        src = getattr(self, "_src", None)
        if src is not None:
            # 注入的伪采集器也需关闭：stop() 语义是释放本会话持有的所有采集资源
            src.close()
            self._src = None
            self._cap = None
        elif self._cap is not None:
            try:
                self._cap.close()
            except Exception:
                pass
            self._cap = None

    # ---------- 编码与发送 ----------

    def _on_encoder_built(self, enc, w, h, bitrate):
        """编码器建成回调（pipeline.FrameEncoder 调用）。"""
        log.info("频道[%s] 共享编码器: %s（%dx%d，%d Kbps）",
                 self._name, enc.name, w, h, bitrate // 1000)

    def _encode_send(self, bgr, ts_us, force_key=False):
        """缩放→编码→发送一帧；返回 (缩放后BGR, 完整消息字节, 是否视频) 或 None。

        编码/回退逻辑在 pipeline.FrameEncoder（第 110 条，与 host 共用）；本方法只负责
        "帧去哪"——打包协议消息并经 _tx_lock 发送。
        """
        ef = self._fe.encode(bgr, ts_us, force_key=force_key, scale=1.0,
                             quality=self._quality, fps=self._fps,
                             bitrate=self._bitrate)
        if ef is None:
            return None
        if ef.is_video:
            # getattr 兜底：测试里大量 `ScreenShareSession.__new__(...)` 绕过 __init__
            # 造实例（仓库既有约定），缺 _seq 时按 v2.3 无序号布局出包——本就向后兼容，
            # 好过 AttributeError 打断采集线程。
            _seq = getattr(self, "_seq", None)
            msg = pack_video(ef.data, ef.ts, ef.is_key, codec=ef.codec_id,
                             seq=_seq.next() if _seq is not None else None)
        else:
            msg = pack_frame(ef.data, ts_us)
        if not self._send_msg(msg):
            return None
        return ef.frame, msg, ef.is_video

    def _send_key_response(self, last_frame, last_packed, last_is_video):
        """静止/空闲期关键帧请求响应：视频=强制 IDR；JPEG=重发缓存帧。

        返回是否**真的送出了**关键帧/缓存帧（codec-008，2026-09-18）：旧实现四条
        失败路径（无缓存帧 / 强制 IDR 异常 / 无输出 / 发送失败）不给调用方任何信号，
        而调用方无条件清除 key_evt——第 3 条"请求只在成功后消费"被回退成"发失败也丢"。
        """
        if last_packed is None:
            return False
        if last_is_video and last_frame is not None:
            res, err = self._fe.force_idr(last_frame)
            if err is not None:
                log.warning("频道[%s] 静止期强制 IDR 失败: %s", self._name, err)
                return False
            if res is None:
                # codec-011（2026-09-20）：这里旧实现直接 `return False`，走不到函数
                # 末尾"重发缓存帧"的分支。而 `force_idr` 返回 (None, None) 的可达形态
                # 正是 `not self._encoder.available`（编码器 permafailed，且因为
                # _encode_send 在采集恒 None 时不再被调用，它永不被丢弃）——默认
                # dxgi 后端 + 静止检测关闭时画面静止期间 grab() 恒返回 None，就是常态。
                # 结果：新接入的观众永远等不到 IDR，画面保持黑屏，直到共享端画面发生
                # 变化；调用方按 codec-008 保留了 key_evt 空转重试，所以不丢请求，
                # 只是永远等不到响应。缩放后的缓存帧其实在手，交给 JPEG 回退出帧。
                if last_frame is None:
                    return False
                # codec-018（2026-09-24）：兜底必须**只**走 JPEG 出口——旧实现调
                # _encode_send(force_key=False) 会再次尝试视频编码：libx265 4 帧流水线
                # pre-roll 窗口里 force_idr 刚返回 None，紧接着这一次却可能出包，产出
                # P 帧也算"成功"，调用方随即清掉 key_evt——新观众拿到无参考链的 P 帧，
                # 静止画面下仍黑到下一次真实 IDR，且请求已被消费不再重试。与本函数
                # 下方"非关键帧不发、保留请求重试"是同一条不变量。
                return self._send_jpeg_cached(last_frame)
            nal, is_key, _ms, out_ts = res
            if not is_key:
                # codec-004 同款：强制 IDR 未生效（编码器返回 P 帧）时不发，保留请求重试
                return False
            _seq = getattr(self, "_seq", None)
            if self._send_msg(pack_video(nal, out_ts, True,
                                         codec=self._fe.encoder.codec_id,
                                         seq=_seq.next() if _seq is not None else None)):
                log.info("频道[%s] 静止期响应关键帧请求（%d 字节）",
                         self._name, len(nal))
                return True
            return False
        return bool(self._send_msg(last_packed))  # JPEG：重发自包含缓存帧

    def _send_jpeg_cached(self, frame):
        """把缓存帧编码成 JPEG 发送（自包含、可独立解码）；失败返回 False。

        codec-018：静止期关键帧请求的**唯一**兜底出口——不得走 _encode_send 的
        视频优先路径（那里产出 P 帧也会算成功）。编码异常与发送失败均返回 False，
        由调用方保留请求重试（第 3 条 / codec-008 的成功才消费语义）。
        """
        if frame is None:
            return False
        try:
            jpeg, _ms = pipeline.encode_bgr(frame, 1.0, self._quality, 0)
        except Exception as e:
            log.warning("频道[%s] 缓存帧 JPEG 回退编码失败: %s", self._name, e)
            return False
        return bool(self._send_msg(pack_frame(jpeg, int(time.time() * 1_000_000))))

    def _send_msg(self, msg):
        """经共享写锁发送完整消息；失败置停止位（连接已断由 Channel 层善后）。

        第 96 条：sendall 必须有界，否则死连接上会无限阻塞、拖垮 stop() 的 join
        （见第 97 条）；但本 socket 同时被接收线程 select/recv 复用，viewer 已特意
        置回阻塞（None，viewer.py:294）。旧实现每帧 settimeout(5.0) 且从不还原 →
        跨线程未同步地永久改掉共享 socket 的超时不变量（_stop_share 后仍停在 5.0）。
        改为：在写锁内保存原超时、仅为本次 sendall 临时设 5.0、finally 还原。超时只
        在 sendall 期间生效，发送之间恢复阻塞；接收线程 select 门控 recv 不受影响，
        其它发送方（ping/req_keyframe）也回到 viewer 设定的阻塞基线。
        """
        try:
            with self._tx:
                prev = self._sock.gettimeout()
                try:
                    self._sock.settimeout(5.0)
                    self._sock.sendall(msg)
                finally:
                    self._sock.settimeout(prev)
            return True
        except OSError as e:
            log.warning("频道[%s] 共享发送失败，已停止: %s", self._name, e)
            self.exit_reason = "发送失败（连接已断）: %s" % e
            self._stop.set()
            return False
