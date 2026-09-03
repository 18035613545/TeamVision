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
    enable_dpi_awareness,
    tune_socket,
    parse_message,
    parse_video,
    MSG_FRAME,
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


# 第 79 条：sys.excepthook 的安装移到 main() 里，不在 import 时无条件执行。
# 否则任何 import viewer 的进程（含单元测试套件）都会被全局换成这个会弹**模态**
# messagebox 的钩子；测试期间一旦有异常逃逸到解释器顶层，整套件就卡在 GUI 对话框后面。
# 与 host.py 的 _install_excepthooks()（同样延迟到入口调用）保持一致。

#: 全局热键修饰键
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
#: 按键
VK_X = 0x58
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_1 = 0x31
WM_HOTKEY = 0x0312
#: 各热键独立 ID：1=Ctrl+Alt+X 切换穿透，2=Ctrl+Alt+← 上一个频道，3=Ctrl+Alt+→ 下一个频道，
#: 4=Ctrl+Alt+↑ 上一个画面源，5=Ctrl+Alt+↓ 下一个画面源
HOTKEY_ID_TOGGLE = 1
HOTKEY_ID_PREV = 2
HOTKEY_ID_NEXT = 3
HOTKEY_ID_SRC_PREV = 4
HOTKEY_ID_SRC_NEXT = 5
HOTKEY_ID_DIRECT_BASE = 10  # Alt+1..9 直达频道：ID = 10..18
HOTKEY_DEFS = [
    (HOTKEY_ID_TOGGLE, VK_X),
    (HOTKEY_ID_PREV, VK_LEFT),
    (HOTKEY_ID_NEXT, VK_RIGHT),
    (HOTKEY_ID_SRC_PREV, VK_UP),
    (HOTKEY_ID_SRC_NEXT, VK_DOWN),
]

#: 热键 ID → 可读名称（注册失败时精确告警用，item 41）
HOTKEY_NAMES = {
    HOTKEY_ID_TOGGLE: "Ctrl+Alt+X 切换穿透",
    HOTKEY_ID_PREV: "Ctrl+Alt+← 上一个频道",
    HOTKEY_ID_NEXT: "Ctrl+Alt+→ 下一个频道",
    HOTKEY_ID_SRC_PREV: "Ctrl+Alt+↑ 上一个画面源",
    HOTKEY_ID_SRC_NEXT: "Ctrl+Alt+↓ 下一个画面源",
}

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


