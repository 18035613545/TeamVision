# -*- coding: utf-8 -*-
"""屏幕共享程序的公共协议与配置模块。

本模块被 host.py 和 viewer.py 导入，提供统一的网络协议（v2：类型标记帧/控制消息）、
握手流程、socket 调优、地址解析以及配置文件（config.json）的读写功能。

协议 v2 消息格式（所有消息共享一条 TCP 流）：
    [1 字节 kind][4 字节大端长度][payload]
    kind 0x01 = JPEG 帧：payload = [8 字节微秒采集时间戳 ">Q"][JPEG]（长度 = 8 + len(JPEG)）
    kind 0x02 = 控制消息：payload = UTF-8 JSON（{"action": ...}）
    kind 0x03 = 视频帧：payload = [8 字节微秒采集时间戳 ">Q"][1 字节 codec][1 字节 flags][Annex-B NAL]
                 codec 0x01 = H.264 / 0x02 = HEVC；flags bit0 = 关键帧（含 SPS/PPS/IDR，解码起点）
kind 标记让接收端无需猜测即可区分帧与控制消息（ping/pong 等），并支持
“只解码缓冲区里最新一帧”的低延迟接收策略。

观看组（v2.1，MultiView）：新增 CTRL action cap_probe/cap/watch/peers——
上行帧视为共享声明，下行帧按 watch 源订阅路由；全部向后兼容（未知 action 忽略）。
"""

import copy
import json
import os
import socket
import struct
import sys
import threading
import time

#: 客户端握手魔数（版本 2：kind 标记帧/控制消息；FS01 旧客户端握手将失败并给出明确告警）
MAGIC_CLIENT = b"FS02"
#: 服务端握手魔数
MAGIC_SERVER = b"OK02"

#: 消息 kind：JPEG 帧 / 控制消息 / H.264 视频帧
MSG_FRAME = 0x01
MSG_CTRL = 0x02
MSG_VIDEO = 0x03

#: 视频帧编码类型（视频帧 payload 第 8 字节，供观看端选择解码器，支持流中切换）
CODEC_H264 = 0x01  # H.264 / AVC
CODEC_HEVC = 0x02  # H.265 / HEVC（带宽较 H.264 再降 30-50%）

#: 视频帧 flags：bit0 = 关键帧（含 SPS/PPS/IDR，可作为解码起点）
VIDEO_FLAG_KEY = 0x01

#: 品牌与应用信息
APP_NAME = "队友视野"
APP_VERSION = "1.3.0"
APP_COPYRIGHT = "Copyright © 2026 SakuraVision Team"

#: 配置读写全局锁（防止多线程同时 save_config 导致丢失更新）
_config_lock = threading.Lock()

#: 单条消息 payload 长度上限（32MB，含帧 JPEG 与控制消息，防恶意长度头）。
#: 第 57 条：原 64MB 远高于任何合法帧——8K/33MP 满熵 JPEG 约 8~15MB，H.264/HEVC
#: 关键帧 <1MB，控制 JSON <1MB。降到 32MB 仍留足余量，同时把每连接最坏缓冲内存
#: 直接砍半（解析在攒满声明长度前不会返回，长度头越大占用越久）。
MAX_PAYLOAD = 32 * 1024 * 1024

