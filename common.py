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

import base64
import copy
import hashlib
import hmac
import json
import math
import os
import select
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
from typing import Any

_msvcrt: Any = None
try:
    import msvcrt as _msvcrt  # cfg-004（2026-09-20）：跨进程字节范围锁
except ImportError:               # 非 Windows 下为 None，锁退化为"必成功"
    _msvcrt = None


def _log_warning(fmt, *args):
    """延迟导入 logger 记录告警：logger.py 反向依赖本模块（data_dir/exe_dir），
    模块级 import 会成环（common→logger→半初始化的 common）。"""
    import logger as logger_mod
    logger_mod.get_logger().warning(fmt, *args)

#: 客户端握手魔数（版本 2：kind 标记帧/控制消息；FS01 旧客户端握手将失败并给出明确告警）
MAGIC_CLIENT = b"FS02"
#: 服务端握手魔数
MAGIC_SERVER = b"OK02"

#: C2（实现方案 §7）：协议版本单一载体。v2.2 = D1 挑战-应答认证（chal/auth_proof/
#: reg_proof）上线；v2.3（2026-09-18，auth-002）= proof 绑定 user + 域分隔串。
#: v2.4（2026-10-09，net-045）= 视频帧载荷带 4 字节单调递增帧序号。
#: v2.5（2026-10-23，net-050）= 观看端→共享端的质量反馈（**控制消息**）。
#:
#: v2.5 刻意**不动视频载荷布局**——`VIDEO_SEQ_MINOR` 仍是 4，帧头仍是 14 字节。
#: 反馈走的是 CTRL 通道，两端的 action 分发对未知 action 一律静默忽略
#: （host 的 `_dispatch_ctrl` 末尾无 else；viewer 的 `_handle_ctrl` 注释"其余 action 忽略"），
#: 所以 (2,5) 观看端对 (2,4) 共享端只是反馈没人收，反之亦然，两种组合都照常工作。
#: 握手主判仍是 MAGIC（旧客户端拒绝行为不变），proto 字段随认证期 CTRL 消息携带，
#: 服务端据此判断对端能力（观看端以 auth_result.proto 的次版本号作为探测结果）。
PROTOCOL_VERSION = (2, 5)

#: 观看端要求的最低次版本：v2.2 的 proof 只签 nonce、且原实现信任对端下发的
#: KDF 参数（auth-002）；新版只与 ≥ 该版本的共享端走挑战-应答，旧版本明确拒绝
#: 并提示升级（比"发出去被静默拒绝/降级"可排查）。
PROTO_CHAL_MIN_MINOR = 3

#: net-045（2026-10-09）：对端 proto 次版本 ≥ 该值时，视频帧载荷带 seq。
#: 载荷布局无法自描述（v2.3/v2.4 都以 ts|codec|flags 开头），只能靠它协商；
#: 低于该值一律按 v2.3 的 10 字节头解，避免把 NAL 前 4 字节误当序号丢掉。
VIDEO_SEQ_MINOR = 4

#: C2 §7 第 3 步：proto 强校验开关——"先只加不启用"。当前唯一版本载体仍是
#: FS02 MAGIC；置 True 后服务端会拒绝 proto 不兼容的客户端（D1 的翻转点）。
PROTO_ENFORCE = False


def proto_compatible(msg):
    """判定认证期 CTRL 消息随带的 proto=[major,minor]。

    返回 (present, compatible)：字段缺失视为旧客户端（present=False，
    compatible=True——主判仍是 MAGIC，保持对旧客户端的既有拒绝行为）。
    主版本相同即兼容（次版本只要求客户端与服务端主版本一致）。
    """
    proto = msg.get("proto") if isinstance(msg, dict) else None
    if not isinstance(proto, list) or not proto:
        return False, True
    major = proto[0]
    if isinstance(major, bool) or not isinstance(major, int):
        return True, False
    return True, major == PROTOCOL_VERSION[0]


#: net-047（2026-10-10，产品决策"明确拒绝旧观看端"）：共享端拒绝"次版本 <
#: VIDEO_SEQ_MINOR"的观看端时统一使用的文案。放在协议层常量区，两端与测试共用
#: 一个字面量——host 发它、viewer 认它，历史上"观看端版本过旧，请更新后重试"
#: 那条（PROTO_CHAL_MIN_MINOR 门控）是本条的**子集**语义，两者刻意保持不同字面量，
#: 以便 viewer 能区分"该升级到挑战-应答版本"与"该升级到帧序号版本"。
PROTO_SEQ_REJECT_MSG = "观看端版本过旧，请更新到 1.5.0 或更高版本"


def video_seq_capable(msg):
    """判定对端是否能解 v2.4 的视频帧序号布局。

    返回 (present, capable)：
      - present=False：报文没带可用的 proto 字段。对端无法自证能力，**不据此
        拒绝**——这是刻意保留的缺口：旧版 share.py 中继路径与更早的客户端不带
        该字段，而 host 无法在认证期区分"不带字段的旧观看端"与"不带字段的旧
        中继 peer"（后者只发帧、不解码，放行才对）。它们会走到
        `parse_video_auto` 的 Annex-B 起始码回退。
      - capable=False：**显式声明**了次版本 < `VIDEO_SEQ_MINOR`。这类对端的解析
        路径没有起始码判据也没有回退，把 4 字节序号当 NAL 前导，参考链断裂后
        画面周期性冻结且两端日志正常（libx264 实测 520 帧里 28 帧解不出）。
        这是必须明确拒绝的一类。

    次版本的类型口径**有意放宽**到 int/float：宁可多拒一次畸形客户端，也不放过
    一个声明了旧次版本的对端。bool 仍排除——`isinstance(True, int)` 为真，
    但 `true` 是畸形载荷而不是"次版本 1"，放它进来等于放行一个说不清自己版本的
    对端。主版本不同不归这里管（那是 `proto_compatible` / `PROTO_ENFORCE` 的口径）。
    """
    proto = msg.get("proto") if isinstance(msg, dict) else None
    if not (isinstance(proto, (list, tuple)) and len(proto) >= 2
            and isinstance(proto[1], (int, float)) and not isinstance(proto[1], bool)):
        return False, True
    if proto[0] != PROTOCOL_VERSION[0]:
        return True, True          # 主版本不同：交给既有的版本不兼容分支
    return True, proto[1] >= VIDEO_SEQ_MINOR


class Act:
    """C2：控制消息 action 常量表——两端唯一字面量来源。

    UI 登录对话框的返回值（"login"/"register"）即协议 action，故同样用本表，
    避免同一语义在协议层与 UI 层各写一份字符串。"""

    AUTH_RESULT = "auth_result"
    REQ_KEYFRAME = "req_keyframe"
    WATCH_REJECT = "watch_reject"
    WATCH = "watch"
    UNSHARE = "unshare"
    REGISTER = "register"
    LOGIN = "login"
    PROBE = "probe"
    PING = "ping"
    PONG = "pong"
    PEERS = "peers"
    CAP = "cap"
    CAP_PROBE = "cap_probe"
    # D1（实现方案 §10）：挑战-应答认证。chal=服务端下发 nonce（+salt/iter），
    # auth_proof/reg_proof=观看端以 HMAC(dk, nonce) 应答，口令永不上网。
    CHAL = "chal"
    AUTH_PROOF = "auth_proof"
    REG_PROOF = "reg_proof"
    # net-050（v2.5）：观看端→共享端的质量反馈（解码积压/丢帧/RTT/解码耗时…）。
    # 走 CTRL 通道，**不碰视频载荷布局**——所以它不需要抬 VIDEO_SEQ_MINOR，
    # 与 (2,4) 的对端之间天然互通（对方不认识这个 action 就静默忽略）。
    FEEDBACK = "feedback"
    # net-050（v2.5）：共享端→某个共享成员的调档指令。方向与 FEEDBACK 相反：
    # 观看端把"我这边看得卡"报给共享端，共享端判定该降谁的码率后，
    # 把建议码率发给真正持有编码器的那一端（MultiView 里可能是队友的机器）。
    FB_ADJUST = "fb_adjust"


# ---------------------------------------------------------------------------
# net-050（v2.5）：质量反馈的字段口径——**两端共用的单一真源**
# ---------------------------------------------------------------------------
#
# 为什么要共用一份定义：反馈是**对端给的数**，观看端按一套区间组装、共享端按另一套
# 区间解析的话，两端看到的同一个指标会差出一截（"我报了 900ms 积压，你那边显示 600ms"），
# 排查时根本不知道该信谁。这里把"字段名 + 合法区间"钉在一处，组装与解析都走它。
#
# 字段语义：
#   fps        观看端实际显示帧率（浮点；0 = 暂无画面）
#   lat_ms     观看端观测到的端到端延迟（毫秒；优先 RTT/2，无 RTT 时回退 ts 差估算）
#   rtt_ms     应用层往返时延（毫秒；单调钟测得，不受两端墙钟偏差影响）
#   backlog_ms 解码积压量（毫秒）：相对"本链路历史最优时延"的额外排队延迟
#   drop_pct   网络丢帧率（百分比）：帧序号缺口占应收帧数的比例
#   decode_ms  单帧解码耗时（毫秒）：与 1000/fps 比较可判断"解码受限"还是"链路受限"
FEEDBACK_FIELDS = {
    "fps": (0.0, 240.0),
    "lat_ms": (0.0, 60_000.0),
    "rtt_ms": (0.0, 60_000.0),
    "backlog_ms": (0.0, 60_000.0),
    "drop_pct": (0.0, 100.0),
    "decode_ms": (0.0, 1_000.0),
}

#: 判定"这一端看得卡"的阈值（共享端侧用，观看端 UI 也用它标红）：
#: 积压超过该值 = 观看端排队跟不上（解码受限或下行受限）；丢帧率超过该值 = 链路在丢帧。
FB_STUCK_BACKLOG_MS = 800.0
FB_STUCK_DROP_PCT = 5.0
#: 反馈有效期（秒）：超过即视为过期。观看端可能已经掉线、或它自己卡到发不出反馈——
#: 拿着一条半小时前的旧反馈去降码率，等于按幽灵观众的抱怨调整所有人的画质。
FB_MAX_AGE_S = 10.0


