# -*- coding: utf-8 -*-
"""FPS 画面共享主机端：屏幕采集（mss/dxgi）+ H.264 视频编码（NVENC 硬件/x264 回退）+
TCP 广播 + 樱花 frpc 集成 + 图形控制台。

第七轮优化（pro-video-codec）：
- 用 PyAV/FFmpeg 做 H.264 视频编码替代逐帧 JPEG：NVENC 硬件（GPU）优先、libx264 软件回退，
  带宽相比 JPEG 降低 5-20 倍；编码器不可用自动回退逐帧 JPEG
- 低延迟编码参数：无 B 帧、NVENC tune=ull/zerolatency 或 x264 tune=zerolatency、CBR 码率
- 码率自适应（ABR）：按发送耗时/积压动态调节编码码率（超出预设范围后回落原有的
  缩放→帧率降级），带宽恢复自动回升
- 关键帧请求：观看端请求时强制出 IDR 快速恢复（含 ABR 重建后的参考帧重置）
- 时间戳透传：H.264 帧携带采集时刻，端到端延迟精确测量

依赖：mss、opencv-python（cv2）、numpy；可选 dxcam（host.capture.backend="dxgi"）、
av/PyAV（host.codec.encoder!="jpeg" 时用于 H.264 编码）。
用法示例：python host.py [--port 5700] [--console]
"""

import argparse
import hashlib
import hmac
import json
import os
import queue
import select
import secrets
import socket
import struct
import subprocess
import threading
import time

import cv2
import numpy as np

import logger
import codec as codec_mod

from common import (
    load_config, save_config, server_handshake, pack_frame, pack_video, exe_dir, APP_NAME,
    recv_msg, send_msg, tune_socket, parse_message, MSG_CTRL, MSG_FRAME, MSG_VIDEO,
    VIDEO_FLAG_KEY, CODEC_H264, parse_video,
)

# 采集/静止检测公共件从 screen.py 导入并保持同名（viewer 上传端复用同一实现）
from screen import (
    CaptureManager, STILL_STEP, downsample_frame, motion_changed,
    still_gate_update, calc_output_size, encode_bgr,
)

#: 全局日志器（未初始化时按默认配置惰性初始化）
log = logger.get_logger()


def _sanitize(text, token):
    """把文本中可能出现的 frp token 替换为打码形式，防止敏感信息写入日志。"""
    if not token or not isinstance(text, str):
        return text
    if token in text:
        return text.replace(token, logger.mask_secret(token))
    return text


def _hash_password(password, salt_hex):
    """pbkdf2_hmac(sha256) 加盐哈希，迭代 10 万次，返回十六进制摘要。"""
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return dk.hex()


