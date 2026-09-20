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
import math
import os
import queue
import secrets
import socket
import struct
import sys
import threading
import time
from collections import deque

import win32con
import win32gui
import tkinter as tk
from tkinter import ttk, messagebox

# A3（第 133 条）：重依赖改为 main() 里极简 splash 上屏后由 _load_heavy() 加载。
# onedir 冷启动时 cv2.pyd（~86MB）是导入大头；win32*/tkinter 是 Tk 系最轻的
# 依赖，保留顶层。cv2/np/Image/ImageTk/share_mod 的全部使用点都在函数体内
# （模块导入期零引用），哨兵初值不会被触及。
cv2 = np = Image = ImageTk = share_mod = None


def _load_heavy():
    """惰性加载重依赖（幂等）：cv2 / numpy / PIL / share→pipeline 链。"""
    global cv2, np, Image, ImageTk, share_mod
    if cv2 is not None:
        return
    import cv2 as _cv2
    import numpy as _np
    from PIL import Image as _Image, ImageTk as _ImageTk
    import share as _share
    cv2, np, Image, ImageTk, share_mod = _cv2, _np, _Image, _ImageTk, _share

import logger
import splash
import codec as codec_mod
import diag
import ui_common
import update_check
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
    app_dirs,
    data_dir,
    enable_dpi_awareness,
    tune_socket,
    parse_message,
    message_pump,
    parse_video,
    MSG_FRAME,
    MSG_VIDEO,
    VIDEO_FLAG_KEY,
    CODEC_H264,
    CODEC_HEVC,
    # C1（优化方案 2026-09-17）：原 _cfg_int/_cfg_float 私有副本并入 common。
    safe_float,
    safe_int,
    # C2（实现方案 §7）：action 常量表单点来源
    Act,
    # C2 §7 第 3 步：认证期 CTRL 随带协议版本
    PROTOCOL_VERSION,
    # D1（实现方案 §10）：挑战-应答认证公共件（口令不再过网）
    PBKDF2_ITERATIONS, derive_dk, challenge_mac, b64e, b64d,
    # auth-002（2026-09-18）：挑战参数校验（salt 形态/长度）
    _is_hex_str, KDF_SALT_HEX_LEN,
    # auth-002/auth-003（2026-09-18，协议 v2.3）：proof 绑定用户名；仅与支持该版本的
    # 共享端走挑战-应答（旧版明确拒绝，不再降级明文口令）
    PROTO_CHAL_MIN_MINOR,
)

#: 模块级日志（logger.setup_logger(cfg) 幂等初始化后生效）
log = logger.get_logger()

#: 品牌标题（面板标题/悬浮窗占位文字共用）
APP_TITLE = "%s v%s" % (APP_NAME, APP_VERSION)

#: 模块级 Tk 根窗口引用，供未捕获异常处理定位父窗口
_TK_ROOT = None


# 第 136 条（C3）：兜底钩子实现收拢至 ui_common.install_excepthooks（第 79/100/15 条
# 语义并集）。sys.excepthook 的**安装**仍延迟到 main()，不在 import 时无条件执行：
# 否则任何 import viewer 的进程（含单元测试套件）都会被全局换成会弹**模态**
# messagebox 的钩子；测试期间一旦有异常逃逸到解释器顶层，整套件就卡在 GUI 对话框后面。

#: 全局热键修饰键
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000  # Win7+：按住不连发，避免长按瞬间扫过全部频道
#: 按键
VK_X = 0x58
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_1 = 0x31
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
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

#: 控制台面板深色主题配色（第 136 条 C3：色板单一来源在 ui_common，此处仅重导出）
from ui_common import (  # noqa: E402
    COL_BG, COL_CARD, COL_CTRL, COL_FG, COL_DIM, COL_ACCENT,
    COL_ONLINE, COL_CONNECTING, COL_OFFLINE, COL_ACTIVE_ROW,
)

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


def _viewer_hotkeys_cfg(cfg):
    """安全取 viewer.hotkeys 配置对象（B4，2026-09-11）。

    第 106 条（B16）已为 `viewer.channels` 做了同类规范化，`hotkeys` 漏了：手改成
    `"hotkeys": false` 时 `self.cfg["viewer"].get("hotkeys", {}).get("direct", True)`
    抛 AttributeError——在 `_build_panel` 里会让观看端**完全无法启动**（冒泡到 run()
    → main() 弹错误框后 sys.exit(1)），在 `_hotkey_loop` 里会让热键线程直接死亡且
    已注册的全局热键永不注销。这里统一兜底成 {} 并保留原默认语义。
    """
    viewer_cfg = cfg.get("viewer") if isinstance(cfg, dict) else None
    viewer_cfg = viewer_cfg if isinstance(viewer_cfg, dict) else {}
    hotkeys = viewer_cfg.get("hotkeys")
    if not isinstance(hotkeys, dict):
        if hotkeys is not None:
            log.warning("viewer.hotkeys 不是对象（%r），按默认值处理", hotkeys)
        return {}
    return hotkeys


def _normalize_channel_items(viewer_cfg):
    """把 config.viewer.channels 规范成非空的对象列表（第 106 条 / B16）。

    - 不是列表（手改成 "1.2.3.4:5700" / {}）→ 告警并忽略，回退默认频道；
    - 列表元素不是对象 → 跳过该元素；
    - 全部为空 → 用 server_addr 生成默认频道（向后兼容）。
    旧实现直接对元素调 .get()：字符串会被逐字符遍历、字典会遍历键，启动即崩。
    """
    viewer_cfg = viewer_cfg if isinstance(viewer_cfg, dict) else {}
    items = viewer_cfg.get("channels")
    if not isinstance(items, list):
        if items:
            log.warning("viewer.channels 不是列表（%r），已忽略并按默认频道处理", items)
        items = []
    out = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            log.warning("viewer.channels[%d] 不是对象（%r），已跳过", i, item)
            continue
        out.append(item)
    if not out:
        out = [{"name": "默认频道",
                "addr": viewer_cfg.get("server_addr", "127.0.0.1:5700")}]
    return out