#: 默认配置
DEFAULT_CONFIG = {
    "logs": {
        "level": "info"
    },
    "host": {
        "listen_host": "0.0.0.0",
        "port": 5700,
        "fps": 30,
        "jpeg_quality": 80,
        "scale": 1.0,
        "capture": {
            "monitor": 1,
            "region": None,  # null=全屏；设置区域后改为局部采集
            "backend": "dxgi"  # dxgi（dxcam，低延迟 GPU 采集，不可用自动回退 mss）
        },
        "net": {
            "sndbuf_kb": 1024,   # 发送缓冲（KB）：大帧一次发完，减少分包停顿
            "rcvbuf_kb": 2048,   # 接收缓冲（KB）
            "keepalive": True,
            "keepalive_idle_s": 60,  # 客户端无消息超过该时长视为半开连接并移除
        },
        "perf": {
            "adaptive": True,
            "quality_min": 40,
            "scale_min": 0.25,  # 拥塞时最低缩放（质量→缩放→帧率三级降级，允许更低分辨率保连通）
            "fps_min": 10,      # 拥塞时最低帧率
            # 静止检测：画面无有效变化时暂停编码/发送（省流 + 降 CPU）
            "still": {
                # 默认关闭：判据是整屏变点面积比，小窗视频/按钮/文字这类小的锐利
                # 变化会被判为“无变化”，连续 0.6s 后帧率被压到 probe_fps
                "enabled": False,  # 总开关；true 后恢复停发省流行为
                "probe_fps": 5,    # 静止期探测帧率（变化恢复延迟 ≤ 1/probe_fps + 一帧）
                "still_frames": 3, # 连续 N 帧静止才进入静止态（迟滞，防抖动误入）
                "point_thr": 10,   # 抽稀采样点每通道平均绝对差阈值（0-255），超过视为变点
                "ratio_thr": 0.005 # 变点占全部采样点比例阈值（0-1），超过判定有变化
            }
        },
        "codec": {
            "encoder": "auto",       # auto|nvenc|x264|jpeg；auto=NVENC 硬件优先，x264 回退
            "bitrate_kbps": 2500,    # H.264 目标码率基线（CBR，Kbps）
            "keyint": 60,            # 关键帧间隔（帧数）
            "min_bitrate_kbps": 400,  # 码率自适应下限
            "max_bitrate_kbps": 6000,  # 码率自适应上限
            "target_width": 854,     # 传输分辨率档位（输出宽度上限，0=关闭仅按 scale）
            "preset": ""             # 空=编码器默认低延迟预设；nvenc: p1..p7，x264: veryfast 等
        },
        "frp": {
            "auto_start": False,
            "frpc_path": "frpc.exe",
            "token": "",
            "tunnel_ids": "",
            "public_addr": ""
        },
        "auth": {
            "enabled": False,  # 默认关闭准入（向后兼容）：旧客户端/无配置环境可直接观看
            # 认证阶段「总时限」（第 23 条防 slowloris）；须大于观看端登录框等待（120s），
            # 否则用户还在输入 host 就先断开、明明输对却登录失败（第 31 条）。
            "auth_timeout": 150
        }
    },
    "viewer": {
        "server_addr": "127.0.0.1:5700",
        "display_width": 480,
        "alpha": 0.9,
        "click_through": True,
        "channels": [],
        "wizard_done": False,
        "panel_topmost": True,
        "hotkeys": {
            "direct": True
        },
        "net": {
            "rcvbuf_kb": 4096,  # 接收缓冲（KB）：缓解突发帧积压
            "read_idle_s": 5.0  # 超过该秒数未收到任何数据判定连接停滞
        },
        "ping_interval_s": 2.0,  # 应用层心跳/测 RTT 间隔
        "reconnect": {
            "base_s": 1.0,
            "max_s": 30.0,
            "factor": 2.0    # 指数退避：1 → 2 → 4 → …（成功收帧后重置）
        }
    }
}


def pack_frame(jpeg_bytes, ts=None):
    """将 JPEG 数据打包为一帧消息，返回 [kind=1][4 字节大端长度][8 字节微秒时间戳 ">Q"][JPEG]。

    长度字段 = 8 + len(jpeg_bytes)；ts 为 None 时取当前时刻微秒（int(time.time() * 1_000_000)）。
    """
    if ts is None:
        ts = int(time.time() * 1_000_000)
    return bytes((MSG_FRAME,)) + struct.pack(">I", 8 + len(jpeg_bytes)) + struct.pack(">Q", ts) + jpeg_bytes


