# -*- coding: utf-8 -*-
"""观看端共享上传会话（观看组 MultiView 成员端）。

ScreenShareSession 在独立线程内完成 采集→静止门控→编码→发送：
- 采集/静止门控复用 screen.py（与 host 共享端同构），参数读 cfg["host"]
- 视频编码优先（host.codec.encoder），不可用自动回退 JPEG（与 host 一致）
- 静止期收到 req_keyframe 时补一帧：视频路径强制 IDR（重建编码器后首帧），
  JPEG 路径重发缓存帧（自包含）；保证新观众订阅静止画面可立即起解
- 所有发送经调用方传入的 _tx_lock，与 ping/watch 等控制消息串行，防字节交错
- 发送失败即停止（记日志），由 Channel 层负责提示与重连后的自动重启
"""

import threading
import time

import cv2

import logger
import codec as codec_mod
from common import pack_frame, pack_video
from screen import (CaptureManager, downsample_frame, motion_changed,
                    still_gate_update, calc_output_size, encode_bgr)

log = logger.get_logger()


class ScreenShareSession:
    """上传器：构造后 start()/stop()。capture 可注入伪采集器（测试）。"""

    def __init__(self, sock, tx_lock, cfg, name, capture=None):
        self._sock = sock
        self._tx = tx_lock
        self._name = name
        self._stop = threading.Event()
        self._key_evt = threading.Event()
        host_cfg = ((cfg or {}).get("host", {})) if isinstance(cfg, dict) else {}
        capture_cfg = host_cfg.get("capture", {}) or {}
        codec_cfg = host_cfg.get("codec", {}) or {}
        still_cfg = ((host_cfg.get("perf", {}) or {}).get("still", {})) or {}
        self._backend = capture_cfg.get("backend", "mss")
        self._monitor = int(capture_cfg.get("monitor", 1))
        self._region = capture_cfg.get("region")
        self._fps = max(1, int(host_cfg.get("fps", 30)))
        self._quality = int(host_cfg.get("jpeg_quality", 80))
        self._target_width = int(codec_cfg.get("target_width", 854) or 0)
        self._encoder_sel = codec_cfg.get("encoder", "auto")
        self._bitrate = int(codec_cfg.get("bitrate_kbps", 2500)) * 1000
        self._keyint = max(1, int(codec_cfg.get("keyint", 60)))
        self._preset = codec_cfg.get("preset", "") or ""
        self._still_enabled = bool(still_cfg.get("enabled", False))
        probe_fps = max(1, int(still_cfg.get("probe_fps", 5)))
        self._probe_interval = 1.0 / probe_fps
        still_frames = max(1, int(still_cfg.get("still_frames", 3)))
        self._quiet_ms = still_frames * self._probe_interval
        self._point_thr = int(still_cfg.get("point_thr", 10))
        self._ratio_thr = float(still_cfg.get("ratio_thr", 0.005))
        self._cap = capture
        self._cap_owned = capture is None
        self._encoder = None
        self._thread = None

    # ---------- 生命周期 ----------

    def start(self):
        """启动上传线程（幂等）。"""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="share-upload", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        """停止上传线程并释放采集/编码资源（幂等，最多等 2 秒）。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._close_resources()

    def request_keyframe(self):
        """线程安全：请求尽快补一帧可独立解码的帧（host 转达的 req_keyframe）。"""
        self._key_evt.set()

    @property
    def alive(self):
        return self._thread is not None and self._thread.is_alive()

    # ---------- 主循环 ----------

    def _run(self):
        if self._cap is None:
            try:
                self._cap = CaptureManager(self._backend, self._monitor, self._region)
            except Exception as e:
                log.error("频道[%s] 共享采集初始化失败，已停止: %s", self._name, e)
                return
        interval = 1.0 / self._fps
        still_ref = None
        quiet_since = None
        in_still = False
        consecutive_errors = 0
        last_frame = None   # 最近一次成功发送的缩放后 BGR（静止补帧/强制 IDR 用）
        last_packed = None  # 最近一次发送的完整消息字节（JPEG 补帧用）
        last_is_video = False
        try:
            while not self._stop.is_set():
                t0 = time.perf_counter()
                ts_us = int(time.time() * 1_000_000)
                key_wanted = self._key_evt.is_set()
                if key_wanted:
                    self._key_evt.clear()
                try:
                    bgr = self._cap.grab()
                except Exception as e:
                    consecutive_errors += 1
                    log.warning("频道[%s] 共享采集错误: %s（连续 %d 次）",
                                self._name, e, consecutive_errors)
                    if consecutive_errors >= 5 and self._cap_owned:
                        try:
                            self._cap.close()
                        except Exception:
                            pass
                        try:
                            self._cap = CaptureManager(
                                self._backend, self._monitor, self._region)
                            log.info("频道[%s] 共享采集器已重建", self._name)
                        except Exception as e2:
                            log.error("频道[%s] 共享采集器重建失败: %s", self._name, e2)
                        consecutive_errors = 0
                    time.sleep(0.5)
                    continue
                if bgr is None:
                    if consecutive_errors > 0:
                        consecutive_errors = 0
                    # 采集空帧（dxgi 无新帧）：响应关键帧请求后短等再巡
                    if key_wanted and last_packed is not None:
                        self._send_key_response(last_frame, last_packed, last_is_video)
                    time.sleep(min(0.01, interval))
                    continue
                changed = True
                if self._still_enabled:
                    try:
                        changed, still_ref = motion_changed(
                            still_ref, downsample_frame(bgr),
                            self._point_thr, self._ratio_thr)
                    except Exception:
                        # 探测异常按“有变化”处理：宁可多发一帧，不可画面冻结
                        changed, still_ref = True, downsample_frame(bgr)
                    in_still, quiet_since = still_gate_update(
                        in_still, quiet_since, changed,
                        time.perf_counter(), self._quiet_ms)
                if changed:
                    if consecutive_errors > 0:
                        consecutive_errors = 0
                    res = self._encode_send(bgr, ts_us, force_key=key_wanted)
                    if res is not None:
                        last_frame, last_packed, last_is_video = res
                    elapsed = time.perf_counter() - t0
                    if interval - elapsed > 0:
                        time.sleep(interval - elapsed)
                    continue
                # 无有效变化：静止时按探测间隔巡检；请求关键帧则补帧响应
                if key_wanted and last_packed is not None:
                    self._send_key_response(last_frame, last_packed, last_is_video)
                wait = self._probe_interval if in_still else interval
                elapsed = time.perf_counter() - t0
                if wait - elapsed > 0:
                    time.sleep(wait - elapsed)
        finally:
            self._close_resources()

    def _close_resources(self):
        if self._encoder is not None:
            try:
                self._encoder.close()
            except Exception:
                pass
            self._encoder = None
        # 注入的伪采集器也需关闭：stop() 语义是释放本会话持有的所有采集资源
        if self._cap is not None:
            try:
                self._cap.close()
            except Exception:
                pass
            self._cap = None

    # ---------- 编码与发送 ----------

    def _encode_send(self, bgr, ts_us, force_key=False):
        """缩放→编码→发送一帧；返回 (缩放后BGR, 完整消息字节, 是否视频) 或 None。"""
        w, h = calc_output_size(bgr.shape[1], bgr.shape[0], 1.0, self._target_width)
        if self._encoder is not None and not self._encoder.available:
            try:
                self._encoder.close()
            except Exception:
                pass
            self._encoder = None
        if self._encoder is None and self._encoder_sel != "jpeg":
            enc = codec_mod.VideoEncoder(
                w, h, self._fps, self._bitrate, self._keyint,
                self._encoder_sel, self._preset)
            if enc.available:
                self._encoder = enc
                log.info("频道[%s] 共享编码器: %s（%dx%d，%d Kbps）",
                         self._name, enc.name, w, h, self._bitrate // 1000)
        frame = bgr
        if (w, h) != (bgr.shape[1], bgr.shape[0]):
            frame = cv2.resize(bgr, (w, h), interpolation=cv2.INTER_AREA)
        if self._encoder is not None:
            res = self._encoder.encode(frame, raw_ts=ts_us, force_key=force_key)
            if res is None:
                return None
            nal, is_key, _ms, out_ts = res
            msg = pack_video(nal, out_ts, is_key, codec=self._encoder.codec_id)
            if not self._send_msg(msg):
                return None
            return frame, msg, True
        jpeg, _ms = encode_bgr(bgr, 1.0, self._quality, self._target_width)
        msg = pack_frame(jpeg, ts_us)
        if not self._send_msg(msg):
            return None
        return frame, msg, False

    def _send_key_response(self, last_frame, last_packed, last_is_video):
        """静止/空闲期关键帧请求响应：视频=强制 IDR；JPEG=重发缓存帧。"""
        if last_packed is None:
            return
        if (last_is_video and self._encoder is not None
                and self._encoder.available and last_frame is not None):
            key_ts = int(time.time() * 1_000_000)
            try:
                res = self._encoder.encode(last_frame, raw_ts=key_ts, force_key=True)
            except Exception as e:
                log.warning("频道[%s] 静止期强制 IDR 失败: %s", self._name, e)
                return
            if res is None:
                return
            nal, is_key, _ms, out_ts = res
            if not is_key:
                return
            if self._send_msg(pack_video(nal, out_ts, True,
                                         codec=self._encoder.codec_id)):
                log.info("频道[%s] 静止期响应关键帧请求（%d 字节）",
                         self._name, len(nal))
            return
        self._send_msg(last_packed)  # JPEG：重发自包含缓存帧

    def _send_msg(self, msg):
        """经共享写锁发送完整消息；失败置停止位（连接已断由 Channel 层善后）。"""
        try:
            with self._tx:
                self._sock.settimeout(5.0)
                self._sock.sendall(msg)
            return True
        except OSError as e:
            log.warning("频道[%s] 共享发送失败，已停止: %s", self._name, e)
            self._stop.set()
            return False
