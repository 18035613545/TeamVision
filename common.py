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

#: 单条消息 payload 长度上限（64MB，含帧 JPEG 与控制消息，防恶意长度头）
MAX_PAYLOAD = 64 * 1024 * 1024

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
            "auth_timeout": 60
        }
    },
    "viewer": {
        "server_addr": "127.0.0.1:5700",
        "display_width": 480,
        "alpha": 0.9,
        "click_through": True,
        "channels": [],
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
    return 5 + length, kind, buf[5:5 + length]


def recv_msg(sock):
    """接收一条控制消息并解析为 dict；连接关闭/数据不完整/收到帧时抛异常。"""
    header = recv_exact(sock, 5)
    kind = header[0]
    if kind != MSG_CTRL:
        raise ConnectionError("预期控制消息，实际收到消息类型 %d" % kind)
    (length,) = struct.unpack(">I", header[1:5])
    if length <= 0 or length > MAX_PAYLOAD:
        raise ConnectionError("非法的控制消息长度: %d" % length)
    data = recv_exact(sock, length)
    return json.loads(data.decode("utf-8"))


def recv_frame(sock):
    """接收一帧消息，返回 (时间戳微秒, JPEG 字节)；收到控制消息时抛异常。"""
    header = recv_exact(sock, 5)
    kind = header[0]
    if kind != MSG_FRAME:
        raise ConnectionError("预期画面帧，实际收到消息类型 %d" % kind)
    (length,) = struct.unpack(">I", header[1:5])
    if length < 8 or length > MAX_PAYLOAD:
        raise ConnectionError("非法的帧长度: %d" % length)
    data = recv_exact(sock, length)
    (ts,) = struct.unpack(">Q", data[:8])
    return ts, data[8:]


def recv_message(sock):
    """接收任意消息，返回 ("frame", ts, jpeg) / ("video", payload, None) / ("ctrl", obj, None)。

    供复用流逐条解析三种消息类型；video payload 为原始视频帧 payload（含 ts/flags/NAL），
    调用方用 parse_video 解包。
    """
    header = recv_exact(sock, 5)
    kind = header[0]
    (length,) = struct.unpack(">I", header[1:5])
    if kind not in (MSG_FRAME, MSG_CTRL, MSG_VIDEO):
        raise ConnectionError("非法的消息类型: %d" % kind)
    if length <= 0 or length > MAX_PAYLOAD:
        raise ConnectionError("非法的消息长度: %d" % length)
    payload = recv_exact(sock, length)
    if kind == MSG_FRAME:
        if length < 8:
            raise ConnectionError("帧 payload 不足 8 字节")
        (ts,) = struct.unpack(">Q", payload[:8])
        return "frame", ts, payload[8:]
    if kind == MSG_VIDEO:
        if length < 10:
            raise ConnectionError("视频帧 payload 不足 10 字节")
        return "video", payload, None
    return "ctrl", json.loads(payload.decode("utf-8")), None


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
            for optname, default in (("TCP_KEEPIDLE", None), ("TCP_KEEPINTVL", None),
                                     ("TCP_KEEPCNT", None)):
                if hasattr(socket, optname):
                    try:
                        sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, optname), default or keepalive_idle)
                    except OSError:
                        pass


def recv_exact(sock, n):
    """从套接字循环接收恰好 n 字节；连接提前关闭时抛出 ConnectionError。"""
    chunks = []
    remaining = n
    while remaining > 0:
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


def exe_dir():
    """返回程序资源所在目录：PyInstaller 冻结运行时为可执行文件所在目录，否则为本文件所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _config_path():
    """返回 config.json 的完整路径（与程序资源所在目录同目录）。"""
    return os.path.join(exe_dir(), "config.json")


def _deep_merge(base, override):
    """递归深合并两个字典：override 覆盖 base，base 中缺失的键自动补上。"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config():
    """加载配置：config.json 不存在时先写入 DEFAULT_CONFIG 再返回其深拷贝；
    存在时读取并与 DEFAULT_CONFIG 做递归深合并后返回合并结果。"""
    path = _config_path()
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        return copy.deepcopy(DEFAULT_CONFIG)
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return _deep_merge(DEFAULT_CONFIG, cfg)


def save_config(cfg):
    """以 ensure_ascii=False、indent=2 原子写回 config.json（先写临时文件再替换）。"""
    path = _config_path()
    tmp = path + ".tmp"
    with _config_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