# 第 94 条：回环地址别名，去重时折叠为同一规范主机，避免 localhost 与 127.0.0.1
# 被判为两个不同频道（进而同一用户在第二个频道触发"用户名已存在"→重复登录死循环）。
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _canonical_addr(addr):
    """把地址规范化为 (host_lower, port) 用于去重比较；非法地址抛 ValueError。

    第 94 条：复用 parse_addr 校验端口（非数字/越界/缺端口即抛 ValueError，由调用方在
    面板/向导显示原因），再按规范形去重——主机名小写、缺省端口补 5700、回环别名折叠为
    127.0.0.1。不做 DNS 解析：拼写错误等无法连通的主机名只能在连接期暴露，新增时不强校验。
    """
    host, port = parse_addr(addr)          # 非法端口/空地址在此抛 ValueError
    h = (host or "").strip().lower()
    if h in _LOOPBACK_HOSTS:
        h = "127.0.0.1"
    return h, port


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
        self._video_unavailable_warned = False  # 第 11 条：解码器不可用只告警一次
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
        self._share_sync_timer = None  # 第 61 条：连接后能力确认兜底定时器（stop 时 cancel）
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
                # 第 61 条：定时器设为 daemon 并登记句柄，stop() 时 cancel。否则非守护
                # 定时器会让解释器退出多等 2 秒，且若在 teardown 后触发会调 _start_share
                # 在窗口销毁期间新開上传会话、向正在关闭的 socket 发送。重连先 cancel 旧定时器。
                if self._share_sync_timer is not None:
                    self._share_sync_timer.cancel()
                self._share_sync_timer = threading.Timer(
                    2.0, self._share_sync_after_connect, args=(sock,))
                self._share_sync_timer.daemon = True
                self._share_sync_timer.start()
                self._notify_owner_peers()
                # 第 57 条：rx_buf 用 bytearray，+= 原地扩展（摊还 O(1)），避免攒大帧时
                # bytes += 的整份重拷退化为 O(n²)。配套 del rx_buf[:consumed] 原地弹出，
                # parse_message 统一返回 bytes payload，下游解码不受缓冲区类型影响。
                rx_buf = bytearray()
                last_rx = time.monotonic()
                last_media_rx = last_rx  # 最近一次收到画面帧（第 2/12 条用）
                while self._running:
                    r, _, _ = select.select([sock], [], [], 1.0)
                    if not r:
                        now_m = time.monotonic()
                        # 第 12 条：超过 1 秒无画面帧则衰减 fps，避免死频道锁存旧帧率
                        if now_m - last_media_rx > 1.0:
                            self._note_no_media()
                        # 第 2 条：连接活着（pong 在流）但长时间收不到画面帧 → 主动请求
                        # 关键帧，打破「need_key 门控 + 仅解码失败才请求 + pong 击败停滞
                        # 检测」三者叠加导致的画面永久定格（状态仍显示 receiving）。
                        if (self.status == "receiving"
                                and now_m - last_media_rx > self._read_idle_s):
                            self._request_keyframe(sock)
                        # 完全无任何数据（含 pong）超过阈值：判定连接停滞，退出重连
                        if now_m - last_rx > self._read_idle_s:
                            raise ConnectionError(
                                "%.0f 秒未收到数据，判定连接停滞" % self._read_idle_s)
                        continue
                    chunk = sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("连接关闭")
                    last_rx = time.monotonic()
                    rx_buf += chunk
                    # 解析缓冲区中所有完整消息。第 7 条：JPEG 各帧独立可解，只留最新一份；
                    # H.264/HEVC 必须按序解码批次内全部帧以维持参考帧链（丢中间帧会花屏/
                    # 拖影直到下一个关键帧），故视频帧收集成有序列表逐帧喂解码器。
                    latest_jpeg = None       # (ts, jpeg_bytes)
                    video_batch = []         # [(ts, codec, flags, nal)]，按到达顺序
                    while True:
                        consumed, kind, payload = parse_message(rx_buf)
                        if consumed == 0:
                            break
                        del rx_buf[:consumed]
                        if kind == MSG_VIDEO:
                            try:
                                ts, codec, flags, nal = parse_video(payload)
                            except ValueError:
                                continue
                            video_batch.append((ts, codec, flags, nal))
                        elif kind == MSG_FRAME:
                            if len(payload) < 8:
                                log.warning("频道[%s] 帧 payload 过短，跳过", self.name)
                                continue
                            (ts,) = struct.unpack(">Q", payload[:8])
                            latest_jpeg = (ts, payload[8:])
                        else:  # MSG_CTRL
                            self._handle_ctrl(payload)
                    if video_batch:
                        last_media_rx = time.monotonic()  # 收到画面帧（第 2/12 条）
                        self._decode_video_batch(sock, video_batch)
                    elif latest_jpeg is not None:
                        last_media_rx = time.monotonic()  # 收到画面帧（第 2/12 条）
                        ts, data = latest_jpeg
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

    def _decode_video_batch(self, sock, batch):
        """按到达顺序解码一批 H.264/HEVC 帧（第 7 条）。

        旧路径对一个 recv 批次只解码最新一帧、丢弃中间帧——对 JPEG 无害（各帧独立），
        对 H.264 会打断参考帧链：被跳过的 NAL 不喂解码器，后续 P 帧对错误参考解码出
        花屏/拖影，直到下一个关键帧（约 2 秒）才清。这里把批次内全部 NAL 依序喂给
        有状态解码器以维持参考链，只把最后一帧成功解码的结果存为显示帧。
        """
        if not self._decoder.available:
            # 第 11 条：缺 PyAV/FFmpeg，解码器恒返回空。不再谎报 video_mode/_got_key
            # （否则面板显示 HEVC·某某但永远「连接中…」），只记一次 error，等待 JPEG
            # 帧或用户处理。
            if not self._video_unavailable_warned:
                self._video_unavailable_warned = True
                log.error(
                    "频道[%s] 视频解码器不可用（缺 PyAV/FFmpeg），"
                    "无法解码 H.264/HEVC 流；请安装 pyav 依赖",
                    self.name)
            self.video_mode = False
            self._got_key = False
            return
        last_frame = None
        last_ts = 0
        for ts, codec, flags, nal in batch:
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
                # 仅在收到关键帧后显示：重连/编码切换后首个关键帧前的 P 帧可能对旧参考
                # 解码出脏画面，一律丢弃并请求关键帧。批次内逐帧喂解码器维持参考链，
                # 但只把最后一帧存为显示帧（显示最新画面，避免冗余重绘）。
                last_frame = frames[-1]
                last_ts = ts
            else:
                # 缺参考帧（未收到关键帧/丢关键帧/批次内有缺口）：请求关键帧快速恢复
                self._request_keyframe(sock)
        if last_frame is not None:
            self._store_frame(last_frame, last_ts)
            self._retry_delay = 0.0

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
        elif action == "watch_reject":
            # 第 9 条：host 拒绝切源（订阅自身/源已失效）。本地已提交的新源未生效，
            # 回落 local（host 永不拒绝 local），避免状态栏长期谎报「源:队友·X」。
            # 仅当被拒源仍等于当前 watch_source 时才回落，免得清掉更晚发起的切源。
            rej = msg.get("source")
            with self.lock:
                watching = self.watch_source
            if isinstance(rej, str) and rej == watching and watching != "local":
                log.info("频道[%s] host 拒绝观看源 %s（reason=%s），切回本地画面",
                         self.name, rej, msg.get("reason"))
                if self.switch_source("local"):
                    self._notify_share_error(
                        "无法观看该队友，频道[%s]已切回本地画面" % self.name)
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
            # 第 29 条：host 现在要求登录（此前可能匿名放行过）。复位 _anon_ok 让下一轮
            # 真正弹框询问凭据，而不是无限发空 probe → "需要登录" → 重连（只能重启 viewer）。
            if msg == "需要登录":
                self._anon_ok = False
                self._auth_probed = True
                self.auth_msg = "需要登录"
                continue
            # 第 28 条：注册时用户名被占用并不代表已保存的登录凭据失效，绝不能据此清空
            # 凭据（原 bug：点“注册”输了个已占用的名字，该频道有效的登录凭据被抹掉）。
            if action == "register" and msg == "用户名已存在":
                self._prompt_blocked_until = time.time() + 30
                self.auth_msg = "注册失败：用户名已被占用，请换一个或直接登录"
                log.warning("频道[%s] 注册用户名已被占用：%s", self.name, user)
                return False
            if msg in ("用户名或密码错误", "账号不存在"):
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
        # 第 93 条：结果走"每请求独立 box"，不再用频道上的共享 _cred_result 槽——
        # 超时返回后用户才提交时，结果只写进这个已被放弃的 box，绝不会串给下一次请求。
        box = {}
        done = threading.Event()
        owner._cred_requests.put((self, done, box))
        done.wait(timeout=120)
        if not done.is_set():
            return None
        result = box.get("result")
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

    def _note_no_media(self):
        """超过 1 秒未收到画面帧：按当前时刻衰减 fps 显示（第 12 条）。

        fps 仅在 _store_frame 里重算，断流后窗口里的旧时间戳不会被剪掉，
        导致死频道仍显示 30fps。这里主动剪枝并把 fps 归零，使「画面静止」
        与「对方已死」在帧率显示上可区分。"""
        now = time.monotonic()
        with self.lock:
            cutoff = now - 1.0
            while self._fps_times and self._fps_times[0] <= cutoff:
                self._fps_times.pop(0)
            self.fps = float(len(self._fps_times))

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
                # 通知 host 注销本成员的共享登记（连接保留）：host 广播 roster，
                # 其他观看端据此从源列表移除本端并回落 local
                sock = self._active_sock
                if sock is not None:
                    try:
                        with self._tx_lock:
                            send_msg(sock, {"action": "unshare"})
                    except (OSError, ValueError):
                        pass
                log.info("频道[%s] 共享已关闭", self.name)
                return True, ""
            if self.share_enabled and self._share is not None and self._share.alive:
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
        """切换观看源："local" 或 peers 中的 "peer:<n>"。成功返回 True。

        第 9 条：保持「先提交本地状态、再发包」的顺序——本地解码器/_got_key 必须在
        host 开始转发新源帧前就绪，否则接收线程会对未重建的解码器喂新源数据。但发包
        失败必须回滚 watch_source，否则它停在未生效的新源、状态栏谎报「源:队友·X」，
        而 host 仍在转发原源（local）画面。
        """
        with self.lock:
            valid_ids = [p.get("id") for p in self.peers]
        if source != "local" and source not in valid_ids:
            log.warning("频道[%s] 源 %r 不在可用列表，拒绝切换", self.name, source)
            return False
        with self.lock:
            if self.watch_source == source:
                return True
            prev_source = self.watch_source  # 第 9 条：发包失败时回滚到此源
            self.watch_source = source
            # 重置解码参考状态：新源从关键帧开始（host 门控 + 本地 _got_key 双保险）
            self._got_key = False
            self.video_mode = False
            self.cur_codec = CODEC_H264
            # 第 1 条：cur_codec 强设为 H264 后必须同步重建解码器，否则从 HEVC 源
            # 切到 H.264 源时旧 HEVC 解码器被喂 H.264 数据 → 解码恒失败 → 永久黑屏。
            self._decoder = codec_mod.VideoDecoder(codec=CODEC_H264)
            self.last_frame = None
            # 第 8 条：清 last_frame 的同时递增 frame_serial，强制 poll 重绘，
            # 否则「有无新帧」判据认为没变化，画面会停留在上一个人至少一个 GOP。
            self.frame_serial += 1
        sock = self._active_sock
        if sock is None:
            self._revert_source(prev_source)  # 第 9 条：未发包，回滚避免谎报
            return False
        try:
            with self._tx_lock:
                send_msg(sock, {"action": "watch", "source": source})
                if source == "local":
                    # 本地源由 host 编码线程消费 force_key；看 peer 由 host 转达
                    send_msg(sock, {"action": "req_keyframe", "t": time.time_ns()})
        except (OSError, ValueError):
            self._revert_source(prev_source)  # 第 9 条：发包失败，回滚避免谎报
            return False
        log.info("频道[%s] 观看源切换为 %s", self.name, source)
        return True

    def _revert_source(self, prev_source):
        """第 9 条：切源发包失败后把 watch_source 回滚到原源，避免状态栏谎报。

        只回滚 watch_source（状态栏「源:…」正是读它）；提交时已做的解码门控重置
        （_got_key=False、重建解码器、清 last_frame）保持不变即可——发包失败意味着
        连接已断（sock 为 None 或 sendall 抛 OSError），此刻并无画面流到达，悬浮窗
        显示空帧才是诚实状态；连接恢复后由原源下一个关键帧重新起解、自然重绘。
        """
        with self.lock:
            self.watch_source = prev_source
        log.info("频道[%s] 切源失败，已回滚到原源 %s", self.name, prev_source)

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
            if not self.share_enabled:
                return
            if self._share is not None and self._share.alive:
                return  # 会话仍活着，无需恢复
            if not self.multiview:
                return
            ok, err = self._start_share()
            if not ok:
                self.share_enabled = False
                self._notify_share_error(err)

    def _share_sync_after_connect(self, sock):
        """连接后 2 秒能力确认兜底：cap 未达视为旧 host，自动关共享并提示一次。"""
        # 第 61 条：stop()/request_stop() 置 _running=False 后，即便定时器未及时 cancel
        # 也必须作废，绝不在 teardown 之后新開上传会话。
        if not self._running or self._active_sock is not sock:
            return  # 已停止/已重连/断开：旧定时器作废
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

    def request_stop(self):
        """第 60 条：非阻塞地请求停止——置接收线程标志 + 给共享会话发停止信号，但不 join。

        退出时先对所有频道 request_stop（各上传线程并发收尾），再逐个 stop（此时 join
        基本即刻返回），把 N 个共享频道的 2N 秒串行冻结压到约 2 秒。
        """
        self._running = False
        with self._share_lock:
            if self._share is not None:
                self._share.signal_stop()

    def stop(self):
        """停止接收线程与共享上传（daemon 线程，无需 join）。"""
        self._running = False
        # 第 61 条：cancel 能力确认兜底定时器并清 _active_sock，杜绝退出后定时器触发
        # _share_sync_after_connect → _start_share 在 teardown 期间新開上传会话。
        if self._share_sync_timer is not None:
            self._share_sync_timer.cancel()
            self._share_sync_timer = None
        self._active_sock = None
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
        # 第 98 条：绘制路径缓存——位置只取决于窗口尺寸（=照片尺寸），尺寸未变时跳过
        # 每帧的 update_idletasks + winfo 查询 + geometry 下发；状态文本未变时跳过 reconfigure。
        self._last_pos_size = None    # 上次定位所用的 (display_width, new_h)
        self._last_status_text = None  # 上次写入状态叠加的文本
        self.drag_offset = None
        self._drag_start_root = None
        self.user_moved = False
        self._mainloop_ready = threading.Event()  # mainloop 真正启动后置位，供接收线程安全调度 UI
        self._cred_requests = queue.Queue()  # 认证对话框请求队列（接收线程入队，主线程 poll 处理）
        self._cred_busy = False  # 第 93 条：认证对话框互斥标志，强制"每次只一个"，杜绝 wait_window 嵌套期间再叠一个
        self._channels_lock = threading.RLock()  # 保护频道列表与配置持久化（接收线程也会写凭据）
        self.root = None
        self.label = None
        self.status_overlay = None  # 悬浮窗帧率/延迟状态叠加 Label
        self.menu = None
        self.hwnd = None
        self.panel = None
        self._share_toggle_busy = False  # 第 60 条：共享开关异步执行期间的重入护栏
        self._save_after_id = None  # 第 64 条：viewer 配置去抖落盘的 root.after 句柄

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
        log.info("控制台：Ctrl+Alt+←/→ 切换频道，Ctrl+Alt+↑/↓ 切换画面源，Ctrl+Alt+X 切换鼠标穿透")
        # 置位后接收线程才能安全调用 root.after 弹认证对话框（避免 mainloop 未启动时
        # 从子线程调用 Tk 触发 RuntimeError，导致对话框永远不弹出）
        self._mainloop_ready.set()
        try:
            self.root.mainloop()
        finally:
            self.running = False
            self._flush_pending_save()  # 第 64 条：兜底落盘去抖期间未写出的 viewer 配置
            # 第 60 条：两阶段停止——先并发给所有频道发停止信号（非阻塞），再逐个 join，
            # 避免 N 个共享频道各 join 2 秒串行冻结退出（详见 quit）。
            for ch in self.channels:
                ch.request_stop()
            for ch in self.channels:
                ch.stop()
            try:
                self.root.destroy()
            except Exception:
                pass

    def quit(self):
        """退出：停止所有频道、退出主循环并销毁窗口。"""
        self.running = False
        self._flush_pending_save()  # 第 64 条：兜底落盘去抖期间未写出的 viewer 配置
        # 第 60 条：两阶段停止——先并发给所有频道发停止信号（非阻塞），再逐个 join。
        # 否则每个 ch.stop() 的 share.stop() 各 join 2 秒，N 个共享频道串行冻结主线程 2N 秒。
        for ch in self.channels:
            ch.request_stop()
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
        # item 42：活动频道变化后让面板选中跟随，否则源卡片/预览/共享按钮仍停在旧频道，
        # 而 Ctrl+Alt+↑/↓ 切的是 active_idx 的观看源 → 面板显示的频道与实际操作的频道不一致
        self._panel_selected = self.active_idx
        log.info("切换到频道[%s] %s",
                 self.channels[self.active_idx].name,
                 self.channels[self.active_idx].addr)
        self._refresh_overlay()
        if getattr(self, "panel", None) is not None:
            try:
                self._panel_refresh_sources()
                self._sync_share_button()
            except Exception:
                log.debug("切换频道后面板刷新失败", exc_info=True)

    def add_channel(self, name, addr):
        """新增频道：校验地址合法且不重复（按规范形去重），写回配置，返回新频道下标。"""
        addr = (addr or "").strip()
        if not addr:
            raise ValueError("频道地址不能为空")
        # 第 94 条：先校验+规范化新地址（端口非法即抛 ValueError，由调用方显示原因，
        # 避免垃圾地址落盘后接收线程无限重试只显"断开"）；再按规范形去重，杜绝
        # localhost:5700 与 127.0.0.1:5700 并存触发同用户重复登录死循环。
        new_canon = _canonical_addr(addr)
        with self._channels_lock:
            for ch in self.channels:
                try:
                    ch_canon = _canonical_addr(ch.addr)
                except ValueError:
                    continue   # 既有频道地址非法（历史遗留落盘）：不参与去重，也不阻断新增
                if ch_canon == new_canon:
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
            save_config(self.cfg, "viewer")

    def _schedule_save(self):
        """合并短时间内的多次 viewer 配置写入（第 64 条）。

        ttk.Scale 的 command 在拖动过程中连续触发，若每次都 save_config 整份 JSON
        （写 tmp + os.replace）会造成几十到上百次全盘写、拖动卡顿。这里改为去抖：
        每次改动重置一个 0.5s 定时器，只在停手后落盘一次。所有调用方（面板
        Scale/Checkbutton 回调、root.after 调度的热键）均在主线程，root.after 可直连。
        退出时由 _flush_pending_save 兜底，避免最后一次改动丢失。
        """
        root = self.root
        if root is None:
            return
        if self._save_after_id is not None:
            try:
                root.after_cancel(self._save_after_id)
            except Exception:
                pass
        try:
            self._save_after_id = root.after(500, self._flush_save)
        except Exception:
            self._save_after_id = None

    def _flush_save(self):
        """立即落盘待写的 viewer 配置（去抖到期或退出兜底时调用）。"""
        self._save_after_id = None
        try:
            save_config(self.cfg, "viewer")
        except Exception:
            log.debug("保存 viewer 配置失败", exc_info=True)

    def _flush_pending_save(self):
        """退出兜底：取消未到期的去抖定时器并立即落盘，避免最后一次改动丢失。"""
        if self._save_after_id is None:
            return
        root = self.root
        if root is not None:
            try:
                root.after_cancel(self._save_after_id)
            except Exception:
                pass
        self._flush_save()

    def _post_ui(self, fn):
        """把回调安全调度到主线程（子线程调用，root 未就绪/已销毁/退出中静默丢弃）。

        第 92 条：除 root 为空外，还拦截「mainloop 未就绪」与「已进入退出」两种状态。
        退出瞬间从外部线程调进已销毁的 Tcl 解释器不只是抛异常——可能崩溃/挂起，
        末尾的 except 兜不住。热键线程与诊断线程必须改走这里，而非直接 root.after。
        """
        if not self.running:
            return
        ready = getattr(self, "_mainloop_ready", None)
        if ready is not None and not ready.is_set():
            return
        root = self.root
        if root is None:
            return
        try:
            root.after(0, fn)
        except Exception:
            pass

    def _on_peers_changed(self):
        """频道 peers 更新回调（主线程）：立即刷新画面源卡片与共享按钮。"""
        if self.running and self.panel is not None:
            try:
                self._panel_refresh_sources()
            except Exception:
                pass

    def _panel_target_idx(self):
        """面板操作目标频道：列表选中行优先，无选中时跟随活动频道。"""
        if self._panel_selected is not None and self.channels:
            return min(self._panel_selected, len(self.channels) - 1)
        return self.active_idx if self.channels else None

    # ---------- 设置方法（供面板调用） ----------

    def set_display_width(self, v):
        self.display_width = max(1, int(v))
        self.cfg["viewer"]["display_width"] = self.display_width
        self._schedule_save()  # 第 64 条：拖动滑块时去抖落盘，画面重绘仍即时
        self._refresh_overlay()

    def set_alpha(self, v):
        self.alpha = max(0.0, min(1.0, float(v)))
        try:
            self.root.attributes("-alpha", self.alpha)
        except Exception:
            pass
        self.cfg["viewer"]["alpha"] = self.alpha
        self._schedule_save()  # 第 64 条：拖动滑块时去抖落盘，透明度调整仍即时

    def set_click_through(self, b):
        self.click_through = bool(b)
        self.apply_click_through()
        self.cfg["viewer"]["click_through"] = self.click_through
        self._schedule_save()  # 第 64 条：去抖落盘，穿透切换的 win32 效果仍即时

    def set_panel_topmost(self, b):
        self.panel_topmost = bool(b)
        if self.panel is not None:
            try:
                self.panel.attributes("-topmost", self.panel_topmost)
            except Exception:
                pass
        self.cfg["viewer"]["panel_topmost"] = self.panel_topmost
        self._schedule_save()  # 第 64 条：去抖落盘，置顶切换仍即时

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
        self.menu.add_command(label="切换鼠标穿透 (Ctrl+Alt+X)", command=self.toggle_click_through)
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
            hint = ("Ctrl+Alt+←/→ 换频道   Ctrl+Alt+↑/↓ 换画面源   "
                    "Alt+1..9 直达   Ctrl+Alt+X 穿透")
        else:
            hint = "Ctrl+Alt+←/→ 换频道   Ctrl+Alt+↑/↓ 换画面源   Ctrl+Alt+X 穿透"
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
        self._share_btn = ttk.Button(ops, text="共享本机屏幕", style="Accent.TButton",
                                     command=self._panel_toggle_share)
        self._share_btn.pack(side="left", padx=(6, 0))

        # ---------- 画面源区（卡片，MultiView） ----------
        srccard = ttk.Frame(panel, style="Card.TFrame")
        srccard.pack(fill="x", padx=14, pady=6)
        ttk.Label(srccard, text="画面源（双击或 Ctrl+Alt+↑/↓ 切换观看）",
                  style="Section.TLabel").pack(anchor="w", padx=10, pady=(8, 2))
        src_tree = ttk.Treeview(
            srccard, columns=("mark", "source", "info"),
            show="headings", style="Panel.Treeview", height=4,
        )
        src_tree.heading("mark", text="")
        src_tree.heading("source", text="源")
        src_tree.heading("info", text="说明")
        src_tree.column("mark", width=64, minwidth=56, stretch=False, anchor="center")
        src_tree.column("source", width=180, minwidth=140, stretch=True)
        src_tree.column("info", width=170, minwidth=120, stretch=True)
        src_tree.tag_configure("src_watching", foreground=COL_ONLINE)
        src_tree.pack(fill="x", padx=10, pady=(0, 10))
        src_tree.bind("<<TreeviewSelect>>", self._panel_on_src_select)
        src_tree.bind("<Double-1>", self._panel_on_src_double)
        self._src_tree = src_tree
        self._src_rows = []  # iid -> source id（"local"/"peer:<n>"）对应表
        self._src_selected = None  # 源行选中（iid）

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

    def _panel_visible(self):
        """面板是否真实可见：withdraw（隐藏）后 winfo_viewable() 为假。

        隐藏时周期刷新应跳过加锁拷贝/cvtColor/resize/重建 Treeview 等重活，
        避免不可见地白烧 CPU；但仍需续排定时器，待重新显示后自动恢复。
        查询失败时按"可见"处理，宁可多刷一次也不要让刷新链断掉。
        """
        if self.panel is None:
            return False
        try:
            return bool(self.panel.winfo_viewable())
        except Exception:
            return True

    # ---------- 面板：列表/预览刷新（均为主线程 after 调度） ----------

    def _panel_refresh_list(self):
        """每 500ms 重建频道列表：状态点、帧率、活动行高亮，保留选中行。"""
        if not self.running or self.panel is None:
            return
        if not self._panel_visible():
            # 面板隐藏：跳过重建（不可见时白烧 CPU），但续排定时器待显示后恢复
            try:
                self.panel.after(500, self._panel_refresh_list)
            except Exception:
                pass
            return
        try:
            snapshot = self.get_channels_snapshot()
            n = len(snapshot)
            if n != self._panel_known_count:
                # 行数变化：选中重置为活动频道
                self._panel_selected = self.active_idx if n else None
                self._panel_known_count = n
            tree = self._tree
            # item 40：全删全建会把滚动位置拽回顶部；重建前记下顶部可见比例，重建后恢复
            try:
                y_top = tree.yview()[0]
            except Exception:
                y_top = 0.0
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
            if y_top:
                try:
                    tree.yview("moveto", y_top)
                except Exception:
                    pass
            self._panel_suppress_select = False
            self._sync_share_button()
            self._panel_refresh_sources()
        except Exception:
            # 第 91 条：原本 tree.delete/insert、selection_set、_sync_share_button、
            # _panel_refresh_sources 全裸奔，任一抛异常（如 Treeview 内部 Tcl 错误）会在末尾
            # 续排 after 之前逃出本函数 → 列表刷新永久停更（fps/状态陈旧、源列表不再更新），
            # 而悬浮窗仍在动 → 程序看着「半活」。对照 _show_frame 本就有 try 兜底。
            log.warning("频道列表刷新失败，跳过本次（定时器续排，不停更）", exc_info=True)
        finally:
            # 异常路径下也复位选中抑制标志，避免 tree.insert/selection_set 中途抛出后
            # _panel_suppress_select 卡在 True 致用户点击行永久失效（_panel_on_select 被屏蔽）。
            self._panel_suppress_select = False
            try:
                self.panel.after(500, self._panel_refresh_list)
            except Exception:
                pass

    def _panel_refresh_preview(self):
        """每 200ms 刷新预览区：显示选中（默认活动）频道的最近一帧缩放图。"""
        if not self.running or self.panel is None:
            return
        if not self._panel_visible():
            # 面板隐藏：跳过加锁拷贝/cvtColor/resize/PhotoImage，但续排定时器待显示后恢复
            try:
                self.panel.after(200, self._panel_refresh_preview)
            except Exception:
                pass
            return
        try:
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
                        photo = ImageTk.PhotoImage(img)
                        # 先让 label 指向新图再释放旧引用：否则旧 Tk 图像在 label 仍显示时
                        # 被删除，随后的 configure 可能报 "image ... doesn't exist"
                        self._preview_label.configure(image=photo, text="")
                        self._panel_photo = photo
                        self._panel_last_serial = serial
                        self._panel_last_idx = idx
            else:
                # 仅当目标频道确无可用画面（未在线/首帧未到）才清空预览；
                # 流静止（serial 未变）但频道仍在线时保留上一帧，避免画面反复闪空
                clear = True
                if self.channels and 0 <= idx < len(self.channels):
                    with self.channels[idx].lock:
                        clear = (self.channels[idx].status != "receiving"
                                 or self.channels[idx].last_frame is None)
                if clear:
                    self._panel_last_serial = -1
                    self._panel_last_idx = -1
                    # 先让 label 脱离旧图像再释放引用（同上，避免删除仍在显示的图像）
                    self._preview_label.configure(image=None, text="暂无画面")
                    self._panel_photo = None
            # 预览小字：频道名称与状态
            if name is None:
                self._preview_info.configure(text="", fg=COL_DIM)
            else:
                self._preview_info.configure(
                    text="%s · %s" % (name, STATUS_TEXT.get(status, status)),
                    fg=STATUS_COLOR.get(status, COL_DIM))
        except Exception:
            # 第 91 条：cvtColor/fromarray/resize/PhotoImage/configure 原全裸奔，一帧异常形状
            # （如解码出灰度 (H,W) 而非 (H,W,3)、或通道数不符）的图像会让 cv2.cvtColor 抛异常，
            # 在末尾续排 after 之前逃出 → 预览永久停更（fps/状态陈旧），而悬浮窗仍动 →「半活」。
            # 对照 _show_frame 本就有 try 兜底。兜底后异常被记录、本次跳过，finally 续排定时器。
            log.warning("面板预览刷新失败，跳过本次（定时器续排，不停更）", exc_info=True)
        finally:
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
            # 选中目标频道变化后同步源卡片高亮
            try:
                self._panel_refresh_sources()
            except Exception:
                pass

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

    # ---------- 面板：画面源列表与共享开关 ----------

    def _target_channel(self):
        """返回操作目标频道（选中优先，无选中跟随活动频道）。"""
        idx = self._panel_target_idx()
        if idx is None or not self.channels or not (0 <= idx < len(self.channels)):
            return None
        return self.channels[idx]

    def _sync_share_button(self):
        """按目标频道的 share_enabled 刷新共享按钮文本/样式。"""
        if self._share_btn is None or not self.channels:
            return
        ch = self._target_channel()
        on = bool(ch is not None and ch.share_enabled)
        self._share_btn.configure(
            text="停止共享" if on else "共享本机屏幕",
            style="Panel.TButton" if on else "Accent.TButton")

    def _panel_refresh_sources(self):
        """重建"画面源"卡片行：目标频道的 local + peers，活动频道行加观看标记。"""
        if self._src_tree is None:
            return
        ch = self._target_channel()
        tree = self._src_tree
        # 记住选中行对应源 id，重建后恢复（源 id 是 switch_source 的输入）
        keep = None
        if self._src_selected is not None and self._src_rows:
            try:
                row_idx = int(self._src_selected)
                if 0 <= row_idx < len(self._src_rows):
                    keep = self._src_rows[row_idx]
            except (ValueError, TypeError):
                keep = None
        self._src_rows = []
        rows = []
        if ch is not None:
            with ch.lock:
                peers = list(ch.peers)
                watching = ch.watch_source
            active = (0 <= self.active_idx < len(self.channels)
                      and self.channels[self.active_idx] is ch)
            # local 行
            rows.append(("▶ 观看中" if (active and watching == "local") else "",
                         "本地画面（%s）" % ch.name, "本机共享", "local"))
            # 队友行
            for p in peers:
                sid = p.get("id")
                rows.append(("▶ 观看中" if (active and watching == sid) else "",
                             "队友 · %s" % (p.get("name") or sid),
                             p.get("addr") or "", sid))
        # item 40：同样全删全建，记下滚动比例，重建后恢复，避免源列表被拽回顶部
        try:
            y_top = tree.yview()[0]
        except Exception:
            y_top = 0.0
        tree.delete(*tree.get_children())
        for i, (mark, source, info, sid) in enumerate(rows):
            tags = ("src_watching",) if mark else ()
            tree.insert("", "end", iid=str(i),
                        values=(mark, source, info), tags=tags)
            self._src_rows.append(sid)
        if keep is not None and keep in self._src_rows:
            try:
                tree.selection_set(str(self._src_rows.index(keep)))
            except Exception:
                pass
        if y_top:
            try:
                tree.yview("moveto", y_top)
            except Exception:
                pass

    def _panel_on_src_select(self, event=None):
        """单击源行：仅选中（操作目标），不切换观看。"""
        sel = self._src_tree.selection()
        self._src_selected = sel[0] if sel else None

    def _panel_on_src_double(self, event):
        """双击源行：先切活动频道（若目标非活动）再 switch_source。"""
        item = self._src_tree.identify_row(event.y)
        if not item or not self.channels:
            return
        try:
            row_idx = int(item)
            source = self._src_rows[row_idx]
        except (ValueError, IndexError):
            return
        ch = self._target_channel()
        if ch is None:
            self._panel_set_status("无可用频道", error=True)
            return
        # 目标频道若未在看则先切为活动频道（源切换作用于活动频道）
        if self.channels[self.active_idx] is not ch:
            try:
                idx = self.channels.index(ch)
            except ValueError:
                return
            self.switch_channel(idx)
            self._panel_selected = idx
        if not ch.switch_source(source):
            self._panel_set_status("切换源失败（源不可用或未连接）", error=True)
            return
        self._panel_set_status("已观看：%s" % self._describe_source(ch, source))
        try:
            self._panel_refresh_sources()
        except Exception:
            pass

    def _describe_source(self, ch, source):
        """把源 id 转展示文本（供状态栏/悬浮窗）。"""
        if source == "local":
            return "本地画面（%s）" % ch.name
        with ch.lock:
            for p in ch.peers:
                if p.get("id") == source:
                    return "队友 · %s" % (p.get("name") or source)
        return source

    def _panel_toggle_share(self):
        """共享按钮：开关当前目标频道的屏幕共享。

        第 60 条：set_share 可能阻塞主线程（未确认能力时 _cap_event.wait(2.0)，关闭时
        share.stop() 的 join(2.0)）→ 悬浮窗 poll/面板刷新/对话框整体冻结约 2 秒。改为在
        工作线程执行 set_share，完成后用 _post_ui 回主线程更新状态与按钮；执行期间用
        _share_toggle_busy 护栏忽略重复点击。set_share/_start_share 无 Tk 亲和，可安全离线执行。
        """
        ch = self._target_channel()
        if ch is None:
            self._panel_set_status("无可用频道", error=True)
            return
        if self._share_toggle_busy:
            return
        on = not ch.share_enabled
        self._share_toggle_busy = True
        self._panel_set_status("正在%s共享…" % ("开启" if on else "停止"))

        def work():
            try:
                ok, err = ch.set_share(on)
            except Exception as e:  # 兜底：异常不应让护栏卡死
                ok, err = False, "共享切换失败: %s" % e
            finally:
                self._share_toggle_busy = False

            def done():
                if not ok:
                    self._panel_set_status(err, error=True)
                else:
                    self._panel_set_status(
                        "已开启共享：%s" % ch.name if on else "已停止共享：%s" % ch.name)
                self._sync_share_button()
            self._post_ui(done)

        threading.Thread(target=work, name="share-toggle", daemon=True).start()

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
        tk.Label(body, text="全局热键", bg=COL_CARD, fg=COL_FG,
                 font=("Microsoft YaHei", 10, "bold")).pack(pady=(14, 2))
        for line in (
            "Ctrl+Alt+X：切换鼠标穿透（穿透开启后右键菜单失效，用它恢复控制台）",
            "Ctrl+Alt+← / →：切换上一个 / 下一个频道",
            "Ctrl+Alt+↑ / ↓：切换上一个 / 下一个画面源",
            "Alt+1…9：直达对应频道（可在配置 hotkeys.direct 关闭）",
        ):
            tk.Label(body, text=line, bg=COL_CARD, fg=COL_DIM, anchor="w",
                     justify="left", font=("Microsoft YaHei", 9)).pack(fill="x", padx=24)
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
        if viewer_cfg.get("wizard_done"):
            return
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
            self.cfg["viewer"]["wizard_done"] = True
            save_config(self.cfg, "viewer")
            log.info("首次使用向导完成：新增 %d 个频道，跳过 %d 行", added, failed)
            win.destroy()

        def _skip():
            self.cfg["viewer"]["wizard_done"] = True
            save_config(self.cfg, "viewer")
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
            # 第 92 条：改走 _post_ui，退出瞬间不再调进已销毁的 Tcl 解释器。
            self._post_ui(lambda: self._show_diag_report(lines, btn))

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
        # 第 92 条：在 _channels_lock 下取快照（list 复制），避免主线程增删频道时
        # 工作线程遍历到一半列表被改（IndexError / 报告已不存在的频道）。
        # 锁只护引用复制，循环体里的网络 I/O（3s 超时）在锁外执行，不阻塞主线程增删。
        with self._channels_lock:
            channels = list(self.channels)
        for ch in channels:
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
        # 第 93 条：_cred_busy 强制"每次只一个"——_credentials_dialog 的 wait_window 会开
        # 嵌套主循环，poll 的 after(15) 在嵌套期间照常触发本函数；若无此互斥，第二个频道
        # 的请求会在第一个模态框上再叠一个。忙时直接返回，请求留在队列里待空闲再处理。
        if self._cred_busy:
            return
        try:
            ch, done, box = self._cred_requests.get_nowait()
        except queue.Empty:
            return
        self._cred_busy = True
        result = {}
        try:
            result["value"] = self._credentials_dialog(ch.name)
        except Exception as e:
            result["error"] = e
        finally:
            self._cred_busy = False
        # 结果写进本请求专属 box（非频道共享槽），与 done 配对，杜绝跨请求串结果。
        box["result"] = result
        done.set()

    def poll(self):
        """主循环轮询：每 15ms 检查活动频道是否有新帧。"""
        if not self.running:
            return
        # item 46：先重排下一次 poll，再处理可能阻塞的认证对话框。
        # _credentials_dialog 的 wait_window 会开嵌套主循环，若此时尚未排定下一个 poll
        # 定时器，嵌套期间悬浮窗就停止绘制（画面/fps/延迟定格最长 120s），而面板刷新链
        # 仍在跑，造成面板与悬浮窗互相矛盾。提前重排可让悬浮窗在对话框期间继续刷新
        # （嵌套循环照常触发 after 定时器），且每次只排一个后继，不会形成定时器风暴。
        self.root.after(15, self.poll)
        self._process_cred_requests()
        if not self.channels:
            self._show_text("无频道")
            return
        channel = self.channels[self.active_idx]
        with channel.lock:
            serial = channel.frame_serial
        if self._last_channel_idx == self.active_idx and self._last_frame_serial == serial:
            # 没有新帧：只更新叠加状态，避免反复复制/缩放/绘制同一画面
            self._update_status_overlay()
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
        """刷新悬浮窗状态叠加：帧率/延迟，MultiView 下追加观看源名。"""
        if self.status_overlay is None:
            return
        text = "-"
        if self.channels and 0 <= self.active_idx < len(self.channels):
            ch = self.channels[self.active_idx]
            fps_txt = "%dfps" % round(ch.fps) if ch.fps > 0 else "-"
            lat = ch.effective_latency_ms()
            lat_txt = "%dms" % round(lat) if lat > 0 else "-"
            text = "%s · %s" % (fps_txt, lat_txt)
            with ch.lock:
                src = ch.watch_source
            if src != "local":
                text += " · 源:%s" % self._describe_source(ch, src)
        # 第 98 条：poll 每 15ms + 每帧都会调本函数，文本多数周期不变；
        # 未变时跳过 configure（仍走 Tcl 命令派发/选项查询，与解码/贴图抢主线程）。
        if text == self._last_status_text:
            return
        self._last_status_text = text
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
                # 第 98 条：窗口位置只取决于尺寸（=照片 display_width×new_h）；尺寸未变
                # 则跳过每帧的 update_idletasks + winfo 查询 + geometry 下发（60fps 下纯冗余，
                # 与解码/贴图抢主线程和 DWM）。尺寸变化（源分辨率/显示宽度变更）才重新定位。
                size_key = (self.display_width, new_h)
                if size_key != self._last_pos_size:
                    self._position_top_right()
                    self._last_pos_size = size_key
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
        # 第 98 条：内容切到文字占位、窗口尺寸随之改变 → 作废照片尺寸定位缓存，
        # 使下一帧画面恢复时必定重新定位（否则会沿用照片尺寸旧位置而错位）。
        self._last_pos_size = None
        if not self.user_moved:
            self._position_top_right()

    # ---------- 交互 ----------

    def _on_right_click(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _on_drag_start(self, event):
        # item 43：按下时不立即置 user_moved，仅记录偏移与按下点；
        # 真正拖动（位移超过阈值）后才永久关闭自动贴右上角，避免单击误触发
        self.drag_offset = (
            event.x_root - self.root.winfo_x(),
            event.y_root - self.root.winfo_y(),
        )
        self._drag_start_root = (event.x_root, event.y_root)

    def _on_drag_move(self, event):
        if self.drag_offset is None:
            return
        if not self.user_moved and self._drag_start_root is not None:
            dx = event.x_root - self._drag_start_root[0]
            dy = event.y_root - self._drag_start_root[1]
            if abs(dx) < 4 and abs(dy) < 4:
                return  # 视为点击抖动，尚未真正拖动
            self.user_moved = True
        x = event.x_root - self.drag_offset[0]
        y = event.y_root - self.drag_offset[1]
        self.root.geometry("+%d+%d" % (x, y))

    def toggle_click_through(self):
        self.click_through = not self.click_through
        self.apply_click_through()
        self.cfg["viewer"]["click_through"] = self.click_through
        self._schedule_save()  # 第 64 条：去抖落盘，穿透切换的 win32 效果仍即时
        if self.click_through:
            log.info("鼠标穿透已开启，按 Ctrl+Alt+X 可关闭")
        else:
            log.info("鼠标穿透已关闭，可拖动窗口，右键菜单/Esc 可用")

    def apply_click_through(self):
        """设置/清除 WS_EX_TRANSPARENT（穿透）与 WS_EX_LAYERED（穿透或半透明所需）。

        WS_EX_LAYERED 不能与 Tk 的 -alpha 互相踩踏：只要开启穿透或 alpha<1.0
        就必须保持 LAYERED，并在每次切换后重新写入 alpha，否则分层窗口会失去
        透明属性（关掉穿透后变全不透明）或在 alpha==1.0 时根本不渲染。
        """
        try:
            alpha = float(getattr(self, "alpha", 1.0))
        except (TypeError, ValueError):
            alpha = 1.0
        alpha = max(0.0, min(1.0, alpha))
        need_layered = bool(self.click_through) or alpha < 1.0
        ex_style = win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE)
        if self.click_through:
            ex_style |= win32con.WS_EX_TRANSPARENT
        else:
            ex_style &= ~win32con.WS_EX_TRANSPARENT
        if need_layered:
            ex_style |= win32con.WS_EX_LAYERED
        else:
            ex_style &= ~win32con.WS_EX_LAYERED
        win32gui.SetWindowLong(self.hwnd, win32con.GWL_EXSTYLE, ex_style)
        if need_layered:
            try:
                win32gui.SetLayeredWindowAttributes(
                    self.hwnd, 0, int(round(alpha * 255)), win32con.LWA_ALPHA,
                )
            except Exception:
                log.debug("SetLayeredWindowAttributes 失败", exc_info=True)
        win32gui.SetWindowPos(
            self.hwnd, 0, 0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
            | win32con.SWP_NOZORDER | win32con.SWP_FRAMECHANGED,
        )

    # ---------- 全局热键（Ctrl+Alt+X 穿透 / Ctrl+Alt+←→ 切频道 / Ctrl+Alt+↑↓ 切画面源） ----------

    def start_hotkey(self):
        threading.Thread(
            target=self._hotkey_loop, name="viewer-hotkey", daemon=True
        ).start()

    def _hotkey_loop(self):
        user32 = ctypes.windll.user32
        registered = []
        failed = []
        for hid, vk in HOTKEY_DEFS:
            if user32.RegisterHotKey(None, hid, MOD_CONTROL | MOD_ALT, vk):
                registered.append(hid)
            else:
                failed.append(HOTKEY_NAMES.get(hid, "热键ID %d" % hid))
        direct_enabled = bool(self.cfg["viewer"].get("hotkeys", {}).get("direct", True))
        if direct_enabled:
            for i in range(9):
                hid = HOTKEY_ID_DIRECT_BASE + i
                if user32.RegisterHotKey(None, hid, MOD_ALT, VK_1 + i):
                    registered.append(hid)
                else:
                    failed.append("Alt+%d 直达频道" % (i + 1))
        if not registered:
            log.warning("全局热键全部注册失败（可能已被其他程序占用）：%s",
                        "、".join(failed) if failed else "未知")
            return
        if failed:
            log.warning("部分全局热键注册失败（可能已被其他程序占用）：%s", "、".join(failed))
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
                        self._post_ui(self.toggle_click_through)
                    elif msg.wParam == HOTKEY_ID_PREV:
                        self._post_ui(lambda: self._step_channel(-1))
                    elif msg.wParam == HOTKEY_ID_NEXT:
                        self._post_ui(lambda: self._step_channel(1))
                    elif msg.wParam == HOTKEY_ID_SRC_PREV:
                        self._post_ui(lambda: self._step_source(-1))
                    elif msg.wParam == HOTKEY_ID_SRC_NEXT:
                        self._post_ui(lambda: self._step_source(1))
                    elif HOTKEY_ID_DIRECT_BASE <= msg.wParam < HOTKEY_ID_DIRECT_BASE + 9:
                        self._post_ui(lambda idx=msg.wParam - HOTKEY_ID_DIRECT_BASE:
                                      self.switch_channel(idx))
                except Exception:
                    pass
        finally:
            for hid in registered:
                user32.UnregisterHotKey(None, hid)

    def _step_channel(self, delta):
        """按步长切换频道（-1 上一个 / +1 下一个），越界时 clamp。"""
        self.switch_channel(self.active_idx + delta)

    def _step_source(self, delta):
        """按步长循环切换活动频道的画面源（-1 上一个 / +1 下一个），到头回绕。

        源顺序为 本地画面 → 队友1 → 队友2 → …；无队友共享时不切换，仅提示。
        """
        if not self.channels or not (0 <= self.active_idx < len(self.channels)):
            return
        ch = self.channels[self.active_idx]
        with ch.lock:
            sources = ["local"] + [p.get("id") for p in ch.peers if p.get("id")]
            current = ch.watch_source
        if len(sources) < 2:
            log.debug("频道[%s] 无可切换的队友画面", ch.name)
            if self.panel is not None:
                self._panel_set_status("暂无可切换的队友画面（需队友先共享）")
            return
        try:
            cur_idx = sources.index(current)
        except ValueError:
            cur_idx = 0  # 当前源已失效（队友退出但回落尚未生效）：从本地画面起算
        target = sources[(cur_idx + delta) % len(sources)]
        if not ch.switch_source(target):
            if self.panel is not None:
                self._panel_set_status("切换画面源失败（源不可用或未连接）", error=True)
            return
        desc = self._describe_source(ch, target)
        log.info("频道[%s] 热键切换画面源：%s", ch.name, desc)
        # 不调 _refresh_overlay：切源已清空 last_frame，强制重绘会刷出"连接中…"假状态；
        # 悬浮窗由 poll 在新源首个关键帧到达时重绘，状态行每 15ms 更新即可给出反馈
        if self.panel is not None:
            self._panel_set_status("已观看：%s" % desc)
            try:
                self._panel_refresh_sources()
            except Exception:
                pass


def main():
    # 第 79 条：仅在作为 GUI 应用运行时安装兜底 excepthook（覆盖 load_config /
    # ViewerApp 构造等位于下方 try 之外、异常会逃逸到解释器顶层的启动阶段）。
    # 不在 import 时安装，避免劫持测试套件等仅 import viewer 的进程的全局错误处理。
    sys.excepthook = _handle_uncaught
    parser = argparse.ArgumentParser(description="FPS 画面观看端（TCP 客户端 + 置顶悬浮窗）")
    parser.add_argument(
        "--addr", help="服务端地址 host:port，可覆盖配置文件中的 viewer.server_addr"
    )
    args = parser.parse_args()

    cfg = load_config()
    logger.setup_logger(cfg)
    if args.addr:
        cfg["viewer"]["server_addr"] = args.addr
        save_config(cfg, "viewer")
        log.info("已使用命令行地址 %s 并写入配置", args.addr)

    enable_dpi_awareness()

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