class AccountManager:
    """服务端账户注册/认证与 accounts.json 持久化（线程安全，不存明文密码）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._path = os.path.join(exe_dir(), "accounts.json")
        self._users = {}  # 用户名 -> {salt, hash, created, last_login}
        self._load()

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._users = data.get("users", {})
        except (OSError, ValueError):
            self._users = {}

    def _save(self):
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"users": self._users}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def register(self, user, password):
        """注册新账户，返回 (ok: bool, 提示语)。"""
        user = (user or "").strip()
        if not user or not password:
            return False, "用户名与密码不能为空"
        with self._lock:
            if user in self._users:
                return False, "用户名已存在"
            salt = secrets.token_hex(16)
            self._users[user] = {
                "salt": salt,
                "hash": _hash_password(password, salt),
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._save()
        log.info("账户注册成功: %s", user)
        return True, "注册成功"

    def authenticate(self, user, password):
        """认证账户，返回 (ok: bool, 提示语)；账号不存在与密码错误给出不同提示。"""
        user = (user or "").strip()
        with self._lock:
            record = self._users.get(user)
            if record is None:
                return False, "账号不存在"
            if not hmac.compare_digest(record["hash"], _hash_password(password, record["salt"])):
                return False, "用户名或密码错误"
            record["last_login"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._save()
        log.info("账户登录成功: %s", user)
        return True, "登录成功"


class ClientInfo:
    """单个观看端连接的信息：地址、登录用户名、连接时刻、累计发送字节与 1 秒滑动窗口速率。

    每个客户端都配有独立发送队列与发送线程（“只保留最新一帧”）。这样某个慢客户端
    只影响它自己的发送进度，不会阻塞其他观看端接收画面。
    """

    def __init__(self, addr, username=None):
        self.addr = addr
        self.username = username
        self.connected_at = time.monotonic()
        self.bytes_sent = 0
        self._window = []  # [(monotonic, bytes)]
        self._lock = threading.Lock()
        self.last_rx_at = time.monotonic()  # 最近一次收到客户端数据时刻（keepalive 判定）
        self.rtt_ms = 0.0  # 最近一次应用层 ping/pong 往返时延（毫秒，0=未知）

        # 独立发送通道：容量为 1，只保留最新帧；网络慢时自动丢弃中间帧
        self.send_queue = queue.Queue(maxsize=1)
        self.dropped_frames = 0
        self.last_drop_at = 0.0
        self.last_send_ms = 0.0
        self.last_send_at = 0.0
        self._stop = threading.Event()
        self._sender_active = False
        # MultiView 订阅状态：watch_source 默认 local（旧 viewer 语义不变）
        self.watch_source = "local"  # "local" 或共享成员的 "peer:<n>"
        self.need_key = True         # 视频订阅者是否在等关键帧（新连接/切换源后为 True）
        self.share_id = None         # 登记为共享成员后的 "peer:<n>"；None=未共享
        # 本连接全部 socket 写入（发送线程帧 + 各线程控制消息）共用写锁，
        # 防止多线程 sendall 字节交错破坏帧协议（新增跨连接转发后成为必要）
        self._write_lock = threading.Lock()

    def note_rx(self, now=None):
        """记录一次客户端活跃（收到了它的任何数据，用于半开连接检测）。"""
        self.last_rx_at = now if now is not None else time.monotonic()

    def record_rtt(self, rtt_ms):
        """记录一次应用层 ping/pong 往返时延（毫秒）。"""
        with self._lock:
            self.rtt_ms = rtt_ms

    def send_ctrl(self, sock, obj):
        """发送一条控制消息（与帧发送共用写锁，防字节交错）。"""
        with self._write_lock:
            send_msg(sock, obj)

    def enqueue_frame(self, frame):
        """把最新一帧放入该客户端的发送队列，旧帧直接丢弃，绝不阻塞广播线程。"""
        try:
            self.send_queue.get_nowait()
            now = time.monotonic()
            with self._lock:
                self.dropped_frames += 1
                self.last_drop_at = now
        except queue.Empty:
            pass
        try:
            self.send_queue.put_nowait(frame)
        except queue.Full:
            # 极端竞态下仍不阻塞，直接丢弃本帧即可（下一帧会替换）
            pass

    def record_sent(self, nbytes, now=None, send_ms=0.0):
        now = now if now is not None else time.monotonic()
        with self._lock:
            self.bytes_sent += nbytes
            self._window.append((now, nbytes))
            cutoff = now - 1.0
            while self._window and self._window[0][0] <= cutoff:
                self._window.pop(0)
            if send_ms > 0:
                self.last_send_ms = send_ms
                self.last_send_at = now

    def rate_kbps(self, now=None):
        """最近 1 秒平均发送速率（KB/s）。"""
        now = now if now is not None else time.monotonic()
        cutoff = now - 1.0
        total = 0.0
        with self._lock:
            for t, n in self._window:
                if t > cutoff:
                    total += n
        return total / 1024.0  # 窗口约 1 秒

    def has_recent_drop(self, now=None):
        """最近 1 秒内是否发生过因发送队列满而丢帧。"""
        now = now if now is not None else time.monotonic()
        with self._lock:
            return self.last_drop_at > 0 and (now - self.last_drop_at) < 1.0

    def stop_sender(self):
        """通知发送线程退出。"""
        self._stop.set()

    @property
    def send_stop(self):
        return self._stop


def _client_sender_loop(sock, info, runtime, send_timeout=2.0):
    """单个客户端的独立发送线程：只发送属于该客户端的最新帧。

    使用容量为 1 的队列，发送慢时自动丢弃中间帧；任一客户端发送失败只移除
    该客户端，不会阻塞/拖累其他观看端。
    """
    try:
        while not info.send_stop.is_set() and not runtime.stop_event.is_set():
            try:
                frame = info.send_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                sock.settimeout(send_timeout)
                start = time.perf_counter()
                with info._write_lock:
                    sock.sendall(frame)
                info.record_sent(
                    len(frame),
                    send_ms=(time.perf_counter() - start) * 1000.0,
                )
            except OSError:
                _drop_client(runtime, sock, info, "发送失败")
                return
    finally:
        info._sender_active = False


def _drop_client(runtime, sock, info, reason="断开"):
    """把指定客户端从广播列表移除并关闭连接（幂等）。"""
    with runtime.clients_lock:
        removed = runtime.clients.pop(sock, None)
        remaining = len(runtime.clients)
    if removed is None:
        return False
    if getattr(removed, "share_id", None) is not None:
        runtime._unregister_sharer(removed)
    stop_sender = getattr(removed, "stop_sender", None)
    if callable(stop_sender):
        stop_sender()
    try:
        sock.close()
    except OSError:
        pass
    if hasattr(removed, "_lock"):
        with removed._lock:
            total_mb = removed.bytes_sent / (1024.0 * 1024.0)
    else:
        total_mb = getattr(removed, "bytes_sent", 0) / (1024.0 * 1024.0)
    log.info("客户端 %s %s已移除（累计发送 %.1f MB，剩余 %d 个客户端）",
             removed.addr, reason, total_mb, remaining)
    return True



class FrpManager:
    """樱花 frpc 子进程生命周期管理器：start/stop/is_running，供 GUI 随时启停。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self._proc = None
        self._external_running = False  # 由 SakuraFrpService/Launcher 托管时不再重复拉起 frpc
        self._lock = threading.Lock()

    @staticmethod
    def _sakura_service_running():
        """检测本机是否已运行 SakuraFrpService（樱花启动器守护进程）。"""
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq SakuraFrpService.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return "SakuraFrpService.exe" in (result.stdout or "")
        except Exception:
            return False

    @staticmethod
    def _resolve_sakura_credentials(frp_token, frp_tunnels):
        """从 SakuraFrpLauncher/SakuraFrpService 配置读取访问密钥与隧道 ID。

        程序优先使用 config.json 中用户填写的值；为空时自动读取本机已安装的
        SakuraFrp 服务配置，避免用户重复输入 token/隧道 ID。
        """
        token = frp_token or ""
        tunnel_ids = frp_tunnels or ""
        if token and tunnel_ids:
            return token, tunnel_ids

        candidates = []
        program_data = os.environ.get("ProgramData")
        if program_data:
            candidates.append(os.path.join(program_data, "SakuraFrpService", "config.json"))
        for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "ProgramW6432"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(os.path.join(base, "SakuraFrpLauncher", "config.json"))

        for path in candidates:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            if not token:
                token = str(data.get("token") or "").strip()
            if not tunnel_ids:
                ids = data.get("auto_start_tunnels") or []
                id_list = []
                for item in ids:
                    if isinstance(item, bool):
                        continue
                    if isinstance(item, (int, float)):
                        id_list.append(str(int(item)))
                    elif isinstance(item, str) and item.strip().isdigit():
                        id_list.append(item.strip())
                tunnel_ids = ",".join(id_list)
            if token and tunnel_ids:
                break
        return token, tunnel_ids

    def start(self):
        """按当前配置拉起 frpc；成功返回 (True, 提示)，失败返回 (False, 原因)。"""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return True, "frpc 已在运行"
            # 如果用户已经通过 SakuraFrpLauncher/Service 管理隧道，则不重复启动 frpc
            if self._sakura_service_running():
                self._external_running = True
                return True, "已检测到 SakuraFrpService 正在运行，隧道由樱花启动器管理，无需重复启动"
            frp = self.cfg.get("host", {}).get("frp", {})
            frpc_path = frp.get("frpc_path", "frpc.exe")
            token, tunnel_ids = self._resolve_sakura_credentials(
                frp.get("token", ""), frp.get("tunnel_ids", ""))
            if not token or not tunnel_ids:
                return False, "host.frp 缺少 token 或 tunnel_ids，请先在 frpc 区填写（无法从 SakuraFrpLauncher 自动读取时）"
            if not os.path.isabs(frpc_path):
                candidate = os.path.join(exe_dir(), frpc_path)
                if os.path.exists(candidate):
                    frpc_path = candidate
            # Sakura frp 官方下载的 Windows 客户端默认名为 frpc_windows_amd64.exe，
            # 也兼容手动改名为 frpc.exe 的场景
            if not os.path.exists(frpc_path):
                search_dirs = []
                raw_dir = os.path.dirname(frpc_path)
                if raw_dir:
                    search_dirs.append(raw_dir if os.path.isabs(raw_dir) else os.path.join(exe_dir(), raw_dir))
                search_dirs.append(exe_dir())
                # SakuraFrpLauncher 的默认安装目录（用户常见安装位置）
                for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "ProgramW6432"):
                    base = os.environ.get(env_name)
                    if base:
                        search_dirs.append(os.path.join(base, "SakuraFrpLauncher"))
                for search_dir in search_dirs:
                    for alt_name in ("frpc.exe", "frpc_windows_amd64.exe", "frpc_windows_386.exe"):
                        alt = os.path.join(search_dir, alt_name)
                        if os.path.exists(alt):
                            frpc_path = alt
                            break
                    if os.path.exists(frpc_path):
                        break
            if not os.path.exists(frpc_path):
                return False, "未找到 frpc 程序: %s（请将 Sakura frp 客户端放至 exe 同目录，或填写 frpc_path）" % frpc_path
            try:
                proc = subprocess.Popen(
                    [frpc_path, "-f", "%s:%s" % (token, tunnel_ids)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            except Exception as e:
                return False, "启动 frpc 失败: %s" % _sanitize(str(e), token)
            self._proc = proc
            threading.Thread(target=self._read_output, args=(proc, token), daemon=True).start()
            return True, "frpc 已启动（隧道: %s）" % tunnel_ids

    @staticmethod
    def _read_output(proc, token):
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                log.info("frpc: %s", _sanitize(line, token))
        except Exception as e:
            log.debug("frpc 输出读取结束: %s", _sanitize(str(e), token))

    def stop(self):
        with self._lock:
            external = self._external_running
            proc, self._proc = self._proc, None
        if external:
            log.info("SakuraFrpService 正在管理隧道，未停止外部樱花服务")
            return "樱花启动器正在托管隧道，未停止外部服务"
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
            log.info("frpc 已终止")
            return "frpc 已终止"
        return "frpc 未在运行"

    def is_running(self):
        with self._lock:
            if self._external_running:
                return True
            return self._proc is not None and self._proc.poll() is None


class HostRuntime:
    """共享端运行时：共享状态（客户端/性能/采集参数/停止事件）+ frpc 管理。

    GUI 与命令行模式共用；GUI 通过 get_snapshot() 读取状态，通过
    update_capture() 修改采集源（写回配置并即时生效）。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.stop_event = threading.Event()
        self.clients = {}  # sock -> ClientInfo
        self.clients_lock = threading.Lock()
        host = cfg["host"]
        capture = host.get("capture", {})
        self.slot = {
            "jpeg": None,
            "ts": 0,
            "video": None,       # H.264/H.265 Annex-B 帧（视频编码路径）
            "video_ts": 0,       # 该视频帧的采集时刻微秒（packet.pts 透传）
            "is_key": False,     # 视频帧是否关键帧
            "video_codec": CODEC_H264,  # 视频帧编码类型（协议 codec 字节，H.264/HEVC）
            "quality": host["jpeg_quality"],
            "scale": host.get("scale", 1.0),
            "fps": host.get("fps", 60),
            "encode_ms": 0.0,
            "bitrate": 0,        # 当前视频编码码率（bps，0=JPEG 模式）
            "out_w": 0,        # 最近一帧实际编码输出宽度（target_width 压制后）
            "out_h": 0,        # 最近一帧实际编码输出高度
            "monitor": capture.get("monitor", 1),
            "region": capture.get("region"),
            "backend": capture.get("backend", "mss"),
        }
        self.slot_lock = threading.Lock()
        self.force_key = False  # 观看端请求关键帧（slot_lock 保护），编码线程消费后清空
        self.video_encoder = None  # 共享的 VideoEncoder 实例（编码线程创建，perf 线程调码率）
        self.codec_lock = threading.Lock()
        # 当前 ABR 目标码率（bps）：ABR 调整后更新，编码线程重建编码器时沿用，
        # 避免重建回到基础码率导致码率震荡
        self.target_bitrate = int(host.get("codec", {}).get("bitrate_kbps", 6000)) * 1000
        self.stats = {"capture_fps": 0, "encode_fps": 0, "send_fps": 0,
                  "encode_ms": 0.0, "avg_send_ms": 0.0, "worst_rtt_ms": 0.0,
                  "codec_name": "", "bitrate_kbps": 0, "keyint": 0,
                  "keyframe_total": 0, "abr_changes": 0, "still": False}
        self.stats_lock = threading.Lock()
        self.accounts = AccountManager()
        self.frp = FrpManager(cfg)
        self._next_share_id = 0  # 共享成员序号（连接递增分配，断开不复用）
        self._server = None
        self._server_thread = None

    def start_server(self):
        """后台线程启动 TCP 服务与采集/发送流水线。"""
        self._server_thread = threading.Thread(target=run_server, args=(self,), daemon=True)
        self._server_thread.start()

    def shutdown(self):
        self.stop_event.set()
        try:
            if self._server is not None:
                self._server.close()
        except OSError:
            pass
        # 停止所有客户端发送线程并关闭连接，避免退出时残留 daemon 线程占用 socket
        with self.clients_lock:
            clients = list(self.clients.items())
        for sock, info in clients:
            info.stop_sender()
            try:
                sock.close()
            except OSError:
                pass
        self.frp.stop()
        if self._server_thread is not None:
            self._server_thread.join(timeout=3)

    def _register_sharer(self, sock, info):
        """登记共享成员（首个上行帧时调用）：分配 peer:<n> 并广播 roster。"""
        with self.clients_lock:
            if info.share_id is not None:
                return info.share_id
            self._next_share_id += 1
            info.share_id = "peer:%d" % self._next_share_id
        self.broadcast_roster()
        log.info("共享成员 %s 上线：%s（%s）", info.addr, info.share_id,
                 info.username or "-")
        return info.share_id

    def _unregister_sharer(self, info):
        """注销共享成员（连接断开时调用）并广播 roster；返回是否真的有注销。"""
        with self.clients_lock:
            if info.share_id is None:
                return False
            info.share_id = None
        self.broadcast_roster()
        return True

    def broadcast_roster(self):
        """向所有存活客户端推送一次 peers 名单（id/name/addr）。"""
        with self.clients_lock:
            snapshot = list(self.clients.items())
            rows = []
            for _, c in snapshot:
                if not c.share_id:
                    continue
                if isinstance(c.addr, tuple):
                    host, port = c.addr[0], c.addr[1]
                else:
                    host, port = str(c.addr), None
                if host.startswith("::ffff:"):
                    host = host[7:]  # IPv4-mapped 剥前缀，显示真实 IPv4
                addr_txt = host if port is None else "%s:%s" % (host, port)
                rows.append({"id": c.share_id,
                             "name": (c.username or "").strip() or addr_txt,
                             "addr": addr_txt})
        msg = {"action": "peers", "peers": rows}
        for sock, info in snapshot:
            try:
                info.send_ctrl(sock, msg)
            except OSError:
                log.debug("向 %s 推送 roster 失败（连接可能已断）", info.addr)

    def update_capture(self, monitor=None, region=None, backend=None):
        """更新采集源配置并即时生效（写配置 + 写共享槽，采集线程每帧读取）。"""
        capture = self.cfg["host"].setdefault("capture", {})
        if monitor is not None:
            capture["monitor"] = monitor
        if region is not None:
            capture["region"] = region
        if backend is not None:
            capture["backend"] = backend
        save_config(self.cfg)
        with self.slot_lock:
            if monitor is not None:
                self.slot["monitor"] = monitor
            if region is not None:
                self.slot["region"] = region
            if backend is not None:
                self.slot["backend"] = backend

    def get_snapshot(self):
        """返回供 GUI 刷新的状态快照（线程安全）。"""
        with self.clients_lock:
            clients = []
            for info in self.clients.values():
                rate = info.rate_kbps()
                lock = getattr(info, "_lock", None)
                if lock is not None:
                    with lock:
                        total_mb = info.bytes_sent / (1024.0 * 1024.0)
                else:
                    total_mb = getattr(info, "bytes_sent", 0) / (1024.0 * 1024.0)
                clients.append({
                    "addr": info.addr,
                    "username": info.username,
                    "uptime_s": time.monotonic() - info.connected_at,
                    "rate_kbps": rate,
                    "total_mb": total_mb,
                    "rtt_ms": info.rtt_ms,
                    "dropped_frames": info.dropped_frames,
                })
        with self.stats_lock:
            perf = dict(self.stats)
        with self.slot_lock:
            perf["quality"] = self.slot["quality"]
            perf["scale"] = self.slot["scale"]
            perf["fps_now"] = self.slot.get("fps", self.cfg["host"].get("fps", 30))
            perf["bitrate_kbps"] = self.slot.get("bitrate", 0) // 1000
            capture = {
                "monitor": self.slot["monitor"],
                "region": self.slot["region"],
                "backend": self.slot["backend"],
            }
        return {
            "port": self.cfg["host"]["port"],
            "public_addr": self.cfg["host"].get("frp", {}).get("public_addr", ""),
            "auth_enabled": bool(self.cfg["host"].get("auth", {}).get("enabled", False)),
            "clients": clients,
            "perf": perf,
            "capture": capture,
            "frp_running": self.frp.is_running(),
        }


def handle_client(sock, addr, runtime):
    """处理单个客户端连接：握手、账户准入（可选）、加入集合（记录统计）、循环 recv 检测断开。"""
    try:
        server_handshake(sock)
    except Exception as e:
        log.warning("客户端 %s 握手失败: %s", addr, e)
        try:
            sock.close()
        except OSError:
            pass
        return
    # 低延迟调优：TCP_NODELAY 禁用 Nagle + 加大收发缓冲 + TCP keepalive
    net_cfg = runtime.cfg["host"].get("net", {})
    tune_socket(
        sock,
        sndbuf=int(net_cfg.get("sndbuf_kb", 1024)) * 1024,
        rcvbuf=int(net_cfg.get("rcvbuf_kb", 2048)) * 1024,
        keepalive=bool(net_cfg.get("keepalive", True)),
        keepalive_idle=int(net_cfg.get("keepalive_idle_s", 60)),
    )
    # 账户准入阶段：host.auth.enabled=true 时，未完成登录的连接不进入广播列表
    auth_cfg = runtime.cfg["host"].get("auth", {})
    username = None
    if auth_cfg.get("enabled", False):
        auth_timeout = float(auth_cfg.get("auth_timeout", 60))  # 与 DEFAULT_CONFIG 一致
        sock.settimeout(auth_timeout)
        try:
            msg = recv_msg(sock)
            action = msg.get("action")
            user = (msg.get("user") or "").strip()
            password = msg.get("pass") or ""
            if action == "login":
                ok, text = runtime.accounts.authenticate(user, password)
            elif action == "register":
                ok, text = runtime.accounts.register(user, password)
            elif action == "probe":
                # 观看端无凭据时的探测：告知需要登录，但不作为认证失败告警记录
                ok, text = False, "需要登录"
            else:
                ok, text = False, "不支持的认证请求"
            send_msg(sock, {"action": "auth_result", "ok": ok, "msg": text})
            if not ok:
                if action != "probe":
                    log.warning("客户端 %s 认证失败: %s（%s）", addr, text, user or "-")
                sock.close()
                return
            username = user
        except socket.timeout:
            log.warning("客户端 %s 未在 %.0f 秒内完成登录，已断开", addr, auth_timeout)
            sock.close()
            return
        except Exception as e:
            log.warning("客户端 %s 认证异常: %s", addr, e)
            sock.close()
            return
    else:
        # 关闭准入时仍兼容新版观看端：新版观看端握手后总会尝试发送 auth 消息。
        # 这里短暂等待并回复“认证已关闭”，旧客户端不发送 auth 则直接放行，不阻塞服务。
        # 等待窗口取 2 秒：经公网/内网穿透的高延迟连接也能收到 probe，避免观看端
        # 误判为“需要登录”而陷入反复重连。
        try:
            sock.settimeout(2.0)
            msg = recv_msg(sock)
            user = (msg.get("user") or "").strip()
            send_msg(sock, {"action": "auth_result", "ok": True,
                            "msg": "账户准入已关闭，无需登录"})
            if user:
                username = user
                log.info("客户端 %s 在准入关闭状态发送登录：%s（已直接放行）", addr, user)
            else:
                log.info("客户端 %s 准入已关闭，直接放行", addr)
        except socket.timeout:
            log.debug("客户端 %s 未发送可选 auth，按旧客户端直连处理", addr)
        except Exception as e:
            log.debug("客户端 %s 可选 auth 处理忽略：%s", addr, e)
    # 认证阶段设置的 socket 超时在进入接收循环前复位为阻塞模式：
    # 接收循环用 select 门控 recv，残留超时可能让合法慢速连接被误判为接收错误
    try:
        sock.settimeout(None)
    except OSError:
        pass
    info = ClientInfo(addr, username)
    info.note_rx()  # 认证完成即视为一次活跃
    # 先标记发送线程激活再加入集合，避免广播线程在窗口期内走阻塞的同步发送路径
    info._sender_active = True
    with runtime.clients_lock:
        runtime.clients[sock] = info
        count = len(runtime.clients)
    # 每个客户端一个独立发送线程：慢客户端只淘汰自己，不拖累其他观看端
    threading.Thread(
        target=_client_sender_loop,
        args=(sock, info, runtime),
        name="client-sender-%s" % addr[0],
        daemon=True,
    ).start()
    # 新连接入册后补发一次 roster（含本连接在内广播，旧客户端忽略未知 action）
    runtime.broadcast_roster()
    log.info("客户端接入: %s（%s，当前 %d 个客户端）", addr, username or "未登录", count)
    reason = "断开"
    try:
        # 控制消息接收循环：客户端只回发 JSON 控制消息（心跳 ping 等）。
        # 用 select 空闲轮询 + 应用层活跃时间戳做半开连接检测，避免与发送线程
        # 共享 settimeout 造成相互干扰；收到 ping 立即回 pong（同机测 RTT）。
        idle_s = float(net_cfg.get("keepalive_idle_s", 60))
        rx_buf = b""
        while not runtime.stop_event.is_set():
            r, _, _ = select.select([sock], [], [], 1.0)
            if not r:
                if time.monotonic() - info.last_rx_at > idle_s:
                    reason = "空闲超时（半开连接）"
                    break
                continue
            try:
                data = sock.recv(65536)
            except OSError:
                reason = "接收错误"
                break
            if not data:
                break
            info.note_rx()
            rx_buf += data
            # 逐条解析缓冲区内完整消息：CTRL 即时处理；上行帧（MSG_VIDEO/MSG_FRAME）
            # 视为共享声明：首帧登记为共享成员并路由给 peer 源订阅者
            protocol_error = False
            while True:
                consumed, kind, payload = parse_message(rx_buf)
                if consumed == 0:
                    break
                rx_buf = rx_buf[consumed:]
                if kind == MSG_VIDEO:
                    if info.share_id is None:
                        runtime._register_sharer(sock, info)
                    try:
                        _ts, _codec, flags, _nal = parse_video(payload)
                    except ValueError:
                        log.warning("客户端 %s 上行视频帧损坏，忽略", addr)
                        continue
                    _fwd_member_frame(runtime, info, kind, payload,
                                      is_key=bool(flags & VIDEO_FLAG_KEY))
                elif kind == MSG_FRAME:
                    if info.share_id is None:
                        runtime._register_sharer(sock, info)
                    _fwd_member_frame(runtime, info, kind, payload, is_jpeg=True)
                else:  # MSG_CTRL
                    try:
                        msg = json.loads(payload.decode("utf-8"))
                    except ValueError:
                        reason = "协议错误（控制消息 JSON 损坏）"
                        protocol_error = True
                        break
                    if isinstance(msg, dict) and msg.get("action") == "ping":
                        info.send_ctrl(sock, {"action": "pong", "t": msg.get("t", 0)})
                    elif isinstance(msg, dict) and msg.get("action") == "cap_probe":
                        info.send_ctrl(sock, {"action": "cap", "multiview": True})
                    elif isinstance(msg, dict) and msg.get("action") == "watch":
                        src = msg.get("source") or "local"
                        if src == "local" or src in _live_share_ids(runtime):
                            info.watch_source = src
                            info.need_key = True  # 切换源：重新从关键帧开始收
                            if src != "local":
                                member = _sharer_sock(runtime, src)
                                if member is not None:
                                    runtime.clients[member].send_ctrl(
                                        member, {"action": "req_keyframe"})
                        else:
                            log.debug("watch 无效源 %r（%s），保持原源 %r",
                                      src, addr, info.watch_source)
                    elif isinstance(msg, dict) and msg.get("action") == "req_keyframe":
                        if info.watch_source.startswith("peer:"):
                            member = _sharer_sock(runtime, info.watch_source)
                            if member is not None:
                                # 转达成员端：其共享会话强制出一帧关键帧
                                runtime.clients[member].send_ctrl(
                                    member, {"action": "req_keyframe"})
                        else:
                            with runtime.slot_lock:
                                runtime.force_key = True  # 原逻辑：本地下一帧 IDR
                            log.debug("客户端 %s 请求关键帧，已置位强制 IDR", addr)
            if protocol_error:
                break  # 协议错误：立即断开，避免异常字节滞留缓冲区导致内存增长
    except Exception as e:
        reason = "异常: %s" % e
        log.debug("客户端 %s %s", addr, reason)
    finally:
        with runtime.clients_lock:
            info = runtime.clients.pop(sock, None)
            remaining = len(runtime.clients)
        if info is not None:
            if info.share_id is not None:
                runtime._unregister_sharer(info)
            info.stop_sender()
        try:
            sock.close()
        except OSError:
            pass
        if info is not None:
            with info._lock:
                total_mb = info.bytes_sent / (1024.0 * 1024.0)
            log.info("客户端断开: %s（%s，%s，剩余 %d 个客户端，累计发送 %.1f MB）",
                     addr, username or "未登录", reason, remaining, total_mb)
        else:
            log.info("客户端断开: %s（%s，%s，剩余 %d 个客户端）",
                     addr, username or "未登录", reason, remaining)


def route_frame(runtime, source, frame, is_key=False, is_jpeg=False, send_timeout=2.0):
    """把一帧消息投递给 watch_source == source 的存活客户端（订阅路由）。

    语义与旧 broadcast_frame 完全一致：_sender_active 者走 enqueue_frame（容量 1，
    发送慢丢中间帧）；否则同步 sendall fallback（测试/旧式直连）。新增关键帧门控：
    视频订阅者在 need_key 期间（新连接/切换源后）只收关键帧，收到后清除；
    JPEG 帧自包含，始终放行。
    """
    if not frame:
        return
    with runtime.clients_lock:
        targets = []
        for sock, info in runtime.clients.items():
            if info.watch_source != source:
                continue
            if not is_jpeg and info.need_key and not is_key:
                continue  # 等待关键帧：GOP 中间帧无法起解，丢弃
            if not is_jpeg and is_key:
                info.need_key = False
            targets.append((sock, info))
    for sock, info in targets:
        if getattr(info, "_sender_active", False):
            info.enqueue_frame(frame)
        else:
            # 兼容未启动独立发送线程的调用（测试/旧式直连）：保持原同步发送语义
            with info._write_lock:
                try:
                    sock.settimeout(send_timeout)
                    sock.sendall(frame)
                    info.record_sent(len(frame))
                except OSError:
                    _drop_client(runtime, sock, info, "发送失败")


def _live_share_ids(runtime):
    """当前在册共享成员 id 集合（watch 源校验用）。"""
    with runtime.clients_lock:
        return {c.share_id for c in runtime.clients.values() if c.share_id}


def _sharer_sock(runtime, share_id):
    """按共享 id 查成员连接 socket；不存在返回 None。"""
    with runtime.clients_lock:
        for s, c in runtime.clients.items():
            if c.share_id == share_id:
                return s
    return None


def _fwd_member_frame(runtime, sharer, kind, payload, is_key=False, is_jpeg=False):
    """把成员上行帧原样重打包后路由给该 peer 源的订阅者。

    帧 payload 格式与下行完全一致，重打包字节序列等价原消息（kind+len+payload）。
    """
    raw = bytes((kind,)) + struct.pack(">I", len(payload)) + payload
    route_frame(runtime, sharer.share_id, raw, is_key=is_key, is_jpeg=is_jpeg)


def run_server(runtime):
    """启动 TCP 服务并运行 采集→编码→发送 三级流水线，直到 stop_event 置位。

    流水线（每级只保留“最新一帧”，天然丢弃中间帧，采集与发送解耦）：
      采集线程：按目标帧率把原始帧写入 raw 槽（编码不在采集线程，避免编码耗时
                拖慢采集节奏；连续采集失败自动重建采集后端实现故障恢复）
      编码线程：消费 raw 槽最新帧 -> JPEG -> 写入 jpeg 槽（缩放/质量自适应）
      发送线程：消费 jpeg 槽最新帧 -> 广播到各客户端独立发送队列
      性能线程：每秒输出统计 -> 自适应降级（质量->缩放->帧率）
    """
    cfg = runtime.cfg
    listen_host = cfg["host"]["listen_host"]
    port = cfg["host"]["port"]
    base_fps = cfg["host"]["fps"]
    fps = base_fps
    base_quality = cfg["host"]["jpeg_quality"]
    base_scale = cfg["host"].get("scale", 1.0)
    perf_cfg = cfg["host"].get("perf", {})
    quality_min = perf_cfg.get("quality_min", 40)
    scale_min = perf_cfg.get("scale_min", 0.6)
    fps_min = max(1, perf_cfg.get("fps_min", 15))
    # 静止检测配置（perf.still，全部可配；enabled=false 关闭后行为与旧版一致）
    still_cfg = perf_cfg.get("still", {})
    still_enabled = bool(still_cfg.get("enabled", True))
    still_probe_interval = 1.0 / max(1, int(still_cfg.get("probe_fps", 5)))
    still_frames = max(1, int(still_cfg.get("still_frames", 3)))
    point_thr = int(still_cfg.get("point_thr", 10))
    ratio_thr = float(still_cfg.get("ratio_thr", 0.005))
    # 连续无变化满该时长才判定静止（默认 3 次探测 × 0.2s = 0.6s，仅连续累计，
    # 间歇内容不会因零星停顿跨帧累计误入静止）
    still_quiet_ms = still_frames * still_probe_interval
    # H.264 视频编码配置
    codec_cfg = cfg["host"].get("codec", {})
    encoder_sel = codec_cfg.get("encoder", "auto")
    base_bitrate = int(codec_cfg.get("bitrate_kbps", 6000)) * 1000
    min_bitrate = int(codec_cfg.get("min_bitrate_kbps", 1000)) * 1000
    max_bitrate = int(codec_cfg.get("max_bitrate_kbps", 20000)) * 1000
    keyint = max(1, int(codec_cfg.get("keyint", 30)))
    codec_preset = codec_cfg.get("preset", "") or ""
    server = None
    # frp 隧道（本地回连目标为 localhost）在 SYSTEM 服务上下文解析 localhost 时
    # 优先走 IPv6 ::1；fps-host 必须同时监听 IPv4/IPv6 回环，否则公网链路握手即断。
    # 双栈 socket（AF_INET6 + IPV6_V6ONLY=0）一个端口同时覆盖 0.0.0.0 与 ::。
    try:
        server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("::", port))
        server.listen(5)
    except OSError:
        # IPv6 不可用（系统禁用 IPv6）时的回退：仅 IPv4
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((listen_host, port))
        server.listen(5)
    runtime._server = server
    log.info("服务已启动，监听 %s:%d（fps=%d 质量=%d，退出按钮/Ctrl+C 退出）",
             listen_host, port, fps, base_quality)

    def accept_loop():
        while not runtime.stop_event.is_set():
            try:
                sock, addr = server.accept()
            except OSError:
                break  # server socket 已关闭
            threading.Thread(
                target=handle_client, args=(sock, addr, runtime), daemon=True
            ).start()

    threading.Thread(target=accept_loop, daemon=True).start()

    def _capture_loop():
        """采集线程：按目标帧率把原始帧写入 raw 槽，只统计帧率，不负责编码与发送。

        编码移动到独立线程，避免编码耗时拉长采集间隔（采集节奏 = 1/fps 严格节流）。
        连续采集失败超过阈值时重建采集后端（dxcam 设备丢失等场景自动恢复）。
        """
        # 1 秒滑动窗口：记录每帧采集完成的时刻，用于统计实际采集帧率
        fps_window = []
        with runtime.slot_lock:
            backend = runtime.slot["backend"]
            monitor = runtime.slot["monitor"]
            region = runtime.slot["region"]
        cap = CaptureManager(backend, monitor, region)
        consecutive_errors = 0
        still_ref = None      # 最近一次发送帧的抽稀参考图
        quiet_since = None    # 连续无变化起始时刻（perf_counter）；None=上一帧有变化
        in_still = False      # 是否处于静止停发态
        was_still = False     # 上一帧静止态（边沿触发日志与统计）
        try:
            while not runtime.stop_event.is_set():
                with runtime.slot_lock:
                    backend = runtime.slot["backend"]
                    monitor = runtime.slot["monitor"]
                    region = runtime.slot["region"]
                    fps_now = runtime.slot.get("fps", fps)
                try:
                    cap.set_params(backend, monitor, region)
                except Exception as e:
                    consecutive_errors += 1
                    log.warning("采集参数重建失败: %s（连续 %d 次）", e, consecutive_errors)
                    time.sleep(0.5)
                    continue
                frame_interval = 1.0 / fps_now if fps_now and fps_now > 0 else 0.0
                frame_start = time.perf_counter()
                ts_micros = int(time.time() * 1_000_000)  # 采集开始时刻
                try:
                    bgr = cap.grab()
                    if bgr is None:
                        time.sleep(0.01)  # 无新帧（dxgi），短暂等待后重试
                        continue
                except Exception as e:
                    consecutive_errors += 1
                    log.warning("采集错误: %s（连续 %d 次）", e, consecutive_errors)
                    if consecutive_errors >= 5:
                        log.warning("采集后端持续异常，尝试重建采集器（故障恢复）")
                        try:
                            cap.close()
                        except Exception:
                            pass
                        try:
                            cap = CaptureManager(backend, monitor, region)
                        except Exception as e2:
                            log.error("重建采集后端失败: %s", e2)
                        consecutive_errors = 0
                    time.sleep(0.5)
                    continue
                if consecutive_errors > 0:
                    consecutive_errors = 0  # 恢复成功，清空连续错误计数
                # ---- 静止检测闸门（画面无有效变化时不写 raw 槽，编码/发送自然停摆）----
                if still_enabled:
                    try:
                        changed, still_ref = motion_changed(
                            still_ref, downsample_frame(bgr), point_thr, ratio_thr)
                    except Exception:
                        # 探测异常按“有变化”处理：宁可多发一帧，不可画面冻结
                        changed, still_ref = True, downsample_frame(bgr)
                    in_still, quiet_since = still_gate_update(
                        in_still, quiet_since, changed, time.perf_counter(), still_quiet_ms)
                    if in_still and not was_still:
                        log.info("画面静止，暂停发送（%d fps 探测）", int(1 / still_probe_interval))
                        with runtime.stats_lock:
                            runtime.stats["still"] = True
                    elif was_still and not in_still:
                        log.info("画面变化，恢复发送")
                        with runtime.stats_lock:
                            runtime.stats["still"] = False
                    was_still = in_still
                    if not changed:
                        if in_still:
                            # 静止期：按探测间隔巡检，降低采集开销
                            time.sleep(still_probe_interval)
                        else:
                            # 未入静止：按正常帧节奏继续巡检。不按探测间隔空等——
                            # 间歇内容（打字/低频更新）不会因零星停顿被压到探测速率
                            elapsed = time.perf_counter() - frame_start
                            wait = frame_interval - elapsed
                            if wait > 0:
                                time.sleep(wait)
                        continue
                now = time.perf_counter()
                with runtime.slot_lock:
                    runtime.slot["raw"] = bgr
                    runtime.slot["raw_ts"] = ts_micros
                # 1 秒滑动窗口统计实际采集帧率
                fps_window.append(now)
                while fps_window and now - fps_window[0] > 1.0:
                    fps_window.pop(0)
                if len(fps_window) >= 2:
                    capture_fps = (len(fps_window) - 1) / (fps_window[-1] - fps_window[0])
                elif fps_window:
                    capture_fps = 1.0
                else:
                    capture_fps = 0.0
                with runtime.stats_lock:
                    runtime.stats["capture_fps"] = capture_fps
                # 帧间节流：目标间隔 1/fps，按每次循环开始时刻计算剩余等待，避免累计漂移
                if frame_interval > 0:
                    elapsed = time.perf_counter() - frame_start
                    wait = frame_interval - elapsed
                    if wait > 0:
                        time.sleep(wait)
        finally:
            cap.close()

    def _encode_loop():
        """编码线程：消费 raw 槽最新帧 -> H.264（NVENC/x264）或 JPEG 回退 -> 写共享槽。

        视频编码路径：
        - 时间戳透传（packet.pts=采集时刻），编码缓冲延迟如实计入端到端延迟测量
        - 关键帧标记（is_key）供发送端与观看端同步；force_key 强制 IDR
          （观看端关键帧请求 / ABR 重建后重置参考帧）
        - scale/fps 自适应变化自动重建编码器；编码器不可用自动回退逐帧 JPEG
        """
        last_raw_ts = -1
        target_width = int(codec_cfg.get("target_width", 854) or 0)
        last_frame = None   # 最近一次成功编码（缩放后）的帧：静止期强制关键帧恢复用
        # 1 秒滑动窗口：记录每次编码完成时刻，用于统计编码帧率
        enc_window = []
        error_cooldown_until = 0.0
        encoder = None
        enc_fps_now = 0
        while not runtime.stop_event.is_set():
            with runtime.slot_lock:
                raw = runtime.slot.get("raw")
                raw_ts = runtime.slot.get("raw_ts", 0)
                scale = runtime.slot["scale"]
                quality = runtime.slot["quality"]
                fps_now = runtime.slot.get("fps", fps)
            if raw is None or raw_ts == last_raw_ts:
                # 无新帧：若画面静止且有观看端请求关键帧（新观众接入/花屏恢复），
                # 用最近一次编码帧强制出一帧 IDR（新 pts 绕过发送端同帧去重）
                if runtime.force_key:
                    with runtime.slot_lock:
                        need_key = runtime.force_key
                        runtime.force_key = False
                    if (need_key and encoder is not None and encoder.available
                            and last_frame is not None):
                        key_ts = int(time.time() * 1_000_000)
                        res = encoder.encode(last_frame, raw_ts=key_ts, force_key=True)
                        if res is not None:
                            nal, is_key, encode_ms, out_ts = res
                            with runtime.slot_lock:
                                runtime.slot["video"] = nal
                                runtime.slot["video_ts"] = out_ts
                                runtime.slot["is_key"] = is_key
                                runtime.slot["video_codec"] = encoder.codec_id
                                runtime.slot["encode_ms"] = encode_ms
                                runtime.slot["bitrate"] = encoder.bitrate
                            with runtime.stats_lock:
                                runtime.stats["encode_ms"] = encode_ms
                            log.info("静止期响应关键帧请求（%d 字节）", len(nal))
                time.sleep(0.001)
                continue
            # 有新帧即将编码：原子读取并清除强制关键帧标志（仅在真正编码时消费）
            with runtime.slot_lock:
                force_key = runtime.force_key
                runtime.force_key = False
            now = time.perf_counter()
            try:
                # 有效输出尺寸：scale 缩放 + target_width 上限压制（分辨率档位）
                (w, h) = calc_output_size(raw.shape[1], raw.shape[0], scale, target_width)
                if encoder is not None and not encoder.available:
                    encoder = None  # 编码器失效（如 GPU 驱动错误）：下次重建/回退
                if encoder is None and encoder_sel != "jpeg":
                    with runtime.codec_lock:
                        target_b = runtime.target_bitrate
                    enc_tmp = codec_mod.VideoEncoder(
                        w, h, fps_now, target_b, keyint, encoder_sel, codec_preset)
                    if enc_tmp.available:
                        encoder = enc_tmp
                        enc_fps_now = fps_now
                        with runtime.codec_lock:
                            runtime.video_encoder = encoder
                        with runtime.stats_lock:
                            runtime.stats["codec_name"] = encoder.name
                            runtime.stats["keyint"] = keyint
                        log.info("启用视频编码器: %s（%dx%d，码率 %d Kbps，keyint %d）",
                                 encoder.name, w, h, target_b // 1000, keyint)
                if encoder is not None:
                    if fps_now != enc_fps_now:
                        encoder.set_fps(fps_now)  # 帧率档变化重建
                        enc_fps_now = fps_now
                    frame = raw
                    if (w, h) != (raw.shape[1], raw.shape[0]):
                        frame = cv2.resize(raw, (w, h), interpolation=cv2.INTER_AREA)
                    result = encoder.encode(frame, raw_ts=raw_ts, force_key=force_key)
                    if result is None:
                        # 编码器初始化/pre-roll 或编码失败：不消费 raw_ts，下轮重试
                        time.sleep(0.001)
                        continue
                    nal, is_key, encode_ms, out_ts = result
                    last_frame = frame  # 缓存缩放后帧：静止期强制关键帧恢复用
                    with runtime.slot_lock:
                        runtime.slot["video"] = nal
                        runtime.slot["video_ts"] = out_ts
                        runtime.slot["is_key"] = is_key
                        runtime.slot["video_codec"] = encoder.codec_id
                        runtime.slot["encode_ms"] = encode_ms
                        runtime.slot["bitrate"] = encoder.bitrate
                        runtime.slot["out_w"] = w
                        runtime.slot["out_h"] = h
                    with runtime.stats_lock:
                        runtime.stats["keyframe_total"] = encoder.keyframe_sent
                        runtime.stats["encode_ms"] = encode_ms
                    last_raw_ts = raw_ts
                else:
                    # JPEG 回退（无可用视频编码器）
                    jpeg, encode_ms = encode_bgr(raw, scale, quality, target_width)
                    jw, jh = calc_output_size(
                        raw.shape[1], raw.shape[0], scale, target_width)
                    with runtime.slot_lock:
                        runtime.slot["jpeg"] = jpeg
                        runtime.slot["ts"] = raw_ts  # 帧采集时间戳原样透传
                        runtime.slot["encode_ms"] = encode_ms
                        runtime.slot["bitrate"] = 0
                        runtime.slot["out_w"] = jw
                        runtime.slot["out_h"] = jh
                    with runtime.stats_lock:
                        runtime.stats["encode_ms"] = encode_ms
                    last_raw_ts = raw_ts
            except Exception as e:
                if now >= error_cooldown_until:
                    log.warning("编码错误: %s，10 秒内不再重复告警", e)
                    error_cooldown_until = now + 10.0
                time.sleep(0.005)
                continue
            enc_window.append(now)
            while enc_window and now - enc_window[0] > 1.0:
                enc_window.pop(0)
            if len(enc_window) >= 2:
                enc_fps = (len(enc_window) - 1) / (enc_window[-1] - enc_window[0])
            elif enc_window:
                enc_fps = 1.0
            else:
                enc_fps = 0.0
            with runtime.stats_lock:
                runtime.stats["encode_fps"] = enc_fps
        # 线程退出：关闭编码器并清空共享引用
        if encoder is not None:
            encoder.close()
            with runtime.codec_lock:
                if runtime.video_encoder is encoder:
                    runtime.video_encoder = None

    def _send_loop():
        """发送线程：取共享槽最新帧广播并统计带宽；同帧去重，发送慢时跳过中间帧。

        视频编码路径广播 H.264 帧（pack_video），JPEG 回退路径广播 JPEG（pack_frame）；
        两种路径独立去重（同一时刻编码线程只写其中一种）。
        """
        last_sent_video_ts = -1  # 已发送的最后一帧视频时间戳
        last_sent_jpeg_ts = -1   # 已发送的最后一帧 JPEG 时间戳
        # 1 秒滑动窗口：元素为 (完成时刻, 发送耗时毫秒)
        send_window = []
        while not runtime.stop_event.is_set():
            with runtime.slot_lock:
                video = runtime.slot["video"]
                video_ts = runtime.slot["video_ts"]
                is_key = runtime.slot["is_key"]
                video_codec = runtime.slot["video_codec"]
                jpeg = runtime.slot["jpeg"]
                ts = runtime.slot["ts"]
            if video is not None and video_ts != last_sent_video_ts:
                send_start = time.perf_counter()
                try:
                    route_frame(
                        runtime, "local",
                        pack_video(video, video_ts, is_key, codec=video_codec),
                        is_key=is_key, send_timeout=2.0)
                    last_sent_video_ts = video_ts  # 仅发送成功后才视为已发送
                except Exception as e:
                    log.warning("广播错误: %s", e)
            elif jpeg is not None and ts != last_sent_jpeg_ts:
                send_start = time.perf_counter()
                try:
                    route_frame(runtime, "local", pack_frame(jpeg, ts),
                                is_jpeg=True, send_timeout=2.0)
                    last_sent_jpeg_ts = ts  # 仅发送成功后才视为已发送
                except Exception as e:
                    log.warning("广播错误: %s", e)
            else:
                time.sleep(0.0005)  # 槽内尚无新帧，短暂等待
                continue
            send_ms = (time.perf_counter() - send_start) * 1000.0
            now = time.perf_counter()
            send_window.append((now, send_ms))
            while send_window and now - send_window[0][0] > 1.0:
                send_window.pop(0)
            if len(send_window) >= 2:
                send_fps = (len(send_window) - 1) / (send_window[-1][0] - send_window[0][0])
            elif send_window:
                send_fps = 1.0
            else:
                send_fps = 0.0
            avg_send_ms = sum(ms for _, ms in send_window) / len(send_window) if send_window else 0.0
            with runtime.stats_lock:
                runtime.stats["send_fps"] = send_fps
                runtime.stats["avg_send_ms"] = avg_send_ms

    def _perf_loop():
        """性能统计 + 带宽/RTT 汇总 + 动态自适应节拍：每秒评估一次。"""
        while not runtime.stop_event.is_set():
            time.sleep(1.0)
            with runtime.stats_lock:
                capture_fps = runtime.stats["capture_fps"]
                encode_fps = runtime.stats.get("encode_fps", 0)
                send_fps = runtime.stats["send_fps"]
                encode_ms = runtime.stats["encode_ms"]
                avg_send_ms = runtime.stats["avg_send_ms"]
                still_now = bool(runtime.stats.get("still", False))
            with runtime.slot_lock:
                quality = runtime.slot["quality"]
                scale = runtime.slot["scale"]
                fps_now = runtime.slot.get("fps", fps)
                out_w = runtime.slot.get("out_w", 0)
                out_h = runtime.slot.get("out_h", 0)
            budget_ms = 1000.0 / max(fps_now, 1) * 0.8
            # 各客户端带宽汇总（KB/s、累计 MB、RTT）；同时汇总真实发送耗时与丢帧信号
            client_send_ms = []
            recent_drop = False
            worst_rtt = 0.0
            with runtime.clients_lock:
                for info in runtime.clients.values():
                    if info.last_send_ms > 0:
                        client_send_ms.append(info.last_send_ms)
                    if info.has_recent_drop():
                        recent_drop = True
                    worst_rtt = max(worst_rtt, info.rtt_ms)
                bw_lines = []
                for info in runtime.clients.values():
                    rate = info.rate_kbps()
                    lock = getattr(info, "_lock", None)
                    if lock is not None:
                        with lock:
                            total_mb = info.bytes_sent / (1024.0 * 1024.0)
                    else:
                        total_mb = getattr(info, "bytes_sent", 0) / (1024.0 * 1024.0)
                    rtt_txt = ("RTT=%.0fms " % info.rtt_ms) if info.rtt_ms > 0 else ""
                    bw_lines.append(
                        "%s%s %s%.0f KB/s %.1f MB（丢 %d 帧）" % (
                            info.addr,
                            ("(%s)" % info.username) if info.username else "",
                            rtt_txt, rate, total_mb, info.dropped_frames))
            client_avg_ms = (sum(client_send_ms) / len(client_send_ms)) if client_send_ms else 0.0
            effective_send_ms = max(avg_send_ms, client_avg_ms)
            with runtime.stats_lock:
                runtime.stats["avg_send_ms"] = effective_send_ms
                runtime.stats["worst_rtt_ms"] = worst_rtt
            log.info(
                "性能: 采集 %d fps / 编码 %d fps / 发送 %d fps / 帧率 %d / 质量 %d / 缩放 %.2f / 输出 %dx%d / 编码 %.1f ms / 发送 %.1f ms%s",
                capture_fps, encode_fps, send_fps, fps_now, quality, scale,
                out_w, out_h, encode_ms, effective_send_ms,
                "（画面静止，暂停发送）" if still_now else
                ("（有客户端丢帧）" if recent_drop else ""),
            )
            if bw_lines:
                log.info("带宽: %s", "；".join(bw_lines))
            # 每次评估时从 runtime.cfg 读取开关，让 GUI 修改可以即时生效
            adaptive_now = bool(runtime.cfg["host"].get("perf", {}).get("adaptive", True))
            if not adaptive_now:
                continue
            if still_now:
                # 画面静止停发：发送耗时≈0 会让常规评估误入"回升"分支，
                # 反复重建编码器（浪费 CPU）。静止期只统计不评估，恢复后自动继续。
                continue
            # 同步读取用户最新基准值（GUI 修改 fps/质量/缩放后，回升目标随之更新，
            # 避免自适应回升"对抗"用户手动设置）
            base_fps = max(1, int(runtime.cfg["host"].get("fps", 60)))
            base_quality = int(runtime.cfg["host"].get("jpeg_quality", 80))
            base_scale = float(runtime.cfg["host"].get("scale", 1.0))
            now_micros = int(time.time() * 1_000_000)
            with runtime.slot_lock:
                # 最新帧时间戳（视频或 JPEG 路径），计算积压/卡顿信号
                frame_ts = runtime.slot["video_ts"] if runtime.slot["video_ts"] > 0 else runtime.slot["ts"]
                video_mode = runtime.slot["bitrate"] > 0  # 有视频编码器在产出
                cur_bitrate = runtime.slot["bitrate"]
            lag_us = now_micros - frame_ts if frame_ts > 0 else 0
            congested = effective_send_ms > budget_ms or recent_drop or (frame_ts > 0 and lag_us > 800_000)

            def _set_encoder_bitrate(bps, cooldown=0.0):
                """调整视频编码器码率；重建后强制关键帧重置解码端参考帧。返回是否成功。

                cooldown>0 时限制重建频率（回升场景），避免每评估周期都重建；
                降级场景 cooldown=0 保持即时响应。
                """
                if cooldown > 0:
                    now_mono = time.monotonic()
                    with runtime.stats_lock:
                        last_abr = runtime.stats.get("last_abr_at", 0.0)
                    if now_mono - last_abr < cooldown:
                        return False
                with runtime.codec_lock:
                    enc = runtime.video_encoder
                    runtime.target_bitrate = bps  # 记录目标码率：编码线程重建时沿用
                if enc is None or not enc.available:
                    return False
                try:
                    changed = enc.set_bitrate(bps)
                except Exception as e:
                    log.warning("ABR 码率调整失败: %s", e)
                    return False
                if changed:
                    with runtime.slot_lock:
                        runtime.force_key = True
                    with runtime.stats_lock:
                        runtime.stats["abr_changes"] += 1
                        runtime.stats["last_abr_at"] = time.monotonic()
                    log.info("ABR 码率调整: %d Kbps（编码器重建，下帧关键帧）", bps // 1000)
                return changed

            if congested:
                # 降级：视频模式 码率->缩放->帧率；JPEG 模式 质量->缩放->帧率
                level = ""
                if video_mode and cur_bitrate > min_bitrate:
                    new_b = max(min_bitrate, int(cur_bitrate * 0.7))
                    _set_encoder_bitrate(new_b)
                    level = "码率 %d Kbps" % (new_b // 1000)
                elif not video_mode and quality > quality_min:
                    quality -= 10
                    level = "质量 %d" % quality
                elif scale > scale_min:
                    scale = max(scale_min, round(scale * 0.8, 3))
                    level = "缩放 %.2f" % scale
                elif fps_now > fps_min:
                    fps_now = max(fps_min, int(fps_now / 2))
                    level = "帧率 %d" % fps_now
                with runtime.slot_lock:
                    runtime.slot["quality"] = quality
                    runtime.slot["scale"] = scale
                    if fps_now != runtime.slot["fps"]:
                        runtime.slot["fps"] = fps_now
                        log.info("自适应帧率已更新为 %d fps", fps_now)
                # 已到降级下限（质量/缩放/帧率均不能再降）时 level 为空：
                # 不再重复打印空的"降级"日志，避免持续拥塞时每秒一条噪音
                if level:
                    log.warning("自适应降级: %s（发送耗时 %.0f ms%s）",
                                level, effective_send_ms,
                                "，丢帧" if recent_drop else "")
            elif (send_fps >= 5 and not recent_drop
                  and effective_send_ms < budget_ms * 0.5
                  and (fps_now < base_fps or scale < base_scale
                       or (video_mode and cur_bitrate < max_bitrate)
                       or (not video_mode and quality < base_quality))):
                # 回升：视频模式 帧率->缩放->码率；JPEG 模式 帧率->缩放->质量
                # 活动门限 send_fps>=5：内容为稀疏/间歇变化（打字、低频 UI 更新）时
                # 发送耗时≈0 会让回升误判链路空闲，每 1-2s 重建一次编码器（关键帧风暴）；
                # 发送不足时冻结参数，密集内容恢复后自然回升
                level = ""
                if fps_now < base_fps:
                    fps_now = min(base_fps, fps_now * 2)
                    level = "帧率 %d" % fps_now
                elif scale < base_scale:
                    scale = min(base_scale, round(scale / 0.8, 3))
                    level = "缩放 %.2f" % scale
                elif video_mode and cur_bitrate < max_bitrate:
                    new_b = min(max_bitrate, int(cur_bitrate * 1.3))
                    _set_encoder_bitrate(new_b, cooldown=2.0)  # 回升限频，避免频繁重建
                    level = "码率 %d Kbps" % (new_b // 1000)
                else:
                    quality = min(base_quality, quality + 10)
                    level = "质量 %d" % quality
                with runtime.slot_lock:
                    runtime.slot["quality"] = quality
                    runtime.slot["scale"] = scale
                    if fps_now != runtime.slot["fps"]:
                        runtime.slot["fps"] = fps_now
                        log.info("自适应帧率已更新为 %d fps", fps_now)
                log.info("自适应回升: %s", level)

    # 采集、编码、发送与性能节拍线程均为 daemon，随 stop_event 置位后退出
    capture_thread = threading.Thread(target=_capture_loop, daemon=True)
    encode_thread = threading.Thread(target=_encode_loop, daemon=True)
    send_thread = threading.Thread(target=_send_loop, daemon=True)
    perf_thread = threading.Thread(target=_perf_loop, daemon=True)
    capture_thread.start()
    encode_thread.start()
    send_thread.start()
    perf_thread.start()

    try:
        # 主线程等待停止事件（Ctrl+C 或 GUI 退出时置位 stop_event 再让线程退出）
        while not runtime.stop_event.is_set():
            runtime.stop_event.wait(0.5)
    except KeyboardInterrupt:
        runtime.stop_event.set()
    finally:
        server.close()
        capture_thread.join(timeout=2)
        encode_thread.join(timeout=2)
        send_thread.join(timeout=2)
        perf_thread.join(timeout=2)
        log.info("服务已停止")


def print_public_addr(cfg):
    """打印公网地址提示（日志输出，windowed 模式亦安全）。"""
    public_addr = cfg["host"].get("frp", {}).get("public_addr", "")
    if public_addr:
        log.info(">>> 请将公网地址发给队友: %s <<<", public_addr)
    else:
        log.info("未配置 host.frp.public_addr，请告知队友你的公网地址；")
        log.info("或在 frpc 区 / config.json 的 host.frp.public_addr 中填写隧道公网地址。")


def main():
    parser = argparse.ArgumentParser(description="FPS 画面共享主机端（屏幕采集 + JPEG 编码 + TCP 广播 + 樱花 frpc）")
    parser.add_argument("--port", type=int, help="可选：覆盖配置中的监听端口（会持久化到 config.json）")
    parser.add_argument("--console", action="store_true", help="强制命令行模式（不显示图形控制台）")
    args = parser.parse_args()

    cfg = load_config()
    logger.setup_logger(cfg)
    if args.port is not None:
        cfg["host"]["port"] = args.port
        save_config(cfg)
        log.info("监听端口已覆盖为 %d 并写入 config.json", args.port)

    runtime = HostRuntime(cfg)

    # 自动启动 frpc（配置了 auto_start 时）
    if cfg["host"].get("frp", {}).get("auto_start"):
        ok, msg = runtime.frp.start()
        (log.info if ok else log.warning)("%s", msg)

    # 图形控制台模式（默认）：GUI 主循环为前台线程，服务在后台线程运行
    gui = None
    if not args.console:
        try:
            import host_ui
            gui = host_ui.HostConsole(runtime)
        except Exception as e:
            log.warning("图形控制台不可用，回退命令行模式: %s", e)
            gui = None

    if gui is not None:
        print_public_addr(cfg)
        runtime.start_server()
        try:
            gui.run()
        except Exception:
            log.exception("图形控制台运行异常")
        finally:
            runtime.shutdown()
            log.info("已退出，再见！")
        return

    # 命令行模式（--console 或 GUI 不可用）：主线程阻塞运行服务，行为与旧版一致
    print_public_addr(cfg)
    try:
        run_server(runtime)
    except KeyboardInterrupt:
        log.info("正在退出...")
    except Exception:
        log.exception("程序发生错误")
        try:
            import tkinter.messagebox as mb
            mb.showerror(APP_NAME, "程序发生错误，详见 logs 目录日志文件")
        except Exception:
            pass
    finally:
        runtime.shutdown()
        log.info("已退出，再见！")


if __name__ == "__main__":
    main()