def pack_video(video_bytes, ts=None, keyframe=False, codec=CODEC_H264):
    """将视频帧（Annex-B NAL）打包为视频帧消息。

    返回 [kind=3][4 字节大端长度][8 字节微秒采集时间戳 ">Q"][1 字节 codec][1 字节 flags][NAL]。
    codec 指示 H.264/HEVC，flags bit0 = 关键帧标记；长度字段 = 10 + len(video_bytes)。
    """
    if ts is None:
        ts = int(time.time() * 1_000_000)
    flags = VIDEO_FLAG_KEY if keyframe else 0
    return (bytes((MSG_VIDEO,)) + struct.pack(">I", 10 + len(video_bytes))
            + struct.pack(">Q", ts) + bytes((codec,)) + bytes((flags,)) + video_bytes)


def parse_video(payload):
    """从视频帧 payload 解包出 (时间戳微秒, codec, flags, Annex-B NAL 字节)。"""
    if len(payload) < 10:
        raise ValueError("视频帧 payload 不足 10 字节")
    (ts,) = struct.unpack(">Q", payload[:8])
    return ts, payload[8], payload[9], payload[10:]


def unpack_frame(data):
    """从帧 payload 中解包出 (时间戳微秒, JPEG 字节) 元组；数据不完整或损坏时抛出 ValueError。

    注意：v2 使用包类型标记后不再需要手工解帧头，这里保留用于解析帧 payload（较 verify 脚本使用）。
    """
    if len(data) < 8:
        raise ValueError("帧 payload 不足 8 字节，无法解析时间戳")
    (ts,) = struct.unpack(">Q", data[:8])
    return ts, data[8:]


def pack_msg(obj):
    """把 dict 序列化为 [kind=2][4 字节大端长度][UTF-8 JSON]，用于 auth/ping/pong 等控制消息。"""
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return bytes((MSG_CTRL,)) + struct.pack(">I", len(data)) + data


def send_msg(sock, obj):
    """发送一条控制消息（JSON dict）。"""
    sock.sendall(pack_msg(obj))


def parse_message(buf):
    """从接收缓冲区解析出最早的一条完整协议消息。

    返回 (consumed, kind, payload)：
      - 缓冲区含完整消息时返回首条消息的消费字节数、kind 与 payload；
      - 不足一条（等待更多数据）时返回 (0, None, None)；
      - 校验失败（非法 kind/长度）时抛出 ValueError。
    """
    if len(buf) < 5:
        return 0, None, None
    kind = buf[0]
    (length,) = struct.unpack(">I", buf[1:5])
    if kind not in (MSG_FRAME, MSG_CTRL, MSG_VIDEO):
        raise ValueError("非法的消息类型: %d" % kind)
    if length <= 0 or length > MAX_PAYLOAD:
        raise ValueError("非法消息长度: %d" % length)
    if len(buf) < 5 + length:
        return 0, None, None
    # 第 57 条：rx_buf 改用 bytearray 后切片会是 bytearray，而 pack_video/pack_frame
    # 做 bytes(...) + payload，bytes + bytearray 抛 TypeError。统一转 bytes，保证
    # 中继路径（host 转发上行帧）与缓冲区类型无关。
    return 5 + length, kind, bytes(buf[5:5 + length])


def recv_msg(sock, deadline=None):
    """接收一条控制消息并解析为 dict；连接关闭/数据不完整/收到帧时抛异常。

    deadline 给定时，整条消息（头 + 体）的接收受总时限约束（第 23 条防 slowloris）。
    """
    header = recv_exact(sock, 5, deadline)
    kind = header[0]
    if kind != MSG_CTRL:
        raise ConnectionError("预期控制消息，实际收到消息类型 %d" % kind)
    (length,) = struct.unpack(">I", header[1:5])
    if length <= 0 or length > MAX_PAYLOAD:
        raise ConnectionError("非法的控制消息长度: %d" % length)
    data = recv_exact(sock, length, deadline)
    return json.loads(data.decode("utf-8"))


