# -*- coding: utf-8 -*-
"""FPS 画面观看端：TCP 客户端接收解码 + 屏幕右上角置顶悬浮窗（多频道）。

每个画面来源对应一个 Channel 实例，各自独立连接、接收与重连；
悬浮窗只显示当前活动频道（self.active_idx）的最新帧。

依赖：opencv-python、numpy、Pillow、pywin32。
"""

import argparse
import base64
import ctypes
import ctypes.wintypes
import json
import os
import queue
import select
import socket
import struct
import sys
import threading
import time

import cv2
import numpy as np
import win32con
import win32gui
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk

import logger
import splash
import codec as codec_mod
import share as share_mod
from common import (
    APP_NAME,
    APP_VERSION,
    APP_COPYRIGHT,
    load_config,
    save_config,
    client_handshake,
    send_msg,
    recv_msg,
    parse_addr,
    exe_dir,
    tune_socket,
    parse_message,
    parse_video,
    MSG_FRAME,
    MSG_CTRL,
    MSG_VIDEO,
    VIDEO_FLAG_KEY,
    CODEC_H264,
    CODEC_HEVC,
)

#: 模块级日志（logger.setup_logger(cfg) 幂等初始化后生效）
log = logger.get_logger()

#: 品牌标题（面板标题/悬浮窗占位文字共用）
APP_TITLE = "%s v%s" % (APP_NAME, APP_VERSION)

#: 模块级 Tk 根窗口引用，供未捕获异常处理定位父窗口
_TK_ROOT = None