def parse_feedback(msg):
    """解析一条质量反馈报文；返回规范化 dict，**不可用时返回 None**（绝不抛）。

    规范化 = 只保留 `FEEDBACK_FIELDS` 里存在且合法的字段，数值一律夹到合法区间。
    缺字段不算错（对端可能只报部分指标），一个合法字段都没有才算不可用。

    为什么夹紧而不是丢弃越界值：反馈是**观感数据**，不是控制指令——一个越界的
    backlog 最多让共享端误判一次该不该降档，而丢弃它会让"对端报了个离谱值"这件事
    变成静默无感。这里把不可解析的（非数值/布尔/NaN）丢掉，把可解析的越界值夹住。
    """
    if not isinstance(msg, dict):
        return None
    out = {}
    for name, (lo, hi) in FEEDBACK_FIELDS.items():
        v = msg.get(name)
        if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv != fv:          # NaN：任何比较都假，夹紧会把它变成上界，必须单独剔掉
            continue
        out[name] = max(lo, min(hi, fv))
    if not out:
        return None
    return out


def feedback_is_stuck(fb):
    """判断一条反馈是否表示"这一端看得卡"；fb 为 None 或字段缺失时返回 False。"""
    if not isinstance(fb, dict):
        return False
    backlog = fb.get("backlog_ms")
    drop = fb.get("drop_pct")
    if isinstance(backlog, (int, float)) and backlog >= FB_STUCK_BACKLOG_MS:
        return True
    if isinstance(drop, (int, float)) and drop >= FB_STUCK_DROP_PCT:
        return True
    return False


def feedback_is_decode_bound(fb, fps=None):
    """判断"卡"的原因是**解码受限**（该削画幅/帧率）还是**链路受限**（该削码率）。

    判据：单帧解码耗时占掉了帧预算的多少。预算 = 1000/fps 毫秒；解码耗时超过
    预算的 80% 即判为解码受限——此时**降码率帮不上忙**（解码量由像素数×帧数决定，
    与码率无关），该动的是分辨率与帧率。

    fps 缺省时用反馈里自带的 fps；两者都没有则判不了，返回 False（按链路受限处理，
    与既有 ABR 口径一致：宁可少动一项，也不要在判据不足时乱改画质）。
    """
    if not isinstance(fb, dict):
        return False
    decode_ms = fb.get("decode_ms")
    if not isinstance(decode_ms, (int, float)) or decode_ms <= 0:
        return False
    rate = fps if isinstance(fps, (int, float)) and fps and fps > 0 else fb.get("fps")
    if not isinstance(rate, (int, float)) or not rate or rate <= 0:
        return False
    budget_ms = 1000.0 / float(rate)
    return decode_ms >= budget_ms * 0.8


#: D1：PBKDF2 参数单一真源（host.AccountManager 与 viewer 挑战流共用，
#: 保证既有账户（10 万次迭代派生）无需迁移即可继续登录）。
PBKDF2_ITERATIONS = 100_000

#: auth-002（2026-09-18）：KDF 参数的合法区间。iter/salt 可能来自链路上对端
#:（D1 挑战由服务端下发、注册 salt 由观看端上报），使用前必须校验。
KDF_ITER_MIN = 1_000
KDF_ITER_MAX = 2_000_000
#: salt 约定长度：32 位十六进制 = 16 字节（secrets.token_hex(16)），与两端生成口径一致
KDF_SALT_HEX_LEN = 32


def _is_hex_str(text, length=None):
    """是否为（指定长度的）十六进制字符串。"""
    if not isinstance(text, str) or not text:
        return False
    if length is not None and len(text) != length:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in text)


def derive_dk(password, salt_hex, iterations=PBKDF2_ITERATIONS):
    """由口令与盐派生密钥 dk（bytes）。salt 为十六进制字符串。

    auth-002（2026-09-18）：参数可能来自链路上对端，使用前强校验——
    iterations 必须落在 [KDF_ITER_MIN, KDF_ITER_MAX]：对端回 {iter:1} 即得
    "攻击者选盐、一次迭代"的离线口令爆破 oracle（比正常快 ~10^5 倍），
    回 {iter:10^9} 则本端在 PBKDF2 上挂死（重连即再挂）。越界抛 ValueError
    （调用方按协议错误处理）而非静默钳制——静默钳制会与对端算出不同 dk，
    表现为"密码正确却登录失败"，比明确拒绝更难排查。
    salt 限 16~64 位偶数长度十六进制（服务端约定 32 位）。

    auth-020（2026-09-20）：本函数只承诺抛 ValueError，所以 OverflowError 也必须归一
    ——`Infinity` 是合法 JSON，`int(float('inf'))` 抛 OverflowError，旧写法会让它逃出
    去（调用方按 docstring 只准备了 ValueError）。
    """
    try:
        it = int(iterations)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("KDF 迭代次数非法: %r" % (iterations,)) from None
    if not (KDF_ITER_MIN <= it <= KDF_ITER_MAX):
        raise ValueError("KDF 迭代次数越界: %d（允许 %d~%d）" % (it, KDF_ITER_MIN, KDF_ITER_MAX))
    if (not _is_hex_str(salt_hex) or not (16 <= len(salt_hex) <= 64)
            or len(salt_hex) % 2):
        raise ValueError("salt 必须是 16~64 位偶数长度十六进制字符串")
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               bytes.fromhex(salt_hex), it)


def challenge_mac(dk, nonce, user=""):
    """挑战-应答核心：proof = HMAC-SHA256(key=dk, msg=域分隔串 | user | nonce)。

    auth-002（2026-09-18，协议 v2.3）：proof 输入绑定 user 与域分隔串——
    旧实现只签 nonce，同一口令/密钥在"换账号名"或其它 HMAC 用途之间可被搬运。
    两端必须传同一个 user（登录/注册链路上即消息里的用户名）。
    """
    msg = (b"tv-auth-v2|" + str(user).encode("utf-8", "replace") + b"|" + nonce)
    return hmac.new(dk, msg, hashlib.sha256).digest()


def b64e(data):
    """bytes → base64 字符串（JSON 消息里传 nonce/proof 用）。"""
    return base64.b64encode(data).decode("ascii")


def b64d(text):
    """base64 字符串 → bytes；非法输入抛 binascii.Error（调用方按协议错误处理）。"""
    return base64.b64decode(text.encode("ascii"), validate=True)


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
APP_VERSION = "1.5.0"
APP_COPYRIGHT = "Copyright © 2026 TeamVision Team"
#: 作者署名（显示于启动画面与 exe 版本信息）
APP_AUTHOR = "by 西琳"

#: 配置读写全局锁（防止多线程同时 save_config 导致丢失更新）
_config_lock = threading.Lock()

#: 单条消息 payload 长度上限（32MB，含帧 JPEG 与控制消息，防恶意长度头）。
#: 第 57 条：原 64MB 远高于任何合法帧——8K/33MP 满熵 JPEG 约 8~15MB，H.264/HEVC
#: 关键帧 <1MB，控制 JSON <1MB。降到 32MB 仍留足余量，同时把每连接最坏缓冲内存
#: 直接砍半（解析在攒满声明长度前不会返回，长度头越大占用越久）。
MAX_PAYLOAD = 32 * 1024 * 1024

#: 配置结构版本：用于把 1.x 遗留配置一次性迁移到更安全的默认值（见 _upgrade_config）。
#: 1 = 1.3.x 及更早（监听 0.0.0.0、准入默认关闭）；2 = 默认只监听本机 + 准入默认开启。
CONFIG_VERSION = 2

#: B10（2026-09-11）：认证窗口的默认值与**下界**（秒）。
#: 观看端在发出凭据前的等待 = 等 Tk 就绪（最长 60s，viewer.C19）+ 登录框等待（120s）
#: → 最坏 ≈180s，且该等待发生在**服务端 deadline 已经开始计时之后**。认证窗口若小于
#: 这个值，用户还在输入 host 就已断开（第 31 条"输对了也登录失败"）。默认给 240s
#: 留出余量；host 侧用 AUTH_TIMEOUT_MIN_S 做下界约束，迁移时也一并抬高。
AUTH_TIMEOUT_DEFAULT_S = 240
AUTH_TIMEOUT_MIN_S = 240