def tune_socket(sock, sndbuf=None, rcvbuf=None, keepalive=True, keepalive_idle=60):
    """低延迟网络调优：禁用 Nagle（TCP_NODELAY）+ 加大收发缓冲 + TCP keepalive。

    所有 setsockopt 都单独 try/except：个别平台不支持特定选项时静默忽略，
    保证在任何环境都能工作。
    """
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    if sndbuf:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, int(sndbuf))
        except OSError:
            pass
    if rcvbuf:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, int(rcvbuf))
        except OSError:
            pass
    if keepalive:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            keepalive_idle = 0
        if keepalive_idle > 0:
            # 第 59 条：三个选项语义不同，不能全取 keepalive_idle。
            # KEEPIDLE=首次探测前空闲秒数；KEEPINTVL=探测间隔；KEEPCNT=探测次数。
            # 原 `default or keepalive_idle`（default 恒 None）使 INTVL=CNT=60，
            # 需约 1 小时才判定半开连接。改为 idle + 3 次 × ~10 秒间隔 ≈ 90 秒内判定。
            intvl = max(1, int(keepalive_idle) // 6)
            cnt = 3
            for optname, value in (("TCP_KEEPIDLE", int(keepalive_idle)),
                                   ("TCP_KEEPINTVL", intvl),
                                   ("TCP_KEEPCNT", cnt)):
                if hasattr(socket, optname):
                    try:
                        sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, optname), value)
                    except OSError:
                        pass


def recv_exact(sock, n, deadline=None):
    """从套接字循环接收恰好 n 字节；连接提前关闭时抛出 ConnectionError。

    deadline（time.monotonic() 时刻）给定时，强制整次接收的总墙钟上限：每次 recv 前
    把超时收紧到剩余时间，剩余耗尽抛 socket.timeout。用于认证阶段防 slowloris——
    否则攻击者声称一个超长 payload 后每 (timeout-1) 秒喂 1 字节，单次 recv_exact 即可
    无限期占用线程（每次 recv 都在 per-recv 超时内返回，永不触发超时）。
    """
    chunks = []
    remaining = n
    while remaining > 0:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout("接收总时限已耗尽")
            sock.settimeout(left)
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("连接提前关闭，收到的数据不完整")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def client_handshake(sock):
    """客户端握手：发送 MAGIC_CLIENT，并校验服务端回复为 MAGIC_SERVER。"""
    sock.sendall(MAGIC_CLIENT)
    response = recv_exact(sock, len(MAGIC_SERVER))
    if response != MAGIC_SERVER:
        raise ConnectionError("服务端握手响应无效：%r" % response)


def server_handshake(sock):
    """服务端握手：校验客户端发来的 MAGIC_CLIENT，校验通过后回复 MAGIC_SERVER。"""
    magic = recv_exact(sock, len(MAGIC_CLIENT))
    if magic != MAGIC_CLIENT:
        raise ConnectionError("客户端握手请求无效：%r" % magic)
    sock.sendall(MAGIC_SERVER)


def parse_addr(addr_str, default_port=5700):
    """解析 "host:port" 地址字符串为 (host, port) 元组；端口缺省时使用 default_port。

    端口非数字/越界时抛出 ValueError（带明确提示），避免空端口等输入静默陷入重连循环。
    """
    addr_str = addr_str.strip()
    if not addr_str:
        raise ValueError("地址不能为空")
    if ":" in addr_str:
        host, port_str = addr_str.rsplit(":", 1)
        host = host.strip()
        port_str = port_str.strip()
        if not port_str:
            raise ValueError("地址缺少端口号: %s" % addr_str)
        try:
            port = int(port_str)
        except ValueError:
            raise ValueError("端口无效: %s" % port_str)
        if not (1 <= port <= 65535):
            raise ValueError("端口超出范围 1~65535: %d" % port)
        return host, port
    return addr_str, default_port