class Channel:
    """单个画面来源：独立接收线程 + 最新帧缓存（BGR numpy 数组）。"""

    def __init__(self, name, addr, auth=None, owner=None, ephemeral=False):
        self.name = name
        self.addr = addr
        self.auth = auth or {}
        self.owner = owner
        # 第 100 条：命令行 --addr 临时加入的频道不写回 config.json（见 _save_channels）
        self.ephemeral = bool(ephemeral)
        self.username = (auth or {}).get("username", "")
        self.auth_msg = ""
        self._prompt_blocked_until = 0.0  # 取消登录后的冷却时刻，避免频繁重连狂弹对话框
        # 第 133 条（2026-09-11）：服务端最近一次拒绝原因与最近一次尝试的用户名——
        # 回填进登录框（红字提示 + 预填用户名），并在日志里明确写出"已弹出登录框"。
        # 原实现只有冷却和 auth_msg，用户看不到"为什么失败"，也看不到任何"弹过框"的痕迹。
        self._auth_error_text = ""
        self._last_auth_user = ""
        self._password = None  # 本次会话内的明文密码缓存，用于重连自动登录
        self._auth_probed = False  # 已用空凭据探测过服务端是否需要登录（避免准入关闭时仍弹框）
        self._anon_ok = False  # 已确认服务端准入关闭（匿名放行），重连时直接走 probe 不再弹框
        # D1（实现方案 §10）：服务端是否支持挑战-应答（None=未知，首次登录前用 probe
        # 回包里的 proto 字段判定；老服务端无该字段 → False → 走旧版直发口令路径）。
        self._chal_capable = None
        # 网络/稳定性参数（从 viewer 配置读取，缺省用安全默认值）
        viewer_cfg = (owner.cfg if owner is not None else {}).get("viewer", {}) or {}
        net_cfg = viewer_cfg.get("net", {}) or {}
        rc_cfg = viewer_cfg.get("reconnect", {}) or {}
        self._rcvbuf = safe_int(net_cfg.get("rcvbuf_kb", 4096), 4096, 0) * 1024
        self._read_idle_s = safe_float(net_cfg.get("read_idle_s", 5.0), 5.0, 0.5, 600.0)
        self._ping_interval = safe_float(viewer_cfg.get("ping_interval_s", 2.0), 2.0, 0.2, 600.0)
        self._backoff_base = safe_float(rc_cfg.get("base_s", 1.0), 1.0, 0.1, 600.0)
        self._backoff_max = safe_float(rc_cfg.get("max_s", 30.0), 30.0, 0.5, 3600.0)
        self._backoff_factor = safe_float(rc_cfg.get("factor", 2.0), 2.0, 1.0, 10.0)
        self._retry_delay = 0.0  # 当前重连等待（指数退避）；成功收帧后归零
        # H.264/H.265(HEVC) 视频解码（host 视频编码路径）；JPEG 回退仍兼容
        self._decoder = codec_mod.VideoDecoder()
        self._dec_lock_obj = None  # 惰性创建，见 _dec_lock()
        self._got_key = False          # 是否已收到关键帧（视频解码起点）
        #: net-017（2026-09-18）：积压追新标志——延迟/批次超阈值时置位，只喂关键帧
        #: 并请求 IDR，直到关键帧到位（否则积压永远消化不完、延迟只增不减）
        self._resync = False
        self._resync_at = 0.0
        # 第 105 条（B6）：解码"代数"。切源/重连时 +1，_decode_video_batch 进入时
        # 快照并逐帧校验——主线程切源会重置解码器，而接收线程可能正在用**旧源**的
        # NAL 循环，若不校验就会把旧源帧喂进新解码器（或把旧源画面贴到新源标签下），
        # 并反复重建解码器。不一致即整批丢弃。
        self._gen = 0
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
        # 第 108 条（B13）：可中断的退避等待。stop() 置位后接收线程立即退出，
        # 不再睡满退避（默认 30s，配置上限 1h）——否则"删掉再加同一地址"会累积
        # 多个仍在睡的 channel-recv 线程。
        self._stop_evt = threading.Event()
        self._active_sock = None  # 当前活动 socket（控制心跳线程与接收线程共享；断线即清空）
        self._share_sync_timer = None  # 第 61 条：连接后能力确认兜底定时器（stop 时 cancel）
        self._fps_times = []  # 1 秒滑动窗口内各帧的时间戳
        self._latency_samples = []  # 最近 5 帧延迟样本（微秒），用于滑动平均平滑
        # E4（实现方案 §11.4）：30 秒 P50/P95 样本环（≈900 帧@30fps 封顶，防内存漂移），
        # 元素 (monotonic 到达时刻, latency_us)；_p_stats_cache = (算得时刻, p50, p95, n)，
        # 最长 5 秒重算一次（悬浮窗 15ms 轮询直接读缓存）。
        self._latency_ring = deque(maxlen=900)
        self._p_stats_cache = None
        threading.Thread(
            target=self._receiver_loop, name="channel-recv", daemon=True
        ).start()

    # ---------- 接收线程 ----------

    def _dec_lock(self):
        """解码器专用锁（惰性创建：测试常用 __new__ 构造 Channel，可能尚未跑 __init__）。

        第 116 条补丁：切源/重连（主线程）set_codec/reset → close()，与接收线程
        _ctx.decode 并发会拆掉正在用的 FFmpeg 上下文（未定义行为）。所有 decoder
        操作都必须持此锁。
        """
        lock = self.__dict__.get("_dec_lock_obj")
        if lock is None:
            lock = threading.Lock()
            self.__dict__["_dec_lock_obj"] = lock
        return lock

    def _invalidate_media(self):
        """作废帧缓存并强制主界面重绘（接收线程状态跃迁时调用）。

        ui-002（2026-09-18）：poll 的"无变化"判据只看 frame_serial，而状态跃迁
        （连接中/需登录/已断开）既不递增 serial 也不清 last_frame——曾出过一帧后
        "已断开，正在重连…"分支永不可达，用户面对静止的最后一帧直到重连成功；
        第 12 条的 fps 衰减同样在"泵退出"路径上失效。三处跃迁点统一经此收口。
        """
        with self.lock:
            self.frame_serial += 1
            self.last_frame = None
        self._reset_stats()   # ui-010：断线/重连即作废旧路径的帧率与延迟样本
        self._note_no_media()

    def _receiver_loop(self):
        """接收线程主循环：解析地址 → 连接 → 握手 → 实时收帧；异常时指数退避重连。

        稳定性：无新数据超过 read_idle_s 判定连接停滞；成功后重置退避；
        及时性：用缓冲区逐条解析协议消息，一次循环只解码缓冲区里最新的一帧，
        天然丢弃突发积压的中间帧（防止解码积压放大端到端延迟）。
        """
        while self._running:
            # ui-002：进入新一轮连接即作废旧帧缓存，断线期间悬浮窗不得定格最后一帧
            self.status = "connecting"
            self._invalidate_media()
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
                # C15（2026-09-11）：把"隧道不可达"报成可归因的错误。`recv_exact` 在
                # "TCP 已建立但对端不发数据即关闭"时抛「连接提前关闭，收到的数据不完整」，
                # 而这句话在**隧道/端口映射没指向共享端、frpc 未运行、共享端崩溃**三种
                # 完全不同的故障下都会出现，且共享端在这三种情况下都不留日志——用户只能
                # 在两端之间盲猜（本次 A0 故障即因此拖延定位）。
                try:
                    client_handshake(sock)
                except Exception as e:
                    raise ConnectionError(
                        "%s · TCP 已连通但未收到服务端握手：通常是内网穿透/端口映射没有"
                        "指向共享端的监听端口（樱花 frpc 日志会写「连接映射目标 "
                        "[tcp://localhost:PORT] 失败」），也可能共享端未启动、或只监听了"
                        "另一地址族（如 IPv4 回环而 frpc 走 ::1）" % e)
                # auth-017（2026-09-20）：探测标记必须**按连接**复位。旧实现它是频道级
                # 且只置一次，于是"上一次输错口令"之后的重连把 `_ask_credentials()`
                # 当成该连接的**首条消息**——而 host 的首消息档只有 30 秒
                # （AUTH_FIRST_MSG_S），从握手完成就开始计时，把用户输入口令的时间也算进
                # 去（观看端内部最长 60s 等界面就绪 + 120s 等输入）。用户超过 30 秒才
                # 按确定 → sendall 仍成功但 host 的 recv 已超时 → "认证通信失败"、弹框
                # 作废、循环往复。先探测可让 host 走 150 秒的"等凭据输入"档。
                # ui-026（2026-09-20）：本轮认证前清空 auth_msg，认证失败时**保留**
                # _authenticate 写入的具体原因（旧实现在下面无条件覆写成套话，
                # 10 条分支里的"服务端版本过旧""挑战参数异常""认证通信失败"等真因
                # 在面板/poll/日志三处全部不可见）。
                self._auth_probed = False
                self.auth_msg = ""
                if not self._authenticate(sock):
                    self.status = "auth"
                    self.auth_msg = self.auth_msg or "需要登录后观看"
                    self._invalidate_media()   # ui-002：不得让旧画面盖住"需要登录"提示
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
                self._gen = getattr(self, "_gen", 0) + 1   # 第 105 条（B6）：作废在途批次（旧连接的 NAL）
                # 第 116 条（C6）：重连尽量复用已有解码上下文（flush_buffers），
                # 编码类型变化时才 set_codec 重建；原先每次重连都 new VideoDecoder。
                # 持 _dec_lock：接收线程可能正卡在 decode() 里。
                with self._dec_lock():
                    self._decoder.set_codec(self.cur_codec)
                    self._decoder.reset()
                # MultiView：重置能力状态并发 cap_probe；host 侧新连接默认 local，
                # 若上次在观看 peer 源则重新订阅；成功后按需自动重启共享
                self.multiview = False
                self._cap_event.clear()
                try:
                    # 第 100 条：三条消息各自有界（_send_locked），避免对端不读时
                    # 接收线程在连接初始化阶段就无限阻塞。
                    self._send_locked(sock, {"action": Act.REQ_KEYFRAME, "t": time.time_ns()})
                    self._send_locked(sock, {"action": Act.CAP_PROBE})
                    if self.watch_source != "local":
                        self._send_locked(sock, {"action": Act.WATCH,
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
                # C2（实现方案 §7 第 2 步）：select 1s + recv + bytearray 累积 +
                # 逐条排空的骨架并入 common.message_pump（与 host._client_recv_loop
                # 共用一份）；停滞判定、关键帧请求、批解码语义仍留在下面的回调里，
                # 第 2/7/12/57 条行为逐分支不变。
                last_rx = time.monotonic()
                last_media_rx = last_rx  # 最近一次收到画面帧（第 2/12 条用）
                latest_jpeg = None       # (ts, jpeg_bytes)
                video_batch = []         # [(ts, codec, flags, nal)]，按到达顺序

                def _on_idle(now, buf):
                    # 第 12 条：超过 1 秒无画面帧则衰减 fps，避免死频道锁存旧帧率
                    if now - last_media_rx > 1.0:
                        self._note_no_media()
                    # 第 2 条：连接活着（pong 在流）但长时间收不到画面帧 → 主动请求
                    # 关键帧，打破「need_key 门控 + 仅解码失败才请求 + pong 击败停滞
                    # 检测」三者叠加导致的画面永久定格（状态仍显示 receiving）。
                    if (self.status == "receiving"
                            and now - last_media_rx > self._read_idle_s):
                        self._request_keyframe(sock)
                    # 完全无任何数据（含 pong）超过阈值：判定连接停滞，退出重连
                    if now - last_rx > self._read_idle_s:
                        raise ConnectionError(
                            "%.0f 秒未收到数据，判定连接停滞" % self._read_idle_s)
                    return None

                def _on_rx(now):
                    # net-036（2026-09-20）：旧实现由泵在**每个 recv 分块**上调这里，
                    # 于是 last_rx 按字节刷新——对端（含被劫持的隧道中转）每 5 秒内
                    # 滴 1 字节即可让停滞判定永不成立，频道被钉在 status="receiving"
                    # + 画面定格。改由 _on_buf 在"消费掉完整消息"时刷新（与 host 侧
                    # net-015 同口径）。
                    nonlocal last_rx
                    last_rx = now

                def _on_buf(event, buf, now, n_msgs=0):
                    if event == "post" and n_msgs > 0:
                        _on_rx(now)

                def _on_tick(now, buf):
                    # net-036 + net-030：停滞判定旧实现只在 select 空转满 1 秒时评估，
                    # 持续滴字节的对端让 `not r` 永不成立 → 判据一次都不跑。
                    if now - last_rx > self._read_idle_s:
                        raise ConnectionError(
                            "%.0f 秒未收到完整消息，判定连接停滞" % self._read_idle_s)
                    return None

                def _on_eof():
                    raise ConnectionError("连接关闭")

                def _on_chunk():
                    nonlocal latest_jpeg
                    latest_jpeg = None
                    del video_batch[:]

                def _on_message(kind, payload, raw):
                    nonlocal latest_jpeg
                    # 第 7 条：JPEG 各帧独立可解，只留最新一份；H.264/HEVC 必须按序
                    # 解码批次内全部帧以维持参考帧链（丢中间帧会花屏/拖影直到下一个
                    # 关键帧），故视频帧收集成有序列表逐帧喂解码器。
                    if kind == MSG_VIDEO:
                        try:
                            ts, codec, flags, nal = parse_video(payload)
                        except ValueError:
                            return None
                        video_batch.append((ts, codec, flags, nal))
                    elif kind == MSG_FRAME:
                        if len(payload) < 8:
                            log.warning("频道[%s] 帧 payload 过短，跳过", self.name)
                            return None
                        (ts,) = struct.unpack(">Q", payload[:8])
                        latest_jpeg = (ts, payload[8:])
                    else:  # MSG_CTRL
                        self._handle_ctrl(payload)
                    return None

                def _on_batch(buf):
                    nonlocal last_media_rx
                    if video_batch:
                        last_media_rx = time.monotonic()  # 收到画面帧（第 2/12 条）
                        self._check_backlog(sock, video_batch)
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

                message_pump(sock,
                             should_stop=lambda: not self._running,
                             on_idle=_on_idle, on_rx=None, on_eof=_on_eof,
                             on_buf=_on_buf, on_tick=_on_tick,
                             on_chunk=_on_chunk, on_message=_on_message,
                             on_batch=_on_batch)
            except Exception as exc:
                self._active_sock = None
                log.warning("频道[%s] 连接断开: %s", self.name, exc)
            finally:
                if self.status != "auth":
                    self.status = "disconnected"
                self._active_sock = None
                # ui-002：断线即作废旧帧缓存并衰减 fps——否则"已断开，正在重连…"
                # 永不可达，用户面对定格画面把断线误当成"画面卡住"
                self._invalidate_media()
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
            # 第 108 条（B13）：Event.wait 替代 sleep —— stop()/request_stop() 置位后
            # 立刻返回并退出循环，线程回收即时。
            if self._stop_evt.wait(self._retry_delay):
                break
            self._retry_delay = min(
                self._retry_delay * self._backoff_factor, self._backoff_max)

    def _send_locked(self, sock, obj, timeout=5.0):
        """在 _tx_lock 内发送一条控制消息，并把 sendall 的阻塞时间收紧到 timeout 秒。

        第 100 条：接收循环把共享 socket 置为阻塞（None），而 ping/req_keyframe/watch/
        unshare 这些控制发送既无超时、又可能由接收线程自己发起（_request_keyframe 在
        停滞分支里被调用）。对端停止读取时 sendall 会阻塞到 TCP 重传超时（几十秒到
        数分钟），于是 select 停滞检测与重连全部失效、画面永久定格。这里临时收紧、
        finally 还原（与 share.py 的 _send_msg 同一不变量：接收循环的阻塞基线必须保持）。

        对不支持 gettimeout/settimeout 的替身 socket（测试）自动跳过收紧与还原。
        """
        with self._tx_lock:
            prev = None
            saved = False
            try:
                prev = sock.gettimeout()
                saved = True
            except Exception:
                saved = False
            if saved:
                try:
                    sock.settimeout(timeout)
                except Exception:
                    saved = False
            try:
                send_msg(sock, obj)
            finally:
                if saved:
                    try:
                        sock.settimeout(prev)
                    except Exception:
                        pass

    def _send_ctrl_async(self, obj, timeout=2.0):
        """fire-and-forget 控制消息：丢给短生命周期工作线程发送（ui-009，2026-09-18）。

        Tk 主线程直接同步 `_send_locked`（锁内 sendall 最长 5s）时，对端停读会让
        整个界面冻结 5–10 秒；UNSHARE 这类不需要回执的消息经此发送，主线程只付
        一次线程启动成本。发送失败由接收循环的重连逻辑自然兜底。
        """
        sock = self._active_sock
        if sock is None:
            return False
        threading.Thread(
            target=self._send_ctrl_worker, args=(sock, obj, timeout),
            name="channel-ctrl-send", daemon=True,
        ).start()
        return True

    def _send_ctrl_worker(self, sock, obj, timeout):
        try:
            self._send_locked(sock, obj, timeout=timeout)
        except (OSError, ValueError):
            pass

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
                self._send_locked(sock, {"action": Act.PING, "t": time.time_ns()})
            except (OSError, ValueError):
                return  # 连接已断/发送超时，接收循环会负责重连

    def _request_keyframe(self, sock):
        """向服务端请求关键帧（带 0.5s 冷却），用于首连/丢关键帧/花屏快速恢复。

        ui-011（2026-09-18）：冷却用**单调钟**——墙钟后跳会让
        `now - _key_requested_at < 0.5` 长期为真，关键帧自救被永久静默（画面
        花屏后只能等下一个自然 GOP）。
        """
        now = time.monotonic()
        if now - self._key_requested_at < 0.5:
            return
        self._key_requested_at = now
        try:
            self._send_locked(sock, {"action": Act.REQ_KEYFRAME, "t": time.time_ns()})
        except (OSError, ValueError):
            pass

    #: net-017（2026-09-18）：判定"积压"的两个信号——单批帧数（一次 recv 排空
    #: 掉大批帧 = 明显落后）与端到端延迟（采集时刻到现在的差，秒级即不可接受）
    _BACKLOG_BATCH_FRAMES = 12
    _BACKLOG_LAG_US = 2_500_000
    _BACKLOG_COOLDOWN_S = 10.0

    def _check_backlog(self, sock, batch):
        """积压检测（net-017）：落后明显时进入追新模式。

        docstring 声称"天然丢弃突发积压的中间帧"，但实现为保参考帧链必须逐帧解码
        ——解码慢于到达时积压只能在 socket 缓冲里越堆越多，延迟爬升且永不回落。
        这里检测到积压即重置解码器、请求关键帧，并让后续批次只喂关键帧直到 IDR
        到位（参考链反正已断，跳过 P 帧可瞬间消化积压）。
        """
        now_m = time.monotonic()
        if now_m - getattr(self, "_resync_at", 0.0) < self._BACKLOG_COOLDOWN_S:
            return
        lag_us = time.time_ns() // 1000 - int(batch[-1][0])
        # 延迟窗口取 (0, 10s]：负数/超大值是跨机时钟偏差或脏数据（同 _store_frame 口径）
        lagged = 0 <= lag_us <= 10_000_000 and lag_us > self._BACKLOG_LAG_US
        if not lagged and len(batch) < self._BACKLOG_BATCH_FRAMES:
            return
        self._resync_at = now_m
        self._resync = True
        try:
            with self._dec_lock():
                self._decoder.reset()
        except Exception:
            pass
        self._got_key = False
        self._request_keyframe(sock)
        log.warning("频道[%s] 检测到解码积压（本批 %d 帧，延迟 %.1f 秒），"
                    "进入追新模式：只收关键帧直到恢复",
                    self.name, len(batch), max(0.0, lag_us) / 1e6)

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
        # 第 105 条（B6）：批次进入时快照代数；主线程切源/重连会 +1，逐帧校验，
        # 一旦变化就整批丢弃（含尚未喂给解码器的旧源 NAL），避免脏帧与解码器反复重建。
        # getattr 兜底：部分测试替身只实现被断言到的接口。
        gen = getattr(self, "_gen", 0)
        for ts, codec, flags, nal in batch:
            if gen != getattr(self, "_gen", 0):
                log.debug("频道[%s] 解码批次作废（切源/重连），丢弃 %d 帧",
                          self.name, len(batch))
                return
            if codec != self.cur_codec:
                # 编码切换（H.264 <-> HEVC）：重建解码器并从关键帧重新开始
                with self._dec_lock():
                    self._decoder.set_codec(codec)
                self.cur_codec = codec
                self._got_key = False
                self._request_keyframe(sock)
            if getattr(self, "_resync", False) and not (flags & VIDEO_FLAG_KEY):
                continue   # net-017：追新模式丢弃中间 P 帧（参考链已断，等 IDR）
            if flags & VIDEO_FLAG_KEY:
                self._got_key = True
                self.video_mode = True
                self._resync = False   # net-017：关键帧到位，退出追新模式
            with self._dec_lock():
                frames = self._decoder.decode(nal)
            if getattr(self._decoder, "needs_keyframe", False):
                # B6（2026-09-11）：解码器内部已复位（缺参考帧/坏 NAL → flush_buffers），
                # 参考链被清空，但 _got_key 仍为 True——此后某个 P 帧若被解出（PyAV 在
                # 缺参考时并非总是失败）会跳过"仅关键帧后显示"门控，把脏帧直接贴到悬浮窗。
                # 这里作废门控并请求关键帧，等待下一个 IDR 重新起解。
                self._got_key = False
                try:
                    self._decoder.clear_needs_keyframe()
                except Exception:
                    pass
                self._request_keyframe(sock)
                frames = []
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
        if action == Act.PONG:
            t = msg.get("t")
            if isinstance(t, int) and t > 0:
                rtt_ms = (time.time_ns() - t) / 1_000_000.0
                if 0 < rtt_ms < 60_000:  # 过滤异常值（>60s 视为脏数据）
                    self.round_trip_ms = rtt_ms
        elif action == Act.CAP:
            if msg.get("multiview"):
                self.multiview = True
                self._cap_event.set()
                self._maybe_resume_share()
        elif action == Act.PEERS:
            rows = msg.get("peers")
            if isinstance(rows, list):
                with self.lock:
                    self.multiview = True
                    self.peers = [p for p in rows
                                  if isinstance(p, dict) and isinstance(p.get("id"), str)]
                self._cap_event.set()
                self._on_peers_updated()
                self._maybe_resume_share()
        elif action == Act.REQ_KEYFRAME:
            # host 转达（别的 viewer 正在观看本机共享）：请求上传器补关键帧
            self.request_keyframe()
        elif action == Act.WATCH_REJECT:
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
                    action, user, password = Act.PROBE, "", ""
                elif not self._auth_probed:
                    # 首次无凭据时先发探测：服务端准入关闭会直接放行，
                    # 就不需要再弹登录框；服务端仍要求认证时下一轮再弹框。
                    self._auth_probed = True
                    action, user, password = Act.PROBE, "", ""
                else:
                    result = self._ask_credentials()
                    if result is None:
                        if getattr(self, "_cred_evicted", False):
                            # ui-024：请求被更新的请求顶掉 ≠ 用户取消——不设 30 秒
                            # 静默窗口，如实报原因并按重连节奏重试
                            self._cred_evicted = False
                            self.auth_msg = "登录请求被更新的请求替换，稍后自动重试"
                            self._retry_delay = 0.0
                            log.warning("频道[%s] 登录请求被更新的请求顶掉，"
                                        "不按用户取消处理（不设 30 秒静默）", self.name)
                            return False
                        if getattr(self, "_cred_timeout", False):
                            # ui-003：等待超时/被中断 ≠ 用户取消——不设冷却、不谎报
                            # "用户取消登录"，如实记账后按重连节奏重试
                            self._cred_timeout = False
                            self.auth_msg = "登录等待超时（120 秒），稍后自动重试"
                            self._retry_delay = 0.0
                            log.warning("频道[%s] 登录框等待超时（120 秒），"
                                        "不按用户取消处理（不设冷却）", self.name)
                            return False
                        self._prompt_blocked_until = time.monotonic() + 30
                        self.auth_msg = "用户取消登录"
                        return False
                    action, user, password = result
                    self.username = user
            else:
                action = Act.LOGIN
            try:
                if user:
                    self._last_auth_user = user   # 第 133 条：失败后预填用
                sock.settimeout(5.0)
                if action in (Act.LOGIN, Act.REGISTER) and self._chal_capable is None:
                    # D1 能力探测：先发一次 probe 读取服务端回包中的 proto（新版必带；
                    # 旧版无该字段 → False）。probe 若命中准入关闭会直接 ok=True，本轮即出结果。
                    send_msg(sock, {"action": Act.PROBE, "user": "", "pass": "",
                                    "proto": list(PROTOCOL_VERSION)})
                    probe = recv_msg(sock)
                    proto = probe.get("proto")
                    self._chal_capable = bool(
                        isinstance(proto, (list, tuple)) and len(proto) >= 2
                        and proto[0] == PROTOCOL_VERSION[0]
                        and proto[1] >= PROTO_CHAL_MIN_MINOR)
                    if probe.get("ok"):
                        reply = probe
                    elif self._chal_capable:
                        reply = self._auth_via_challenge(sock, action, user, password)
                    else:
                        # auth-003（2026-09-18）：不再回退到"明文口令过网"——中间人
                        # 剥掉 probe 的 proto 字段即可把新版观看端降级到旧直发路径，
                        # 明文口令连同账号被链路/隧道嗅探。服务端不支持挑战-应答
                        # （或探测被篡改）时明确拒绝并提示升级共享端。
                        self.auth_msg = ("共享端不支持安全登录（挑战-应答），"
                                         "为避免口令明文过网已拒绝；请更新共享端后重试")
                        log.warning("频道[%s] 服务端 proto=%r 无挑战-应答能力，"
                                    "已拒绝明文口令登录（auth-003）",
                                    self.name, proto)
                        return False
                elif action in (Act.LOGIN, Act.REGISTER) and self._chal_capable:
                    reply = self._auth_via_challenge(sock, action, user, password)
                elif action in (Act.LOGIN, Act.REGISTER):
                    # 能力缓存已判定服务端不支持挑战-应答（如上次探测后重连）——
                    # 同样拒绝明文口令（auth-003：不留降级路径）
                    self.auth_msg = ("共享端不支持安全登录（挑战-应答），"
                                     "为避免口令明文过网已拒绝；请更新共享端后重试")
                    log.warning("频道[%s] 服务端无挑战-应答能力（缓存），已拒绝明文口令登录",
                                self.name)
                    return False
                else:
                    send_msg(sock, {"action": action, "user": user, "pass": password,
                                    "proto": list(PROTOCOL_VERSION)})
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
                self._auth_error_text = ""
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
            if action == Act.REGISTER and msg == "用户名已存在":
                # 第 133 条：带原因重新弹框（而不是静默 30 秒），让用户能立刻改名重试
                self._auth_error_text = "注册失败：用户名已被占用，请换一个或改点「登录」"
                self.auth_msg = self._auth_error_text
                self._retry_delay = 0.0
                log.warning("频道[%s] 注册用户名已被占用：%s（稍后重新弹出登录框）", self.name, user)
                return False
            if action == Act.REGISTER and msg:
                # 第 131 条：服务端关闭了自助注册（bootstrap/closed 策略）或账户文件写不
                # 进去时，必须把原因显示出来并停止本轮重试；否则用户看到的只是"输完又弹
                # 框"，误以为是自己密码打错了。
                # 第 133 条：原因同时写进登录框（红字），并在约 1 秒后重新弹出，用户可直接
                # 改成「登录」用已有账户进去，不必等 30 秒冷却。
                self._auth_error_text = "注册失败：%s" % msg
                self.auth_msg = self._auth_error_text
                self._retry_delay = 0.0
                log.warning("频道[%s] 注册被拒绝：%s（稍后重新弹出登录框）", self.name, msg)
                return False
            if msg == "观看端版本过旧，请更新后重试":
                # D1：服务端只接受挑战-应答，本轮却走了旧直发路径（能力缓存失效的
                # 窄窗口，如服务端运行中改了配置）。复位缓存，重连后重新探测。
                self._chal_capable = None
                self._auth_error_text = msg
                self.auth_msg = msg
                self._retry_delay = 0.0
                log.warning("频道[%s] 服务端仅接受挑战-应答认证，已复位能力缓存", self.name)
                return False
            if msg in ("用户名或密码错误", "账号不存在"):
                # 第 133 条（2026-09-11）：**不再**在这里设置 30 秒弹框冷却。
                # 旧行为：凭据错误 → _prompt_blocked_until=+30 → 下一次重连
                # _ask_credentials 立刻返回 None（被当成"用户取消"）→ 登录框消失，而
                # 重连退避又逐次涨到 30 秒；用户输错一次后，长时间再也看不到登录页面，
                # 只会以为"根本没有登录入口"（本次用户报障正是此现象）。
                # 现在：清除失效凭据 → 把服务端原因带进登录框（红字）→ 复位重连退避，
                # 约 1 秒后重新弹框并预填上次的用户名，用户可立即重输。
                self._auth_error_text = "%s（用户名须与共享端账户完全一致，区分大小写）" % msg
                self.auth_msg = "认证失败：%s" % msg
                # 凭据失效（改密/账号删除等）：清除缓存，重连时重新询问，避免无限重试旧凭据
                self._password = None
                self.username = ""
                self._retry_delay = 0.0
                if self.auth:
                    self.auth = {}
                    try:
                        if self.owner is not None:
                            self.owner._save_channels()
                    except Exception:
                        pass
                log.warning("频道[%s] %s（已清除本地凭据，约 1 秒后重新弹出登录框）",
                            self.name, msg)
                return False
            # 其余情况（如服务端异常响应）继续循环，下一轮会弹框询问注册或登录
        return False

    def _auth_via_challenge(self, sock, action, user, password):
        """D1（实现方案 §10.1）：挑战-应答一轮往返，口令不过网。

        登录:   {login,user} → {chal,nonce,salt,iter} → {auth_proof,user,proof=HMAC(dk,nonce)}
        注册:   {register,user} → {chal,nonce,iter}（salt 由本端生成）
                → {reg_proof,user,salt,dk,proof}
        dk = pbkdf2_hmac-sha256(口令, salt, iter)，与服务端账户表存的是同一把密钥。
        返回服务端 auth_result 字典（失败时含 msg），交回 _authenticate 的既有分支处理。
        """
        send_msg(sock, {"action": action, "user": user, "pass": "",
                        "proto": list(PROTOCOL_VERSION)})
        sock.settimeout(10.0)  # 含本端 PBKDF2 十万次迭代（慢机可达数百毫秒）
        chal = recv_msg(sock)
        if not isinstance(chal, dict):
            return {"ok": False, "msg": "服务端响应异常"}
        if chal.get("action") != Act.CHAL:
            # 服务端直接拒绝（如账号不存在/版本过旧）：chal 本身就是 auth_result
            return chal
        try:
            nonce = b64d(chal.get("nonce") or "")
            iter_ = int(chal.get("iter") or PBKDF2_ITERATIONS)
        except (ValueError, TypeError):
            return {"ok": False, "msg": "挑战消息格式非法"}
        # auth-002（2026-09-18）：挑战参数由对端提供，使用前强校验——
        #  · iter 与本地常量不一致即判服务端异常拒登：防中间人回 {iter:1, salt:自选}
        #    得到"一次迭代"的离线口令爆破 oracle，或回超大 iter 让本端挂死；
        #  · nonce 必须非空且足够长：空 nonce 会让 proof 退化为常量 HMAC(dk,"")，
        #    可永久重放。
        if iter_ != PBKDF2_ITERATIONS:
            log.warning("频道[%s] 服务端挑战 iter=%r 与本地常量（%d）不一致，拒绝登录",
                        self.name, iter_, PBKDF2_ITERATIONS)
            return {"ok": False, "msg": "服务端挑战参数异常（iter），已拒绝登录"}
        if len(nonce) < 16:
            log.warning("频道[%s] 服务端挑战 nonce 过短（%d 字节），拒绝登录",
                        self.name, len(nonce))
            return {"ok": False, "msg": "服务端挑战参数异常（nonce），已拒绝登录"}
        if action == Act.REGISTER:
            salt = secrets.token_hex(16)  # 注册路径：服务端还没有该账户记录，salt 本端生成
        else:
            salt = chal.get("salt") or ""
        if not salt:
            return {"ok": False, "msg": "挑战消息缺少 salt"}
        if not _is_hex_str(salt, KDF_SALT_HEX_LEN):
            log.warning("频道[%s] 服务端挑战 salt 格式非法（%r），拒绝登录",
                        self.name, salt)
            return {"ok": False, "msg": "挑战消息格式非法（salt 须为 32 位十六进制）"}
        try:
            dk = derive_dk(password, salt, iter_)
        except ValueError:
            return {"ok": False, "msg": "挑战消息格式非法（salt 非十六进制）"}
        proof = b64e(challenge_mac(dk, nonce, user))   # auth-002：proof 绑定用户名
        if action == Act.REGISTER:
            reply_msg = {"action": Act.REG_PROOF, "user": user, "salt": salt,
                         "dk": dk.hex(), "proof": proof,
                         "proto": list(PROTOCOL_VERSION)}
        else:
            reply_msg = {"action": Act.AUTH_PROOF, "user": user, "proof": proof,
                         "proto": list(PROTOCOL_VERSION)}
        send_msg(sock, reply_msg)
        result = recv_msg(sock)
        if not isinstance(result, dict):
            return {"ok": False, "msg": "服务端响应异常"}
        return result

    def _abandon_cred_request(self, box, done):
        """放弃本次登录请求（ui-003，2026-09-18）：标记超时并置位 done。

        旧实现超时直接 `return None`，队列里的请求仍"待弹"——用户几小时后回来时
        主线程把它当成新请求再弹一个**幽灵登录框**（第 108/B15 的 `done.is_set()`
        判据依赖 done 置位，超时分支恰恰不置位）；调用方还把 None 当"用户取消"
        记 30 秒静默冷却，明明没人点过取消。
        """
        self._cred_timeout = True
        box["result"] = {"error": TimeoutError("登录等待超时，请求已作废")}
        done.set()

    def _ask_credentials(self):
        """请求主线程弹出登录/注册对话框；返回 ("login"|"register", user, password) 或 None。

        接收线程只把请求放入队列并等待结果；对话框由主线程（poll 回调）直接创建，
        与首启向导同款标准模态窗口。跨线程 root.after 派生的对话框在 Windows 上存在
        键盘输入无法到达的问题（焦点上下文与 Tk 内部状态不同步）。
        """
        # ui-011（2026-09-18）：冷却用单调钟——墙钟后跳（对时/NTP）会让
        # `time.time() < blocked_until` 长期为真，登录框"永久静默"
        if time.monotonic() < self._prompt_blocked_until:
            # 第 133 条：冷却必须留痕——原来这里静默 return None，用户与日志都无从知道
            # "登录框被压住了"，只能看到"没有登录页面"。
            log.info("频道[%s] 登录框处于冷却中（%.0f 秒后可再次弹出；上次为手动取消）",
                     self.name,
                     max(0.0, self._prompt_blocked_until - time.monotonic()))
            return None
        owner = self.owner
        if owner is None:
            return None
        # 等待 Tk root 创建且 mainloop 真正启动后再入队：接收线程在 __init__ 期间即可能
        # 到达此处（root 尚为 None）或处于首启向导 wait_window 期间（mainloop 未启动）。
        # 轮询等待而不是直接返回 None，可避免误入 30 秒冷却后狂重试。
        # C19（2026-09-11）：轮询改用 _stop_evt.wait(0.1)——原来 time.sleep(0.1) 不受
        # stop()/request_stop() 影响，删除频道后接收线程仍会在这里空转最长 60 秒。
        stop_evt = getattr(self, "_stop_evt", None)
        deadline = time.time() + 60
        while time.time() < deadline:
            if not getattr(self, "_running", True):
                return None
            if stop_evt is not None and stop_evt.is_set():
                return None
            root = owner.root
            ready = getattr(owner, "_mainloop_ready", None)
            if root is not None and (ready is None or ready.is_set()):
                break
            if stop_evt is not None:
                if stop_evt.wait(0.1):
                    return None
            else:
                time.sleep(0.1)
        else:
            return None
        # 第 93 条：结果走"每请求独立 box"，不再用频道上的共享 _cred_result 槽——
        # 超时返回后用户才提交时，结果只写进这个已被放弃的 box，绝不会串给下一次请求。
        box = {}
        done = threading.Event()
        #: ui-003（2026-09-18）：本次等待是否超时/被中断（而非用户取消）
        self._cred_timeout = False
        #: ui-024（2026-09-20）：本次等待是否被"更新的登录请求"从队列里淘汰
        self._cred_evicted = False
        # 第 108 条（B15）：队列限长——接收线程 done.wait(120) 超时后会重连并再次入队，
        # 用户几小时后回来关掉第一个框，主线程会接着弹出积压的第二、第三个"幽灵登录框"。
        # 超过 4 个就丢掉最旧的。
        #
        # A3（2026-09-11）：淘汰**必须唤醒被淘汰的等待者**。原实现只 get_nowait 丢掉最旧
        # 请求，其 done 永远不会被置位（主线程也从队列里取不到它了），该频道的接收线程只能
        # 空等满 120 秒——多频道（≥5）同时登录时必现"某个频道一直连不上，过两分钟自己好了"。
        try:
            while owner._cred_requests.qsize() >= 4:
                old_ch, old_done, old_box = owner._cred_requests.get_nowait()
                old_box["result"] = {"error": RuntimeError("登录请求被更新的请求替换")}
                old_done.set()
        except queue.Empty:
            pass
        owner._cred_requests.put((self, done, box))
        # 第 133 条：弹出动作必须留痕（用户报"没有登录页面"时，日志要能直接回答"弹没弹"）
        _last_user = getattr(self, "_last_auth_user", "") or ""
        log.info("频道[%s] 需要账户（服务端要求登录），已弹出登录框%s",
                 self.name, ("；上次尝试：%s" % _last_user) if _last_user else "")
        # A3：等待必须可被打断——done.wait(120) 不受 _running/_stop_evt 影响时，删除一个
        # 正在等登录的频道会让 channel-recv 线程与其 socket 残留最长 120 秒（B13 已把退避
        # 等待改成可中断 Event.wait，这条路径漏了）。这里改成 0.5 秒一轮的可中断等待。
        waited = 0.0
        while not done.is_set():
            if done.wait(0.5):
                break
            if stop_evt is not None and stop_evt.is_set():
                self._abandon_cred_request(box, done)
                return None
            if not getattr(self, "_running", True):
                self._abandon_cred_request(box, done)
                return None
            waited += 0.5
            if waited >= 120.0:
                self._abandon_cred_request(box, done)
                return None
        result = box.get("result")
        if isinstance(result, dict) and "error" in result:
            # ui-024（2026-09-20）：被"更新的登录请求"从队列里淘汰 ≠ 用户取消。
            # 旧实现在这里只 return None，`_cred_timeout` 保持 False，于是
            # `_authenticate` 走"用户取消"分支：给这个从未被用户取消的频道设了
            # `_prompt_blocked_until = +30s`（无辜频道半分钟不再弹框），
            # 并把真因连日志一起写成"用户取消登录"。
            self._cred_evicted = True
            return None
        if result is None or "value" not in result:
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
        fresh = 0 <= latency_us <= 10_000_000
        if fresh:
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
            if fresh:
                self._latency_ring.append((now, latency_us))

    def latency_percentiles(self):
        """E4（§11.4）：30 秒窗口的 (p50_ms, p95_ms, 样本数)；最长 5 秒重算一次。

        样本为墙钟 ts 差，跨机时钟偏差会整体平移绝对值——消费侧（面板/悬浮窗/
        诊断报告）以相对趋势为主，另给 RTT/2 参考（effective_latency_ms）。
        样本不足 10 个时 p50/p95 记 0（视作"暂无"），避免个别抖动霸榜。
        """
        with self.lock:
            cache = self._p_stats_cache
            now = time.monotonic()
            if cache is not None and now - cache[0] < 5.0:
                return cache[1], cache[2], cache[3]
            vals = sorted(v for t, v in self._latency_ring if now - t <= 30.0)
            n = len(vals)
            if n < 10:
                p50 = p95 = 0.0
            else:
                def _pct(p):
                    idx = min(n - 1, max(0, int(math.ceil(p * n / 100.0)) - 1))
                    return vals[idx] / 1000.0
                p50 = _pct(50)
                p95 = _pct(95)
            self._p_stats_cache = (now, p50, p95, n)
            return p50, p95, n

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

    def _reset_stats(self):
        """清空帧率/延迟样本（ui-010，2026-09-18）。

        切源/断线重连时旧样本必须作废：`_fps_times` 混入两条路径会虚报（新旧源
        各半 → fps 近 2 倍），`_latency_samples`/`_latency_ring` 混入两个源/两条
        路径的 P50/P95 不再代表当前链路。属性访问走 getattr——测试常经 __new__
        构造 Channel，未必跑过 __init__。

        调用方已持 `self.lock` 时必须走 `_reset_stats_locked()`——`threading.Lock`
        不可重入（switch_source 内部即持锁调用）。
        """
        with self.lock:
            self._reset_stats_locked()

    def _reset_stats_locked(self):
        """`_reset_stats` 的锁内实现体（调用方持 self.lock）。"""
        for name in ("_fps_times", "_latency_samples"):
            seq = getattr(self, name, None)
            if seq is not None:
                try:
                    seq.clear()
                except Exception:
                    pass
        ring = getattr(self, "_latency_ring", None)
        if ring is not None:
            try:
                ring.clear()
            except Exception:
                pass
        self._p_stats_cache = None
        self.latency_ms = 0.0
        self.fps = 0.0

    # ---------- MultiView：共享开关 / 观看源切换 ----------

    def request_keyframe(self):
        """供接收线程响应 host 转达的 req_keyframe：提示上传器补关键帧。

        第 106 条（B7）：**不取 _share_lock**。本方法跑在接收线程的控制消息路径上，
        而停止共享/恢复共享会持该锁做 join（最多 2 秒）；被挡在锁上期间接收线程不
        select/recv，socket 缓冲积压，锁一释放就一口气解几十帧——用户感知是"点一下
        共享，画面卡 2 秒然后跳帧"。`_share` 只做整体替换，无锁读引用是安全的。
        """
        session = self._share
        if session is not None:
            session.request_keyframe()

    def set_share(self, on):
        """开/关本频道屏幕共享。返回 (ok, 提示)；开时校验对方能力（cap 确认）。

        第 106 条（B7）：`_share_lock` 只保护"状态提交"，所有可能阻塞的操作
        （`_cap_event.wait(2.0)`、`_stop_share` 的 join、`_send_locked`）都放到锁外。
        否则接收线程处理 cap/peers（`_maybe_resume_share`）时会等在锁上最长 2 秒
        不读 socket。
        """
        if not on:
            with self._share_lock:
                self.share_enabled = False
            self._stop_share()          # 锁外 join（最多 2 秒）
            # 通知 host 注销本成员的共享登记（连接保留）：host 广播 roster，
            # 其他观看端据此从源列表移除本端并回落 local
            # ui-009：UNSHARE 不需要回执，异步发送（主线程不得同步等 5 秒超时）
            self._send_ctrl_async({"action": Act.UNSHARE})
            log.info("频道[%s] 共享已关闭", self.name)
            return True, ""
        with self._share_lock:
            if self.share_enabled and self._share is not None and self._share.alive:
                return True, ""
        if not self.multiview:
            # 能力尚未确认：cap_probe 已随连接发出，等 cap/peers（≤2s）
            if not self._cap_event.wait(2.0):
                return False, "对方不支持共享上传（旧版服务端或无响应）"
        if self.status != "receiving":
            return False, "频道未连接，无法开启共享"
        with self._share_lock:
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
            # 第 116 条（C6）：切源复用解码上下文 + flush，不再 new VideoDecoder
            # （原先从 HEVC 源切回 H.264 时靠重建切换编码器；现走 set_codec）。
            # 持 _dec_lock：防止与接收线程 decode 并发 close 上下文。
            with self._dec_lock():
                self._decoder.set_codec(CODEC_H264)
                self._decoder.reset()
            self.last_frame = None
            # 第 105 条（B6）：切源同时作废在途解码批次——接收线程可能正拿着旧源的
            # NAL 循环，不校验会把旧源帧喂进刚建好的解码器、或把旧源画面贴到新源
            # 标签下（并反复重建解码器）。
            self._gen = getattr(self, "_gen", 0) + 1
            # 第 8 条：清 last_frame 的同时递增 frame_serial，强制 poll 重绘，
            # 否则「有无新帧」判据认为没变化，画面会停留在上一个人至少一个 GOP。
            self.frame_serial += 1
            # ui-010：切源后旧源的帧率/延迟样本立即作废（避免混入两个源的统计）；
            # 此处已持 self.lock，必须走 *_locked 版本（Lock 不可重入）
            self._reset_stats_locked()
            self._resync = False
        sock = self._active_sock
        if sock is None:
            self._revert_source(prev_source)  # 第 9 条：未发包，回滚避免谎报
            return False
        try:
            # 第 100 条：有界发送；超时抛 socket.timeout（OSError 子类）→ 回滚
            # ui-009：本方法由 Tk 主线程调用（源列表/热键），超时从 5s 收紧到 1.2s
            # ——对端停读时界面最多卡 1.2s，失败仍走既有的回滚分支
            self._send_locked(sock, {"action": Act.WATCH, "source": source},
                              timeout=1.2)
        except (OSError, ValueError):
            self._revert_source(prev_source)  # 第 9 条：发包失败，回滚避免谎报
            return False
        if source == "local":
            # ui-025（2026-09-20）：WATCH 与 REQ_KEYFRAME 旧写法在同一个 try 内，
            # 于是"第一条已成功（host 真的切源了）、第二条超时"会去回滚本地
            # watch_source 却不补发 WATCH —— host 持续转发新源、状态栏显示旧源，
            # 用户再点回该源又被 `watch_source == source` 短路、不发包，
            # 错位只能等一次断线重连才收敛。关键帧请求是**尽力而为**的加速手段，
            # 失败不该推翻已经生效的订阅；这里把两次发包的失败处理分开。
            try:
                self._send_locked(sock, {"action": Act.REQ_KEYFRAME, "t": time.time_ns()},
                                  timeout=1.2)
            except (OSError, ValueError) as e:
                log.info("频道[%s] 已切到 %s，但关键帧请求未送达（%s）——"
                         "等该源下一个关键帧起解，不因此回滚订阅",
                         self.name, source, e)
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
            except Exception:
                pass
            # ui-013（2026-09-18）：旧会话的清理（stop() 内含 join 最长 2 秒）不得
            # 在持 _share_lock 的调用路径上做——交给后台线程（第 106 条同款要求）；
            # 本方法调用方持锁，若在此 join，接收线程处理 cap/peers 会等锁不读 socket。
            old, self._share = self._share, None

            def _cleanup(session=old):
                try:
                    session.stop()
                except Exception:
                    pass

            threading.Thread(target=_cleanup, name="share-cleanup",
                             daemon=True).start()
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
        """停止并清空共享会话（可在任意线程调用，第 106 条 / B7）。

        锁内只做"取走引用"，`session.stop()`（join 最多 2 秒）放到锁外执行——
        否则接收线程的 `request_keyframe`/`_maybe_resume_share` 会在锁上等满 2 秒
        不读 socket，表现为卡顿后突发解码。
        """
        with self._share_lock:
            session = self._share
            self._share = None
        if session is not None:
            try:
                session.stop()
            except Exception:
                pass

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

    def check_share_alive(self):
        """B8（2026-09-11）：共享会话自行终止时，本端 UI 与对端 host 都必须收到通知。

        原来 `ScreenShareSession` 的线程可能因采集后端不可用 / 连环编码失败 / 未预期异常
        而终止，**连接仍然健在**：
          - 对端 host 不知道 → `share_id` 仍是 `peer:N`，该成员继续挂在所有观众端的
            「画面源」列表里（"幽灵源"：选中后永远收不到帧，停在最后一帧或全黑）；
          - 本端也不知道 → `Channel.share_enabled` 仍为 True，面板按钮仍显示「停止共享」，
            用户以为自己在共享。
        本方法由主线程 poll 周期调用（见 `ViewerApp._check_share_sessions`），发现会话已死
        即：关开关 → 清理会话 → 若连接仍在补发一次 unshare → 状态栏提示原因。
        """
        with self._share_lock:
            if not self.share_enabled or self._share is None:
                return
            session = self._share
        try:
            if session.alive:
                return
        except Exception:
            return
        reason = getattr(session, "exit_reason", None) or "上传会话已结束"
        with self._share_lock:
            self.share_enabled = False
        self._stop_share()
        # ui-009：本方法由主线程 poll 调用，UNSHARE 异步发送
        self._send_ctrl_async({"action": Act.UNSHARE})
        log.warning("频道[%s] 共享已停止：%s", self.name, reason)
        self._notify_share_error("共享已停止：%s" % reason)

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
        # 第 108 条（B13）：打断退避等待，线程立即回收（getattr 兼容测试替身）
        stop_evt = getattr(self, "_stop_evt", None)
        if stop_evt is not None:
            stop_evt.set()
        with self._share_lock:
            if self._share is not None:
                self._share.signal_stop()

    def stop(self):
        """停止接收线程与共享上传（daemon 线程，无需 join）。"""
        self._running = False
        stop_evt = getattr(self, "_stop_evt", None)
        if stop_evt is not None:
            stop_evt.set()   # 第 108 条（B13）：打断退避等待
        # A3（2026-09-11）：唤醒仍在等登录框的接收线程——stop() 之前无法打断
        # _ask_credentials 的等待，删除频道后线程与其 socket 会残留最长 120 秒。
        self._wake_pending_credentials("频道已停止")
        # 第 61 条：cancel 能力确认兜底定时器并清 _active_sock，杜绝退出后定时器触发
        # _share_sync_after_connect → _start_share 在 teardown 期间新開上传会话。
        if self._share_sync_timer is not None:
            self._share_sync_timer.cancel()
            self._share_sync_timer = None
        self._active_sock = None
        self._stop_share()

    def _wake_pending_credentials(self, reason):
        """A3：把本频道在等登录框的请求就地置位，让接收线程立即可返回。

        队列元素为 (channel, done, box)；只动属于本频道的项，其他频道的等待不受影响。
        """
        owner = getattr(self, "owner", None)
        q = getattr(owner, "_cred_requests", None) if owner is not None else None
        if q is None:
            return
        try:
            with q.mutex:
                for ch, done, box in list(q.queue):
                    if ch is self:
                        box["result"] = {"error": RuntimeError(reason)}
                        done.set()
        except Exception:
            pass


class ViewerApp:
    """观看端应用：多频道接收 + tkinter 悬浮窗 + 控制台面板 + 全局热键。"""

    def __init__(self, cfg, addr_override=None):
        self.cfg = cfg
        viewer_cfg = cfg["viewer"]
        # 第 100 条：显示宽度/透明度来自配置，手改成 0/负数/字符串时不应让每帧
        # resize 抛异常被吞成"永久占位文字"。这里与 set_display_width 同口径夹紧。
        self.display_width = safe_int(viewer_cfg.get("display_width", 480), 480, 1, 4096)
        self.alpha = safe_float(viewer_cfg.get("alpha", 0.9), 0.9, 0.0, 1.0)
        self.click_through = bool(viewer_cfg.get("click_through", True))
        self.panel_topmost = bool(viewer_cfg.get("panel_topmost", True))

        # ---- 第 100 条：先把接收线程依赖的全部状态建好，再创建频道 ----
        # Channel.__init__ 末尾就启动接收线程，而 _receiver_loop 会读 owner._mainloop_ready
        # （等界面就绪再连接）与 owner.running/_cred_requests。旧顺序把这些属性放在频道
        # 列表之后 24 行创建，getattr(..., None) 拿到 None 就把"等待界面就绪"整个跳过
        # （实测 200/200 次子线程都先于父线程到达该行）→ 首启向导期间就连接，服务端
        # auth_timeout 到点即断，用户"明明输对了却登录失败"。
        self.running = True
        self.has_frame = False
        self._last_text = None
        self._photo = None
        self._last_frame_serial = -1
        self._last_channel_idx = -1
        self._last_channel_status = None  # ui-002：poll 判据含状态维度
        # A4 §3.2：poll 空闲退避的当前间隔（毫秒）。收到新帧或任何"需要灵敏响应"
        # 的状态即重置 15；仅在已连上（receiving）且画面静止时沿 15→33→50 退避。
        self._poll_idle_ms = 15
        # 第 98 条：绘制路径缓存——位置只取决于窗口尺寸（=照片尺寸），尺寸未变时跳过
        # 每帧的 update_idletasks + winfo 查询 + geometry 下发；状态文本未变时跳过 reconfigure。
        self._last_pos_size = None    # 上次定位所用的 (display_width, new_h)
        self._last_status_text = None  # 上次写入状态叠加的文本
        # ui-012：绘制失败计数与最近一次告警时刻（限流用）
        self._draw_fails = 0
        self._draw_fail_warn_at = 0.0
        self.drag_offset = None
        self._drag_start_root = None
        self.user_moved = False
        self._mainloop_ready = threading.Event()  # mainloop 真正启动后置位，供接收线程安全调度 UI
        self._cred_requests = queue.Queue()  # 认证对话框请求队列（接收线程入队，主线程 poll 处理）
        # 第 108 条（B14）：工作线程 → 主线程的 UI 回调队列。原来直接 root.after(0, fn)，
        # running 判断与调用之间存在窗口：quit() 销毁 Tcl 解释器后仍可能向其投递命令
        # （解释器层崩溃/挂起，except 兜不住）。改为只入队，由 15ms 的 poll 排空。
        self._ui_queue = queue.SimpleQueue()
        self._cred_busy = False  # 第 93 条：认证对话框互斥标志，强制"每次只一个"，杜绝 wait_window 嵌套期间再叠一个
        self._channels_lock = threading.RLock()  # 保护频道列表与配置持久化（接收线程也会写凭据）
        self.root = None
        self.label = None
        self.status_overlay = None  # 悬浮窗帧率/延迟状态叠加 Label
        self.menu = None
        self.hwnd = None
        self.panel = None
        # B4（实现方案 §6）：更新提示后缀，拼在面板底部状态标签的默认文本后
        self._update_hint = ""
        self._share_toggle_busy = False  # 第 60 条：共享开关异步执行期间的重入护栏
        self._save_after_id = None  # 第 64 条：viewer 配置去抖落盘的 root.after 句柄
        self._share_alive_check_at = 0.0  # B8：共享会话存活巡检的节流时刻
        # 全局热键状态（注册结果供面板提示/失败上屏；tid 供退出时 PostThreadMessage 唤醒）
        self._hotkey_tid = None
        self._hotkey_registered = set()
        self._hotkey_failed = []
        self._hint_label = None
        self._hotkey_notice_synced = False
        self.channels = []
        self.active_idx = 0

        # 频道列表：为空时回退到 server_addr 生成默认单频道（向后兼容）
        # 第 106 条（B16）：先规范化——手改成字符串/对象时旧实现会遍历字符并调 .get，
        # 抛 AttributeError 让 ViewerApp 构造失败（启动直接弹错误框）。
        channel_items = _normalize_channel_items(viewer_cfg)
        self.channels = [
            Channel(
                item.get("name") or ("频道%d" % (i + 1)),
                item.get("addr", ""),
                auth=item.get("auth") if isinstance(item.get("auth"), dict) else None,
                owner=self,
            )
            for i, item in enumerate(channel_items)
        ]
        # 第 100 条：命令行 --addr 是"本次运行生效"的覆盖。旧实现只写 server_addr，
        # 而 server_addr 仅在频道列表为空时被读取 → 已有频道时该参数被静默忽略。
        # 这里把它作为**临时频道**插到首位并设为活动频道（不写回配置，见 _save_channels）。
        if addr_override:
            self.channels.insert(0, Channel("命令行地址", addr_override,
                                            owner=self, ephemeral=True))
        self.active_idx = 0

    # ---------- 生命周期 ----------

    def run(self, root=None):
        """启动 Tk 窗口、控制台面板、热键监听并进入主循环。

        A3（第 133 条）：main() 在重依赖加载前已建好带极简 splash 的 root 并传入——
        这里的"关闭 splash"时机点即清空 splash 内容并把同一个 root 交给
        _build_ui 改造成悬浮窗（不新建 Tk，避免双解释器实例）。root 为 None
        （测试/e2e 直接构造）时维持旧行为自行创建。
        """
        global _TK_ROOT
        self.root = root if root is not None else tk.Tk()
        _TK_ROOT = self.root
        if root is not None:
            try:
                root.overrideredirect(False)
                for w in list(root.winfo_children()):
                    w.destroy()
            except tk.TclError:
                pass  # splash root 已被外部销毁：照常继续（_build_ui 重设一切）
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
        # B4（实现方案 §6）：启动后台查一次更新，命中时只改面板状态栏默认文本，失败静默
        update_check.start_background_check(
            self.cfg, lambda msg: self._post_ui(lambda m=msg: self._set_update_hint(m)))
        try:
            self.root.mainloop()
        finally:
            self.running = False
            self._stop_hotkeys()  # 第 C8 条：打断 GetMessageW，释放全局热键
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
        self._stop_hotkeys()  # 第 C8 条：打断 GetMessageW，释放全局热键
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
            if was_active:
                self.active_idx = max(0, min(i, len(self.channels) - 1))
            elif self.active_idx > i:
                self.active_idx -= 1
        # 第 106 条（B7）：stop()（含共享会话 join，最多 2 秒）与落盘都移到
        # _channels_lock 之外——接收线程的 _save_channels 也在抢这把锁，被挡住就
        # 是 2 秒不读帧；主线程这边则是"删除频道"卡住界面。
        removed.stop()
        log.info("删除频道[%s] %s", removed.name, removed.addr)
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
                "auth_msg": ch.auth_msg,  # 第 104 条（B10）：认证失败具体原因，面板据此显示
                "fps": fps,
                "latency": latency,  # 端到端延迟（RTT/2，0 表示暂无数据）
                "latency_p95": ch.latency_percentiles()[1],  # E4：30s 窗口 P95（ms，0=暂无）
                "video": ch.video_mode,  # 是否视频编码流（展示用）
                "codec": ch.cur_codec,   # 当前视频流编码类型（H.264/HEVC，展示用）
            })
        return snapshot

    def _save_channels(self):
        """将当前频道列表（name/addr/auth）写回配置（线程安全）。

        第 100 条：跳过命令行 `--addr` 临时加入的频道（ch.ephemeral），
        避免每次带参数启动都往 config.json 里追加一条永久频道。
        """
        with self._channels_lock:
            self.cfg["viewer"]["channels"] = [
                {
                    "name": ch.name,
                    "addr": ch.addr,
                    **({"auth": ch.auth} if ch.auth else {}),
                }
                for ch in self.channels
                if not getattr(ch, "ephemeral", False)
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
        """把回调交给主线程执行（子线程调用，退出中静默丢弃）。

        第 92 条：除 root 为空外，还拦截「mainloop 未就绪」与「已进入退出」两种状态。
        第 108 条（B14）：改为**只入队**，不再从工作线程直接 `root.after`。原来的
        `running` 判断与 `root.after` 之间是 TOCTOU 窗口——quit() 正在销毁 Tcl 解释器时
        仍可能向其投递命令，那是解释器层崩溃/挂起，末尾的 except 兜不住。回调统一由
        `poll` 在 Tk 主线程里 `_drain_ui_queue` 执行。
        """
        if not self.running:
            return
        ready = getattr(self, "_mainloop_ready", None)
        if ready is not None and not ready.is_set():
            return
        try:
            self._ui_queue.put(fn)
        except Exception:
            pass

    def _drain_ui_queue(self):
        """主线程：执行工作线程投递的 UI 回调（第 108 条 / B14）。

        单次最多执行 64 个，避免某次 poll 被大量回调拖住；剩余的下一个 15ms 周期继续。
        """
        for _ in range(64):
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                log.warning("UI 回调执行失败", exc_info=True)

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
        panel.title("%s · 画面控制台" % APP_TITLE)
        panel.configure(bg=COL_BG)
        self.panel = panel
        if self.panel_topmost:
            panel.attributes("-topmost", True)

        # ---------- 深色主题（第 136 条 C3：配置块收拢至 ui_common.build_style，
        # 逐行照搬，Panel.* 命名与原内联块完全一致） ----------
        # ui-018：viewer 面板不套用 host 的基础 Treeview/TCombobox 基础样式，
        # 保持收拢前的外观（§8 逐字节不变）
        ui_common.build_style(panel, include_base=False)

        # ---------- 顶部：标题 + 快捷键提示 ----------
        header = ttk.Frame(panel, style="Panel.TFrame")
        header.pack(fill="x", padx=14, pady=(12, 4))
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        if bool(_viewer_hotkeys_cfg(self.cfg).get("direct", True)):
            hint = ("Ctrl+Alt+←/→ 换频道   Ctrl+Alt+↑/↓ 换画面源   "
                    "Alt+1..9 直达   Ctrl+Alt+X 穿透")
        else:
            hint = "Ctrl+Alt+←/→ 换频道   Ctrl+Alt+↑/↓ 换画面源   Ctrl+Alt+X 穿透"
        self._hint_label = ttk.Label(header, text=hint,
                                     style="Subtitle.TLabel")
        self._hint_label.pack(anchor="w", pady=(2, 0))

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
        self._preview_info_cache = None      # 预览小字缓存 (text, fg)，未变则跳过 configure

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
        """每 500ms 刷新频道列表：状态点、帧率、活动行高亮，保留选中行。

        第 115 条（C4）：行数不变时只 `tree.item(...)` 更新值/标签发生变化的行，
        不再全删全建（原先每 500ms delete+insert 全部行，滚动位置与焦点都被拽动）。
        """
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
            children = tree.get_children()
            self._panel_suppress_select = True
            if len(children) != n or n == 0:
                # 行数变化/首次：全量重建；item 40：记下顶部可见比例，重建后恢复
                try:
                    y_top = tree.yview()[0]
                except Exception:
                    y_top = 0.0
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
                if y_top:
                    try:
                        tree.yview("moveto", y_top)
                    except Exception:
                        pass
            else:
                # 行数不变：只更新值/标签变化的行（C4 增量刷新）
                for i, item in enumerate(snapshot):
                    latency = item["latency"]
                    name_txt = item["name"]
                    if item.get("video"):
                        codec_tag = "HEVC" if item.get("codec") == CODEC_HEVC else "H264"
                        name_txt = codec_tag + "·" + name_txt
                    values = ("●", name_txt, item["addr"],
                              "%d" % round(item["fps"]),
                              "-" if latency <= 0 else "%d" % round(latency))
                    tags = ["st_%s" % item["status"]]
                    if i == self.active_idx:
                        tags.insert(0, "row_active")
                    iid = str(i)
                    try:
                        cur_values = tuple(tree.item(iid, "values"))
                        cur_tags = tuple(tree.item(iid, "tags"))
                    except Exception:
                        continue
                    if cur_values != values or cur_tags != tuple(tags):
                        tree.item(iid, values=values, tags=tags)
            if self._panel_selected is not None and n and 0 <= self._panel_selected < n:
                tree.selection_set(str(self._panel_selected))
                tree.focus(str(self._panel_selected))
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
            auth_msg = ""
            serial = 0
            changed = True
            if self.channels and 0 <= idx < len(self.channels):
                ch = self.channels[idx]
                name, status = ch.name, ch.status
                auth_msg = ch.auth_msg
                with ch.lock:
                    serial = ch.frame_serial
                changed = (idx != self._panel_last_idx or serial != self._panel_last_serial)
                if changed:
                    with ch.lock:
                        if ch.last_frame is not None:
                            frame = ch.last_frame   # 第 105 条（B5）：只取引用
            if frame is not None:
                if changed:
                    fh, fw = frame.shape[:2]
                    if fw > 0 and fh > 0:
                        # 等比缩放到宽度 320；过高时按高度上限 165 收缩，防止面板被撑开
                        new_w, new_h = 320, max(1, int(round(fh * 320.0 / fw)))
                        if new_h > 165:
                            new_w, new_h = max(1, int(round(fw * 165.0 / fh))), 165
                        # 阶段 A1（优化方案 2026-09-17）：与 _show_frame 同款"先缩放后转换"
                        small = cv2.resize(frame, (new_w, new_h),
                                           interpolation=cv2.INTER_AREA)
                        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                        photo = ImageTk.PhotoImage(Image.fromarray(rgb))
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
                    # 第 102 条：清空图像必须用 ""，**不能用 None**。tkinter 的
                    # _options() 会丢弃值为 None 的选项，`configure(image=None, ...)`
                    # 实际只发 -text，label 的 -image 仍指向旧图；随后
                    # `self._panel_photo = None` 让 PhotoImage.__del__ 执行
                    # `image delete pyimageN`，label 从此持有已删除的图像名 →
                    # 之后任何 configure 都抛 TclError: image "pyimageN" doesn't
                    # exist（实测每次切源必现，预览永久失效并刷屏）。
                    self._preview_label.configure(image="", text="暂无画面")
                    self._panel_photo = None
            # 预览小字：频道名称与状态（C4：文本未变则跳过 configure）
            if name is None:
                info_text, info_fg = "", COL_DIM
            else:
                info_text = "%s · %s" % (
                    name, auth_msg if status == "auth" and auth_msg
                    else STATUS_TEXT.get(status, status))
                info_fg = STATUS_COLOR.get(status, COL_DIM)
            prev_info = getattr(self, "_preview_info_cache", None)
            if prev_info != (info_text, info_fg):
                self._preview_info_cache = (info_text, info_fg)
                self._preview_info.configure(text=info_text, fg=info_fg)
        except Exception:
            # 第 91 条：cvtColor/fromarray/resize/PhotoImage/configure 原全裸奔，一帧异常形状
            # （如解码出灰度 (H,W) 而非 (H,W,3)、或通道数不符）的图像会让 cv2.cvtColor 抛异常，
            # 在末尾续排 after 之前逃出 → 预览永久停更（fps/状态陈旧），而悬浮窗仍动 →「半活」。
            # 对照 _show_frame 本就有 try 兜底。兜底后异常被记录、本次跳过，finally 续排定时器。
            # 第 102 条：同一条 TclError 每 200ms 刷一条会淹没真问题（实测单次会话 66 条），
            # 改为 60 秒最多记一次（首次仍可见，供排查）。
            now_err = time.monotonic()
            if now_err - getattr(self, "_preview_err_at", 0.0) >= 60.0:
                self._preview_err_at = now_err
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
        """重建"画面源"卡片行：目标频道的 local + peers，活动频道行加观看标记。

        第 115 条（C4）：行数与 (mark, source, info, sid) 序列均未变时跳过全删全建；
        仅值变化的行走 tree.item 更新。
        """
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
                # B8：共享者侧会话已停但仍未注销的"幽灵源"标出无信号，避免观众选中后
                # 停在最后一帧却毫无提示
                pname = "队友 · %s" % (p.get("name") or sid)
                if p.get("stale"):
                    pname += "（无信号）"
                rows.append(("▶ 观看中" if (active and watching == sid) else "",
                             pname,
                             p.get("addr") or "", sid))
        # 结构（sid 序列）未变：只更新值变化的行，避免每 500ms 全删全建
        if (self._src_rows
                and len(self._src_rows) == len(rows)
                and [r[3] for r in rows] == list(self._src_rows)):
            for i, (mark, source, info, _sid) in enumerate(rows):
                values = (mark, source, info)
                tags = ("src_watching",) if mark else ()
                iid = str(i)
                try:
                    cur_values = tuple(tree.item(iid, "values"))
                    cur_tags = tuple(tree.item(iid, "tags"))
                except Exception:
                    continue
                if cur_values != values or cur_tags != tags:
                    tree.item(iid, values=values, tags=tags)
            return
        # item 40：同样全删全建，记下滚动比例，重建后恢复，避免源列表被拽回顶部
        try:
            y_top = tree.yview()[0]
        except Exception:
            y_top = 0.0
        self._src_rows = []
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
        self._status_label.configure(text=self._panel_default_status_text(),
                                     fg=COL_DIM)

    def _panel_default_status_text(self):
        base = "就绪"
        return base + (" · " + self._update_hint if self._update_hint else "")

    def _set_update_hint(self, msg):
        """B4（实现方案 §6）：状态栏追加一行更新提示（不弹窗、不打断其他临时消息）。"""
        self._update_hint = msg
        if self._status_after_id is None:
            try:
                self._status_label.configure(text=self._panel_default_status_text())
            except Exception:
                pass

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

    def _credentials_dialog(self, channel_name, error_text="", default_user=""):
        """弹出登录/注册模态对话框（主线程执行）；返回 ("login"|"register", user, password) 或 None。

        第 133 条（2026-09-11）：新增 error_text / default_user 两个可选参数——
        服务端上一次的拒绝原因以红字直接写在框里，用户名预填上次尝试值。
        旧实现每次都是空白框且不显示任何原因，用户只能靠面板状态栏去猜。
        """
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
        user_var = tk.StringVar(value=default_user)
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
        err = tk.Label(body, text=error_text, bg=COL_CARD, fg=COL_OFFLINE,
                       font=("Microsoft YaHei", 9), wraplength=340, justify="left")
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
                  command=lambda: _submit(Act.LOGIN)).pack(side="left")
        tk.Button(btns, text="注册并登录", bg=COL_CTRL, fg=COL_FG, relief="flat",
                  activebackground="#3a3a52", activeforeground=COL_FG,
                  padx=14, pady=4,
                  command=lambda: _submit(Act.REGISTER)).pack(side="left", padx=(8, 0))
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
        # 强制聚焦输入框，确保用户可直接输入（也便于自动化驱动）
        # 第 133 条：预填了用户名时把焦点放到密码框、用户名全选——用户只需补密码，
        # 想改名时直接输入即覆盖。
        try:
            if default_user:
                user_entry.selection_range(0, "end")
                pass_entry.focus_force()
            else:
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
            p50, p95, pn = ch.latency_percentiles()
            if pn >= 10:
                lines.append("频道「%s」延迟 P50/P95（近 30s，%d 样本）：%.0f / %.0f ms"
                             "（墙钟差含时钟偏差，看相对趋势）" % (
                                 ch.name, pn, p50, p95))
            else:
                lines.append("频道「%s」延迟 P95：样本不足（近 30s 仅 %d 帧）" % (
                    ch.name, pn))
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
        # app_dirs()：onedir 打包时 exe 在 <安装根>\fps-viewer\，frpc.exe 通常放在安装根
        search_bases = list(app_dirs())
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
        tk.Button(btns, text="导出 zip", bg=COL_CTRL, fg=COL_FG, relief="flat",
                  activebackground="#3a3a52", activeforeground=COL_FG,
                  padx=14, pady=4,
                  command=lambda: self._export_diag_zip(report)).pack(side="right", padx=(0, 8))
        tk.Button(btns, text="关闭", bg=COL_CTRL, fg=COL_FG, relief="flat",
                  activebackground="#3a3a52", activeforeground=COL_FG,
                  padx=14, pady=4, command=win.destroy).pack(side="right", padx=(0, 8))

    def _export_diag_zip(self, report_text):
        """B2（第 134 条）：把脱敏配置 + 近两日日志 + 本报告打成 zip。"""
        try:
            logs_dir = os.path.join(data_dir(), "logs")
            # 计划 §4.2.2：zip 统一落到 logs 目录，便于与支持者沟通固定路径
            path = diag.build_report_zip(logs_dir, config=self.cfg,
                                         logs_dir=logs_dir,
                                         extra_text=report_text,
                                         pid=os.getpid())
            try:
                self.panel.clipboard_clear()
                self.panel.clipboard_append(report_text)
            except Exception:
                pass
            messagebox.showinfo(
                "导出成功",
                "诊断包已保存到：\n%s\n\n配置中的 token/密码已自动脱敏；"
                "日志仍可能包含 IP、地址等信息，请只发给信任的支持者。" % path,
                parent=self.panel)
        except Exception as e:
            log.warning("诊断包导出失败: %s", e)
            messagebox.showerror("导出失败", "诊断包导出失败：%s" % e,
                                 parent=self.panel)

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
        # ui-014（2026-09-18）：宽度要包含状态叠加行（长队名/P95 会把叠加行撑宽）——
        # 旧实现只看 root 当前宽度或画面 label，叠加行变宽时悬浮窗被推出屏幕右缘
        # 且不再修正。
        overlay = getattr(self, "status_overlay", None)
        w = max(self.root.winfo_width() or 0,
                self.label.winfo_reqwidth() if self.label is not None else 0,
                overlay.winfo_reqwidth() if overlay is not None else 0)
        if w <= 0:
            w = self.label.winfo_reqwidth() if self.label is not None else 320
        # C9（2026-09-11）：原实现还算了 `h = winfo_height()...` 但从未使用（只用 w），
        # 属于死代码，删掉以免误导。
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
        # 第 108 条（B15）：丢弃陈旧请求——等待者已超时（done 已置位）或频道已被删除时，
        # 再弹一个模态框只会打扰用户，且结果写进无人等待的 box。删除频道的情况顺手
        # 置位 done，让接收线程立刻从 done.wait(120) 返回。
        if done.is_set():
            return
        channels = getattr(self, "channels", None)
        if channels is not None and ch not in channels:
            box["result"] = {"error": RuntimeError("频道已删除")}
            done.set()
            return
        self._cred_busy = True
        result = {}
        # 第 133 条：把服务端上一次的拒绝原因与上次尝试的用户名带进登录框（红字+预填）。
        # 读后即清：同一条原因只提示一次，避免用户改用新账号时还看到旧报错。
        error_text = getattr(ch, "_auth_error_text", "") or ""
        default_user = getattr(ch, "_last_auth_user", "") or ""
        if not isinstance(error_text, str):      # 测试替身（MagicMock）容错
            error_text = ""
        if not isinstance(default_user, str):
            default_user = ""
        try:
            ch._auth_error_text = ""
        except Exception:
            pass
        try:
            result["value"] = self._credentials_dialog(ch.name, error_text, default_user)
        except Exception as e:
            result["error"] = e
        finally:
            self._cred_busy = False
        # 结果写进本请求专属 box（非频道共享槽），与 done 配对，杜绝跨请求串结果。
        box["result"] = result
        done.set()

    def _sync_hotkey_notice_once(self):
        """poll 首次拿到注册结果时上屏（覆盖 _post_ui 在 mainloop ready 前丢弃的窗口）。"""
        if getattr(self, "_hotkey_notice_synced", False):
            return
        if not self._hotkey_registered and not self._hotkey_failed:
            return  # 热键线程尚未完成注册
        self._hotkey_notice_synced = True
        try:
            self._notify_hotkey_result(
                list(self._hotkey_registered), list(self._hotkey_failed))
        except Exception:
            pass

    def _check_share_sessions(self):
        """B8：周期性检查各频道的共享会话是否已自行终止（主线程，最长 0.5 秒一次）。

        原实现只在收到 cap/peers 或重连时才有机会发现会话已死（`_maybe_resume_share`）；
        连接保持期间不再检查，于是"共享者以为还在共享、对端挂着死源"可以持续到用户手动
        点一下。这里把"会话已死"从"等下一次 cap/peers"变成"最长一个刷新周期内自愈"。
        """
        now = time.monotonic()
        if now - getattr(self, "_share_alive_check_at", 0.0) < 0.5:
            return
        self._share_alive_check_at = now
        for ch in list(self.channels):
            try:
                ch.check_share_alive()
            except Exception:
                pass

    _POLL_BACKOFF_MS = (15, 33, 50)  # A4 §3.2：空闲退避序列，50ms 封顶

    def poll(self):
        """主循环轮询：检查活动频道是否有新帧（A4 §3.2 后退避到 15/33/50ms）。"""
        if not self.running:
            return
        # item 46：先重排下一次 poll，再处理可能阻塞的认证对话框。
        # _credentials_dialog 的 wait_window 会开嵌套主循环，若此时尚未排定下一个 poll
        # 定时器，嵌套期间悬浮窗就停止绘制（画面/fps/延迟定格最长 120s），而面板刷新链
        # 仍在跑，造成面板与悬浮窗互相矛盾。提前重排可让悬浮窗在对话框期间继续刷新
        # （嵌套循环照常触发 after 定时器），且每次只排一个后继，不会形成定时器风暴。
        self.root.after(self._poll_idle_ms, self.poll)
        self._drain_ui_queue()          # 第 108 条（B14）：执行工作线程投递的 UI 回调
        self._sync_hotkey_notice_once()  # 热键注册结果若在 ready 前被丢弃，这里补上屏
        self._process_cred_requests()
        self._check_share_sessions()    # B8：共享会话自行终止时自愈（最长 0.5 秒一次）
        if not self.channels:
            self._show_text("无频道")
            self._poll_idle_ms = 15     # 无频道时需保持灵敏（可能随时添加/连接）
            return
        channel = self.channels[self.active_idx]
        with channel.lock:
            serial = channel.frame_serial
        status = channel.status
        # ui-002：判据加入状态维度——状态跃迁（断开/重连/认证）即使序列号未变
        # 也必须重绘，否则"已断开，正在重连…"永不可达
        if (self._last_channel_idx == self.active_idx
                and self._last_frame_serial == serial
                and getattr(self, "_last_channel_status", None) == status):
            # 没有新帧：只更新叠加状态，避免反复复制/缩放/绘制同一画面
            self._update_status_overlay()
            # A4 §3.2：仅"已连上且画面静止"才退避；连接中/重连/认证等状态下一拍
            # 可能出现文本或画面变化，保持 15ms 不放大。
            if status == "receiving":
                try:
                    i = self._POLL_BACKOFF_MS.index(self._poll_idle_ms)
                except ValueError:
                    i = 0
                self._poll_idle_ms = self._POLL_BACKOFF_MS[
                    min(i + 1, len(self._POLL_BACKOFF_MS) - 1)]
            else:
                self._poll_idle_ms = 15
            return
        self._poll_idle_ms = 15         # 收到新帧（或切源）即重置，保证收帧灵敏
        frame = None
        with channel.lock:
            if channel.last_frame is not None:
                # 第 105 条（B5）：只取引用，不再 copy()。last_frame 永远整体替换、
                # 从不原地修改（_store_frame 赋新数组、switch_source 置 None），
                # 而下方 cvtColor/Image.fromarray 都只读；1080p 下每帧省 ~6MB 拷贝，
                # 也缩短了持锁时间（接收线程 _store_frame 等的是同一把锁）。
                frame = channel.last_frame
        self._last_channel_status = status
        # ui-002：仅连接态才贴帧。断开/认证/连接中的旧缓存帧（如新路径未作废）
        # 一律让位给状态文字，避免把断线显示成"画面卡住"
        if frame is not None and status == "receiving":
            self._show_frame(frame, serial)
        else:
            self.has_frame = False
            if channel.status == "disconnected":
                self._show_text("已断开，正在重连…")
            elif channel.status == "auth":
                # 第 104 条（B10）：显示具体原因（密码错/撞名/取消/通信失败），
                # 而不是统一的"需要登录"——auth_msg 原来只写不读，用户无从判断。
                self._show_text(channel.auth_msg or "需要登录")
            else:
                self._show_text("连接中…")

    def _refresh_overlay(self):
        """切换频道后立即刷新悬浮窗（不等待下一个 poll 周期）。"""
        if not self.channels:
            return
        # 切换后强制重绘一次，避免沿用上一频道的帧缓存判断
        self._last_channel_idx = -1
        self._last_frame_serial = -1
        self._last_channel_status = None
        self._poll_idle_ms = 15  # A4 §3.2：切源后恢复灵敏轮询
        channel = self.channels[self.active_idx]
        frame = None
        serial = 0
        with channel.lock:
            serial = channel.frame_serial
            if channel.last_frame is not None:
                frame = channel.last_frame   # 第 105 条（B5）：只取引用，无需拷贝
        if frame is not None:
            self._show_frame(frame, serial)
        elif channel.status == "disconnected":
            self._show_text("已断开，正在重连…")
        elif channel.status == "auth":
            self._show_text(channel.auth_msg or "需要登录")
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
            p95 = ch.latency_percentiles()[1]
            if p95 > 0:
                lat_txt = "%s·P95 %dms" % (lat_txt, round(p95))
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
            frame_h, frame_w = frame_bgr.shape[:2]
            if frame_w <= 0 or frame_h <= 0:
                return
            new_h = max(1, int(round(frame_h * self.display_width / float(frame_w))))
            # 阶段 A1（优化方案 2026-09-17）：先缩放后转换。旧链对全分辨率帧做
            # cvtColor 再交给 PIL resize，转换输出的 99.9% 随即被丢弃——实测 1080p→854px
            # 每帧 10.9ms → 1.5ms。INTER_AREA 自带下采样抗锯齿，画质不低于原 PIL.BILINEAR。
            small = cv2.resize(frame_bgr, (self.display_width, new_h),
                               interpolation=cv2.INTER_AREA)
            rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            photo = ImageTk.PhotoImage(Image.fromarray(rgb))
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
        except Exception as e:
            # ui-012（2026-09-18）：首帧全量 traceback、之后每 10 秒汇总一条——旧实现
            # 每帧打整段 traceback 且主线程同步写盘，绘制持续失败时 UI 卡 + 日志爆量
            now_f = time.monotonic()
            self._draw_fails += 1
            if self._draw_fails == 1:
                log.warning("画面绘制失败，跳过该帧: %s", e, exc_info=True)
                self._draw_fail_warn_at = now_f
            elif now_f - self._draw_fail_warn_at >= 10.0:
                log.warning("画面绘制持续失败（近 10 秒 %d 次），最近错误: %s",
                            self._draw_fails - 1, e)
                self._draw_fail_warn_at = now_f
                self._draw_fails = 1

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
        # 第 102 条：必须用 "" 清空 -image。`configure(image=None, ...)` 里 None 会被
        # tkinter 丢弃（只发 -text），而 Label 的 compound 默认是 "none"——只要还挂着
        # 图像就只显示图像、不显示文字。于是断线/切源后用户看到的是**上一帧静止画面**
        # 而不是"已断开，正在重连…"（实测：文字布局宽度 481 vs 图片布局 54）。
        self.label.configure(image="", text=text)
        self._photo = None  # label 已脱离图像，可安全释放 Tk 侧对象
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
        """设置/清除悬浮窗扩展样式，兼顾可用性与反作弊误报特征。

        - WS_EX_TRANSPARENT：鼠标穿透（仅穿透开启时）
        - WS_EX_LAYERED：穿透或半透明所需
        - WS_EX_NOACTIVATE：穿透时点击不抢游戏焦点（Discord/OBS 悬浮层同款）
        - WS_EX_TOOLWINDOW：穿透时不进 Alt+Tab / 任务栏，避免被当成前台应用窗口

        WS_EX_LAYERED 不能与 Tk 的 -alpha 互相踩踏：只要开启穿透或 alpha<1.0
        就必须保持 LAYERED，并在每次切换后重新写入 alpha，否则分层窗口会失去
        透明属性（关掉穿透后变全不透明）或在 alpha==1.0 时根本不渲染。

        第 C10 条：hwnd 失效/样式写失败只记日志不抛——从 _build_ui 抛出会启动即
        弹框退出；从热键抛出会打断 toggle 且 click_through 标志与 win32 实态不一致。
        """
        hwnd = getattr(self, "hwnd", None)
        if not hwnd:
            return
        try:
            try:
                alpha = float(getattr(self, "alpha", 1.0))
            except (TypeError, ValueError):
                alpha = 1.0
            alpha = max(0.0, min(1.0, alpha))
            need_layered = bool(self.click_through) or alpha < 1.0
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if self.click_through:
                ex_style |= win32con.WS_EX_TRANSPARENT
                ex_style |= win32con.WS_EX_NOACTIVATE
                ex_style |= win32con.WS_EX_TOOLWINDOW
            else:
                ex_style &= ~win32con.WS_EX_TRANSPARENT
                ex_style &= ~win32con.WS_EX_NOACTIVATE
                ex_style &= ~win32con.WS_EX_TOOLWINDOW
            if need_layered:
                ex_style |= win32con.WS_EX_LAYERED
            else:
                ex_style &= ~win32con.WS_EX_LAYERED
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)
            if need_layered:
                try:
                    win32gui.SetLayeredWindowAttributes(
                        hwnd, 0, int(round(alpha * 255)), win32con.LWA_ALPHA,
                    )
                except Exception:
                    log.debug("SetLayeredWindowAttributes 失败", exc_info=True)
            win32gui.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                | win32con.SWP_NOZORDER | win32con.SWP_FRAMECHANGED,
            )
        except Exception:
            log.warning("应用鼠标穿透样式失败（hwnd 可能已失效）", exc_info=True)

    # ---------- 全局热键（Ctrl+Alt+X 穿透 / Ctrl+Alt+←→ 切频道 / Ctrl+Alt+↑↓ 切画面源） ----------

    def start_hotkey(self):
        threading.Thread(
            target=self._hotkey_loop, name="viewer-hotkey", daemon=True
        ).start()

    def _stop_hotkeys(self):
        """退出时唤醒热键线程（GetMessageW 阻塞中），使其走到 UnregisterHotKey。

        第 C8 条：原先 quit() 只置 running=False，热键线程卡在 GetMessageW，
        finally 里的 UnregisterHotKey 基本执行不到；若进程未及时退出会残留占用
        Ctrl+Alt+X / Alt+1..9。用 PostThreadMessageW(WM_QUIT) 打断消息等待。
        """
        tid = getattr(self, "_hotkey_tid", None)
        if tid:
            try:
                ctypes.windll.user32.PostThreadMessageW(int(tid), WM_QUIT, 0, 0)
            except Exception:
                log.debug("PostThreadMessageW(WM_QUIT) 失败", exc_info=True)
        else:
            # ui-015（2026-09-18）：_hotkey_tid 在线程注册阶段结束才回填，启动期退出
            # 时它为 None。旧实现在这里用 UnregisterHotKey(None, hid) "尽力注销"——
            # 热键是**线程私有**的，主线程调用（hWnd=None）只会注销本线程自己的热键，
            # 是空操作（注释与实现漂移）。真正有效的路径是 quit() 置 running=False 后
            # 注册线程在 finally 里自行注销；这里只留一条日志便于排查。
            log.debug("退出时热键线程尚未回填 tid，注销交由注册线程 finally 完成（已注册 %d 个）",
                      len(getattr(self, "_hotkey_registered", ()) or ()))

    def _notify_hotkey_result(self, registered, failed):
        """主线程：把热键注册结果反映到面板提示与状态栏。

        此前只写 WARNING 日志——Ctrl+Alt+←/→/X 在 Windows 上极易被显卡驱动/
        系统快捷键占用（ERROR_HOTKEY_ALREADY_REGISTERED），用户看不到原因，
        只觉得「快捷键坏了」。
        """
        self._hotkey_registered = set(registered)
        self._hotkey_failed = list(failed)
        self._update_hotkey_hint()
        if not registered:
            msg = "全局热键全部注册失败（被其他程序占用），请用面板按钮操作"
            if failed:
                msg += "：" + "、".join(failed)
            if self.panel is not None:
                try:
                    self._panel_set_status(msg, error=True)
                except Exception:
                    pass
        elif failed:
            if self.panel is not None:
                try:
                    self._panel_set_status(
                        "部分热键被占用失效：" + "、".join(failed), error=True)
                except Exception:
                    pass

    def _update_hotkey_hint(self):
        """按实际注册成功的热键刷新面板副标题，避免提示与现实不符。"""
        label = getattr(self, "_hint_label", None)
        if label is None:
            return
        ok = getattr(self, "_hotkey_registered", set()) or set()
        parts = []
        if HOTKEY_ID_PREV in ok and HOTKEY_ID_NEXT in ok:
            parts.append("Ctrl+Alt+←/→ 换频道")
        if HOTKEY_ID_SRC_PREV in ok and HOTKEY_ID_SRC_NEXT in ok:
            parts.append("Ctrl+Alt+↑/↓ 换画面源")
        if any(HOTKEY_ID_DIRECT_BASE <= h < HOTKEY_ID_DIRECT_BASE + 9 for h in ok):
            parts.append("Alt+1..9 直达")
        if HOTKEY_ID_TOGGLE in ok:
            parts.append("Ctrl+Alt+X 穿透")
        text = "   ".join(parts) if parts else "全局热键均被占用，请用面板操作"
        try:
            label.configure(text=text)
        except Exception:
            pass

    def _hotkey_loop(self):
        user32 = ctypes.windll.user32
        registered = []
        # B4：注册与配置读取整体纳入 try/finally——原实现中 `cfg["viewer"].get("hotkeys",{})`
        # 的 AttributeError 发生在 try/finally 之前，热键线程直接死亡，刚注册成功的
        # Ctrl+Alt+X / Ctrl+Alt+←→ 永不注销（全局热键泄漏到进程退出）。
        try:
            try:
                self._hotkey_tid = int(threading.get_native_id())
            except Exception:
                self._hotkey_tid = None
            failed = []
            mods = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
            for hid, vk in HOTKEY_DEFS:
                if user32.RegisterHotKey(None, hid, mods, vk):
                    registered.append(hid)
                else:
                    failed.append(HOTKEY_NAMES.get(hid, "热键ID %d" % hid))
            direct_enabled = bool(_viewer_hotkeys_cfg(self.cfg).get("direct", True))
            if direct_enabled:
                for i in range(9):
                    hid = HOTKEY_ID_DIRECT_BASE + i
                    if user32.RegisterHotKey(None, hid, MOD_ALT | MOD_NOREPEAT, VK_1 + i):
                        registered.append(hid)
                    else:
                        failed.append("Alt+%d 直达频道" % (i + 1))
            self._hotkey_registered = set(registered)
            self._hotkey_failed = list(failed)
            if not registered:
                log.warning("全局热键全部注册失败（可能已被其他程序占用）：%s",
                            "、".join(failed) if failed else "未知")
                self._post_ui(lambda: self._notify_hotkey_result([], list(failed)))
                return
            if failed:
                log.warning("部分全局热键注册失败（可能已被其他程序占用）：%s", "、".join(failed))
            else:
                log.info("全局热键注册成功 %d 个", len(registered))
            # 结果上屏：若此刻 mainloop 尚未 ready，回调会被 _post_ui 丢弃——
            # poll 启动后由 _sync_hotkey_notice 补发（见 poll）。
            self._post_ui(lambda: self._notify_hotkey_result(
                list(registered), list(failed)))
            msg = ctypes.wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:  # WM_QUIT 或错误
                    break
                if msg.message != WM_HOTKEY:
                    continue
                if not self.running:
                    break
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
                    log.debug("热键回调入队失败", exc_info=True)
        except Exception:
            log.warning("热键线程异常（已注册热键将全部注销）", exc_info=True)
        finally:
            for hid in registered:
                try:
                    user32.UnregisterHotKey(None, hid)
                except Exception:
                    pass
            self._hotkey_registered = set()
            self._hotkey_tid = None
            log.debug("热键已注销 %d 个", len(registered))

    def _step_channel(self, delta):
        """按步长切换频道（-1 上一个 / +1 下一个），到头回绕（与切源一致）。

        旧实现越界 clamp：停在首/末频道时按 ←/→ 看起来「热键失灵」。
        """
        if not self.channels:
            return
        n = len(self.channels)
        cur = self.active_idx if 0 <= self.active_idx < n else 0
        self.switch_channel((cur + delta) % n)

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
    # 第 100 条 + 第 136 条（C3）：主线程/工作线程两份兜底合并进 ui_common，
    # get_root 保留"以 Tk 根窗口为父弹窗"的原语义。
    ui_common.install_excepthooks(APP_NAME, get_root=lambda: _TK_ROOT)
    parser = argparse.ArgumentParser(description="FPS 画面观看端（TCP 客户端 + 置顶悬浮窗）")
    parser.add_argument(
        "--addr", help="服务端地址 host:port，本次运行作为首个频道使用（无频道时覆盖 viewer.server_addr）"
    )
    args = parser.parse_args()

    cfg = load_config()
    logger.setup_logger(cfg)
    addr_override = None
    if args.addr:
        # 第 100 条：server_addr 仅在频道列表为空时被读取，已有频道时旧实现把参数
        # 静默忽略（却写盘并打印"已使用"）。改为：始终作为临时频道生效；只有确实
        # 没有频道时才写回 server_addr（保持向后兼容的持久化语义）。
        addr_override = args.addr
        if not (cfg["viewer"].get("channels") or []):
            cfg["viewer"]["server_addr"] = args.addr
            save_config(cfg, "viewer")
        log.info("已使用命令行地址 %s（作为首个频道，不写入配置）", args.addr)

    enable_dpi_awareness()

    # A3（第 133 条）：先上屏纯 tkinter 极简 splash，再加载 cv2/numpy/PIL/share——
    # 双击到 splash 可见的间隔只剩 Python/Tk 启动本身。splash 失败时静默降级为
    # 旧行为（root 由 run() 自建）。
    splash_root = None
    try:
        splash_root = tk.Tk()
        splash.show_fast_splash(splash_root)
    except Exception:
        log.debug("极简 splash 创建失败，降级为常规启动", exc_info=True)
        if splash_root is not None:
            try:
                splash_root.destroy()
            except Exception:
                pass
        splash_root = None
    _load_heavy()

    app = ViewerApp(cfg, addr_override=addr_override)
    try:
        app.run(root=splash_root)
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