#: 默认配置
DEFAULT_CONFIG = {
    "config_version": CONFIG_VERSION,
    "logs": {
        "level": "info"
    },
    # B4（实现方案 §6）：启动后查一次 GitHub Releases 最新 tag，仅状态栏提示，
    # 不做自动更新；结果缓存 24h。
    # dist-004（2026-09-18）：**默认关闭**——开启意味着每 24h 向 api.github.com
    # 暴露使用者 IP 与"在用本程序"的事实，按第 131 条默认安全口径应为 opt-in；
    # 需要更新提示时在共享端控制台勾选（或手改本键为 true）。
    "update_check": {
        "enabled": False
    },
    "host": {
        # 默认只监听本机：默认形态下不存在对外监听的端口（局域网内其他人连不上，
        # 杀软/审计也不会把「屏幕采集 + 对外监听 + 无鉴权」判成高危组合）。
        # 需要局域网直连时在共享端控制台把监听地址改成 0.0.0.0；公网共享走 frp 隧道。
        "listen_host": "127.0.0.1",
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
            "max_clients": 32,   # 同时在册连接上限（含握手中，第 103 条）
            "msg_timeout_s": 15.0,  # 单条消息（头+体）接收总时限，防 slowloris（第 103 条）
            "upstream_max_kbps": 0,  # 单成员上行带宽上限（0=自动 2×codec.bitrate_kbps，第 107 条）
            "upstream_max_fps": 0    # 单成员上行帧率上限（0=自动 max(8, 2×fps)，第 107 条）
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
            "bitrate_kbps": 2500,    # H.264 目标码率基线（VBR，峰值 1.5×；Kbps）
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
            # 默认开启准入：观看端必须注册/登录后才能取流。1.x 默认关闭（任何能连上
            # 的人都能看屏），升级后由 _upgrade_config 做一次性迁移。
            "enabled": True,
            # 自助注册策略：
            #   "open"      = 任何能连上共享端的客户端都可以自行注册（默认，兼容旧体验）
            #   "bootstrap" = 只有第一个账户可以自助注册，此后注册关闭
            #   "closed"    = 完全关闭自助注册，账户只能由共享端控制台添加
            "allow_register": "open",
            # D1（实现方案 §10.2）：挑战-应答认证模式。
            #   "require"       = 默认。口令不再过网，观看端必须走 chal/auth_proof；
            #                     旧版观看端的明文凭据被拒并提示「观看端版本过旧」。
            #   "allow_legacy"  = 兼容窗口：旧观看端可继续直发凭据（host 记 WARNING，
            #                     含用户名与来源地址）。一个发布周期后删除该分支。
            "chal_response": "require",
            # 认证阶段「总时限」（第 23 条防 slowloris）；须大于观看端「等界面就绪（最长
            # 60s）+ 登录框等待（120s）」的最坏合计 ≈180s（第 31 条：否则用户还在输入
            # host 就先断开、明明输对却登录失败）。B10（2026-09-11）：默认值从 150 提到
            # 240，并在 host 侧加下界约束 + 迁移时一并抬高——旧值只留 30s 余量，公网高
            # 延迟/慢用户下同样会踩到。
            "auth_timeout": AUTH_TIMEOUT_DEFAULT_S
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


#: 自助注册策略的合法取值（host.auth.allow_register）
REGISTER_POLICIES = ("open", "bootstrap", "closed")


def register_policy(auth_cfg):
    """把 host.auth.allow_register 归一为 open / bootstrap / closed。

    布尔按兼容口径归一（True→open、False→closed），缺失键/null 取默认 open；
    **非法取值（错别字/数字/其它类型）一律 fail-closed 归一为 closed** 并告警。

    auth-007（2026-09-18）：旧口径对非法取值 fail-open（"colosed" 这类错别字 →
    open），公网隧道下注册静默全开、任意人可自建账号看屏；安全开关的兜底取向必须
    与"默认安全"口径一致。host 与共享端控制台共用同一份语义，防止界面显示的策略
    与服务端实际执行的策略不一致。
    """
    raw = auth_cfg.get("allow_register", "open") if isinstance(auth_cfg, dict) else "open"
    if raw is True:
        return "open"
    if raw is False:
        return "closed"
    if raw is None:
        return "open"          # 键存在但为 null：视作未设置，走默认
    policy = str(raw).strip().lower()
    if policy in REGISTER_POLICIES:
        return policy
    try:
        import logger
        logger.get_logger().warning(
            "host.auth.allow_register 取值非法（%r），按 closed 处理（不开放自助注册）", raw)
    except Exception:
        pass
    return "closed"


# 第 114 条（C3）：预打包 Struct，替代原先 bytes((kind,))+struct.pack+... 的多段拼接。
# 每帧从 5–6 次临时分配降到 1 次 header pack + 1 次 payload 连接。
_FRAME_HDR = struct.Struct(">BIQ")      # kind, length, ts
_VIDEO_HDR = struct.Struct(">BIQBB")    # kind, length, ts, codec, flags
_MSG_HDR = struct.Struct(">BI")         # kind, length（中继/控制消息共用）
_LEN_U32 = struct.Struct(">I")

# net-045（2026-10-09，协议 v2.4）：视频帧载荷新增 4 字节**单调递增帧序号**。
# 动机：此前载荷只有 ts/codec/flags，没有序号，接收端一旦发现损伤只能整体重置
# 解码器（`_invalidate_media`），把本来还能解码的帧一并丢掉；有了序号就能改成
# "丢弃到下一个可解码点"，这是裸 TCP 队头阻塞下唯一能做的选择性恢复。
#
# 两种布局（codec/flags 位置不变，seq 插在 flags 之后、NAL 之前）：
#   v2.3  ts(8) | codec(1) | flags(1) | NAL              —— 载荷头 10 字节
#   v2.4  ts(8) | codec(1) | flags(1) | seq(4) | NAL      —— 载荷头 14 字节
# 布局无法自描述（两种都以 ts/codec/flags 开头），所以必须由对端 proto 次版本
# 协商决定用哪种，见 VIDEO_SEQ_MINOR。
_VIDEO_SEQ_HDR = struct.Struct(">BIQBBI")  # kind, length, ts, codec, flags, seq
_VIDEO_PAYLOAD_BASE = 10                    # v2.3：ts(8)+codec(1)+flags(1)
_VIDEO_SEQ_PAYLOAD_BASE = 14                # v2.4：再 + seq(4)
_SEQ_U32 = struct.Struct(">I")


def pack_frame(jpeg_bytes, ts=None):
    """将 JPEG 数据打包为一帧消息，返回 [kind=1][4 字节大端长度][8 字节微秒时间戳 ">Q"][JPEG]。

    长度字段 = 8 + len(jpeg_bytes)；ts 为 None 时取当前时刻微秒（int(time.time() * 1_000_000)）。
    """
    if ts is None:
        ts = int(time.time() * 1_000_000)
    return _FRAME_HDR.pack(MSG_FRAME, 8 + len(jpeg_bytes), ts) + jpeg_bytes


def pack_video(video_bytes, ts=None, keyframe=False, codec=CODEC_H264, seq=None):
    """将视频帧（Annex-B NAL）打包为视频帧消息。

    v2.3 返回 [kind=3][4B 大端长度][8B 微秒 ts][1B codec][1B flags][NAL]（长度 = 10 + N）。
    v2.4（`seq` 非 None 时）在 flags 之后追加 4 字节大端单调递增序号
    （长度 = 14 + N），见 net-045。`seq=None` 即保持 v2.3 布局，供与未升级对端通信。
    """
    if ts is None:
        ts = int(time.time() * 1_000_000)
    flags = VIDEO_FLAG_KEY if keyframe else 0
    if seq is None:
        return (_VIDEO_HDR.pack(MSG_VIDEO, _VIDEO_PAYLOAD_BASE + len(video_bytes),
                                ts, codec, flags)
                + video_bytes)
    return (_VIDEO_SEQ_HDR.pack(MSG_VIDEO, _VIDEO_SEQ_PAYLOAD_BASE + len(video_bytes),
                                ts, codec, flags, seq & 0xFFFFFFFF)
            + video_bytes)


def parse_video(payload, expect_seq=False):
    """从视频帧 payload 解包出 (时间戳微秒, codec, flags, NAL 字节, seq)。

    `expect_seq=True` 按 v2.4 布局解（seq 在 flags 之后）；否则按 v2.3 解，seq 返回
    None。**返回元组恒为 5 元**——不要按对端版本改元组长度，否则调用点的解包
    会随协议版本而错位。
    """
    base = _VIDEO_SEQ_PAYLOAD_BASE if expect_seq else _VIDEO_PAYLOAD_BASE
    if len(payload) < base:
        raise ValueError("视频帧 payload 不足 %d 字节" % base)
    (ts,) = struct.unpack(">Q", payload[:8])
    codec, flags = payload[8], payload[9]
    if not expect_seq:
        return ts, codec, flags, payload[_VIDEO_PAYLOAD_BASE:], None
    (seq,) = _SEQ_U32.unpack(payload[10:_VIDEO_SEQ_PAYLOAD_BASE])
    return ts, codec, flags, payload[_VIDEO_SEQ_PAYLOAD_BASE:], seq


class SeqCounter:
    """net-045（2026-10-09）：每路视频流的单调递增帧序号发生器。

    为什么不用 `itertools.count`：打包发生在 encode 线程、而静止期关键帧应答走
    另一条路径，`next()` 虽然 GIL 下原子，但把"取号 + 打包"拆到两处后容易在
    异常重试里重复发同一个号。这里显式持锁。

    **不跳过 0**：接收侧判断"这帧有没有序号"用的是 `None`（v2.3 布局）而不是 0，
    所以 0 是合法序号。若在这里跳过 0，接收端按相对增量算缺口时，
    `0xFFFFFFFF → 1` 会被算成 delta=2，凭空记一次丢帧——回绕反而制造假告警。
    序号 32 位无符号自然回绕（按 30fps 约 4.9 天绕一圈），只用于识别丢帧/错序，
    不参与解码，回绕本身无害。
    """

    __slots__ = ("_lock", "_n")

    def __init__(self, start=1):
        self._lock = threading.Lock()
        self._n = int(start) & 0xFFFFFFFF

    def next(self):
        """取下一个序号（32 位无符号，含 0，自然回绕）。"""
        with self._lock:
            n = self._n
            self._n = (n + 1) & 0xFFFFFFFF
            return n


def _annexb_strength(data):
    """NAL 的 Annex-B 起始码强度：4 字节零填充 → 4，3 字节 → 3，否则 0。

    R8-NET-02（2026-10-10）：判据原来只有"像/不像"两档，而它是布局的唯一线索
    （载荷无法自描述）。两档不够用——`expect_seq=False` 收到 v2.4 帧时，按 v2.3
    解出的 `nal = seq(4) + 真 NAL`，`seq == 1` 或 `seq ∈ [256,511]` 时 seq 自己的
    前 3~4 字节就构成起始码，两档判据"成立"因而永不回退，错位 NAL（多一截
    `00 00 01 XX`）被当成真载荷喂解码器：XX & 0x1F ∈ [1,5] 时是合法的切片 NAL 头
    → 参考链断裂，每个 GOP 末段 6~10 帧解不出，画面周期性冻结。

    分档的依据是编码器输出的硬约束：H.264/H.265 的 Annex-B 流恒用 4 字节零填充起始码，
    所以"真 NAL"的强度恒为 4；被 seq 前缀污染出的伪起始码顶多是 3（`00 00 01 xx`），
    强度 3 < 4 因而一定输。唯一例外是 `seq == 1`——它的 4 字节恰好就是
    `00 00 00 01`，两端强度同为 4，由 `parse_video_auto` 按协商布局打破平局。
    """
    if data[:4] == b"\x00\x00\x00\x01":
        return 4
    if data[:3] == b"\x00\x00\x01":
        return 3
    return 0


def _looks_like_annexb(data):
    """NAL 是否以 Annex-B 起始码开头（3 字节或 4 字节零填充形式）。"""
    return _annexb_strength(data) > 0


def peek_video_header(payload):
    """只取视频帧头 (ts, codec, flags)，**不碰 NAL**——布局无关。

    net-045：中继路径（host 转发共享者上行帧）唯一的需要是从 flags 取关键帧位做门控，
    而 flags 在 v2.3/v2.4 里都位于第 9 字节，NAL 起点却随 seq 插入而整体后移。
    用完整解析在这里会两头出错：按错误布局解，NAL 会错位；而靠"NAL 是否像 Annex-B"
    猜布局，又会把非 Annex-B 的合法载荷误判成损坏帧直接丢弃——中继的职责是校验
    结构、转发原字节，不该替解码器判断 NAL 内容。
    """
    if len(payload) < _VIDEO_PAYLOAD_BASE:
        raise ValueError("视频帧 payload 不足 %d 字节" % _VIDEO_PAYLOAD_BASE)
    (ts,) = struct.unpack(">Q", payload[:8])
    return ts, payload[8], payload[9]


def parse_video_auto(payload, expect_seq=False):
    """net-045：按协商结果解帧，但对**布局不符**做回退，避免混合版本互相错解。

    为什么必须回退：载荷布局无法自描述，而 MultiView 允许旧版 share.py 的共享者
    连到新版 host.py 再被中继给新版观看端。此时观看端按 v2.4 解，会把 NAL 前 4 字节
    当成序号吃掉，剩下的 NAL 变成 `01 65 …` 这种缺起始码的碎片——画面直接花掉，
    而且没有任何报错可查。

    R8-NET-02（2026-10-10）：旧实现是"按协商布局解，解出的 NAL 像 Annex-B 就**立即
    返回**"，而该判据与布局不是一一映射——`expect_seq=False` 收到 v2.4 帧时，按 v2.3
    解出的 `nal = seq(4) + 真 NAL`，只要 seq 的前 3~4 字节自己构成起始码就会被采信：
      · `seq == 1` → 前缀 `00 00 00 01`；
      · `seq ∈ [256,511]` → 前缀 `00 00 01 xx`，且 `xx & 0x1F ∈ [1,5]` 时是合法的
        切片 NAL 头。
    实测后果（libx264/H.264，520 帧）：参考链断裂，每个 GOP 末段 6~10 帧解不出，
    共丢 28 帧，画面周期性冻结 0.2~0.33 秒；而 `seq` 单调递增必经过这段区间。

    现在的判据：**两种布局都产出候选**，只把起始码强度 > 0 的收进来，再按
    `(强度, 是否协商布局, 原尝试顺序)` 降序取第一名。
      · 真 NAL 的起始码强度恒为 4——H.264/H.265 编码器输出固定使用 4 字节零填充
        起始码，`pack_video` 直接拼接编码器输出，所以"真 NAL 强度 4"是硬约束；
      · 被 seq 前缀污染出的伪起始码顶多是 3（`00 00 01 xx`），强度 3 < 4，一定输；
      · `seq == 1` 时前缀恰好是 `00 00 00 01`，两端强度同为 4——这是**不可判定**的真
        歧义（该序号与真 NAL 起始码逐字节相同），由"是否协商布局"打破；若连协商
        方向也相同含义（`expect_seq=False` 时 v2.3 就是第一顺位），则按**原尝试顺序**
        返回，逐字保持修前行为。
    两种布局都不产出候选（强度为 0）即判定损坏，抛 ValueError。

    判据边界（如实登记）：真 NAL 用 3 字节起始码时，上述强度差会消失（两边都是 3），
    此时若帧确实带 seq 则仍可能错解或判损坏。真实编码器不会输出 3 字节起始码，属合成
    数据才有的形态。
    """
    order = (True, False) if expect_seq else (False, True)
    last_err = None
    best = None
    for rank, use_seq in enumerate(order):
        try:
            ts, codec, flags, nal, seq = parse_video(payload, expect_seq=use_seq)
        except ValueError as e:      # 载荷长度不够该布局的最小头
            last_err = e
            continue
        strength = _annexb_strength(nal)
        if strength <= 0:
            last_err = ValueError("NAL 缺 Annex-B 起始码（疑似布局不符）")
            continue
        # 末位用 -rank（原尝试顺序）：强度与协商方向都打平时保持修前的先到先得——
        # 修前就是"按 order 逐个试，第一个像 Annex-B 的就返回"，所以 `seq == 1`
        # 且 expect_seq=False 时返回的是**协商布局**（v2.3）的解析结果。
        key = (strength, use_seq == expect_seq, -rank)
        if best is None or key > best[0]:
            best = (key, (ts, codec, flags, nal, seq))
    if best is not None:
        return best[1]
    raise last_err or ValueError("视频帧 payload 无法解出 NAL")


def pack_msg(obj):
    """把 dict 序列化为 [kind=2][4 字节大端长度][UTF-8 JSON]，用于 auth/ping/pong 等控制消息。

    net-038（2026-09-24）：`ensure_ascii=False` 下 json.dumps 会把对端可控文本里的
    游离代理位（`json.loads('"\\ud800x"')` 原样产出）保留在输出里，严格 `.encode("utf-8")`
    随即抛 UnicodeEncodeError——而全仓发送点只挡 OSError，异常会打断整条 roster 广播
    并继续上抛。与 derive_hmac 同口径：非严格编码 + 替换，产出仍是可解码的 UTF-8。
    """
    text = json.dumps(obj, ensure_ascii=False)
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        data = text.encode("utf-8", "replace")
    return _MSG_HDR.pack(MSG_CTRL, len(data)) + data


def send_msg(sock, obj):
    """发送一条控制消息（JSON dict）。"""
    sock.sendall(pack_msg(obj))


def _scan_message(buf):
    """校验消息头并判断是否已收完整；返回 (kind, length)，数据不足时返回 None。

    非法 kind/长度抛 ValueError（协议错误，调用方应断开连接）。
    """
    if len(buf) < 5:
        return None
    kind = buf[0]
    (length,) = _LEN_U32.unpack(buf[1:5])
    if kind not in (MSG_FRAME, MSG_CTRL, MSG_VIDEO):
        raise ValueError("非法的消息类型: %d" % kind)
    if length <= 0 or length > MAX_PAYLOAD:
        raise ValueError("非法消息长度: %d" % length)
    if len(buf) < 5 + length:
        return None
    return kind, length


def parse_message(buf):
    """从接收缓冲区解析出最早的一条完整协议消息。

    返回 (consumed, kind, payload)：
      - 缓冲区含完整消息时返回首条消息的消费字节数、kind 与 payload；
      - 不足一条（等待更多数据）时返回 (0, None, None)；
      - 校验失败（非法 kind/长度）时抛出 ValueError。
    """
    scanned = _scan_message(buf)
    if scanned is None:
        return 0, None, None
    kind, length = scanned
    # 第 57 条：rx_buf 改用 bytearray 后切片会是 bytearray，而 pack_video/pack_frame
    # 做 bytes(...) + payload，bytes + bytearray 抛 TypeError。统一转 bytes，保证
    # 中继路径（host 转发上行帧）与缓冲区类型无关。
    return 5 + length, kind, bytes(buf[5:5 + length])


def parse_message_raw(buf):
    """同 parse_message，但返回**整条原始消息**（含 5 字节头）的一份 bytes。

    中继转发（A4 §3.1）需要的正是完整消息而非 payload：原先 payload 切片 +
    _fwd_member_frame 重打包共 2 次整帧拷贝，这里一次物化即可直接入队。
    返回值与线上字节逐字节相同，转发路径不得再修改任何字节。
    """
    scanned = _scan_message(buf)
    if scanned is None:
        return 0, None, None
    kind, length = scanned
    return 5 + length, kind, bytes(memoryview(buf)[:5 + length])


def message_pump(sock, *, should_stop, on_idle, on_message,
                 on_rx=None, on_recv_error=None, on_eof=None,
                 on_chunk=None, on_batch=None, on_buf=None, on_tick=None,
                 raw_kinds=()):
    """C2（实现方案 §7 第 2 步）：两端共用的 socket 接收泵。

    骨架 = select(1s) → recv → bytearray 累积 → parse_message(_raw) 逐条排空。
    超时判定、按 kind 解析/分发、退出语义全部留在回调里（host 的 keepalive/
    slowloris 与 viewer 的 read_idle/重连分类保持各自配置，泵不统一默认值）：

      should_stop()            每轮开始检查；True → 泵以 None 返回。
      on_idle(now, buf)        select 超时；返回非 None 原因串结束泵（host 半开/
                               单条消息超时）；viewer 的停滞判定改为 raise。
      on_rx(now)               收到非空数据块（在累积前）。
      on_recv_error(exc)       recv OSError；返回原因串结束泵；返回 None/未提供
                               则重新抛出（viewer 借此让异常到达外层重连分类）。
      on_eof()                 对端关闭：返回原因串结束泵，或直接 raise（viewer）。
      on_chunk()               数据已并入缓冲区、排空开始之前（viewer 重置批处理）。
      on_message(kind, payload, raw)
                               每条完整消息；raw_kinds 命中的 kind 走
                               parse_message_raw（payload 为 memoryview 切片、
                               raw 为整条 bytes，A4 零拷贝中继），否则
                               parse_message（payload 为 bytes，raw=None）。
                               返回非 None 原因串**立即**结束泵（host fatal/协议错误）。
      on_batch(buf)            一次排空结束后（viewer 的批解码点）。
      on_buf(event, buf, now, n_msgs=0)
                               缓冲区状态位：event="pre_empty"（recv 前缓冲区为空，
                               新消息首字节到达；host 记 partial_since 起点）/"post"
                               （排空后，n_msgs=本轮**真正消费掉的完整消息条数**）。
                               net-015（2026-09-18）：host 只应在 n_msgs>0 时重置
                               "单条消息总时限"起点——旧实现无条件重置，攻击者发
                               `长度=32MB-1` 的头上限后每 <15s 滴 1 字节即可把
                               计时起点无限后移（第 103 条 slowloris 防线被绕过）。

    on_tick(now, buf)          每轮**收到数据**之后（与 on_idle 互补，二者不重叠）；
                               返回非 None 原因串结束泵。net-030/net-036：只放超时
                               判定，不要放"每秒至多一次"的副作用（那些属 on_idle）。

    返回结束原因（str）或 None（should_stop 置位）。
    """
    buf = bytearray()
    raw_set = frozenset(raw_kinds)
    while not should_stop():
        r, _, _ = select.select([sock], [], [], 1.0)
        if not r:
            reason = on_idle(time.monotonic(), buf)
            if reason is not None:
                return reason
            continue
        try:
            data = sock.recv(65536)
        except OSError as e:
            reason = on_recv_error(e) if on_recv_error is not None else None
            if reason is None:
                raise
            return reason
        if not data:
            return on_eof() if on_eof is not None else None
        now = time.monotonic()
        if on_rx is not None:
            on_rx(now)
        if on_buf is not None and not buf:
            on_buf("pre_empty", buf, now)
        buf += data
        if on_chunk is not None:
            on_chunk()
        n_msgs = 0
        while True:
            if raw_set and buf and buf[0] in raw_set:
                consumed, kind, raw = parse_message_raw(buf)
                if consumed == 0:
                    break
                del buf[:consumed]
                reason = on_message(kind, memoryview(raw)[5:], raw)
            else:
                consumed, kind, payload = parse_message(buf)
                if consumed == 0:
                    break
                del buf[:consumed]
                reason = on_message(kind, payload, None)
            n_msgs += 1
            if reason is not None:
                return reason
        if on_batch is not None:
            on_batch(buf)
        if on_buf is not None:
            on_buf("post", buf, time.monotonic(), n_msgs)
        # net-030 / net-036（2026-09-20）：停滞判据必须也在**有数据**的轮次上评估。
        # 旧实现只在 select 空转满 1 秒时调 on_idle，于是"每 0.4~0.9 秒投 1 字节"的
        # 对端让 `not r` 永不成立 → 单条消息总时限与半开/停滞判定一次都不执行
        # （实测 12 秒内 on_idle 调用 0 次）。记账口径（net-015 只在完整消息时刷新）
        # 是对的，缺的是评估时机；这里在每轮 recv 之后补一次。
        # 与 on_idle 分开一个钩子：on_idle 还带 fps 衰减、请求关键帧等副作用，
        # 那些应当保持"每秒至多一次"，只有超时判定需要在数据路径上加密。
        if on_tick is not None:
            reason = on_tick(time.monotonic(), buf)
            if reason is not None:
                return reason
    return None


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


def _win_keepalive_ioctl(sock, idle_s, intvl_s):
    """Windows 上用 WSAIoctl(SIO_KEEPALIVE_VALS) 设置 TCP keepalive 空闲/间隔。

    net-024（2026-09-18）：`socket.TCP_KEEPIDLE` 等常量自 Python 3.13 才在 Windows
    提供；py<3.13 时上面的 setsockopt 分支整段是死代码。这里走 Winsock 原生
    ioctl（探测次数沿用系统默认），失败静默（与其它调优项同口径）。
    """
    try:
        import ctypes
        fd = sock.fileno()
        if fd is None or fd < 0:
            return
        inbuf = struct.pack("III", 1, max(1, int(idle_s)) * 1000,
                            max(1, int(intvl_s)) * 1000)
        out_len = ctypes.c_ulong(0)
        ws2 = ctypes.WinDLL("ws2_32")
        ws2.WSAIoctl.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_ulong,
            ctypes.c_char_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_void_p, ctypes.c_void_p,
        ]
        ws2.WSAIoctl(ctypes.c_void_p(fd), 0x98000004, inbuf, len(inbuf),
                     None, 0, ctypes.byref(out_len), None, None)
    except Exception:
        pass


def tune_socket(sock, sndbuf=None, rcvbuf=None, keepalive=True, keepalive_idle=60):
    """低延迟网络调优：禁用 Nagle（TCP_NODELAY）+ 加大收发缓冲 + TCP keepalive。

    所有 setsockopt 都单独 try/except：个别平台不支持特定选项时静默忽略，
    保证在任何环境都能工作。
    """
    # net-024（2026-09-18）：除 OSError 外还要接 TypeError/ValueError——Windows
    # 上超界值 setsockopt 抛 OSError(WSAEINVAL) 的兄弟平台/替身对象会抛
    # TypeError，旧实现漏防会沿调用线程上抛（"调优失败"打死整条连接）。
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (OSError, TypeError, ValueError):
        pass
    if sndbuf:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, int(sndbuf))
        except (OSError, TypeError, ValueError):
            pass
    if rcvbuf:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, int(rcvbuf))
        except (OSError, TypeError, ValueError):
            pass
    if keepalive:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except (OSError, TypeError, ValueError):
            keepalive_idle = 0
        if keepalive_idle > 0 and not hasattr(socket, "TCP_KEEPIDLE") \
                and sys.platform == "win32":
            # net-024：Python < 3.13 的 Windows 没有 TCP_KEEPIDLE/KEEPINTVL 常量
            # （内核支持），旧实现整段跳过 → SO_KEEPALIVE 只是打开了 2 小时默认
            # 空闲的开关，等于没调优。用 Winsock 原生 ioctl 补上空闲/探测间隔。
            _win_keepalive_ioctl(sock, keepalive_idle,
                                 max(1, int(keepalive_idle) // 6))
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
                raise TimeoutError("接收总时限已耗尽")
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


def server_handshake(sock, deadline=None):
    """服务端握手：校验客户端发来的 MAGIC_CLIENT，校验通过后回复 MAGIC_SERVER。

    net-031（2026-09-20）：deadline 给定时整次握手（4 字节魔数）受**总时限**约束，
    与同族认证路径（net-022）同口径。旧实现不传 deadline，只靠 `settimeout(10)` 的
    每次 recv 超时——每 (timeout−1) 秒喂 1 字节的对端可把单条连接的最坏时长拉到
    `4 × 10s = 40s`，以"每约 5 秒新建一条连接"的速率即可长期占满 max_pending_auth
    名额（速率型 DoS；攻击停止后 ≤40 秒自愈）。
    """
    magic = recv_exact(sock, len(MAGIC_CLIENT), deadline)
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
    # net-018（2026-09-18）：IPv6 支持——旧实现把 `[::1]:5700` 的 host 解析成
    # "[::1]"（连接必然失败），把裸 `::1` 的 "1" 当成端口（连到端口 1）。
    if addr_str.startswith("["):
        end = addr_str.find("]")
        if end <= 1:
            raise ValueError("IPv6 地址缺少 ']': %s" % addr_str)
        host = addr_str[1:end].strip()
        rest = addr_str[end + 1:].strip()
        if not rest:
            return host, default_port
        if not rest.startswith(":"):
            raise ValueError("IPv6 地址后缀非法（应为 [地址] 或 [地址]:端口）: %s" % addr_str)
        port_str = rest[1:].strip()
        if not port_str:
            raise ValueError("地址缺少端口号: %s" % addr_str)
        try:
            port = int(port_str)
        except ValueError:
            raise ValueError("端口无效: %s" % port_str) from None
        if not (1 <= port <= 65535):
            raise ValueError("端口超出范围 1~65535: %d" % port)
        return host, port
    if addr_str.count(":") >= 2:
        # net-018（2026-09-18）的初衷：裸 IPv6（无方括号、无端口）整串都是主机，
        # 旧实现 rsplit 后把最后一段当端口（`::1` → 端口 1）。
        # net-028（2026-09-20）：但"整串作主机"无条件下放把另一种原本可用的写法改坏了——
        # `::1:5700`（裸 IPv6 + 端口，A0 双栈回环下 frpc 回连的常见手写形式）被整串当
        # 主机名返回 ('::1:5700', 5700)。更隐蔽的是 Windows 的 getaddrinfo 不会报错，
        # 而是把这串曲解成 `::0.1.87.0` 之类的地址，比"连不上"更难查。
        # 收口：末段是合法端口、且**去掉末段后剩余部分本身是合法 IPv6** 时按 host:port
        # 拆分；否则维持 net-018 的"整串作主机"。用 ipaddress 判定而非数冒号，
        # 才能把 `::1`（末段"1"看着像端口、前缀":"却不合法）与 `::1:5700` 分开。
        # 惰性导入：ipaddress 是纯 Python 标准库，只在多冒号分支才需要。
        host_part, sep, port_part = addr_str.rpartition(":")
        if sep and port_part.isdigit() and _is_ipv6_literal(host_part):
            port = int(port_part)
            # 与其余分支同一口径校验范围：漏掉这步会让 `::1:0` / `::1:99999` 静默
            # 返回一个不可能连通的端口（其它形态同样输入都会抛 ValueError）。
            if not (1 <= port <= 65535):
                raise ValueError("端口超出范围 1~65535: %d" % port)
            return host_part, port
        return addr_str, default_port
    if ":" in addr_str:
        host, port_str = addr_str.rsplit(":", 1)
        host = host.strip()
        port_str = port_str.strip()
        if not port_str:
            raise ValueError("地址缺少端口号: %s" % addr_str)
        try:
            port = int(port_str)
        except ValueError:
            raise ValueError("端口无效: %s" % port_str) from None
        if not (1 <= port <= 65535):
            raise ValueError("端口超出范围 1~65535: %d" % port)
        # net-027（2026-09-20）：`:5700` 这类"冒号前为空"的输入旧实现直接返回
        # ('', 5700)。它是第 94 条地址校验的口径缺口（net-018 之前就存在，实测复刻
        # 旧实现同样返回空主机），不是 IPv6 改造带进来的。空主机经
        # socket.create_connection(('',5700)) → getaddrinfo('', …) 会解析到本机链路
        # IPv6（fe80::…），表现为观看端每轮重连白等 10 秒再进入无限退避。
        if not host:
            raise ValueError("地址缺少主机: %s" % addr_str)
        return host, port
    return addr_str, default_port


def _is_ipv6_literal(text):
    """text 是否是一个合法的 IPv6 字面量（用于 parse_addr 的无括号形式判定）。"""
    if ":" not in text:
        return False
    try:
        import ipaddress
        ipaddress.IPv6Address(text.strip().split("%")[0])
        return True
    except Exception:
        return False


def cfg_section(cfg, key, default=None):
    """取配置里「应为字典」的节；缺失或值不是字典时回退 default（缺省 {}）并告警。

    ui-022 / ui-030 / auth-022（2026-09-20）同族收口：旧写法一律是
    `cfg["host"].get("frp", {})`，它只挡了"键不存在"、挡不住"值不是字典"——
    手改 config.json 把某节写成 `false` / `0` / `""` / `[]` 后，
    `.get("frp", {}).get("public_addr")` 直接 AttributeError。`_deep_merge` 只在
    "双方都是字典"时才递归合并，非字典的非 None 值会原样覆盖默认字典（1004 行），
    所以合并后确实可能不是字典。
    `load_config` 已加了加载期类型校验（见 _normalize_dict_sections），本函数用于
    非 load_config 来源（测试、share.py 自造 cfg、面板局部字典）的同一口径兜底，
    避免每个读取点各写一遍 isinstance。
    """
    if default is None:
        default = {}
    if not isinstance(cfg, dict):
        return copy.deepcopy(default)
    value = cfg.get(key, default)
    if isinstance(value, dict):
        return value
    if value is not None:
        _log_warning("配置节 %s 不是对象（%r），按默认值处理", key, value)
    return copy.deepcopy(default)


def safe_int(value, default, lo=None, hi=None, warn=True, clamp=True):
    """把配置值安全转为 int：非法（字符串/None/布尔）时回退 default；越界时按 clamp 决定。

    第 100 条：`config.json` 手改成 `"fps": "60"` 时，`slot["fps"]` 是 str，采集线程
    的 `fps_now > 0` 会抛 TypeError 并把线程直接打死（服务仍在 accept、观看端能收
    pong，但一帧都没有）。所有来自配置的数值都经此转换。

    cfg-008（2026-09-20）：docstring 与实现相反——旧首行称"超出范围时回退 default"，
    实际旧实现的 lo/hi 是**钳制**（`safe_int(0, 5700, lo=1) -> 1`，default 完全不参与）。
    钳制对 fps/quality/scale 是合适的（第 100 条要的就是这个，clamp=True 保持原行为）；
    对**端口**这类"越界值根本不该被用上"的键是错的：`"port": 0` 旧实现会把监听端口变成
    1 且零提示。这类键请传 clamp=False，语义变为"越界同非法 → 回退 default + WARNING"。
    注意：不改变已有调用点的行为，只把语义写清并补上另一种模式。

    warn=False 用于每秒/每帧执行的热路径（避免同一条告警刷满日志）。
    """
    if isinstance(value, bool):
        value = int(value)
    try:
        out = int(value)
    except (TypeError, ValueError, OverflowError):
        # net-025（2026-09-18）：OverflowError 也要接住——合法 JSON 里的
        # `Infinity`/`1e999` 解析成 float('inf')，`int(inf)` 抛 OverflowError，
        # 旧实现漏防会沿调用线程上抛（配置可写即可打死采集/编码线程）。
        if warn:
            _log_warning("配置值 %r 不是整数，回退默认值 %r", value, default)
        return int(default)
    if lo is not None and out < lo:
        if not clamp:
            if warn:
                _log_warning("配置值 %r 小于下限 %d，回退默认值 %r", value, lo, default)
            return int(default)
        out = int(lo)
    if hi is not None and out > hi:
        if not clamp:
            if warn:
                _log_warning("配置值 %r 超出上限 %d，回退默认值 %r", value, hi, default)
            return int(default)
        out = int(hi)
    return out


def safe_float(value, default, lo=None, hi=None, warn=True):
    """把配置值安全转为 float：非法时回退 default，并按需夹到 [lo, hi]。

    net-025（2026-09-18）：NaN/±inf（JSON 合法但异常：`Infinity`/`1e999`）一律
    视为非法——NaN 与任何值比较均为 False，能穿透 lo/hi 夹取直达下游。
    """
    try:
        out = float(value)
        if not math.isfinite(out):
            raise ValueError("非有限数: %r" % (value,))
    except (TypeError, ValueError):
        if warn:
            _log_warning("配置值 %r 不是数字，回退默认值 %r", value, default)
        out = float(default)
    if lo is not None and out < lo:
        out = float(lo)
    if hi is not None and out > hi:
        out = float(hi)
    return out


def normalize_region(region):
    """校验并规范化采集区域；非法（缺键/非数字/宽高非正）时返回 None（全屏）。

    第 100 条：旧实现把配置里的 region 原样透传，缺键时 screen.CaptureManager.grab
    每次 KeyError 被吞成 None（画面永久冻结、无任何提示），host_ui._region_text 也会
    KeyError 让整个图形控制台起不来。
    """
    if region is None:
        return None
    if not isinstance(region, dict):
        _log_warning("采集区域配置不是对象（%r），按全屏处理", region)
        return None
    try:
        left = int(region["left"])
        top = int(region["top"])
        width = int(region["width"])
        height = int(region["height"])
    except (KeyError, TypeError, ValueError, OverflowError):
        # net-025：Infinity/1e999 走到 int() 时抛 OverflowError，旧实现漏防会
        # 直接打断 GUI 构造/采集线程
        _log_warning("采集区域配置缺少 left/top/width/height 或非整数（%r），按全屏处理", region)
        return None
    if width <= 0 or height <= 0:
        _log_warning("采集区域取值非法（%r），按全屏处理", region)
        return None
    # capture-008（2026-09-24）：虚拟桌面坐标可为负——副屏摆在主屏左侧/上方是极常见的
    # 双屏布局，旧实现要求 left/top >= 0，把合法区域判成"区域格式错误"，该屏只能全屏
    # 采集。宽度/高度仍须为正；越界由 host_ui 按虚拟桌面并集边界单独校验。
    return {"left": left, "top": top, "width": width, "height": height}


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


#: 数据目录缓存（config.json/accounts.json/日志共用；None=尚未探测）
_DATA_DIR = None


def _probe_writable(d):
    """探测目录是否可写（创建目录 + 写一个探针文件再删除），绝不抛异常。

    cfg-002（2026-09-18）：探针文件名加线程号——旧实现只用 PID，同一进程的两个
    线程首次同时探测时互删对方的探针（A 写、B 删、A 的 os.remove 抛 FileNotFound
    Error）→ 误判"不可写"→ config/日志整体回退 %TEMP%。删除失败也视为本次探测成功。
    """
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".wprobe_%d_%d"
                             % (os.getpid(), threading.get_ident()))
        with open(probe, "w", encoding="utf-8") as f:
            f.write("x")
        try:
            os.remove(probe)
        except OSError:
            pass   # 被他人（清理/杀软）清掉也算本次探测成功
        return True
    except OSError:
        return False


def app_dirs():
    """与程序相关的目录列表（按优先级）：exe 目录，以及冻结运行时的上一级安装根目录。

    onedir 打包（PyInstaller COLLECT）后 exe 位于 `<安装根>\\fps-host\\` 或
    `<安装根>\\fps-viewer\\`，而 frpc.exe 等外部程序按习惯放在安装根。搜索外部程序
    时必须把上一级一并纳入，否则同一条配置在 onefile（旧）与 onedir（新）下行为不同。
    """
    dirs = [exe_dir()]
    if getattr(sys, "frozen", False):
        parent = os.path.dirname(exe_dir())
        if parent and os.path.abspath(parent) != os.path.abspath(exe_dir()):
            dirs.append(parent)
    return dirs


def _data_dir_candidates():
    """默认数据目录候选（按优先级）。

    冻结运行（exe）：先 `%LOCALAPPDATA%\\TeamVision`，再 exe 目录。
      onedir 打包后共享端与观看端分处 `<安装根>\\fps-host\\`、`<安装根>\\fps-viewer\\`
      两个子目录，各自以 exe 目录为数据目录会导致 config.json / accounts.json 分家
      （观看端读不到共享端的频道与账户），而且安装包升级覆盖程序目录时会连配置一起删掉。
      统一落到同一个用户目录即可让两端共享配置，也让升级不丢设置。
    源码运行：exe_dir() 优先（开发/测试时配置就在项目目录里，便于直接查看与改动）。
    两者最后都回退到临时目录。
    """
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if getattr(sys, "frozen", False):
        if local:
            candidates.append(os.path.join(local, "TeamVision"))
        candidates.append(exe_dir())
    else:
        candidates.append(exe_dir())
        if local:
            candidates.append(os.path.join(local, "TeamVision"))
    candidates.append(os.path.join(tempfile.gettempdir(), "TeamVision"))
    return candidates


def data_dir(candidates=None):
    """返回**可写**的数据目录，config.json / accounts.json / logs 共用（第 102 条）。

    安装到 Program Files（机器级目录）后非管理员运行时 exe_dir() 不可写：原实现把
    config.json 与 accounts.json 都写在那里，于是 `load_config` 首次启动就抛
    PermissionError（host 的 excepthook 还没装 → windowed 打包下静默退出、无日志，
    即用户口中的"双击没反应"），改设置时 save_config 抛异常被 Tk 回调吞掉，表现为
    "设置老是自己消失"。logger 早就有同样的回退（第 13 条），这里把它统一到
    common，让配置/账户/日志落在同一个可写目录。

    便携（绿色版）行为不变：exe_dir() 可写时仍然优先用它。
    candidates 仅用于单测注入（给定时不写缓存）；全部不可写时返回 None。
    """
    global _DATA_DIR
    cache = candidates is None
    if cache and _DATA_DIR is not None:
        return _DATA_DIR
    if cache:
        candidates = _data_dir_candidates()
    for d in candidates or []:
        if d and _probe_writable(d):
            if cache:
                _DATA_DIR = d
            return d
    return None


def _migrate_from_exe_dir(name):
    """返回可写数据目录下 name 的路径；若该文件只在旧的 exe 目录存在则先复制过去。

    用于从"绿色版/机器级安装"平滑迁移：老用户 exe 同目录已有 config.json 或
    accounts.json，升级后数据目录变到 %LOCALAPPDATA%\\TeamVision 时不能丢设置与账户。
    冻结运行（onedir）时 exe 位于 `<安装根>\\fps-host\\`，而 onefile 时代的配置就在
    `<安装根>\\`，因此把安装根也作为旧位置候选，否则老用户升级后等于重置了设置。
    任何失败都静默（迁移只是尽力而为，绝不影响启动）。
    """
    base = data_dir() or exe_dir()
    path = os.path.join(base, name)
    legacy_candidates = []
    if getattr(sys, "frozen", False):
        parent = os.path.dirname(exe_dir())
        if parent:
            legacy_candidates.append(os.path.join(parent, name))
    legacy_candidates.append(os.path.join(exe_dir(), name))
    for legacy in legacy_candidates:
        if os.path.abspath(legacy) == os.path.abspath(path):
            continue
        if os.path.exists(path) or not os.path.exists(legacy):
            continue
        try:
            shutil.copyfile(legacy, path)
            try:
                import logger
                logger.get_logger().info(
                    "%s 已从 %s 迁移到可写数据目录 %s", name, os.path.dirname(legacy), base)
            except Exception:
                pass
        except OSError:
            pass
        break
    return path


def _config_path():
    """返回 config.json 的完整路径（可写数据目录，见 data_dir）。"""
    return _migrate_from_exe_dir("config.json")


def accounts_path():
    """返回 accounts.json 的完整路径（可写数据目录，见 data_dir）。"""
    return _migrate_from_exe_dir("accounts.json")


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


def _upgrade_config(raw):
    """就地把 1.x 遗留配置升级到当前结构版本；返回 (changes, notes)。

    changes = 实际改动（触发落盘 + 升级告警）；notes = 纯说明（只记 INFO，不落盘、
    不影响版本判定）——cfg-015（2026-09-24）：旧实现把"监听地址为回环，已启用双栈
    回环监听"这类**说明**也塞进 changes，于是 v1 配置本就合规也会落盘并打 WARNING
    "配置已升级到 v2（更安全的默认值）：监听地址为回环…"，用户以为监听被改过（D
    系列最需避免的误导排障点）。

    只处理「结构版本落后」这一种情况，且只在用户**显式**写了旧的安全默认值时才改
    （0.0.0.0 / 准入关闭），以免覆盖用户自己的选择；改动会逐条写进日志，用户照着
    就能在控制台改回去。迁移只发生一次：写回时同时写入 config_version。
    """
    try:
        # cfg-007（2026-09-20）：`Infinity` / `-Infinity` / `NaN` 是 Python json 默认
        # 接受的合法 JSON，而 `int(float('inf'))` 抛的是 OverflowError——旧实现只列
        # (TypeError, ValueError)，异常一路逃到 load_config 的兜底 except，
        # 触发 _recover_corrupt_config：整份用户配置被改名留存并写回默认值。
        # 一个数字写错就能让端口、频道列表、frp 配置、码率设置全部"消失"。
        # net-025 已给 safe_int/safe_float/normalize_region 补过 OverflowError，这处同族漏网。
        version = int(raw.get("config_version") or 1)
    except (TypeError, ValueError, OverflowError):
        version = 1
    if version >= CONFIG_VERSION:
        # C12（2026-09-11）：结构版本已是最新时没有迁移动作，但这里仍返回 []。
        # 版本号缺失/落后的情况由上面的分支写回（见下方 raw["config_version"]）。
        return [], []
    changes = []
    notes = []
    host = raw.get("host")
    if isinstance(host, dict):
        # cfg-014（2026-09-24）：归一化口径必须一致——旧实现"不安全值"用精确匹配、
        # "安全值"用 `.strip().lower()`，粘贴成 `" 0.0.0.0"`（首位空格）时迁移不触发、
        # 日志不提，而 host 侧 family 判定用的还是未归一串。
        raw_lh = host.get("listen_host")
        lh = raw_lh.strip().lower() if isinstance(raw_lh, str) else None
        if lh is not None and lh in ("", "0.0.0.0", "::"):
            host["listen_host"] = DEFAULT_CONFIG["host"]["listen_host"]
            changes.append("监听地址 0.0.0.0 → %s（只允许本机与 frp 隧道接入；"
                           "同时监听 127.0.0.1 与 [::1]，frpc 以 localhost 回连亦可达）"
                           % DEFAULT_CONFIG["host"]["listen_host"])
        elif lh in ("127.0.0.1", "localhost", "::1"):
            # A0（2026-09-11）：已是回环地址的配置无需改动（host 侧会做回环双栈监听），
            # 但若配了 frp 隧道则记一条说明，避免用户仍按旧文档以为"隧道必失效"。
            frp = host.get("frp")
            if isinstance(frp, dict) and (str(frp.get("token") or "").strip()
                                          or str(frp.get("tunnel_ids") or "").strip()):
                notes.append("监听地址为回环（%s），已启用 IPv4+IPv6 双栈回环监听以兼容 "
                             "frpc 的 localhost 回连" % host["listen_host"])
        auth = host.get("auth")
        if isinstance(auth, dict) and not auth.get("enabled", False):
            auth["enabled"] = True
            changes.append("账户准入 关闭 → 开启（观看端需注册/登录）")
            # B10（2026-09-11）：迁移的目标是"更安全"，不能顺手把可用性弄坏——准入一开，
            # 认证窗口就必须覆盖观看端"等界面就绪 + 等用户输凭据"的最坏耗时，否则会重新
            # 引入第 31 条「输对了也登录失败」（随包 config.json 一度写的就是 60 秒）。
            try:
                current = float(auth.get("auth_timeout", AUTH_TIMEOUT_DEFAULT_S))
            except (TypeError, ValueError):
                current = 0.0
            if current < AUTH_TIMEOUT_MIN_S:
                auth["auth_timeout"] = AUTH_TIMEOUT_DEFAULT_S
                changes.append("认证窗口 %s → %d 秒（覆盖观看端最坏等待 ≈180 秒）"
                               % (("%g" % current) if current else "非法值",
                                  AUTH_TIMEOUT_DEFAULT_S))
    if not changes:
        # C12：即便本次没有实际改动，也要补写结构版本号，避免每次启动都重跑升级判定
        if version < CONFIG_VERSION:
            raw["config_version"] = CONFIG_VERSION
        return [], notes
    raw["config_version"] = CONFIG_VERSION
    return changes, notes


def _persist_upgrade(path, raw, changes, silent=False, notes=()):
    """把升级后的配置写回磁盘并记录改动清单；写失败只告警（下次启动会再试一次）。

    silent=True（C12：只补写缺失的结构版本号、无实际迁移动作）时不打印"配置已升级"；
    notes 是纯说明（cfg-015），始终只记 INFO，绝不冒充"已升级"。
    """
    try:
        with _config_lock:
            # cfg-001：与 save_config 同一套跨进程保护（唯一临时名 + replace 重试）
            fd = acquire_file_lock(path)
            if fd is None:
                raise OSError("未能取得 config.json 的跨进程锁，本次升级写回已放弃")
            tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(raw, f, ensure_ascii=False, indent=2)
                _replace_retry(tmp, path)
            finally:
                # cfg-006（2026-09-20）：临时名唯一（PID+线程），写失败后旧实现不清理，
                # 下次启动也不会覆盖回收 → 磁盘上永久留下含**明文 frp token**的残渣
                # （实测留下 config.json.<pid>.<tid>.tmp，内含 SECRET-TOKEN）。
                # save_config 的写盘分支有 remove，这里此前漏了——清理口径不对称。
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                release_file_lock(path, fd)
    except OSError as e:
        try:
            import logger
            logger.get_logger().warning("配置升级未能写回 %s（%s），本次运行按升级后的值生效", path, e)
        except Exception:
            pass
        return
    try:
        import logger
        if notes:
            logger.get_logger().info("配置说明：%s", "；".join(notes))
        if not silent:
            logger.get_logger().warning(
                "配置已升级到 v%d（更安全的默认值）：%s。需要局域网直连或匿名观看时，"
                "请在共享端控制台的「传输参数」里改回并重新应用。",
                CONFIG_VERSION, "；".join(changes))
    except Exception:
        pass


def _normalize_dict_sections(base, out, path=""):
    """递归校验「默认配置里是字典的节」在合并结果里必须仍是字典（ui-022 / auth-022）。

    在 load_config 出口处做一次，而不是让 host / viewer / 面板 / 诊断包每个读取点
    各自 isinstance 兜底——auth-022 的危害恰恰来自"兜底取向各写各的"：同一条
    `"auth": false` 让准入 fail-open、注册策略却按默认 open，两道开关一起失效。
    非字典的节回退为默认子树（与"用户没写这一节"等价）并记 WARNING。

    cfg-007 之外这里还顺带挡住 `_deep_merge` 的语义空洞：它只对 None 跳过，
    非字典的非 None 值会原样覆盖默认字典（1004 行）。
    """
    fixed = 0
    for key, bval in base.items():
        if not isinstance(bval, dict) or not isinstance(out, dict):
            continue
        sub = out.get(key)
        here = "%s.%s" % (path, key) if path else key
        if not isinstance(sub, dict):
            if sub is not None:
                _log_warning("配置节 %s 不是对象（%r），整体回退默认值", here, sub)
            out[key] = copy.deepcopy(bval)
            fixed += 1
            continue
        fixed += _normalize_dict_sections(bval, sub, here)
    return fixed


def _atomic_write_json(path, obj):
    """原子写一份 JSON 配置（跨进程锁 + 唯一临时名 + os.replace）；返回是否成功。

    cfg-011（2026-09-24）：`load_config` 首建与 `_recover_corrupt_config` 原先直接
    `open(path, "w")` 截断写，绕过 cfg-001/004 的"锁 + tmp + os.replace"——host 与
    viewer 设计上同目录并发首启时对端会读到半截文件，随即判损坏、改名留存并把整份
    配置重置为默认；恢复期与对端读写重叠同理。与 `save_config` 的 docstring
    "原子写回"自相矛盾。
    """
    fd = acquire_file_lock(path)
    if fd is None:
        _log_warning("config.json 未取得跨进程锁，放弃写入 %s", path)
        return False
    try:
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
            _replace_retry(tmp, path)
            return True
        except Exception as e:
            # cfg-012 同款：清理放 finally 语义（非 OSError 也要清 tmp 并留痕）
            try:
                os.remove(tmp)
            except OSError:
                pass
            _log_warning("config.json 写入失败 %s（%s）", path, e)
            return False
    finally:
        release_file_lock(path, fd)


def load_config():
    """加载配置：config.json 不存在时先写入 DEFAULT_CONFIG 再返回其深拷贝；
    存在时读取、做一次结构升级（见 _upgrade_config）并与 DEFAULT_CONFIG 深合并后返回。

    第 16 条：文件损坏（非法 JSON / 顶层非字典 / 合并异常）时不再抛出原始
    traceback 让程序起不来，而是把损坏文件重命名留存、回退默认配置并重新落盘。
    第 102 条：写入失败（目录只读/磁盘满）也只告警并返回默认配置——启动阶段
    绝不能因为"存不下配置"而崩溃（windowed 打包下表现为静默退出、无日志）。
    """
    path = _config_path()
    if not os.path.exists(path):
        # cfg-011：首建也走原子替换 + 跨进程锁（详见 _atomic_write_json）
        if not _atomic_write_json(path, DEFAULT_CONFIG):
            _log_warning("config.json 无法写入 %s，本次运行使用默认配置", path)
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("config.json 顶层不是对象（实际 %s）" % type(cfg).__name__)
        before_version = cfg.get("config_version")
        changes, notes = _upgrade_config(cfg)
        merged = _deep_merge(DEFAULT_CONFIG, cfg)
        # ui-022 / auth-022（2026-09-20）：合并后再统一做一次"应为字典的节"类型校验，
        # 让所有下游读取点（get_snapshot、面板构造、诊断包、采集热路径）不必各写各的
        # isinstance，也不会出现"这一处兜成 False、那一处兜成 open"的取向打架。
        _normalize_dict_sections(DEFAULT_CONFIG, merged)
        if changes:
            _persist_upgrade(path, cfg, changes, notes=notes)
        elif (cfg.get("config_version") == CONFIG_VERSION
              and before_version != CONFIG_VERSION):
            # C12：只是补写缺失的结构版本号（值本身已是安全默认），静默落盘，
            # 避免每次启动都重跑升级判定。
            _persist_upgrade(path, cfg, [], silent=True, notes=notes)
        return merged
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
    # cfg-011：回写默认配置同样走原子路径（旧实现截断写：与对端读写重叠时
    # 可能喂出半截文件 → 下一次启动再判损坏 → 恢复循环）
    _atomic_write_json(path, DEFAULT_CONFIG)
    try:
        import logger
        suffix = "，原文件已留存为 %s" % os.path.basename(backup) if backup else ""
        logger.get_logger().warning("config.json 损坏（%s），已回退默认配置%s", exc, suffix)
    except Exception:
        pass


def acquire_file_lock(path, timeout=2.0):
    """跨进程互斥（cfg-001 → cfg-004/005 重做）：返回锁 fd，拿不到返回 None。

    旧实现是"`O_CREAT|O_EXCL` 排他创建 + 完成后删除"，并用**锁文件年龄 >10 秒**判定
    陈旧再 `os.remove` 接管。两个洞：
      ① 接管阈值 10 秒 > 等待上限 2 秒 —— 持有者崩溃/被强杀后的整个 10 秒窗口里，
         每个写入者都等满 2 秒然后**无保护地写**，恰好是 cfg-001/auth-009 要根治的
         场景（host 与 viewer 互相陈旧覆盖、双开实例抹掉账户）；
      ② Windows 语义 —— `os.remove` 作用于仍被其它进程以句柄打开的文件抛
         `PermissionError [WinError 32]`（Python 的 os.open 不带 FILE_SHARE_DELETE），
         而这里 `except OSError: pass` 直接吞掉 → **活着的竞争者根本无法被接管**，
         fail-open 是必然路径而非小概率。
    现在改用 OS 级字节范围锁（msvcrt.locking）：进程退出或被强杀时句柄由内核关闭、
    锁立即释放，因此既不需要"陈旧判定"（cfg-005 的墙钟回拨问题同此消失），也不存在
    无保护写。锁文件留在磁盘上不删（删除本身就是那个竞争点）。
    拿不到锁**不再放行**：调用方必须放弃本次写并告警（fail-closed）。
    """
    lock_path = path + ".lock"
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    except OSError as e:
        _log_warning("跨进程锁文件 %s 打开失败（%s）", lock_path, e)
        return None
    if _msvcrt is None:
        return fd                      # 非 Windows 退化为"必成功"（本项目只发 Windows 产物）
    while True:
        try:
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                try:
                    os.close(fd)
                except OSError:
                    pass
                return None
            time.sleep(0.05)


def release_file_lock(path, fd):
    """释放 `acquire_file_lock` 持有的锁（幂等；path 形参保留以兼容调用点）。"""
    if fd is None:
        return
    if _msvcrt is not None:
        try:
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    try:
        os.close(fd)
    except OSError:
        pass


def _replace_retry(src, dst, attempts=5, delay=0.05):
    """os.replace 重试封装（cfg-001）：Windows 上目标被他人持有句柄（杀软扫描 /
    另一实例正在读）时 replace 抛 PermissionError，短暂重试即可成功。"""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def save_config(cfg, section=None):
    """以 ensure_ascii=False、indent=2 原子写回 config.json（先写临时文件再替换）。

    第 17 条：host 与 viewer 同目录同时运行，各持完整配置快照并整文件回写，会用
    陈旧快照静默还原对方刚改的设置（用户感知“设置老是自己消失”）。传入 section
    （如 "host"/"viewer"）时改为读-改-写：重新读取磁盘上的最新配置，只把本进程拥有
    的 section 覆盖上去再落盘，从而保留另一进程的 section。section=None 时维持原
    整文件回写行为。

    cfg-001（2026-09-18）：第 17 条修复不彻底——进程内锁对**双开实例**互不排斥，
    且读-改-写用固定 `.tmp`、`os.replace` 撞读即静默失败。现在整体放进跨进程文件锁，
    临时文件名唯一（PID+线程），replace 失败重试。
    """
    path = _config_path()
    with _config_lock:
        fd = acquire_file_lock(path)
        if fd is None:
            # cfg-004（2026-09-20）：拿不到跨进程锁时旧实现**照常完成整份写盘**
            # （fail-open），而 Windows 下活锁根本无法接管 → 竞争期间每次都无保护地写，
            # 恰好重现 cfg-001 要根治的"host 与 viewer 互相陈旧覆盖"。
            # 现在放弃本次写并明确告警：内存里的改动仍然生效，用户重试一次即可落盘，
            # 远比另一进程的整份配置被踩掉好。
            _log_warning("config.json 未取得跨进程锁（另一实例正持有），"
                         "本次写盘已放弃：改动仅在本次运行内生效，请稍后重试保存")
            return False
        try:
            out = cfg
            if section is not None:
                base = {}
                if os.path.exists(path):
                    try:
                        with open(path, encoding="utf-8") as f:
                            base = json.load(f)
                    except Exception as e:
                        # cfg-010（2026-09-24）：旧实现把"读不到"当"没有"（base={}）继续写，
                        # 整份配置塌缩成"只含本进程这一节"（对端整节设置、update_check 等
                        # 无提示消失），且与 load_config 的恢复口径不对称。读基线失败时
                        # 放弃本次写：内存里的改动仍生效，绝不覆盖对端数据。
                        _log_warning("config.json 读基线失败（%s），为不覆盖对端设置本次写盘已放弃", e)
                        return False
                    if not isinstance(base, dict):
                        _log_warning("config.json 顶层不是对象（%s），本次写盘已放弃",
                                     type(base).__name__)
                        return False
                base[section] = cfg.get(section)
                out = base
            # 第 102 条：写盘失败（目录只读/磁盘满/杀软占用）只记一条 ERROR 并返回。
            # 调用方在 Tk 回调/接收线程里，抛出去要么被 Tk 静默吞掉（用户看到"改了没反应"），
            # 要么直接打死接收线程；吞掉 + 明确日志既保住本次运行的行为，又留下可排查的痕迹。
            tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(out, f, ensure_ascii=False, indent=2)
                _replace_retry(tmp, path)
            except Exception as e:
                # cfg-012（2026-09-24）：清理原先只在 `except OSError` 分支——配置里
                # 任一字符串含转义游离代理位时 json.dump 抛 UnicodeEncodeError，
                # 临时文件（含明文 frp token）永久留在磁盘上，且每次保存必失败而
                # 日志零线索（异常不被任何 except 记 ERROR）。改为非 OSError 也清理并留痕。
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                try:
                    import logger
                    logger.get_logger().error(
                        "config.json 写入失败（%s）：本次改动仅在内存生效，重启后丢失", e)
                except Exception:
                    pass
        finally:
            release_file_lock(path, fd)