def enable_dpi_awareness():
    """声明进程 DPI 感知，避免高 DPI 下 DWM 位图拉伸导致画面发虚。

    按优先级尝试三种 API（旧系统缺失某一项时自动降级），非 Windows 直接返回。
    必须在创建任何窗口之前调用一次。返回是否成功声明，绝不抛异常。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
    except Exception:
        return False
    # 1) Per-Monitor V2：SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4)
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True
    except Exception:
        pass
    # 2) Per-Monitor：shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE = 2) == S_OK(0)
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return True
    except Exception:
        pass
    # 3) 传统系统级：user32.SetProcessDPIAware()
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return True
    except Exception:
        pass
    return False


def exe_dir():
    """返回程序资源所在目录：PyInstaller 冻结运行时为可执行文件所在目录，否则为本文件所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _config_path():
    """返回 config.json 的完整路径（与程序资源所在目录同目录）。"""
    return os.path.join(exe_dir(), "config.json")


def _deep_merge(base, override):
    """递归深合并两个字典：override 覆盖 base，base 中缺失的键自动补上。

    第 76 条：override 中显式为 None 的值视为「未指定，沿用默认」而跳过，不覆盖 base。
    否则 config.json 里 `"fps": null` 会让 `host.get("fps", 60)` 返回 None（键存在、
    值为 null，.get 不取默认）→ `slot["fps"]=None` → 采集线程 `1.0/fps_now` 抛异常；
    `"backend": null` 同理会构造 `CaptureManager(None, ...)`。唯一以 null 为合法值的
    键是 host.capture.region（默认即 None=全屏），保留 base 的 None 与 null 覆盖等价，
    第 32 条 region=None「清空回全屏」语义不变。
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if value is None:
            continue
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config():
    """加载配置：config.json 不存在时先写入 DEFAULT_CONFIG 再返回其深拷贝；
    存在时读取并与 DEFAULT_CONFIG 做递归深合并后返回合并结果。

    第 16 条：文件损坏（非法 JSON / 顶层非字典 / 合并异常）时不再抛出原始
    traceback 让程序起不来，而是把损坏文件重命名留存、回退默认配置并重新落盘。
    """
    path = _config_path()
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("config.json 顶层不是对象（实际 %s）" % type(cfg).__name__)
        return _deep_merge(DEFAULT_CONFIG, cfg)
    except Exception as e:
        _recover_corrupt_config(path, e)
        return copy.deepcopy(DEFAULT_CONFIG)


def _recover_corrupt_config(path, exc):
    """第 16 条：损坏的 config.json 重命名留存 + 回写默认配置 + 尽力告警。

    重命名为 config.json.corrupt-<时间戳> 便于用户手动找回原设置；随后写一份干净
    的默认配置，保证下次启动可用。日志用惰性 import（common 被 logger 反向依赖），
    任何失败都静默吞掉——恢复路径本身绝不能再抛异常。
    """
    backup = "%s.corrupt-%d" % (path, int(time.time()))
    try:
        os.replace(path, backup)
    except OSError:
        backup = None
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    try:
        import logger
        suffix = "，原文件已留存为 %s" % os.path.basename(backup) if backup else ""
        logger.get_logger().warning("config.json 损坏（%s），已回退默认配置%s", exc, suffix)
    except Exception:
        pass


def save_config(cfg, section=None):
    """以 ensure_ascii=False、indent=2 原子写回 config.json（先写临时文件再替换）。

    第 17 条：host 与 viewer 同目录同时运行，各持完整配置快照并整文件回写，会用
    陈旧快照静默还原对方刚改的设置（用户感知“设置老是自己消失”）。传入 section
    （如 "host"/"viewer"）时改为读-改-写：重新读取磁盘上的最新配置，只把本进程拥有
    的 section 覆盖上去再落盘，从而保留另一进程的 section。section=None 时维持原
    整文件回写行为。
    """
    path = _config_path()
    tmp = path + ".tmp"
    with _config_lock:
        out = cfg
        if section is not None:
            base = {}
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        base = json.load(f)
                    if not isinstance(base, dict):
                        base = {}
                except Exception:
                    base = {}
            base[section] = cfg.get(section)
            out = base
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