def _handle_uncaught(exc_type, exc_value, exc_tb):
    """未捕获异常兜底：记录日志并弹出友好提示，避免静默崩溃。"""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    log.exception("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))
    parent = None
    try:
        if _TK_ROOT is not None and _TK_ROOT.winfo_exists():
            parent = _TK_ROOT
    except Exception:
        parent = None
    try:
        messagebox.showerror(
            APP_NAME, "程序遇到错误，已写入 logs 目录日志文件", parent=parent
        )
    except Exception:
        try:
            print("程序遇到错误，已写入 logs 目录日志文件")
        except Exception:
            pass


sys.excepthook = _handle_uncaught

#: 全局热键修饰键
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
#: 按键
VK_X = 0x58
VK_LEFT = 0x25
VK_RIGHT = 0x27
VK_1 = 0x31
WM_HOTKEY = 0x0312
#: 各热键独立 ID：1=Ctrl+Alt+X 切换穿透，2=Ctrl+Alt+← 上一个频道，3=Ctrl+Alt+→ 下一个频道
HOTKEY_ID_TOGGLE = 1
HOTKEY_ID_PREV = 2
HOTKEY_ID_NEXT = 3
HOTKEY_ID_DIRECT_BASE = 10  # Alt+1..9 直达频道：ID = 10..18
HOTKEY_DEFS = [
    (HOTKEY_ID_TOGGLE, VK_X),
    (HOTKEY_ID_PREV, VK_LEFT),
    (HOTKEY_ID_NEXT, VK_RIGHT),
]

#: 控制台面板深色主题配色
COL_BG = "#14141f"          # 窗口背景
COL_CARD = "#1e1e2e"        # 卡片/输入区背景
COL_CTRL = "#2a2a3c"        # 控件背景
COL_FG = "#e6e6ef"          # 主要前景文字
COL_DIM = "#9a9ab0"         # 次要文字
COL_ACCENT = "#7c5cff"      # 强调色（按钮/选中高亮）
COL_ONLINE = "#4ade80"      # 在线绿
COL_CONNECTING = "#fbbf24"  # 连接中黄
COL_OFFLINE = "#f87171"     # 断开红
COL_ACTIVE_ROW = "#2a2440"  # 活动频道行高亮背景

#: 频道状态 -> 中文文本 / 状态色
STATUS_TEXT = {
    "receiving": "在线",
    "connecting": "连接中",
    "disconnected": "断开",
    "auth": "需要登录",
}
STATUS_COLOR = {
    "receiving": COL_ONLINE,
    "connecting": COL_CONNECTING,
    "disconnected": COL_OFFLINE,
    "auth": COL_ACCENT,
}


def _dpapi_encrypt(text):
    """用 Windows DPAPI 加密文本为 base64 字符串；不可用时返回 None（记录警告）。"""
    try:
        import win32crypt
        blob = win32crypt.CryptProtectData(text.encode("utf-8"), None, None, None, None, 0)
        return base64.b64encode(blob).decode("ascii")
    except Exception as e:
        log.warning("凭据加密不可用（DPAPI 不可用）：%s", e)
        return None


def _dpapi_decrypt(token):
    """解密 _dpapi_encrypt 产物；失败返回 None。"""
    if not token:
        return None
    try:
        import win32crypt
        blob = base64.b64decode(token.encode("ascii"))
        _, data = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
        return data.decode("utf-8")
    except Exception:
        return None


class Channel:
    """单个画面来源：独立接收线程 + 最新帧缓存（BGR numpy 数组）。"""

    def __init__(self, name, addr, auth=None, owner=None):
        self.name = name
        self.addr = addr
        self.auth = auth or {}
        self.owner = owner
        self.username = (auth or {}).get("username", "")
        self.auth_msg = ""
        self._prompt_blocked_until = 0.0  # 取消登录后的冷却时刻，避免频繁重连狂弹对话框
        self._password = None  # 本次会话内的明文密码缓存，用于重连自动登录
        self._cred_result = None  # 主线程对话框结果缓存（由 ViewerApp._process_cred_requests 写入）
        self._auth_probed = False  # 已用空凭据探测过服务端是否需要登录（避免准入关闭时仍弹框）
        self._anon_ok = False  # 已确认服务端准入关闭（匿名放行），重连时直接走 probe 不再弹框
        # 网络/稳定性参数（从 viewer 配置读取，缺省用安全默认值）
        viewer_cfg = (owner.cfg if owner is not None else {}).get("viewer", {}) or {}
        net_cfg = viewer_cfg.get("net", {}) or {}
        rc_cfg = viewer_cfg.get("reconnect", {}) or {}
        self._rcvbuf = int(net_cfg.get("rcvbuf_kb", 4096)) * 1024
        self._read_idle_s = float(net_cfg.get("read_idle_s", 5.0))  # 停滞判定阈值
        self._ping_interval = float(viewer_cfg.get("ping_interval_s", 2.0))
        self._backoff_base = float(rc_cfg.get("base_s", 1.0))
        self._backoff_max = float(rc_cfg.get("max_s", 30.0))
        self._backoff_factor = float(rc_cfg.get("factor", 2.0))
        self._retry_delay = 0.0  # 当前重连等待（指数退避）；成功收帧后归零
        # H.264/H.265(HEVC) 视频解码（host 视频编码路径）；JPEG 回退仍兼容
        self._decoder = codec_mod.VideoDecoder()
        self._got_key = False          # 是否已收到关键帧（视频解码起点）
        self._key_requested_at = 0.0   # 上次请求关键帧时刻（冷却 0.5s 防刷屏）
        self.video_mode = False        # 当前是否视频编码流（展示用）
        self.cur_codec = CODEC_H264    # 当前视频流编码类型（H.264/HEVC，展示用）
        self.status = "connecting"
        self.fps = 0.0
        self.latency_ms = 0.0  # 时钟同步假设的 ts 差延迟（毫秒，0=暂无）
        self.round_trip_ms = 0.0  # 应用层 ping/pong 往返时延（毫秒，0=未知），展示优先
        self.last_frame = None  # BGR numpy 数组或 None，受 self.lock 保护
        self.frame_serial = 0  # 每收到一帧递增，供主界面判断是否需要重绘
        self.lock = threading.Lock()
        # ---- MultiView 观看组状态 ----
        self._tx_lock = threading.Lock()      # 本连接所有发送共用（ping/控制/上传帧）
        self._share_lock = threading.RLock()  # 共享会话启停串行化
        self.multiview = False                # 对方支持观看组（cap/peers 确认）
        self._cap_event = threading.Event()   # cap/peers 到达置位（set_share 等待）
        self.peers = []                       # [{id,name,addr}]，随 self.lock 读写
        self.watch_source = "local"           # 当前观看源（"local"/"peer:<n>"）
        self.share_enabled = False            # 用户共享开关（不持久化，每次启动默认关）
        self._share = None                    # ScreenShareSession 或 None
        self._running = True
        self._active_sock = None  # 当前活动 socket（控制心跳线程与接收线程共享；断线即清空）
        self._fps_times = []  # 1 秒滑动窗口内各帧的时间戳
        self._latency_samples = []  # 最近 5 帧延迟样本（微秒），用于滑动平均平滑
        threading.Thread(
            target=self._receiver_loop, name="channel-recv", daemon=True
        ).start()

    # ---------- 接收线程 ----------

    def _receiver_loop(self):
        """接收线程主循环：解析地址 → 连接 → 握手 → 实时收帧；异常时指数退避重连。

        稳定性：无新数据超过 read_idle_s 判定连接停滞；成功后重置退避；
        及时性：用缓冲区逐条解析协议消息，一次循环只解码缓冲区里最新的一帧，
        天然丢弃突发积压的中间帧（防止解码积压放大端到端延迟）。
        """
        while self._running:
            self.status = "connecting"
            sock = None
            try:
                host, port = parse_addr(self.addr)
                # 等待 mainloop 启动后再建立连接：若在首启向导期间连接，服务端 auth_timeout
                # 会在用户尚未看到登录框时就断开连接，导致后续登录必然失败
                if self.owner is not None:
                    ready = getattr(self.owner, "_mainloop_ready", None)
                    if ready is not None and not ready.is_set():
                        log.info("频道[%s] 等待界面就绪后连接 %s:%d …", self.name, host, port)
                        ready.wait(timeout=60)
                        if not self._running:
                            break
                log.info("频道[%s] 正在连接 %s:%d …", self.name, host, port)
                sock = socket.create_connection((host, port), timeout=10)
                tune_socket(sock, rcvbuf=self._rcvbuf, keepalive=True, keepalive_idle=60)
                client_handshake(sock)
                if not self._authenticate(sock):
                    self.status = "auth"
                    self.auth_msg = "需要登录后观看"
                    log.warning("频道[%s] 未通过认证（%s）", self.name, self.auth_msg)
                    raise ConnectionError("认证未通过")
                # 认证阶段设置的 socket 超时（connect 10s / 认证 5s）在进入接收循环前复位：
                # 接收循环用 select 门控 recv，残留超时可能让慢速合法连接被误判为停滞
                try:
                    sock.settimeout(None)
                except OSError:
                    pass
                self._active_sock = sock
                # 心跳/测 RTT 线程：每 ping_interval 发一次 ping，pong 由接收循环解析。
                # 显式传入本次连接的 socket：旧连接的心跳线程不随 _active_sock 复用到新连接
                threading.Thread(
                    target=self._control_ping_loop, args=(sock,),
                    name="channel-ping", daemon=True,
                ).start()
                log.info("频道[%s] 连接成功，开始接收画面", self.name)
                self.status = "receiving"
                self.video_mode = False
                self._got_key = False
                # 重连后重建解码器：丢弃旧连接的参考帧，避免新流 P 帧对着旧参考解码出脏画面
                self._decoder = codec_mod.VideoDecoder(codec=self.cur_codec)
                # MultiView：重置能力状态并发 cap_probe；host 侧新连接默认 local，
                # 若上次在观看 peer 源则重新订阅；成功后按需自动重启共享
                self.multiview = False
                self._cap_event.clear()
                try:
                    with self._tx_lock:
                        send_msg(sock, {"action": "req_keyframe", "t": time.time_ns()})
                        send_msg(sock, {"action": "cap_probe"})
                        if self.watch_source != "local":
                            send_msg(sock, {"action": "watch",
                                            "source": self.watch_source})
                except (OSError, ValueError):
                    pass
                threading.Timer(2.0, self._share_sync_after_connect,
                                args=(sock,)).start()
                self._notify_owner_peers()
                rx_buf = b""
                last_rx = time.monotonic()
                while self._running:
                    r, _, _ = select.select([sock], [], [], 1.0)
                    if not r:
                        # 无数据即空闲；超过阈值判定停滞（半开/被墙等场景快速退出重连）
                        if time.monotonic() - last_rx > self._read_idle_s:
                            raise ConnectionError(
                                "%.0f 秒未收到数据，判定连接停滞" % self._read_idle_s)
                        continue
                    chunk = sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("连接关闭")
                    last_rx = time.monotonic()
                    rx_buf += chunk
                    # 解析缓冲区中所有完整消息；帧只保留最新一份，控制消息即时处理
                    latest = None  # (kind, ts, data)
                    while True:
                        consumed, kind, payload = parse_message(rx_buf)
                        if consumed == 0:
                            break
                        rx_buf = rx_buf[consumed:]
                        if kind == MSG_VIDEO:
                            try:
                                ts, codec, flags, nal = parse_video(payload)
                            except ValueError:
                                continue
                            latest = (MSG_VIDEO, ts, (codec, flags, nal))
                        elif kind == MSG_FRAME:
                            if len(payload) < 8:
                                log.warning("频道[%s] 帧 payload 过短，跳过", self.name)
                                continue
                            (ts,) = struct.unpack(">Q", payload[:8])
                            latest = (MSG_FRAME, ts, payload[8:])
                        else:  # MSG_CTRL
                            self._handle_ctrl(payload)
                    if latest is not None:
                        kind, ts, data = latest
                        if kind == MSG_VIDEO:
                            codec, flags, nal = data
                            if codec != self.cur_codec:
                                # 编码切换（H.264 <-> HEVC）：重建解码器并从关键帧重新开始
                                self._decoder.set_codec(codec)
                                self.cur_codec = codec
                                self._got_key = False
                                self._request_keyframe(sock)
                            if flags & VIDEO_FLAG_KEY:
                                self._got_key = True
                                self.video_mode = True
                            frames = self._decoder.decode(nal)
                            if frames and self._got_key:
                                # 仅在收到关键帧后显示：重连/编码切换后首个关键帧前的
                                # P 帧可能对旧参考解码出脏画面，一律丢弃并请求关键帧
                                self._store_frame(frames[-1], ts)
                                self._retry_delay = 0.0
                            else:
                                # 缺参考帧（未收到关键帧/丢关键帧）：请求关键帧快速恢复
                                self._request_keyframe(sock)
                        else:  # MSG_FRAME（JPEG 回退路径）
                            self.video_mode = False
                            frame = cv2.imdecode(
                                np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR
                            )
                            if frame is not None:
                                self._store_frame(frame, ts)
                                self._retry_delay = 0.0
                            else:
                                log.warning("频道[%s] 帧解码失败，跳过该帧", self.name)
            except Exception as exc:
                self._active_sock = None
                log.warning("频道[%s] 连接断开: %s", self.name, exc)
            finally:
                if self.status != "auth":
                    self.status = "disconnected"
                self._active_sock = None
                # 断线瞬间停止共享上传（复用新连接时按开关自动重启）
                self._stop_share()
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
            if not self._running:
                break
            # 失败后指数退避：1 → 2 → 4 → … → 上限（首连失败也按 base 起步）
            if self._retry_delay <= 0:
                self._retry_delay = self._backoff_base
            log.info("频道[%s] %.1f 秒后重新连接…", self.name, self._retry_delay)
            time.sleep(self._retry_delay)
            self._retry_delay = min(
                self._retry_delay * self._backoff_factor, self._backoff_max)

    def _control_ping_loop(self, sock):
        """心跳/测 RTT 控制线程：周期发送 ping 到服务端，pong 由接收循环解析。

        线程绑定创建时的 socket：一旦该连接断开（_active_sock 不再是本 socket），
        立即退出；重连后由新连接的心跳线程接管，避免多次重连累积多个 ping 线程
        同时对同一连接发心跳。
        """
        while self._running:
            time.sleep(self._ping_interval)
            if self._active_sock is not sock:
                return  # 连接已重建/断开：旧连接的心跳线程退出
            try:
                with self._tx_lock:
                    send_msg(sock, {"action": "ping", "t": time.time_ns()})
            except (OSError, ValueError):
                return  # 连接已断，接收循环会负责重连

    def _request_keyframe(self, sock):
        """向服务端请求关键帧（带 0.5s 冷却），用于首连/丢关键帧/花屏快速恢复。"""
        now = time.time()
        if now - self._key_requested_at < 0.5:
            return
        self._key_requested_at = now
        try:
            with self._tx_lock:
                send_msg(sock, {"action": "req_keyframe", "t": time.time_ns()})
        except (OSError, ValueError):
            pass

    def _handle_ctrl(self, payload):
        """处理一条控制消息（pong/cap/peers/req_keyframe）；未知 action 忽略。"""
        try:
            msg = json.loads(payload)
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        action = msg.get("action")
        if action == "pong":
            t = msg.get("t")
            if isinstance(t, int) and t > 0:
                rtt_ms = (time.time_ns() - t) / 1_000_000.0
                if 0 < rtt_ms < 60_000:  # 过滤异常值（>60s 视为脏数据）
                    self.round_trip_ms = rtt_ms
        elif action == "cap":
            if msg.get("multiview"):
                self.multiview = True
                self._cap_event.set()
                self._maybe_resume_share()
        elif action == "peers":
            rows = msg.get("peers")
            if isinstance(rows, list):
                with self.lock:
                    self.multiview = True
                    self.peers = [p for p in rows
                                  if isinstance(p, dict) and isinstance(p.get("id"), str)]
                self._cap_event.set()
                self._on_peers_updated()
                self._maybe_resume_share()
        elif action == "req_keyframe":
            # host 转达（别的 viewer 正在观看本机共享）：请求上传器补关键帧
            self.request_keyframe()
        # 其余 action 忽略（旧 host 兼容已靠此：cap_probe 无响应即旧版）

    def effective_latency_ms(self):
        """展示用延迟：优先 RTT/2（应用层往返，跨机器时钟无关）；无 RTT 时回退 ts 差估算。"""
        if self.round_trip_ms > 0:
            return self.round_trip_ms / 2.0
        return self.latency_ms

    def _authenticate(self, sock):
        """与服务端完成认证阶段：优先用已保存/缓存凭据自动登录，必要时弹框询问。"""
        attempts = 0
        while attempts < 3:
            attempts += 1
            user = self.username
            password = self._password
            if not user or not password:
                password = _dpapi_decrypt(self.auth.get("password_enc")) if self.auth else None
            if not user or not password:
                if self._anon_ok:
                    # 已确认准入关闭（匿名放行）：每次重连直接 probe，不弹框
                    action, user, password = "probe", "", ""
                elif not self._auth_probed:
                    # 首次无凭据时先发探测：服务端准入关闭会直接放行，
                    # 就不需要再弹登录框；服务端仍要求认证时下一轮再弹框。
                    self._auth_probed = True
                    action, user, password = "probe", "", ""
                else:
                    result = self._ask_credentials()
                    if result is None:  # 用户取消
                        self._prompt_blocked_until = time.time() + 30
                        self.auth_msg = "用户取消登录"
                        return False
                    action, user, password = result
                    self.username = user
            else:
                action = "login"
            try:
                send_msg(sock, {"action": action, "user": user, "pass": password})
                sock.settimeout(5.0)
                reply = recv_msg(sock)
            except Exception as exc:
                self.auth_msg = "认证通信失败: %s" % exc
                log.warning("频道[%s] 认证通信失败: %s", self.name, exc)
                return False
            if reply.get("ok"):
                self._auth_probed = True
                if user:
                    self._password = password
                    enc = _dpapi_encrypt(password)
                    if enc is not None:
                        self.auth = {"username": user, "password_enc": enc}
                        try:
                            if self.owner is not None:
                                self.owner._save_channels()
                        except Exception as exc:  # 配置写失败不应中断视频流
                            log.warning("频道[%s] 凭据持久化失败: %s", self.name, exc)
                else:
                    # 无用户名即匿名放行（准入关闭）：记住以便重连直接 probe
                    self._anon_ok = True
                self.auth_msg = ""
                log.info("频道[%s] 以用户 %s 登录成功", self.name, user or "匿名(准入关闭)")
                return True
            msg = reply.get("msg", "")
            if msg in ("用户名已存在", "用户名或密码错误", "账号不存在"):
                self._prompt_blocked_until = time.time() + 30
                self.auth_msg = "认证失败：%s" % msg
                # 凭据失效（改密/账号删除等）：清除缓存，重连时重新询问，避免无限重试旧凭据
                self._password = None
                self.username = ""
                if self.auth:
                    self.auth = {}
                    try:
                        if self.owner is not None:
                            self.owner._save_channels()
                    except Exception:
                        pass
                log.warning("频道[%s] %s", self.name, msg)
                return False
            # 其余情况（如服务端异常响应）继续循环，下一轮会弹框询问注册或登录
        return False

    def _ask_credentials(self):
        """请求主线程弹出登录/注册对话框；返回 ("login"|"register", user, password) 或 None。

        接收线程只把请求放入队列并等待结果；对话框由主线程（poll 回调）直接创建，
        与首启向导同款标准模态窗口。跨线程 root.after 派生的对话框在 Windows 上存在
        键盘输入无法到达的问题（焦点上下文与 Tk 内部状态不同步）。
        """
        if time.time() < self._prompt_blocked_until:
            return None
        owner = self.owner
        if owner is None:
            return None
        # 等待 Tk root 创建且 mainloop 真正启动后再入队：接收线程在 __init__ 期间即可能
        # 到达此处（root 尚为 None）或处于首启向导 wait_window 期间（mainloop 未启动）。
        # 轮询等待而不是直接返回 None，可避免误入 30 秒冷却后狂重试。
        deadline = time.time() + 60
        while time.time() < deadline:
            root = owner.root
            ready = getattr(owner, "_mainloop_ready", None)
            if root is not None and (ready is None or ready.is_set()):
                break
            time.sleep(0.1)
        else:
            return None
        done = threading.Event()
        owner._cred_requests.put((self, done))
        done.wait(timeout=120)
        if not done.is_set():
            return None
        result = getattr(self, "_cred_result", None)
        self._cred_result = None
        if result is None or "error" in result or "value" not in result:
            return None
        return result.get("value")

    def _store_frame(self, frame, ts_us):
        """缓存最新帧并刷新 1 秒滑动窗口 fps 与端到端延迟（最近 5 帧滑动平均）。

        仅对解码成功的有效帧调用；无效帧/解码失败不计入延迟统计。
        """
        now = time.monotonic()
        self._fps_times.append(now)
        cutoff = now - 1.0
        while self._fps_times and self._fps_times[0] <= cutoff:
            self._fps_times.pop(0)
        # 端到端估算延迟 = 到达时刻（微秒）− 采集时间戳（时钟近似同步假设）。
        # 时钟不同步时会出现异常值：负数与超过 10 秒的值一律丢弃（不进入统计），
        # 避免跨机器时钟偏差导致面板/悬浮窗显示巨大的虚假延迟
        latency_us = time.time_ns() // 1000 - ts_us
        if 0 <= latency_us <= 10_000_000:
            self._latency_samples.append(latency_us)
            if len(self._latency_samples) > 5:
                self._latency_samples.pop(0)
        with self.lock:
            self.frame_serial += 1
            self.last_frame = frame
            self.fps = float(len(self._fps_times))
            if self._latency_samples:
                self.latency_ms = (
                    sum(self._latency_samples) / len(self._latency_samples) / 1000.0
                )
            else:
                self.latency_ms = 0.0

    # ---------- MultiView：共享开关 / 观看源切换 ----------

    def request_keyframe(self):
        """供接收线程响应 host 转达的 req_keyframe：提示上传器补关键帧。"""
        with self._share_lock:
            if self._share is not None:
                self._share.request_keyframe()

    def set_share(self, on):
        """开/关本频道屏幕共享。返回 (ok, 提示)；开时校验对方能力（cap 确认）。"""
        with self._share_lock:
            if not on:
                self.share_enabled = False
                self._stop_share()
                log.info("频道[%s] 共享已关闭", self.name)
                return True, ""
            if self.share_enabled and self._share is not None:
                return True, ""
            if not self.multiview:
                # 能力尚未确认：cap_probe 已随连接发出，等 cap/peers（≤2s）
                if not self._cap_event.wait(2.0):
                    return False, "对方不支持共享上传（旧版服务端或无响应）"
            if self.status != "receiving":
                return False, "频道未连接，无法开启共享"
            self.share_enabled = True
            ok, err = self._start_share()
            if not ok:
                self.share_enabled = False
            return ok, err

    def switch_source(self, source):
        """切换观看源："local" 或 peers 中的 "peer:<n>"。成功返回 True。"""
        with self.lock:
            valid_ids = [p.get("id") for p in self.peers]
        if source != "local" and source not in valid_ids:
            log.warning("频道[%s] 源 %r 不在可用列表，拒绝切换", self.name, source)
            return False
        with self.lock:
            if self.watch_source == source:
                return True
            self.watch_source = source
            # 重置解码参考状态：新源从关键帧开始（host 门控 + 本地 _got_key 双保险）
            self._got_key = False
            self.video_mode = False
            self.cur_codec = CODEC_H264
            self.last_frame = None
        sock = self._active_sock
        if sock is None:
            return False
        try:
            with self._tx_lock:
                send_msg(sock, {"action": "watch", "source": source})
                if source == "local":
                    # 本地源由 host 编码线程消费 force_key；看 peer 由 host 转达
                    send_msg(sock, {"action": "req_keyframe", "t": time.time_ns()})
        except (OSError, ValueError):
            return False
        log.info("频道[%s] 观看源切换为 %s", self.name, source)
        return True

    def _start_share(self):
        """创建并启动上传会话（须持 _share_lock）。返回 (ok, err)。"""
        sock = self._active_sock
        if sock is None:
            return False, "频道未连接"
        if self._share is not None:
            try:
                if self._share.alive:
                    return True, ""
                self._share.stop()  # 旧会话已死：清理后重建
            except Exception:
                pass
            self._share = None
        try:
            cfg = self.owner.cfg if self.owner is not None else {}
            session = share_mod.ScreenShareSession(
                sock, self._tx_lock, cfg, self.name)
            session.start()
        except Exception as e:
            log.warning("频道[%s] 共享启动失败: %s", self.name, e)
            return False, "共享启动失败: %s" % e
        self._share = session
        log.info("频道[%s] 已开始共享本机屏幕", self.name)
        return True, ""

    def _stop_share(self):
        """停止并清空共享会话（须持 _share_lock 或由本方法自取）。"""
        with self._share_lock:
            if self._share is not None:
                try:
                    self._share.stop()
                except Exception:
                    pass
                self._share = None

    def _maybe_resume_share(self):
        """cap/peers 确认后：若共享开关仍开且无会话则启动（重连/新连接自动恢复）。"""
        with self._share_lock:
            if not self.share_enabled or self._share is not None:
                return
            if not self.multiview:
                return
            ok, err = self._start_share()
            if not ok:
                self.share_enabled = False
                self._notify_share_error(err)

    def _share_sync_after_connect(self, sock):
        """连接后 2 秒能力确认兜底：cap 未达视为旧 host，自动关共享并提示一次。"""
        if self._active_sock is not sock:
            return  # 已重连/断开：旧定时器作废
        with self._share_lock:
            if not self.share_enabled:
                return
            if self.multiview:
                ok, err = self._start_share()
                if not ok:
                    self.share_enabled = False
                    self._notify_share_error(err)
                return
            self.share_enabled = False
            log.info("频道[%s] 对方不支持共享上传，已自动关闭共享", self.name)
        self._notify_share_error("对方不支持共享上传（旧版服务端），共享已自动关闭")

    def _notify_share_error(self, text):
        """把共享错误提示调度到主线程状态栏（Owner 在且存活时）。"""
        name = self.name
        owner = self.owner
        if owner is None:
            return

        def _show():
            try:
                owner._panel_set_status("频道[%s] %s" % (name, text), error=True)
            except Exception:
                pass

        owner._post_ui(_show)

    def _on_peers_updated(self):
        """peers 更新（接收线程）：观看的 peer 已离线则自动回落 local；通知 owner。"""
        with self.lock:
            watching = self.watch_source
            ids = [p.get("id") for p in self.peers]
        if watching != "local" and watching not in ids:
            log.info("频道[%s] 观看源 %s 已离线，自动切回本地画面", self.name, watching)
            self.switch_source("local")
            self._notify_share_error("队友已离线，频道[%s]已切回本地画面" % self.name)
        self._notify_owner_peers()

    def _notify_owner_peers(self):
        """通知 owner 刷新面板源列表（跨线程经 root.after）。"""
        owner = self.owner
        if owner is None:
            return

        def _refresh():
            try:
                owner._on_peers_changed()
            except Exception:
                pass

        owner._post_ui(_refresh)

    def stop(self):
        """停止接收线程与共享上传（daemon 线程，无需 join）。"""
        self._running = False
        self._stop_share()


class ViewerApp:
    """观看端应用：多频道接收 + tkinter 悬浮窗 + 控制台面板 + 全局热键。"""

    def __init__(self, cfg):
        self.cfg = cfg
        viewer_cfg = cfg["viewer"]
        self.display_width = int(viewer_cfg.get("display_width", 480))
        self.alpha = float(viewer_cfg.get("alpha", 0.9))
        self.click_through = bool(viewer_cfg.get("click_through", True))
        self.panel_topmost = bool(viewer_cfg.get("panel_topmost", True))

        # 频道列表：为空时回退到 server_addr 生成默认单频道（向后兼容）
        channel_items = viewer_cfg.get("channels") or []
        if not channel_items:
            server_addr = viewer_cfg.get("server_addr", "127.0.0.1:5700")
            channel_items = [{"name": "默认频道", "addr": server_addr}]
        self.channels = [
            Channel(
                item.get("name") or ("频道%d" % (i + 1)),
                item.get("addr", ""),
                auth=item.get("auth") if isinstance(item.get("auth"), dict) else None,
                owner=self,
            )
            for i, item in enumerate(channel_items)
        ]
        self.active_idx = 0

        self.running = True
        self.has_frame = False
        self._last_text = None
        self._photo = None
        self._last_frame_serial = -1
        self._last_channel_idx = -1
        self.drag_offset = None
        self.user_moved = False
        self._mainloop_ready = threading.Event()  # mainloop 真正启动后置位，供接收线程安全调度 UI
        self._cred_requests = queue.Queue()  # 认证对话框请求队列（接收线程入队，主线程 poll 处理）
        self._channels_lock = threading.RLock()  # 保护频道列表与配置持久化（接收线程也会写凭据）
        self.root = None
        self.label = None
        self.status_overlay = None  # 悬浮窗帧率/延迟状态叠加 Label
        self.menu = None
        self.hwnd = None
        self.panel = None

    # ---------- 生命周期 ----------

    def run(self):
        """启动 Tk 窗口、控制台面板、热键监听并进入主循环。"""
        global _TK_ROOT
        self.root = tk.Tk()
        _TK_ROOT = self.root
        splash.show_splash(self.root)
        self._build_ui()
        self._maybe_run_wizard()
        self._build_panel()
        self.start_hotkey()
        self._refresh_overlay()
        self.root.after(15, self.poll)
        log.info("频道列表：")
        for i, ch in enumerate(self.channels):
            log.info("  [%d] %s -> %s", i, ch.name, ch.addr)
        log.info("控制台：Ctrl+Alt+←/→ 切换频道，Ctrl+Alt+X 切换鼠标穿透")
        # 置位后接收线程才能安全调用 root.after 弹认证对话框（避免 mainloop 未启动时
        # 从子线程调用 Tk 触发 RuntimeError，导致对话框永远不弹出）
        self._mainloop_ready.set()
        try:
            self.root.mainloop()
        finally:
            self.running = False
            for ch in self.channels:
                ch.stop()
            try:
                self.root.destroy()
            except Exception:
                pass

    def quit(self):
        """退出：停止所有频道、退出主循环并销毁窗口。"""
        self.running = False
        for ch in self.channels:
            ch.stop()
        try:
            self.root.quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---------- 频道管理（供面板调用） ----------

    def switch_channel(self, i):
        """切换到指定频道（下标越界时 clamp），并立即用该频道状态刷新悬浮窗。"""
        if not self.channels:
            return
        self.active_idx = max(0, min(i, len(self.channels) - 1))
        log.info("切换到频道[%s] %s",
                 self.channels[self.active_idx].name,
                 self.channels[self.active_idx].addr)
        self._refresh_overlay()

    def add_channel(self, name, addr):
        """新增频道：校验地址非空且不重复，写回配置，返回新频道下标。"""
        addr = (addr or "").strip()
        if not addr:
            raise ValueError("频道地址不能为空")
        with self._channels_lock:
            for ch in self.channels:
                if ch.addr == addr:
                    raise ValueError("频道地址已存在: %s" % addr)
            name = (name or "").strip() or ("频道%d" % (len(self.channels) + 1))
            self.channels.append(Channel(name, addr, owner=self))
            self._save_channels()
            idx = len(self.channels) - 1
        log.info("新增频道[%s] %s", name, addr)
        return idx

    def remove_channel(self, i):
        """移除频道：若删除的是活动频道则切到相邻频道；写回配置。"""
        if not self.channels:
            return
        i = max(0, min(i, len(self.channels) - 1))
        with self._channels_lock:
            was_active = (i == self.active_idx)
            removed = self.channels.pop(i)
            removed.stop()
            log.info("删除频道[%s] %s", removed.name, removed.addr)
            if was_active:
                self.active_idx = max(0, min(i, len(self.channels) - 1))
            elif self.active_idx > i:
                self.active_idx -= 1
            self._save_channels()
        self._refresh_overlay()

    def get_channels_snapshot(self):
        """返回 [{"name","addr","status","fps","latency","video","codec"}]，实时读取。"""
        snapshot = []
        for ch in self.channels:
            with ch.lock:
                fps = ch.fps
                latency = ch.effective_latency_ms()
            snapshot.append({
                "name": ch.name,
                "addr": ch.addr,
                "status": ch.status,
                "fps": fps,
                "latency": latency,  # 端到端延迟（RTT/2，0 表示暂无数据）
                "video": ch.video_mode,  # 是否视频编码流（展示用）
                "codec": ch.cur_codec,   # 当前视频流编码类型（H.264/HEVC，展示用）
            })
        return snapshot

    def _save_channels(self):
        """将当前频道列表（name/addr/auth）写回配置（线程安全）。"""
        with self._channels_lock:
            self.cfg["viewer"]["channels"] = [
                {
                    "name": ch.name,
                    "addr": ch.addr,
                    **({"auth": ch.auth} if ch.auth else {}),
                }
                for ch in self.channels
            ]
            save_config(self.cfg)

    # ---------- 设置方法（供面板调用） ----------

    def set_display_width(self, v):
        self.display_width = max(1, int(v))
        self.cfg["viewer"]["display_width"] = self.display_width
        save_config(self.cfg)
        self._refresh_overlay()

    def set_alpha(self, v):
        self.alpha = max(0.0, min(1.0, float(v)))
        try:
            self.root.attributes("-alpha", self.alpha)
        except Exception:
            pass
        self.cfg["viewer"]["alpha"] = self.alpha
        save_config(self.cfg)

    def set_click_through(self, b):
        self.click_through = bool(b)
        self.apply_click_through()
        self.cfg["viewer"]["click_through"] = self.click_through
        save_config(self.cfg)

    def set_panel_topmost(self, b):
        self.panel_topmost = bool(b)
        if self.panel is not None:
            try:
                self.panel.attributes("-topmost", self.panel_topmost)
            except Exception:
                pass
        self.cfg["viewer"]["panel_topmost"] = self.panel_topmost
        save_config(self.cfg)

    # ---------- tkinter 悬浮窗 ----------

    def _build_ui(self):
        root = self.root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", self.alpha)
        root.configure(bg="black")

        self.label = tk.Label(
            root, text="%s · 连接中…" % APP_TITLE, fg="white", bg="black",
            font=("Microsoft YaHei", 12),
        )
        self.label.pack()

        # 状态叠加：帧率与端到端延迟（深色半透明感：黑底浅字、小号字体，置于画面下方）
        self.status_overlay = tk.Label(
            root, text="-", fg="#e6e6ef", bg="#000000",
            font=("Microsoft YaHei", 9),
        )
        self.status_overlay.pack(fill="x")

        root.update_idletasks()
        self._position_top_right()

        root.bind("<Escape>", lambda e: self.quit())

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="切换鼠标穿透", command=self.toggle_click_through)
        self.menu.add_command(label="退出", command=self.quit)

        self.label.bind("<Button-3>", self._on_right_click)
        self.label.bind("<Button-1>", self._on_drag_start)
        self.label.bind("<B1-Motion>", self._on_drag_move)

        self.hwnd = self._get_hwnd()
        self.apply_click_through()
        if self.click_through:
            log.info("鼠标穿透已开启，按 Ctrl+Alt+X 关闭穿透后可使用右键菜单/拖动/Esc 退出")
        else:
            log.info("鼠标穿透已关闭，可拖动窗口，右键菜单/Esc 退出，按 Ctrl+Alt+X 开启穿透")

    def _build_panel(self):
        """创建"FPS 队友画面控制台"控制面板（深色主题，纯 tkinter/ttk）。"""
        panel = tk.Toplevel(self.root)
        panel.title("%s · FPS 画面控制台" % APP_TITLE)
        panel.configure(bg=COL_BG)
        self.panel = panel
        if self.panel_topmost:
            panel.attributes("-topmost", True)

        # ---------- 深色主题（基于 clam） ----------
        style = ttk.Style(panel)
        style.theme_use("clam")
        style.configure("Panel.TFrame", background=COL_BG)
        style.configure("Card.TFrame", background=COL_CARD)
        style.configure("Panel.TLabel", background=COL_BG, foreground=COL_FG)
        style.configure("Card.TLabel", background=COL_CARD, foreground=COL_FG)
        style.configure("PanelDim.TLabel", background=COL_BG, foreground=COL_DIM)
        style.configure("CardDim.TLabel", background=COL_CARD, foreground=COL_DIM)
        style.configure("Title.TLabel", background=COL_BG, foreground=COL_FG,
                        font=("Microsoft YaHei", 14, "bold"))
        style.configure("Subtitle.TLabel", background=COL_BG, foreground=COL_DIM,
                        font=("Microsoft YaHei", 8))
        style.configure("Section.TLabel", background=COL_CARD, foreground=COL_DIM,
                        font=("Microsoft YaHei", 9, "bold"))
        style.configure("Panel.TButton", background=COL_CTRL, foreground=COL_FG,
                        bordercolor=COL_CTRL, padding=(10, 4))
        style.map("Panel.TButton",
                  background=[("active", "#3a3a52"), ("pressed", "#33334a")])
        style.configure("Accent.TButton", background=COL_ACCENT,
                        foreground="#ffffff", bordercolor=COL_ACCENT, padding=(12, 4))
        style.map("Accent.TButton",
                  background=[("active", "#8a70ff"), ("pressed", "#6b4de8")])
        style.configure("Panel.TEntry", fieldbackground=COL_CTRL, foreground=COL_FG,
                        bordercolor=COL_CTRL, lightcolor=COL_CTRL,
                        darkcolor=COL_CTRL, insertcolor=COL_FG)
        style.configure("Panel.Treeview", background=COL_CARD, fieldbackground=COL_CARD,
                        foreground=COL_FG, bordercolor=COL_CARD, rowheight=24,
                        font=("Microsoft YaHei", 9))
        style.map("Panel.Treeview",
                  background=[("selected", "#4a3f8a")],
                  foreground=[("selected", "#ffffff")])
        style.configure("Panel.Treeview.Heading", background=COL_CTRL,
                        foreground=COL_FG, bordercolor=COL_CTRL, relief="flat",
                        font=("Microsoft YaHei", 9, "bold"))
        style.map("Panel.Treeview.Heading", background=[("active", COL_CTRL)])
        style.configure("Panel.Horizontal.TScale", background=COL_ACCENT,
                        troughcolor="#3a3a52", bordercolor=COL_CARD,
                        lightcolor=COL_ACCENT, darkcolor=COL_ACCENT)
        style.configure("Panel.TCheckbutton", background=COL_CARD, foreground=COL_FG)
        style.map("Panel.TCheckbutton",
                  background=[("active", COL_CARD)], foreground=[("active", COL_FG)])

        # ---------- 顶部：标题 + 快捷键提示 ----------
        header = ttk.Frame(panel, style="Panel.TFrame")
        header.pack(fill="x", padx=14, pady=(12, 4))
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        if bool(self.cfg["viewer"].get("hotkeys", {}).get("direct", True)):
            hint = "Ctrl+Alt+←/→ 切换   Alt+1..9 直达   Ctrl+Alt+X 穿透"
        else:
            hint = "Ctrl+Alt+←/→ 切换   Ctrl+Alt+X 穿透"
        ttk.Label(header, text=hint,
                  style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))

        # ---------- 频道区（卡片） ----------
        card = ttk.Frame(panel, style="Card.TFrame")
        card.pack(fill="x", padx=14, pady=6)
        ttk.Label(card, text="频道", style="Section.TLabel").pack(anchor="w",
                                                                  padx=10, pady=(8, 2))

        tree = ttk.Treeview(
            card, columns=("status", "name", "addr", "fps", "latency"),
            show="headings", style="Panel.Treeview", height=7,
        )
        tree.heading("status", text="状态")
        tree.heading("name", text="名称")
        tree.heading("addr", text="地址")
        tree.heading("fps", text="帧率")
        tree.heading("latency", text="延迟(ms)")
        tree.column("status", width=40, minwidth=36, stretch=False, anchor="center")
        tree.column("name", width=120, minwidth=80, stretch=True)
        tree.column("addr", width=150, minwidth=100, stretch=True)
        tree.column("fps", width=60, minwidth=50, stretch=False, anchor="center")
        tree.column("latency", width=70, minwidth=60, stretch=False, anchor="center")
        tree.tag_configure("row_active", background=COL_ACTIVE_ROW)
        tree.tag_configure("st_receiving", foreground=COL_ONLINE)
        tree.tag_configure("st_connecting", foreground=COL_CONNECTING)
        tree.tag_configure("st_disconnected", foreground=COL_OFFLINE)
        tree.pack(fill="x", padx=10)
        tree.bind("<<TreeviewSelect>>", self._panel_on_select)
        tree.bind("<Double-1>", self._panel_on_double)
        self._tree = tree

        # 操作行：名称/地址输入 + 添加/切换/删除
        ops = ttk.Frame(card, style="Card.TFrame")
        ops.pack(fill="x", padx=10, pady=(8, 10))
        self._name_var = tk.StringVar()
        self._addr_var = tk.StringVar()
        ttk.Entry(ops, textvariable=self._name_var, width=10,
                  style="Panel.TEntry").pack(side="left", padx=(0, 6))
        ttk.Entry(ops, textvariable=self._addr_var, width=14,
                  style="Panel.TEntry").pack(side="left", padx=(0, 6))
        ttk.Button(ops, text="添加", style="Accent.TButton",
                   command=self._panel_add).pack(side="left", padx=(0, 6))
        ttk.Button(ops, text="切换", style="Panel.TButton",
                   command=self._panel_switch).pack(side="left", padx=(0, 6))
        ttk.Button(ops, text="删除", style="Panel.TButton",
                   command=self._panel_remove).pack(side="left")

        # ---------- 预览区（卡片） ----------
        pcard = ttk.Frame(panel, style="Card.TFrame")
        pcard.pack(fill="x", padx=14, pady=6)
        ttk.Label(pcard, text="预览", style="Section.TLabel").pack(anchor="w",
                                                                  padx=10, pady=(8, 2))
        self._preview_label = tk.Label(pcard, text="暂无画面", bg=COL_CARD, fg=COL_DIM,
                                       font=("Microsoft YaHei", 10), anchor="center")
        self._preview_label.pack(padx=10)
        self._preview_info = tk.Label(pcard, text="", bg=COL_CARD, fg=COL_DIM,
                                      font=("Microsoft YaHei", 9), anchor="w")
        self._preview_info.pack(anchor="w", padx=10, pady=(4, 8))
        self._panel_photo = None  # 保存 ImageTk.PhotoImage 引用，防止被垃圾回收

        # ---------- 设置区（卡片） ----------
        scard = ttk.Frame(panel, style="Card.TFrame")
        scard.pack(fill="x", padx=14, pady=6)
        ttk.Label(scard, text="设置", style="Section.TLabel").pack(anchor="w",
                                                                  padx=10, pady=(8, 2))

        # 显示宽度 160~960
        row1 = ttk.Frame(scard, style="Card.TFrame")
        row1.pack(fill="x", padx=10, pady=4)
        ttk.Label(row1, text="显示宽度", style="Card.TLabel").pack(side="left")
        self._width_var = tk.DoubleVar(value=self.display_width)
        self._width_val = ttk.Label(row1, text=str(int(self.display_width)),
                                    style="CardDim.TLabel", width=5, anchor="e")
        self._width_val.pack(side="right", padx=(8, 0))
        ttk.Scale(row1, from_=160, to=960, variable=self._width_var,
                  style="Panel.Horizontal.TScale",
                  command=self._on_display_width).pack(side="left", fill="x",
                                                       expand=True, padx=10)

        # 透明度 30%~100%（对应悬浮窗 alpha 0.30~1.00）
        row2 = ttk.Frame(scard, style="Card.TFrame")
        row2.pack(fill="x", padx=10, pady=4)
        ttk.Label(row2, text="透明度", style="Card.TLabel").pack(side="left")
        self._alpha_var = tk.DoubleVar(value=self.alpha * 100.0)
        self._alpha_val = ttk.Label(row2, text="%d%%" % int(self.alpha * 100.0),
                                    style="CardDim.TLabel", width=5, anchor="e")
        self._alpha_val.pack(side="right", padx=(8, 0))
        ttk.Scale(row2, from_=30, to=100, variable=self._alpha_var,
                  style="Panel.Horizontal.TScale",
                  command=self._on_alpha).pack(side="left", fill="x",
                                               expand=True, padx=10)

        # 鼠标穿透 / 面板置顶
        row3 = ttk.Frame(scard, style="Card.TFrame")
        row3.pack(fill="x", padx=10, pady=(4, 10))
        self._click_var = tk.BooleanVar(value=self.click_through)
        ttk.Checkbutton(row3, text="鼠标穿透", variable=self._click_var,
                        style="Panel.TCheckbutton",
                        command=self._on_click_through).pack(side="left", padx=(0, 18))
        self._top_var = tk.BooleanVar(value=self.panel_topmost)
        ttk.Checkbutton(row3, text="面板置顶", variable=self._top_var,
                        style="Panel.TCheckbutton",
                        command=self._on_panel_topmost).pack(side="left")

        # ---------- 底部：状态标签 + 退出 ----------
        footer = ttk.Frame(panel, style="Panel.TFrame")
        footer.pack(fill="x", padx=14, pady=(4, 12))
        self._status_label = tk.Label(footer, text="就绪", bg=COL_BG, fg=COL_DIM,
                                      font=("Microsoft YaHei", 9), anchor="w")
        self._status_label.pack(side="left", fill="x", expand=True)
        ttk.Button(footer, text="退出", style="Panel.TButton",
                   command=self.quit).pack(side="right", padx=(8, 0))
        ttk.Button(footer, text="关于", style="Panel.TButton",
                   command=self._show_about).pack(side="right", padx=(8, 0))
        self._diag_btn = ttk.Button(footer, text="一键诊断", style="Panel.TButton",
                                    command=self._run_diagnostics)
        self._diag_btn.pack(side="right", padx=(8, 0))

        # ---------- 面板状态变量 ----------
        self._panel_selected = None          # 预览/操作目标行；None 时跟随活动频道
        self._panel_known_count = None       # 上次列表行数（行数变化时重置选中）
        self._panel_suppress_select = False  # 重建列表时抑制选择事件
        self._status_after_id = None         # 底部状态标签的恢复定时器
        self._panel_last_serial = -1         # 预览已绘制的帧序号，避免重复缩放
        self._panel_last_idx = -1            # 预览已绘制的频道下标

        # 关闭面板 = 隐藏（不退出程序）；overlay 右键菜单增加"显示控制台"
        panel.protocol("WM_DELETE_WINDOW", self._panel_hide)
        self.menu.insert(1, "command", label="显示控制台", command=self._panel_show)

        # 定位到屏幕右上角（悬浮窗左侧留边距）
        panel.update_idletasks()
        panel.geometry("+%d+%d" % (
            panel.winfo_screenwidth() - panel.winfo_reqwidth() - 40, 60))

        # 立即刷新一次，并进入 500ms / 200ms 周期调度
        self._panel_refresh_list()
        self._panel_refresh_preview()

    # ---------- 面板：显示/隐藏 ----------

    def _panel_hide(self):
        """关闭面板：隐藏而非退出程序。"""
        self.panel.withdraw()

    def _panel_show(self):
        """重新显示面板（overlay 右键菜单"显示控制台"）。"""
        self.panel.deiconify()
        self.panel.lift()
        if self.panel_topmost:
            self.panel.attributes("-topmost", True)

    # ---------- 面板：列表/预览刷新（均为主线程 after 调度） ----------

    def _panel_refresh_list(self):
        """每 500ms 重建频道列表：状态点、帧率、活动行高亮，保留选中行。"""
        if not self.running or self.panel is None:
            return
        snapshot = self.get_channels_snapshot()
        n = len(snapshot)
        if n != self._panel_known_count:
            # 行数变化：选中重置为活动频道
            self._panel_selected = self.active_idx if n else None
            self._panel_known_count = n
        tree = self._tree
        self._panel_suppress_select = True
        tree.delete(*tree.get_children())
        for i, item in enumerate(snapshot):
            tags = ["st_%s" % item["status"]]
            if i == self.active_idx:
                tags.insert(0, "row_active")
            latency = item["latency"]
            name_txt = item["name"]
            if item.get("video"):
                codec_tag = "HEVC" if item.get("codec") == CODEC_HEVC else "H264"
                name_txt = codec_tag + "·" + name_txt
            tree.insert("", "end", iid=str(i),
                        values=("●", name_txt, item["addr"],
                                "%d" % round(item["fps"]),
                                "-" if latency <= 0 else "%d" % round(latency)),
                        tags=tags)
        if self._panel_selected is not None and n and 0 <= self._panel_selected < n:
            tree.selection_set(str(self._panel_selected))
            tree.focus(str(self._panel_selected))
        self._panel_suppress_select = False
        try:
            self.panel.after(500, self._panel_refresh_list)
        except Exception:
            pass

    def _panel_refresh_preview(self):
        """每 200ms 刷新预览区：显示选中（默认活动）频道的最近一帧缩放图。"""
        if not self.running or self.panel is None:
            return
        idx = self._panel_selected if self._panel_selected is not None else self.active_idx
        frame = None
        name = None
        status = None
        serial = 0
        changed = True
        if self.channels and 0 <= idx < len(self.channels):
            ch = self.channels[idx]
            name, status = ch.name, ch.status
            with ch.lock:
                serial = ch.frame_serial
            changed = (idx != self._panel_last_idx or serial != self._panel_last_serial)
            if changed:
                with ch.lock:
                    if ch.last_frame is not None:
                        frame = ch.last_frame.copy()
        if frame is not None:
            if changed:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb)
                w, h = img.size
                if w > 0 and h > 0:
                    # 等比缩放到宽度 320；过高时按高度上限 165 收缩，防止面板被撑开
                    new_w, new_h = 320, max(1, int(round(h * 320.0 / w)))
                    if new_h > 165:
                        new_w, new_h = max(1, int(round(w * 165.0 / h))), 165
                    img = img.resize((new_w, new_h), Image.BILINEAR)
                    self._panel_photo = ImageTk.PhotoImage(img)
                    self._preview_label.configure(image=self._panel_photo, text="")
                    self._panel_last_serial = serial
                    self._panel_last_idx = idx
        else:
            self._panel_last_serial = -1
            self._panel_last_idx = -1
            self._panel_photo = None
            self._preview_label.configure(image=None, text="暂无画面")
        # 预览小字：频道名称与状态
        if name is None:
            self._preview_info.configure(text="", fg=COL_DIM)
        else:
            self._preview_info.configure(
                text="%s · %s" % (name, STATUS_TEXT.get(status, status)),
                fg=STATUS_COLOR.get(status, COL_DIM))
        try:
            self.panel.after(200, self._panel_refresh_preview)
        except Exception:
            pass

    # ---------- 面板：列表交互 ----------

    def _panel_on_select(self, event=None):
        """单击行：仅将该行设为预览目标，不切换活动频道。"""
        if self._panel_suppress_select:
            return
        sel = self._tree.selection()
        if sel:
            self._panel_selected = int(sel[0])

    def _panel_on_double(self, event):
        """双击行：切换到该频道。"""
        item = self._tree.identify_row(event.y)
        if item:
            self._panel_switch(int(item))

    def _panel_switch(self, idx=None):
        """切换活动频道到选中行（双击或"切换"按钮）。"""
        if idx is None:
            if self._panel_selected is None:
                self._panel_set_status("请先在列表中选择频道", error=True)
                return
            idx = self._panel_selected
        if not self.channels or not (0 <= idx < len(self.channels)):
            self._panel_set_status("频道不存在", error=True)
            return
        self.switch_channel(idx)
        self._panel_selected = idx
        self._panel_set_status("已切换到：%s" % self.channels[idx].name)

    def _panel_add(self):
        """添加频道（名称+地址），失败时在底部状态标签显示红色错误。"""
        try:
            idx = self.add_channel(self._name_var.get(), self._addr_var.get())
        except ValueError as exc:
            self._panel_set_status(str(exc), error=True)
            return
        self._name_var.set("")
        self._addr_var.set("")
        self._panel_selected = idx
        self._panel_known_count = len(self.channels)  # 同步行数，保留新行选中
        self._panel_set_status("已添加频道：%s" % self.channels[idx].name)

    def _panel_remove(self):
        """删除选中的频道。"""
        idx = self._panel_selected
        if idx is None or not self.channels or not (0 <= idx < len(self.channels)):
            self._panel_set_status("请先在列表中选择要删除的频道", error=True)
            return
        name = self.channels[idx].name
        self.remove_channel(idx)
        self._panel_selected = self.active_idx
        self._panel_known_count = len(self.channels)
        self._panel_set_status("已删除频道：%s" % name)

    # ---------- 面板：底部状态标签 ----------

    def _panel_set_status(self, text, error=False):
        """在底部状态标签显示消息（错误红色/成功绿色），3 秒后恢复默认。"""
        self._status_label.configure(text=text,
                                     fg=COL_OFFLINE if error else COL_ONLINE)
        if self._status_after_id is not None:
            try:
                self.panel.after_cancel(self._status_after_id)
            except Exception:
                pass
        self._status_after_id = self.panel.after(3000, self._panel_reset_status)

    def _panel_reset_status(self):
        self._status_after_id = None
        self._status_label.configure(text="就绪", fg=COL_DIM)

    # ---------- 关于窗口 ----------

    def _show_about(self):
        """弹出深色主题"关于"窗口（产品名/版本/版权/简介/官方文档链接）。"""
        win = tk.Toplevel(self.panel)
        win.title("关于")
        win.configure(bg=COL_BG)
        win.resizable(False, False)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        body = tk.Frame(win, bg=COL_CARD)
        body.pack(fill="both", expand=True, padx=14, pady=14)
        tk.Label(body, text=APP_NAME, bg=COL_CARD, fg=COL_FG,
                 font=("Microsoft YaHei", 18, "bold")).pack(pady=(16, 2))
        tk.Label(body, text="v%s" % APP_VERSION, bg=COL_CARD, fg=COL_DIM,
                 font=("Microsoft YaHei", 10)).pack()
        tk.Label(body, text=APP_COPYRIGHT, bg=COL_CARD, fg=COL_DIM,
                 font=("Microsoft YaHei", 9)).pack(pady=(8, 0))
        tk.Label(body, text="通过樱花内网穿透实时观看队友 FPS 画面",
                 bg=COL_CARD, fg=COL_FG, font=("Microsoft YaHei", 10)).pack(pady=(12, 0))
        tk.Label(body, text="https://doc.natfrp.com/", bg=COL_CARD, fg=COL_ACCENT,
                 font=("Microsoft YaHei", 9)).pack(pady=(4, 0))
        tk.Button(body, text="关闭", bg=COL_CTRL, fg=COL_FG,
                  activebackground="#3a3a52", activeforeground=COL_FG,
                  relief="flat", padx=18, pady=4,
                  command=win.destroy).pack(pady=(14, 16))

    # ---------- 账户认证 ----------

    def _credentials_dialog(self, channel_name):
        """弹出登录/注册模态对话框（主线程执行）；返回 ("login"|"register", user, password) 或 None。"""
        parent = self.panel if self.panel is not None else self.root
        win = tk.Toplevel(parent)
        win.title("账户认证")
        win.configure(bg=COL_BG)
        win.resizable(False, False)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        result = {}

        body = tk.Frame(win, bg=COL_CARD)
        body.pack(fill="both", expand=True, padx=16, pady=16)
        tk.Label(body, text="服务端 [%s] 需要账户" % channel_name, bg=COL_CARD,
                 fg=COL_FG, font=("Microsoft YaHei", 11, "bold")).pack(anchor="w", pady=(4, 10))
        tk.Label(body, text="用户名", bg=COL_CARD, fg=COL_DIM,
                 font=("Microsoft YaHei", 9)).pack(anchor="w")
        user_var = tk.StringVar()
        pass_var = tk.StringVar()
        user_entry = tk.Entry(body, textvariable=user_var, bg=COL_CTRL, fg=COL_FG,
                              insertbackground=COL_FG, relief="flat", highlightthickness=1,
                              highlightbackground="#3a3a52")
        user_entry.pack(fill="x", pady=(2, 8))
        tk.Label(body, text="密码", bg=COL_CARD, fg=COL_DIM,
                 font=("Microsoft YaHei", 9)).pack(anchor="w")
        pass_entry = tk.Entry(body, textvariable=pass_var, show="*", bg=COL_CTRL, fg=COL_FG,
                              insertbackground=COL_FG, relief="flat", highlightthickness=1,
                              highlightbackground="#3a3a52")
        pass_entry.pack(fill="x", pady=(2, 10))
        err = tk.Label(body, text="", bg=COL_CARD, fg=COL_OFFLINE,
                       font=("Microsoft YaHei", 9))
        err.pack(anchor="w", pady=(0, 8))

        def _submit(action):
            user = user_var.get().strip()
            password = pass_var.get()
            if not user or not password:
                err.configure(text="用户名和密码不能为空")
                return
            result["value"] = (action, user, password)
            win.destroy()

        btns = tk.Frame(body, bg=COL_CARD)
        btns.pack(fill="x")
        tk.Button(btns, text="登录", bg=COL_ACCENT, fg="#ffffff", relief="flat",
                  activebackground="#8a70ff", activeforeground="#ffffff",
                  padx=14, pady=4,
                  command=lambda: _submit("login")).pack(side="left")
        tk.Button(btns, text="注册并登录", bg=COL_CTRL, fg=COL_FG, relief="flat",
                  activebackground="#3a3a52", activeforeground=COL_FG,
                  padx=14, pady=4,
                  command=lambda: _submit("register")).pack(side="left", padx=(8, 0))
        tk.Button(btns, text="取消", bg=COL_CTRL, fg=COL_FG, relief="flat",
                  activebackground="#3a3a52", activeforeground=COL_FG,
                  padx=14, pady=4, command=win.destroy).pack(side="right")

        # 窗口居中
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        w = win.winfo_reqwidth()
        h = win.winfo_reqheight()
        win.geometry("+%d+%d" % (max(0, (sw - w) // 2), max(0, (sh - h) // 2)))
        try:
            win.grab_set()
        except Exception:
            pass
        # 强制聚焦用户名输入框，确保用户可直接输入（也便于自动化驱动）
        try:
            user_entry.focus_force()
        except Exception:
            pass
        self.root.wait_window(win)
        return result.get("value")

    # ---------- 首次使用向导 ----------

    def _maybe_run_wizard(self):
        """首次使用向导：无频道且服务端地址为默认值时引导填写共享端地址（同步模态）。"""
        viewer_cfg = self.cfg["viewer"]
        if viewer_cfg.get("channels"):
            return
        if (viewer_cfg.get("server_addr") or "") != "127.0.0.1:5700":
            return

        win = tk.Toplevel(self.root)
        win.title("首次使用向导")
        win.configure(bg=COL_BG)
        win.resizable(False, False)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        # 居中
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        w, h = 480, 270
        win.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 2)))

        tk.Label(win, text="首次使用向导", bg=COL_BG, fg=COL_FG,
                 font=("Microsoft YaHei", 14, "bold")).pack(pady=(14, 4))
        tk.Label(win, text="请输入队友的共享端地址，格式：主机:端口（每行一个，可填写多个）",
                 bg=COL_BG, fg=COL_DIM, font=("Microsoft YaHei", 9)).pack(pady=(0, 6))
        entry = tk.Text(win, bg="#2a2a3c", fg=COL_FG, insertbackground=COL_FG,
                        relief="flat", height=5, font=("Consolas", 10),
                        padx=8, pady=6, highlightthickness=1,
                        highlightbackground="#3a3a52")
        entry.pack(fill="x", padx=16)

        btns = tk.Frame(win, bg=COL_BG)
        btns.pack(fill="x", padx=16, pady=(10, 14))

        def _confirm():
            added, failed = self._wizard_apply(entry.get("1.0", "end"))
            log.info("首次使用向导完成：新增 %d 个频道，跳过 %d 行", added, failed)
            win.destroy()

        def _skip():
            log.info("用户跳过了首次使用向导")
            win.destroy()

        tk.Button(btns, text="跳过", bg=COL_CTRL, fg=COL_FG, relief="flat",
                  activebackground="#3a3a52", activeforeground=COL_FG,
                  padx=16, pady=4, command=_skip).pack(side="right")
        tk.Button(btns, text="开始使用", bg=COL_ACCENT, fg="#ffffff", relief="flat",
                  activebackground="#8a70ff", activeforeground="#ffffff",
                  padx=16, pady=4, command=_confirm).pack(side="right", padx=(0, 8))
        try:
            win.grab_set()
        except Exception:
            pass
        self.root.wait_window(win)

    def _wizard_apply(self, text):
        """解析向导输入：每行"名称 地址"或"地址"，调用 add_channel 添加；返回 (新增数, 跳过行数)。"""
        added = 0
        failed = 0
        idx = 0
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            idx += 1
            if " " in line:
                parts = line.split(None, 1)
                name, addr = parts[0], parts[1].strip()
            else:
                name, addr = "频道%d" % idx, line
            try:
                self.add_channel(name, addr)
                added += 1
            except ValueError as exc:
                failed += 1
                log.warning("向导跳过无效地址 %r：%s", line, exc)
        return added, failed

    # ---------- 一键诊断 ----------

    def _run_diagnostics(self):
        """一键诊断：后台线程执行各项检测，完成后回主线程展示报告。"""
        btn = self._diag_btn
        btn.configure(state="disabled", text="正在检测…")

        def worker():
            try:
                lines = self._collect_diagnostics()
            except Exception as exc:
                log.warning("诊断过程异常：%s", exc)
                lines = ["诊断过程异常：%s" % exc]
            try:
                self.root.after(0, lambda: self._show_diag_report(lines, btn))
            except Exception:
                pass

        threading.Thread(target=worker, name="viewer-diag", daemon=True).start()

    def _collect_diagnostics(self):
        """执行诊断检测（后台线程），返回"项目 → 结果（耗时）"报告行列表。"""
        lines = []
        # 本机网络连通性（阿里 DNS）
        t0 = time.monotonic()
        try:
            s = socket.create_connection(("223.5.5.5", 53), timeout=3)
            s.close()
            ms = int((time.monotonic() - t0) * 1000)
            lines.append("本机网络（阿里 DNS 223.5.5.5）→ 通过（%dms）" % ms)
        except Exception as exc:
            lines.append("本机网络（阿里 DNS 223.5.5.5）→ 失败：%s" % exc)
        # 各频道连通性与握手
        for ch in self.channels:
            t0 = time.monotonic()
            try:
                host, port = parse_addr(ch.addr)
                s = socket.create_connection((host, port), timeout=3)
                try:
                    client_handshake(s)
                finally:
                    s.close()
                ms = int((time.monotonic() - t0) * 1000)
                lines.append("频道[%s] %s → 通过（%dms）" % (ch.name, ch.addr, ms))
            except Exception as exc:
                lines.append("频道[%s] %s → 失败：%s" % (ch.name, ch.addr, exc))
            # 端到端延迟（RTT/2 优先，无 RTT 时回退 ts 差估算）
            eff_lat = ch.effective_latency_ms()
            if eff_lat > 0:
                lat_txt = "%d ms" % round(eff_lat)
            else:
                lat_txt = "暂无数据"
            lines.append("频道「%s」端到端延迟：%s（RTT 实测 %s）" % (
                ch.name, lat_txt,
                ("%.0f ms" % ch.round_trip_ms) if ch.round_trip_ms > 0 else "暂无"))
        # frpc 程序：同时搜索 exe 目录与 SakuraFrpLauncher 默认安装目录
        try:
            frpc_path = self.cfg["host"]["frp"].get("frpc_path", "") or "frpc.exe"
        except Exception:
            frpc_path = "frpc.exe"
        frpc_path = (frpc_path or "").strip() or "frpc.exe"
        candidates = []
        if os.path.isabs(frpc_path):
            candidates.append(frpc_path)
        else:
            candidates.append(os.path.join(exe_dir(), frpc_path))
            for alt_name in ("frpc_windows_amd64.exe", "frpc_windows_386.exe"):
                candidates.append(os.path.join(exe_dir(), alt_name))
        search_bases = [exe_dir()]
        for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "ProgramW6432"):
            base = os.environ.get(env_name)
            if base:
                search_bases.append(os.path.join(base, "SakuraFrpLauncher"))
        for base_dir in search_bases:
            for alt_name in ("frpc.exe", "frpc_windows_amd64.exe", "frpc_windows_386.exe"):
                candidates.append(os.path.join(base_dir, alt_name))
        found = None
        for candidate in candidates:
            try:
                if os.path.exists(candidate):
                    found = os.path.abspath(candidate)
                    break
            except OSError:
                continue
        if found:
            lines.append("frpc 程序 → 存在（%s）" % found)
        else:
            lines.append("frpc 程序 → 未找到（已搜索 exe 目录和 SakuraFrpLauncher 默认目录）")
        return lines

    def _show_diag_report(self, lines, btn):
        """展示诊断报告窗口（主线程）：只读文本 + 复制报告/关闭。"""
        try:
            btn.configure(state="normal", text="一键诊断")
        except Exception:
            pass
        report = "\n".join(lines)
        win = tk.Toplevel(self.panel)
        win.title("一键诊断报告")
        win.configure(bg=COL_BG)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        win.geometry("560x360")
        text = tk.Text(win, bg=COL_CARD, fg=COL_FG, insertbackground=COL_FG,
                       relief="flat", font=("Microsoft YaHei", 10),
                       padx=10, pady=8, wrap="word", state="normal")
        text.insert("1.0", report)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True, padx=12, pady=(12, 4))

        btns = tk.Frame(win, bg=COL_BG)
        btns.pack(fill="x", padx=12, pady=(4, 12))

        def _copy():
            try:
                win.clipboard_clear()
                win.clipboard_append(report)
            except Exception:
                pass

        tk.Button(btns, text="复制报告", bg=COL_CTRL, fg=COL_FG, relief="flat",
                  activebackground="#3a3a52", activeforeground=COL_FG,
                  padx=14, pady=4, command=_copy).pack(side="right")
        tk.Button(btns, text="关闭", bg=COL_CTRL, fg=COL_FG, relief="flat",
                  activebackground="#3a3a52", activeforeground=COL_FG,
                  padx=14, pady=4, command=win.destroy).pack(side="right", padx=(0, 8))

    # ---------- 面板：设置项回调 ----------

    def _on_display_width(self, v):
        val = max(160, min(960, int(float(v))))
        self._width_val.configure(text=str(val))
        self.set_display_width(val)

    def _on_alpha(self, v):
        val = max(30.0, min(100.0, float(v)))
        self._alpha_val.configure(text="%d%%" % int(val))
        self.set_alpha(val / 100.0)

    def _on_click_through(self):
        self.set_click_through(self._click_var.get())

    def _on_panel_topmost(self):
        self.set_panel_topmost(self._top_var.get())

    def _get_hwnd(self):
        """获取 Tk 窗口真实顶层 HWND，失败时回退到 winfo_id()。"""
        try:
            hwnd = win32gui.GetParent(self.root.winfo_id())
            if hwnd:
                return hwnd
        except Exception:
            pass
        return self.root.winfo_id()

    def _position_top_right(self):
        """将窗口定位到屏幕右上角（留 10 像素边距）。"""
        self.root.update_idletasks()
        w = self.root.winfo_width() or self.label.winfo_reqwidth()
        h = self.root.winfo_height() or self.label.winfo_reqheight()
        x = self.root.winfo_screenwidth() - w - 10
        y = 10
        self.root.geometry("+%d+%d" % (x, y))

    def _process_cred_requests(self):
        """主线程：处理认证对话框请求队列（接收线程只入队，由主线程直接建窗以避免
        跨线程建模态对话框导致的键盘输入无法到达问题）。每次只处理一个请求。"""
        try:
            ch, done = self._cred_requests.get_nowait()
        except queue.Empty:
            return
        result = {}
        try:
            result["value"] = self._credentials_dialog(ch.name)
        except Exception as e:
            result["error"] = e
        ch._cred_result = result
        done.set()

    def poll(self):
        """主循环轮询：每 15ms 检查活动频道是否有新帧。"""
        if not self.running:
            return
        self._process_cred_requests()
        if not self.channels:
            self._show_text("无频道")
            self.root.after(15, self.poll)
            return
        channel = self.channels[self.active_idx]
        with channel.lock:
            serial = channel.frame_serial
        if self._last_channel_idx == self.active_idx and self._last_frame_serial == serial:
            # 没有新帧：只更新叠加状态，避免反复复制/缩放/绘制同一画面
            self._update_status_overlay()
            self.root.after(15, self.poll)
            return
        frame = None
        with channel.lock:
            if channel.last_frame is not None:
                frame = channel.last_frame.copy()
        if frame is not None:
            self._show_frame(frame, serial)
        else:
            self.has_frame = False
            if channel.status == "disconnected":
                self._show_text("已断开，正在重连…")
            elif channel.status == "auth":
                self._show_text("需要登录")
            else:
                self._show_text("连接中…")
        self.root.after(15, self.poll)

    def _refresh_overlay(self):
        """切换频道后立即刷新悬浮窗（不等待下一个 poll 周期）。"""
        if not self.channels:
            return
        # 切换后强制重绘一次，避免沿用上一频道的帧缓存判断
        self._last_channel_idx = -1
        self._last_frame_serial = -1
        channel = self.channels[self.active_idx]
        frame = None
        serial = 0
        with channel.lock:
            serial = channel.frame_serial
            if channel.last_frame is not None:
                frame = channel.last_frame.copy()
        if frame is not None:
            self._show_frame(frame, serial)
        elif channel.status == "disconnected":
            self._show_text("已断开，正在重连…")
        elif channel.status == "auth":
            self._show_text("需要登录")
        else:
            self._show_text("连接中…")

    def _update_status_overlay(self):
        """刷新悬浮窗状态叠加：活动频道帧率与端到端延迟（无数据显示 "-"）。"""
        if self.status_overlay is None:
            return
        text = "-"
        if self.channels and 0 <= self.active_idx < len(self.channels):
            ch = self.channels[self.active_idx]
            fps_txt = "%dfps" % round(ch.fps) if ch.fps > 0 else "-"
            lat = ch.effective_latency_ms()
            lat_txt = "%dms" % round(lat) if lat > 0 else "-"
            text = "%s · %s" % (fps_txt, lat_txt)
        self.status_overlay.configure(text=text)

    def _show_frame(self, frame_bgr, serial=None):
        """显示一帧 BGR 画面：转 RGB、等比缩放到 display_width。

        `serial` 为该帧在所属频道中的序号；绘制成功后记录，供 poll 跳过重复帧。
        绘制/转换异常时记录警告并跳过该帧（保留上一帧显示），不崩溃。
        """
        if self.label is None:
            return
        self._update_status_overlay()
        try:
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            w, h = img.size
            if w <= 0 or h <= 0:
                return
            new_h = max(1, int(round(h * self.display_width / float(w))))
            img = img.resize((self.display_width, new_h), Image.BILINEAR)
            photo = ImageTk.PhotoImage(img)
            self.label.configure(image=photo, text="")
            self._photo = photo  # 保持引用，防止被垃圾回收
            self.has_frame = True
            self._last_text = None
            if serial is not None:
                self._last_frame_serial = serial
                self._last_channel_idx = self.active_idx
            if not self.user_moved:
                self._position_top_right()
        except Exception:
            log.warning("画面绘制失败，跳过该帧", exc_info=True)

    def _show_text(self, text):
        """显示占位文字（前缀产品名）；文字与画面状态均未变化时跳过，避免重复刷新。"""
        if self.label is None:
            return
        self._update_status_overlay()
        text = "%s · %s" % (APP_TITLE, text)
        if not self.has_frame and self._last_text == text:
            return
        self.has_frame = False
        self._last_text = text
        self.label.configure(image=None, text=text)
        if not self.user_moved:
            self._position_top_right()

    # ---------- 交互 ----------

    def _on_right_click(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _on_drag_start(self, event):
        self.user_moved = True
        self.drag_offset = (
            event.x_root - self.root.winfo_x(),
            event.y_root - self.root.winfo_y(),
        )

    def _on_drag_move(self, event):
        if self.drag_offset is None:
            return
        x = event.x_root - self.drag_offset[0]
        y = event.y_root - self.drag_offset[1]
        self.root.geometry("+%d+%d" % (x, y))

    def toggle_click_through(self):
        self.click_through = not self.click_through
        self.apply_click_through()
        self.cfg["viewer"]["click_through"] = self.click_through
        save_config(self.cfg)
        if self.click_through:
            log.info("鼠标穿透已开启，按 Ctrl+Alt+X 可关闭")
        else:
            log.info("鼠标穿透已关闭，可拖动窗口，右键菜单/Esc 可用")

    def apply_click_through(self):
        """设置/清除 WS_EX_LAYERED | WS_EX_TRANSPARENT 实现鼠标穿透。"""
        ex_style = win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE)
        if self.click_through:
            ex_style |= win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT
        else:
            ex_style &= ~(win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT)
        win32gui.SetWindowLong(self.hwnd, win32con.GWL_EXSTYLE, ex_style)
        win32gui.SetWindowPos(
            self.hwnd, 0, 0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
            | win32con.SWP_NOZORDER | win32con.SWP_FRAMECHANGED,
        )

    # ---------- 全局热键（Ctrl+Alt+X 穿透 / Ctrl+Alt+←→ 切换频道） ----------

    def start_hotkey(self):
        threading.Thread(
            target=self._hotkey_loop, name="viewer-hotkey", daemon=True
        ).start()

    def _hotkey_loop(self):
        user32 = ctypes.windll.user32
        registered = []
        for hid, vk in HOTKEY_DEFS:
            if user32.RegisterHotKey(None, hid, MOD_CONTROL | MOD_ALT, vk):
                registered.append(hid)
        direct_enabled = bool(self.cfg["viewer"].get("hotkeys", {}).get("direct", True))
        if direct_enabled:
            for i in range(9):
                hid = HOTKEY_ID_DIRECT_BASE + i
                if user32.RegisterHotKey(None, hid, MOD_ALT, VK_1 + i):
                    registered.append(hid)
        if not registered:
            log.warning("全局热键注册失败")
            return
        try:
            msg = ctypes.wintypes.MSG()
            while self.running:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                if msg.message != WM_HOTKEY:
                    continue
                try:
                    if msg.wParam == HOTKEY_ID_TOGGLE:
                        self.root.after(0, self.toggle_click_through)
                    elif msg.wParam == HOTKEY_ID_PREV:
                        self.root.after(0, self._step_channel, -1)
                    elif msg.wParam == HOTKEY_ID_NEXT:
                        self.root.after(0, self._step_channel, 1)
                    elif HOTKEY_ID_DIRECT_BASE <= msg.wParam < HOTKEY_ID_DIRECT_BASE + 9:
                        self.root.after(0, self.switch_channel,
                                        msg.wParam - HOTKEY_ID_DIRECT_BASE)
                except Exception:
                    pass
        finally:
            for hid in registered:
                user32.UnregisterHotKey(None, hid)

    def _step_channel(self, delta):
        """按步长切换频道（-1 上一个 / +1 下一个），越界时 clamp。"""
        self.switch_channel(self.active_idx + delta)


def main():
    parser = argparse.ArgumentParser(description="FPS 画面观看端（TCP 客户端 + 置顶悬浮窗）")
    parser.add_argument(
        "--addr", help="服务端地址 host:port，可覆盖配置文件中的 viewer.server_addr"
    )
    args = parser.parse_args()

    cfg = load_config()
    logger.setup_logger(cfg)
    if args.addr:
        cfg["viewer"]["server_addr"] = args.addr
        save_config(cfg)
        log.info("已使用命令行地址 %s 并写入配置", args.addr)

    app = ViewerApp(cfg)
    try:
        app.run()
    except Exception:
        log.exception("程序异常退出")
        try:
            messagebox.showerror(
                APP_NAME, "程序遇到错误，已写入 logs 目录日志文件"
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
