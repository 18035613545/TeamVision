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
import sys
import threading
import time

import logger
import pipeline

from common import (
    load_config, save_config, server_handshake, pack_frame, pack_video, exe_dir, APP_NAME,
    enable_dpi_awareness, accounts_path,
    recv_msg, send_msg, tune_socket, parse_message, MSG_CTRL, MSG_FRAME, MSG_VIDEO,
    VIDEO_FLAG_KEY, CODEC_H264, parse_video,
)

# 采集/静止检测公共件从 screen.py 导入并保持同名（viewer 上传端复用同一实现）；
# 编码器生命周期与 JPEG 回退由 pipeline.FrameEncoder 提供（第 110 条，与 share.py 共用）。
# encode_bgr 本模块已不再直接调用，但按 test_multiview.TestScreenExtraction 锁定的
# "host 转发 screen 公共件"契约继续保留（外部/测试按 host.encode_bgr 取用）。
from screen import (
    CaptureManager, STILL_STEP, downsample_frame, motion_changed,
    still_gate_update, calc_output_size, encode_bgr,
)

#: 全局日志器（未初始化时按默认配置惰性初始化）
log = logger.get_logger()

#: 第 100 条：握手阶段（等客户端魔数）的总时限。
#: accept() 出来的 socket 超时是 None，server_handshake → recv_exact(deadline=None)
#: 会永久阻塞；不发魔数的连接每个永久占用一个线程 + 一个 fd，而 accept 循环没有
#: 并发上限，一次端口扫描即可把服务耗死。
HANDSHAKE_TIMEOUT_S = 10.0

#: 第 100 条：单个客户端"发送耗时"样本的有效期（秒）。超过该时长没有再发送过的
#: 客户端，其 last_send_ms 不再计入 ABR 的拥塞判据——否则一个已经切走/不再收帧的
#: 慢客户端会把 effective_send_ms 永久钉在高位，使码率/缩放/帧率一路降到地板且永不回升。
SEND_MS_WINDOW_S = 2.0


def _sanitize(text, token):
    """把文本中可能出现的 frp token 替换为打码形式，防止敏感信息写入日志。

    第 100 条：token 也可能是非字符串（config.json 里写成 `"token": 123456`），
    旧实现直接 `token in text` 会抛 TypeError；而调用它的 except 分支里又调一次同一
    函数，于是异常二次抛出、frpc 输出读取线程直接死亡（管道写满后 frpc 阻塞、
    隧道停摆而 GUI 仍显示"运行中"）。这里先把 token 统一转成字符串。
    """
    if not isinstance(text, str):
        return text
    if token is None:
        return text
    token = str(token)
    if not token:
        return text
    if token in text:
        return text.replace(token, logger.mask_secret(token))
    return text


def _safe_int(value, default, lo=None, hi=None, warn=True):
    """把配置值安全转为 int：非法（字符串/None/布尔/超出范围）时回退 default。

    第 100 条：`config.json` 手改成 `"fps": "60"` 时，`slot["fps"]` 是 str，采集线程
    的 `fps_now > 0` 会抛 TypeError 并把线程直接打死（服务仍在 accept、观看端能收
    pong，但一帧都没有）。所有来自配置的数值都经此转换。

    warn=False 用于每秒/每帧执行的热路径（避免同一条告警刷满日志）。
    """
    if isinstance(value, bool):
        value = int(value)
    try:
        out = int(value)
    except (TypeError, ValueError):
        if warn:
            log.warning("配置值 %r 不是整数，回退默认值 %r", value, default)
        out = int(default)
    if lo is not None and out < lo:
        out = int(lo)
    if hi is not None and out > hi:
        out = int(hi)
    return out


def _safe_float(value, default, lo=None, hi=None, warn=True):
    """把配置值安全转为 float：非法时回退 default，并按需夹到 [lo, hi]。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        if warn:
            log.warning("配置值 %r 不是数字，回退默认值 %r", value, default)
        out = float(default)
    if lo is not None and out < lo:
        out = float(lo)
    if hi is not None and out > hi:
        out = float(hi)
    return out


def _normalize_region(region):
    """校验并规范化采集区域；非法（缺键/非数字/宽高非正）时返回 None（全屏）。

    第 100 条：旧实现把配置里的 region 原样透传，缺键时 screen.CaptureManager.grab
    每次 KeyError 被吞成 None（画面永久冻结、无任何提示），host_ui._region_text 也会
    KeyError 让整个图形控制台起不来。
    """
    if region is None:
        return None
    if not isinstance(region, dict):
        log.warning("采集区域配置不是对象（%r），按全屏处理", region)
        return None
    try:
        left = int(region["left"])
        top = int(region["top"])
        width = int(region["width"])
        height = int(region["height"])
    except (KeyError, TypeError, ValueError):
        log.warning("采集区域配置缺少 left/top/width/height 或非整数（%r），按全屏处理", region)
        return None
    if left < 0 or top < 0 or width <= 0 or height <= 0:
        log.warning("采集区域取值非法（%r），按全屏处理", region)
        return None
    return {"left": left, "top": top, "width": width, "height": height}


def _hash_password(password, salt_hex):
    """pbkdf2_hmac(sha256) 加盐哈希，迭代 10 万次，返回十六进制摘要。"""
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return dk.hex()


class AccountManager:
    """服务端账户注册/认证与 accounts.json 持久化（线程安全，不存明文密码）。"""

    def __init__(self):
        self._lock = threading.Lock()
        # 第 102 条：账户文件与 config.json 一样落在**可写**数据目录。安装在
        # Program Files 后非管理员运行时 exe 目录只读，原实现写这里会让注册直接失败。
        self._path = accounts_path()
        self._users = {}  # 用户名 -> {salt, hash, created, last_login}
        self._load()

    def _load(self):
        if not os.path.exists(self._path):
            self._users = {}
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("顶层不是对象")
            users = data.get("users", {})
            if not isinstance(users, dict):
                raise ValueError("users 字段不是对象")
            self._users = users
        except (OSError, ValueError) as e:
            # 第 27 条：accounts.json 损坏时不再静默置空（否则下次注册/登录会把空
            # 字典回写覆盖原文件 → 全体账号蒸发且无日志）。把损坏文件改名留存以便
            # 手动恢复，记一条错误日志，本进程从空账户表重新开始。
            self._users = {}
            backup = "%s.corrupt-%d" % (self._path, int(time.time()))
            try:
                os.replace(self._path, backup)
                log.error("accounts.json 损坏（%s），已留存为 %s 并重置账户表",
                          e, os.path.basename(backup))
            except OSError:
                log.error("accounts.json 损坏（%s）且无法改名留存，已重置账户表", e)

    def _save(self):
        """原子写回账户表；返回是否成功（写失败只告警，绝不改变认证结论）。

        第 102 条：原实现让 OSError 直接抛出——`authenticate` 末尾记 last_login 时
        磁盘满/目录只读会让**口令正确的用户被拒**（且提示是"认证异常"而不是"无法写入
        账户文件"），`register` 则留下"内存里已注册、磁盘上没有"的假成功。
        """
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"users": self._users}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
            return True
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            log.error("accounts.json 写入失败（%s）：账户改动未持久化", e)
            return False

    def register(self, user, password):
        """注册新账户，返回 (ok: bool, 提示语)。"""
        user = (user or "").strip()
        if not user or not password:
            return False, "用户名与密码不能为空"
        # 第 22 条：PBKDF2（10 万次迭代）在锁外计算，避免并发注册/错误口令把全局
        # 账户锁占满导致所有正常登录排队、host CPU 打满。
        salt = secrets.token_hex(16)
        pw_hash = _hash_password(password, salt)
        with self._lock:
            if user in self._users:
                return False, "用户名已存在"
            self._users[user] = {
                "salt": salt,
                "hash": pw_hash,
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            # 第 102 条：写盘失败要回滚内存，否则本次运行能登录、重启后就"账号不存在"
            if not self._save():
                self._users.pop(user, None)
                return False, "无法写入账户文件（磁盘或权限问题），注册未生效"
        log.info("账户注册成功: %s", user)
        return True, "注册成功"

    def authenticate(self, user, password):
        """认证账户，返回 (ok: bool, 提示语)；账号不存在与密码错误给出不同提示。"""
        user = (user or "").strip()
        # 第 22 条：锁内只取校验字段，PBKDF2（10 万次迭代）放到锁外计算，避免并发
        # 错误口令占满全局账户锁让所有正常登录排队、host CPU 打满。
        with self._lock:
            record = self._users.get(user)
            if not isinstance(record, dict):
                return False, "账号不存在"
            salt = record.get("salt")
            expected = record.get("hash")
        # 第 27 条：单条记录字段损坏（缺 salt/hash 或 salt 非十六进制）时优雅拒绝，
        # 不再让 bytes.fromhex 抛错被当成“认证异常”踢连接（否则该账号永远登不进）。
        if not isinstance(salt, str) or not isinstance(expected, str):
            log.warning("账户 %s 记录损坏（缺 salt/hash），拒绝登录", user)
            return False, "账号不存在"
        try:
            matched = hmac.compare_digest(expected, _hash_password(password, salt))
        except (ValueError, TypeError) as e:
            log.warning("账户 %s 口令校验失败（记录损坏）：%s", user, e)
            return False, "账号不存在"
        if not matched:
            return False, "用户名或密码错误"
        with self._lock:
            record = self._users.get(user)
            if isinstance(record, dict):
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
        # 第 107 条（B8）：上行令牌桶（字节 + 帧数）。共享成员的每一帧都会被
        # _fwd_member_frame 复制给所有订阅者，单个成员以线速发 32MB 帧即可把
        # host 的上行与 CPU 打满；这里按成员限速，超限丢帧并计数。
        self._up_tokens = 0.0     # 剩余字节令牌
        self._up_frames = 0.0     # 剩余帧令牌
        self._up_at = 0.0         # 上次补充时刻（0=未初始化 → 首次填满，避免丢首帧）
        self._up_dropped = 0      # 累计被限速丢弃的上行帧
        self._up_warned_at = 0.0  # 上次限速告警时刻（10 秒去重）

    def allow_upstream(self, nbytes, max_kbps, max_fps, now=None):
        """上行令牌桶：放行返回 True，超限返回 False（第 107 条 / B8）。

        桶容量 = **2 秒**额度（带宽 max_kbps、帧率 max_fps），按时间线性补充。
        给 2 秒余量是为了容忍关键帧突发（关键帧通常比均值大数倍，1 秒额度会误伤
        正常共享的首帧）；持续超限才会被削，所以 1:N 放大仍被有效限制。
        """
        now = now if now is not None else time.monotonic()
        burst_bytes = max(1.0, max_kbps * 250.0)      # kbps -> bytes/s = ×1000/8，容量 2 秒
        max_fps = max(1.0, float(max_fps))
        if self._up_at <= 0.0:
            self._up_tokens = burst_bytes             # 首次：填满，不丢首帧
            self._up_frames = max_fps
        else:
            elapsed = max(0.0, now - self._up_at)
            self._up_tokens = min(burst_bytes, self._up_tokens + elapsed * burst_bytes)
            self._up_frames = min(max_fps, self._up_frames + elapsed * max_fps)
        self._up_at = now
        if self._up_tokens < nbytes or self._up_frames < 1.0:
            self._up_dropped += 1
            return False
        self._up_tokens -= nbytes
        self._up_frames -= 1.0
        return True

    def note_rx(self, now=None):
        """记录一次客户端活跃（收到了它的任何数据，用于半开连接检测）。"""
        self.last_rx_at = now if now is not None else time.monotonic()

    def send_ctrl(self, sock, obj, send_timeout=2.0):
        """发送一条控制消息（与帧发送共用写锁，防字节交错）。

        第 54 条：sendall 前设有限发送超时。此前从未收过帧的客户端、或 handle_client
        进 recv 循环前置 None 的客户端，其 socket timeout 仍是 None；对端停止读取但
        ping 线程仍活时 sendall 会**永久阻塞** → 持 _write_lock 不放 → 任何 roster
        事件都挂在它上面，新客户端的 handle_client 在进 recv 循环前就卡死、永不回
        pong → 对端判停滞重连 → 线程堆积。超时抛 socket.timeout（OSError 子类），
        由调用方按发送失败处理。recv 循环用 select 门控，不受此超时影响。
        """
        with self._write_lock:
            try:
                sock.settimeout(send_timeout)
            except OSError:
                pass
            send_msg(sock, obj)

    def enqueue_frame(self, frame):
        """把最新一帧放入该客户端的发送队列，旧帧直接丢弃，绝不阻塞广播线程。

        第 7 条：返回是否因队列满而丢弃了旧帧。视频订阅者据此触发「丢帧→强制关键帧」
        愈合（route_frame 置 need_key 门控 + force_key 强制 IDR），避免被丢弃参考帧的
        后续 P 帧在观看端解出花屏并持续到下一个周期关键帧（约 2 秒）。
        第 104 条（B2）：get+put 必须原子。同一客户端会被**两个线程**并发入队——
        本地源 `_send_loop` 与成员转发 `_fwd_member_frame`——原来的
        get_nowait()/put_nowait() 交错时（T1 get → T2 get 空 → T2 put → T1 put 满）
        会静默丢掉 T1 的帧而 `dropped` 仍为 False，愈合门控不触发 → 花屏持续到下一个
        周期 IDR。这里用 `_lock` 把"取旧帧 + 放新帧"整体串行化（锁内无阻塞调用）。
        """
        with self._lock:
            dropped = False
            try:
                self.send_queue.get_nowait()
                now = time.monotonic()
                self.dropped_frames += 1
                self.last_drop_at = now
                dropped = True
            except queue.Empty:
                pass
            try:
                self.send_queue.put_nowait(frame)
            except queue.Full:
                # 队列容量为 1 且刚清空，正常不可达；真出现也绝不阻塞
                pass
            return dropped

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

    def recent_send_ms(self, now=None, window=SEND_MS_WINDOW_S):
        """最近 window 秒内测得的一次发送耗时（毫秒）；过期或无样本返回 0.0。

        第 100 条：`last_send_ms` 只写不失效——客户端切走观看源或停止收帧后，这个
        样本会永久留在 ABR 的 `client_avg_ms` 里，使 `congested` 恒真、回升分支永不
        可达（码率/缩放/帧率被钉在地板）。这里按 `last_send_at` 做时效过滤。
        """
        now = now if now is not None else time.monotonic()
        with self._lock:
            if self.last_send_at > 0 and (now - self.last_send_at) <= window:
                return self.last_send_ms
            return 0.0

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
        # getattr：测试替身可能只实现被断言到的那部分接口（与上方 share_id/stop_sender
        # 同一防御口径，第 105 条）。
        unregister = getattr(runtime, "_unregister_sharer", None)
        if callable(unregister):
            unregister(removed)
    stop_sender = getattr(removed, "stop_sender", None)
    if callable(stop_sender):
        stop_sender()
    close = getattr(sock, "close", None)   # 同防御口径：内存替身 socket 可能没有 close
    if callable(close):
        try:
            close()
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


def _send_ctrl_or_drop(runtime, sock, info, obj, reason="控制消息写入失败"):
    """发送控制消息；失败即移除连接（第 105 条 / B4）。

    `sendall` 在超时（socket.timeout 是 OSError 子类）或对端不读时可能**已经写出**
    「kind+长度头 + 半截 JSON」。此时连接若仍留在 clients 里，之后每一帧都会接在
    半截消息后面 → 观看端 parse_message 读到错位字节、抛「非法的消息类型」→ 断开
    重连（用户看到周期性掉线）。字节流一旦可能错位就无法恢复，唯一安全的处理是
    关闭连接让观看端重连（重连成本 ~1 秒，错位抖动是永久性的）。

    注意：调用方**不能**在持有 `_roster_lock` 时调用本函数——`_drop_client` →
    `_unregister_sharer` → `broadcast_roster` 会再次获取同一把非可重入锁而自死锁。
    """
    try:
        info.send_ctrl(sock, obj)
        return True
    except OSError as e:
        log.debug("向 %s 发送控制消息失败，按连接损坏处理: %s", info.addr, e)
        _drop_client(runtime, sock, info, reason)
        return False



class FrpManager:
    """樱花 frpc 子进程生命周期管理器：start/stop/is_running，供 GUI 随时启停。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self._proc = None
        self._external_running = False  # 由 SakuraFrpService/Launcher 托管时不再重复拉起 frpc
        self._last_frpc_line = ""  # 第 63 条：frpc 最近一行输出，验活失败时回报原因
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
            # 第 100 条：token/tunnel_ids 可能是配置里的数字（合法 JSON），统一成字符串，
            # 避免下游 _sanitize/打码/子进程参数遇到非字符串类型。
            token = "" if token is None else str(token).strip()
            tunnel_ids = "" if tunnel_ids is None else str(tunnel_ids).strip()
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
            # 第 62 条：本进程自己拉起了 frpc → 必须清掉「外部托管」latch。否则该标志一旦
            # 在早先某次 start() 检测到樱花服务时置真就永不复位，之后 stop() 会走「樱花启动器
            # 正在托管」分支直接返回、永不 terminate 这个自spawn 的 frpc → 退出后孤儿进程占着
            # 隧道与本地端口，下次 start() 又起第二个 frpc（同 token 同隧道，樱花侧互踢）。
            # 不变量：_external_running 与自持有的 _proc 互斥。
            self._external_running = False
            self._proc = proc
        # 第 63 条：出锁后验活，避免 1 秒轮询期间持 _lock 阻塞 is_running/stop。
        # 轮询约 1 秒确认 frpc 没有秒退（token/隧道 ID 错误时通常立即退出）；若退出，
        # 等读取线程 drain 完输出，把最后一行作为失败原因回报，而不是假报「已启动」。
        reader = threading.Thread(
            target=self._read_output, args=(proc, token), daemon=True)
        reader.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.05)
        if proc.poll() is not None:
            reader.join(timeout=0.5)
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            detail = self._last_frpc_line or "详见日志（常见为 token 或隧道 ID 错误）"
            return False, "frpc 启动后立即退出（rc=%s）：%s" % (
                proc.returncode, detail)
        return True, "frpc 已启动（隧道: %s）" % tunnel_ids

    def _read_output(self, proc, token):
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                safe = _sanitize(line, token)
                self._last_frpc_line = safe  # 第 63 条：留存最后一行供验活失败回报
                log.info("frpc: %s", safe)
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


#: 区分「参数未提供」与「显式传 None」的哨兵（第 32 条：region=None 表示清空回全屏，
#: 而非「未指定、保持原值」）。
_UNSET = object()


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
        # 第 103 条（B1）：连接上限与单条消息总时限。原来 accept 循环没有任何并发
        # 上限，每个连接又能把 rx_buf 攒到 MAX_PAYLOAD(32MB) 且永不过期——一次端口
        # 扫描即可把线程/fd/内存耗尽。_conn_count 统计**含握手中**的连接（clients
        # 只在握手完成后才入册，仅看它漏掉握手阶段的洪水）。
        net_cfg = (cfg.get("host") or {}).get("net", {}) if isinstance(cfg.get("host"), dict) else {}
        if not isinstance(net_cfg, dict):
            log.warning("配置 host.net 不是对象（%r），按默认网络参数处理", net_cfg)
            net_cfg = {}
        self.max_clients = _safe_int(net_cfg.get("max_clients", 32), 32, 1, 4096)
        self.msg_timeout = _safe_float(net_cfg.get("msg_timeout_s", 15.0), 15.0, 1.0, 600.0)
        self._conn_lock = threading.Lock()
        self._conn_count = 0
        # 第 104 条（B3）：向 peer 共享者转发关键帧请求的冷却表（share_id -> 时刻）
        self._peer_key_lock = threading.Lock()
        self._peer_key_at = {}
        # 第 55 条：串行化 roster 广播（快照 + 发送），防陈旧快照覆盖新快照。
        # 与 clients_lock 分离：clients_lock 只在快照瞬间持有，_roster_lock 跨越
        # 整个发送过程；获取顺序恒为 _roster_lock → clients_lock，无环、不死锁。
        self._roster_lock = threading.Lock()
        host = cfg["host"]
        # 第 107 条（B8）：单个共享成员的上行限额。0=自动：带宽取目标码率×2，
        # 帧率取 2×fps（share.py 按 host.fps 上传，固定 8fps 会误伤正常共享）。
        codec_sec = host.get("codec", {}) if isinstance(host.get("codec"), dict) else {}
        base_kbps = _safe_int(codec_sec.get("bitrate_kbps", 2500), 2500, 64)
        self.up_max_kbps = _safe_int(net_cfg.get("upstream_max_kbps", 0), 0, 0) \
            or max(600, 2 * base_kbps)
        self.up_max_fps = _safe_int(net_cfg.get("upstream_max_fps", 0), 0, 0) \
            or max(8, 2 * _safe_int(host.get("fps", 30), 30, 1, 240))
        capture = host.get("capture", {})
        self.slot = {
            "jpeg": None,
            "ts": 0,
            "video": None,       # H.264/H.265 Annex-B 帧（视频编码路径）
            "video_ts": 0,       # 该视频帧的采集时刻微秒（packet.pts 透传）
            "is_key": False,     # 视频帧是否关键帧
            "video_codec": CODEC_H264,  # 视频帧编码类型（协议 codec 字节，H.264/HEVC）
            "quality": _safe_int(host.get("jpeg_quality", 80), 80, 1, 100),
            "scale": _safe_float(host.get("scale", 1.0), 1.0, 0.05, 4.0),
            "fps": _safe_int(host.get("fps", 30), 30, 1, 240),
            "encode_ms": 0.0,
            "bitrate": 0,        # 当前视频编码码率（bps，0=JPEG 模式）
            "out_w": 0,        # 最近一帧实际编码输出宽度（target_width 压制后）
            "out_h": 0,        # 最近一帧实际编码输出高度
            "monitor": _safe_int(capture.get("monitor", 1), 1, 0),
            "region": _normalize_region(capture.get("region")),
            "backend": "dxgi" if capture.get("backend") == "dxgi" else "mss",
        }
        self.slot_lock = threading.Lock()
        self.force_key = False  # 观看端请求关键帧（slot_lock 保护），编码线程消费后清空
        self.video_encoder = None  # 共享的 VideoEncoder 实例（编码线程创建，perf 线程调码率）
        self.codec_lock = threading.Lock()
        # 当前 ABR 目标码率（bps）：ABR 调整后更新，编码线程重建编码器时沿用，
        # 避免重建回到基础码率导致码率震荡
        self.target_bitrate = _safe_int(
            host.get("codec", {}).get("bitrate_kbps", 2500), 2500, 64) * 1000
        self.stats = {"capture_fps": 0, "encode_fps": 0, "send_fps": 0,
                  "encode_ms": 0.0, "avg_send_ms": 0.0,
                  "codec_name": "", "keyint": 0,
                  "keyframe_total": 0, "abr_changes": 0, "still": False}
        self.stats_lock = threading.Lock()
        self.accounts = AccountManager()
        self.frp = FrpManager(cfg)
        self._next_share_id = 0  # 共享成员序号（连接递增分配，断开不复用）
        self._server = None
        self._server_thread = None
        # 第 18 条：服务线程致命启动错误（如端口被占）置此，供 GUI 显示而非静默死亡
        self.startup_error = None
        # 第 19 条：服务实际绑定地址（双栈为 "0.0.0.0"，指定地址时为该地址），供 GUI 诚实显示
        self.bound_addr = ""

    def start_server(self):
        """后台线程启动 TCP 服务与采集/发送流水线。"""
        self._server_thread = threading.Thread(target=run_server, args=(self,), daemon=True)
        self._server_thread.start()

    def acquire_conn(self):
        """占用一个连接名额；已达上限返回 False（调用方应关闭连接，第 103 条）。"""
        with self._conn_lock:
            if self._conn_count >= self.max_clients:
                return False
            self._conn_count += 1
            return True

    def release_conn(self):
        """归还连接名额（幂等，计数不小于 0，第 103 条）。"""
        with self._conn_lock:
            if self._conn_count > 0:
                self._conn_count -= 1

    def conn_count(self):
        """当前连接数（含握手中）。"""
        with self._conn_lock:
            return self._conn_count

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
        """向所有存活客户端推送一次 peers 名单（id/name/addr）。

        第 55 条：整个「快照 + 逐个发送」用 _roster_lock 串行化。此前注册/注销先释放
        clients_lock 再广播，线程 A 拿到旧快照 {1} 后可能卡在发送循环里，线程 B 已广播
        完 {1,2}，A 随后把陈旧的 {1} 送达 → 观看端源列表永久少一个正在共享的人（下次
        roster 事件前不自愈）。串行化后每次广播都在持锁时重新快照当前状态，最后一个
        完成的广播必然反映最新名单。send_ctrl 已有有限超时（第 54 条），持锁发送耗时
        有界，不会因某个卡死客户端长期占用 _roster_lock。
        """
        failed = []
        with self._roster_lock:
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
                except OSError as e:
                    # 第 105 条（B4）：可能已写出半截消息，连接必须移除
                    log.debug("向 %s 推送 roster 失败（按连接损坏处理）: %s", info.addr, e)
                    failed.append((sock, info))
        # 出 _roster_lock 后再移除：_drop_client → _unregister_sharer → broadcast_roster
        # 会再次获取同一把非可重入锁，持锁调用会自死锁。
        for sock, info in failed:
            _drop_client(self, sock, info, "控制消息写入失败")

    def update_capture(self, monitor=_UNSET, region=_UNSET, backend=_UNSET):
        """更新采集源配置并即时生效（写配置 + 写共享槽，采集线程每帧读取）。

        第 32 条：用 _UNSET 哨兵区分「未提供」与「显式传 None」。region=None 表示
        清空采集区域回到全屏（GUI「恢复全屏」依赖此语义），而非旧实现里的「跳过不改」。
        """
        capture = self.cfg["host"].setdefault("capture", {})
        if monitor is not _UNSET:
            monitor = _safe_int(monitor, 1, 0)
            capture["monitor"] = monitor
        if region is not _UNSET:
            # 第 100 条：写入前规范化，杜绝"缺键/负值区域"进配置与共享槽
            region = _normalize_region(region)
            capture["region"] = region
        if backend is not _UNSET:
            backend = "dxgi" if backend == "dxgi" else "mss"
            capture["backend"] = backend
        save_config(self.cfg, "host")
        with self.slot_lock:
            if monitor is not _UNSET:
                self.slot["monitor"] = monitor
            if region is not _UNSET:
                self.slot["region"] = region
            if backend is not _UNSET:
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
            "server_up": self._server is not None,
            "bound_addr": self.bound_addr or "0.0.0.0",
            "startup_error": self.startup_error,
        }


def handle_client(sock, addr, runtime):
    """处理单个客户端连接：先占连接名额，超限直接拒绝（第 103 条 / B1）。

    名额在这里占用与归还，覆盖"握手中"的连接——`runtime.clients` 只在握手完成后
    才入册，仅凭它判断会让握手阶段的洪水（10 秒超时内可并发上千条）绕过上限。
    """
    if not runtime.acquire_conn():
        log.warning("连接数已达上限 %d，拒绝来自 %s 的新连接",
                    getattr(runtime, "max_clients", 0), addr)
        try:
            sock.close()
        except OSError:
            pass
        return
    try:
        _handle_client_inner(sock, addr, runtime)
    finally:
        runtime.release_conn()


def _reply_ctrl(sock, info, obj, addr):
    """回一条控制消息；失败返回 False（调用方立即断开，第 105 条 / B4）。

    不能沿用裸 `info.send_ctrl(...)`：写超时后可能已写出半截消息，继续在该连接上
    收发会让后续每个字节都错位。断开由 _client_teardown 统一收尾（移除 + 注销共享
    + 关连接），观看端会自动重连。
    """
    try:
        info.send_ctrl(sock, obj)
        return True
    except OSError as e:
        log.debug("客户端 %s 控制消息发送失败（可能已写出半截消息）: %s", addr, e)
        return False


def _allow_upstream(runtime, info, payload, addr):
    """上行帧限速（第 107 条 / B8）：超限丢帧并计数，10 秒最多告警一次。

    限速发生在**转发之前**，所以超额流量既不会复制给订阅者，也不会触发
    _fwd_member_frame 的整帧重打包（那是每帧 2 次完整拷贝）。
    """
    if info.allow_upstream(len(payload), runtime.up_max_kbps, runtime.up_max_fps):
        return True
    now_m = time.monotonic()
    if now_m - info._up_warned_at >= 10.0:
        info._up_warned_at = now_m
        log.warning("客户端 %s 上行超限（已丢 %d 帧，上限 %d Kbps / %d fps），"
                    "超出部分丢弃", addr, info._up_dropped,
                    runtime.up_max_kbps, runtime.up_max_fps)
    return False


def _forward_upstream_frame(runtime, sock, info, kind, payload, addr):
    """处理客户端上行帧（MSG_VIDEO/MSG_FRAME）：登记共享者 → 校验 → 限速 → 路由。

    第 26 条：中继前校验负载——视频帧走 parse_video 校验结构，JPEG 帧至少含
    SOI(FFD8) 标记；0 字节/损坏负载不得直达订阅端。
    """
    if kind == MSG_VIDEO:
        if info.share_id is None:
            runtime._register_sharer(sock, info)
        try:
            _ts, _codec, flags, _nal = parse_video(payload)
        except ValueError:
            log.warning("客户端 %s 上行视频帧损坏，忽略", addr)
            return
        if not _allow_upstream(runtime, info, payload, addr):   # 第 107 条（B8）
            return
        _fwd_member_frame(runtime, info, kind, payload,
                          is_key=bool(flags & VIDEO_FLAG_KEY))
        return
    # MSG_FRAME：payload = [8B ts][JPEG]
    if len(payload) < 10 or payload[8:10] != b"\xff\xd8":
        log.debug("客户端 %s 上行 JPEG 帧损坏（长度 %d），忽略", addr, len(payload))
        return
    if info.share_id is None:
        runtime._register_sharer(sock, info)
    if not _allow_upstream(runtime, info, payload, addr):       # 第 107 条（B8）
        return
    _fwd_member_frame(runtime, info, kind, payload, is_jpeg=True)


def _client_net_cfg(runtime):
    """取 (host_cfg, net_cfg)；非对象时告警并兜底为空（第 102 条）。"""
    host_cfg = runtime.cfg.get("host", {})
    if not isinstance(host_cfg, dict):
        log.warning("配置 host 不是对象（%r），按默认值处理", host_cfg)
        host_cfg = {}
    net_cfg = host_cfg.get("net", {})
    if not isinstance(net_cfg, dict):
        log.warning("配置 host.net 不是对象（%r），按默认网络参数处理", net_cfg)
        net_cfg = {}
    return host_cfg, net_cfg


def _do_handshake(sock, addr, net_cfg):
    """socket 调优 + 有界握手；失败时关闭连接并返回 False（第 100 条）。

    第 100 条：握手必须有界。accept() 得到的 socket 超时是 None，若客户端连上后
    一个字节都不发（端口扫描 / 崩溃的对端 / 半开连接），server_handshake 会永久
    阻塞在 recv 上：每个这样的连接永久占用一个线程 + 一个 fd，而 accept_loop 没有
    并发上限。这里先做 socket 调优（含 SO_KEEPALIVE，让半开连接也能被内核回收），
    再把本次握手收紧到 HANDSHAKE_TIMEOUT_S。
    """
    tune_socket(
        sock,
        sndbuf=_safe_int(net_cfg.get("sndbuf_kb", 1024), 1024, 0) * 1024,
        rcvbuf=_safe_int(net_cfg.get("rcvbuf_kb", 2048), 2048, 0) * 1024,
        keepalive=bool(net_cfg.get("keepalive", True)),
        keepalive_idle=_safe_int(net_cfg.get("keepalive_idle_s", 60), 60, 1, 3600),
    )
    try:
        sock.settimeout(HANDSHAKE_TIMEOUT_S)
    except OSError:
        pass
    try:
        server_handshake(sock)
    except Exception as e:
        log.warning("客户端 %s 握手失败（%.0f 秒内未完成）: %s",
                    addr, HANDSHAKE_TIMEOUT_S, e)
        try:
            sock.close()
        except OSError:
            pass
        return False
    return True


def _auth_required_phase(sock, addr, runtime, auth_cfg):
    """准入开启：循环处理 login/register/probe；返回 (放行?, 用户名)。

    第 23 条：认证阶段强制「总时限」而非「每次 recv 超时」，防 slowloris 无限占用线程。
    第 31 条：总时限须大于观看端登录框等待（120s），否则用户还在输入 host 就先断开。
    第 104 条（B11）：user/pass 必须是字符串——旧写法 `(msg.get("user") or "").strip()`
    在 {"user":123} 上抛 AttributeError，被外层 except 记成"认证异常"并断开，用户看到
    的是"认证通信失败"，而不是"用户名或密码错误"。
    """
    auth_timeout = _safe_float(auth_cfg.get("auth_timeout", 150), 150, 1, 3600)
    deadline = time.monotonic() + auth_timeout
    sock.settimeout(auth_timeout)
    try:
        while True:
            msg = recv_msg(sock, deadline)
            action = msg.get("action")
            raw_user, raw_pass = msg.get("user"), msg.get("pass")
            if raw_user is not None and not isinstance(raw_user, str):
                send_msg(sock, {"action": "auth_result", "ok": False,
                                "msg": "认证消息格式非法（user 必须是字符串）"})
                log.warning("客户端 %s 认证消息 user 类型非法（%s），已拒绝",
                            addr, type(raw_user).__name__)
                sock.close()
                return False, None
            if raw_pass is not None and not isinstance(raw_pass, str):
                send_msg(sock, {"action": "auth_result", "ok": False,
                                "msg": "认证消息格式非法（pass 必须是字符串）"})
                log.warning("客户端 %s 认证消息 pass 类型非法（%s），已拒绝",
                            addr, type(raw_pass).__name__)
                sock.close()
                return False, None
            user = (raw_user or "").strip()
            password = raw_pass or ""
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
            if ok:
                return True, user
            if action == "probe":
                # 第 30 条：probe 后保持连接，等观看端在同一 socket 提交真实凭据。
                # 旧实现 probe→立刻 close，观看端第二次弹框把凭据发进死连接 →
                # 报「认证通信失败」，重连后再弹一次才成功（首连必失败、弹两次框）。
                continue
            # 真实凭据校验失败：记录并断开（不在单连接内循环，避免放开暴力尝试）
            log.warning("客户端 %s 认证失败: %s（%s）", addr, text, user or "-")
            sock.close()
            return False, None
    except socket.timeout:
        log.warning("客户端 %s 未在 %.0f 秒内完成登录，已断开", addr, auth_timeout)
        sock.close()
        return False, None
    except Exception as e:
        log.warning("客户端 %s 认证异常: %s", addr, e)
        sock.close()
        return False, None


def _auth_optional_phase(sock, addr):
    """准入关闭：兼容新版观看端的可选 auth 消息；返回 (放行?, 用户名)。

    新版观看端握手后总会尝试发送 auth 消息。这里短暂等待并回复“认证已关闭”，
    旧客户端不发送 auth 则直接放行，不阻塞服务。等待窗口取 2 秒：经公网/内网穿透的
    高延迟连接也能收到 probe，避免观看端误判为“需要登录”而陷入反复重连。
    第 95 条：先用 select 探测 2 秒内是否有数据到达，再决定是否调 recv_msg。
    recv_msg 在「读完 5 字节头才发现非 CTRL/坏长度」或「半截消息超时」时抛异常前
    已消费头部（甚至半截负载），流已错位；旧代码两个 except 都 fall-through 进接收
    循环，残留字节被当成下一条消息的头解析 → 非法 kind → 客户端被以「协议错误」踢掉。
    无数据（旧客户端不发 auth）→ 零消费、流对齐、直接放行；有数据但 recv_msg 抛异常
    → 流已错位无法恢复，直接断开，不带病进接收循环。
    """
    try:
        rlist, _, _ = select.select([sock], [], [], 2.0)
    except (OSError, ValueError) as e:
        log.debug("客户端 %s 可选 auth select 失败，按旧客户端直连处理：%s", addr, e)
        rlist = []
    if not rlist:
        log.debug("客户端 %s 未发送可选 auth，按旧客户端直连处理", addr)
        return True, None
    try:
        sock.settimeout(2.0)
        msg = recv_msg(sock)
        user = (msg.get("user") or "").strip()
        send_msg(sock, {"action": "auth_result", "ok": True,
                        "msg": "账户准入已关闭，无需登录"})
        if user:
            log.info("客户端 %s 在准入关闭状态发送登录：%s（已直接放行）", addr, user)
            return True, user
        log.info("客户端 %s 准入已关闭，直接放行", addr)
        return True, None
    except Exception as e:
        # 有数据但解析失败：已消费头部/半截消息，流错位，断开而非带病进接收循环。
        log.debug("客户端 %s 可选 auth 解析失败，断开（流已错位）：%s", addr, e)
        sock.close()
        return False, None


def _do_auth(sock, addr, runtime, host_cfg):
    """账户准入阶段；返回 (是否放行, 用户名)。第 109 条：从 _handle_client_inner 拆出。"""
    auth_cfg = host_cfg.get("auth", {})
    if not isinstance(auth_cfg, dict):
        log.warning("配置 host.auth 不是对象（%r），按准入关闭处理", auth_cfg)
        auth_cfg = {}
    if auth_cfg.get("enabled", False):
        return _auth_required_phase(sock, addr, runtime, auth_cfg)
    return _auth_optional_phase(sock, addr)


def _ctrl_watch(sock, info, runtime, msg, addr):
    """watch：切换订阅源；自身/无效源明确回绝。返回致命原因或 None。

    第 9 条：明确回绝。否则观看端已本地提交 watch_source=该源、状态栏写「源:队友·X」，
    收到的却是 host 本地画面；自身仍在 roster 中，_on_peers_updated 的离线回落也永不
    触发 → 永不自纠。第 25 条：禁止订阅自己——自己的上行帧被回送会形成镜像隧道且
    上下行双倍流量，host 无环路保护。第 56 条：转达关键帧请求时成员掉线不拆观看者。
    """
    src = msg.get("source") or "local"
    if src != "local" and src == info.share_id:
        log.debug("watch 拒绝订阅自身 %r（%s）", src, addr)
        if not _reply_ctrl(sock, info, {"action": "watch_reject",
                                        "source": src, "reason": "self"}, addr):
            return "控制消息写入失败（连接已损坏）"
    elif src == "local" or src in _live_share_ids(runtime):
        info.watch_source = src
        info.need_key = True  # 切换源：重新从关键帧开始收
        if src != "local":
            _send_ctrl_safe(runtime, _sharer_sock(runtime, src),
                            {"action": "req_keyframe"})
    else:
        log.debug("watch 无效源 %r（%s），保持原源 %r", src, addr, info.watch_source)
        if not _reply_ctrl(sock, info, {"action": "watch_reject",
                                        "source": src, "reason": "invalid"}, addr):
            return "控制消息写入失败（连接已损坏）"
    return None


def _dispatch_ctrl(sock, info, runtime, msg, addr, auth_cfg):
    """处理一条控制消息；返回 None 表示继续，返回字符串表示致命原因（需断开）。

    致命原因只有一种：控制消息写失败（连接已损坏，可能已写出半截消息，第 105 条/B4）。
    """
    if not isinstance(msg, dict):
        return None
    action = msg.get("action")
    if action == "ping":
        if not _reply_ctrl(sock, info, {"action": "pong", "t": msg.get("t", 0)}, addr):
            return "控制消息写入失败（连接已损坏）"
    elif action == "cap_probe":
        if not _reply_ctrl(sock, info, {"action": "cap", "multiview": True}, addr):
            return "控制消息写入失败（连接已损坏）"
    elif action == "watch":
        return _ctrl_watch(sock, info, runtime, msg, addr)
    elif action == "unshare":
        # 成员主动停止共享（连接保留）：注销并广播 roster，
        # 让订阅该 peer 的观看端回落 local（viewer peers 处理）
        if info.share_id is not None:
            old_id = info.share_id
            runtime._unregister_sharer(info)
            log.info("共享成员 %s 注销：%s（%s）", addr, old_id, info.username or "-")
    elif action == "req_keyframe":
        if info.watch_source.startswith("peer:"):
            # 转达成员端：其共享会话强制出一帧关键帧（第 56 条：安全发送）
            _send_ctrl_safe(runtime, _sharer_sock(runtime, info.watch_source),
                            {"action": "req_keyframe"})
        else:
            with runtime.slot_lock:
                runtime.force_key = True  # 原逻辑：本地下一帧 IDR
            log.debug("客户端 %s 请求关键帧，已置位强制 IDR", addr)
    elif action in ("probe", "login", "register"):
        # 第 100 条：迟到超过 2 秒的认证消息。准入关闭时上面的可选 auth 窗口（select 2s）
        # 已把本连接按"旧客户端"放行，此后到达的 probe/login 若无人应答，观看端会把
        # 下一条 roster/pong 当认证回复 → 在"无需登录"的服务端上反复弹登录框、最后报
        # "需要登录"。这里补一个明确答复（两种准入状态下都安全）。
        late_user = (msg.get("user") or "").strip()
        if auth_cfg.get("enabled", False):
            if not _reply_ctrl(sock, info, {"action": "auth_result", "ok": False,
                                            "msg": "需要登录"}, addr):
                return "控制消息写入失败（连接已损坏）"
        else:
            if late_user:
                info.username = late_user
            if not _reply_ctrl(sock, info, {"action": "auth_result", "ok": True,
                                            "msg": "账户准入已关闭，无需登录"}, addr):
                return "控制消息写入失败（连接已损坏）"
            log.debug("客户端 %s 迟到的认证消息（%s），已按准入关闭放行", addr, action)
    return None


def _client_recv_loop(sock, info, runtime, addr, net_cfg, auth_cfg):
    """控制消息/上行帧接收循环；返回断开原因（供统一收尾记录）。

    用 select 空闲轮询 + 应用层活跃时间戳做半开连接检测，避免与发送线程共享
    settimeout 造成相互干扰；收到 ping 立即回 pong（同机测 RTT）。
    """
    # 第 102 条：走 _safe_float——手改成 "abc" 时旧写法在每个客户端接入后立刻抛
    # ValueError（表现为"连上就断"），且违反本文件"配置数值一律走 _safe_*"的约定。
    idle_s = _safe_float(net_cfg.get("keepalive_idle_s", 60), 60, 1, 3600)
    # 第 57 条：rx_buf 用 bytearray，+= 原地扩展（摊还 O(1)）；bytes 的 += 每次整份
    # 重拷，攒一条大帧时退化为 O(n²) memcpy。配套 del rx_buf[:consumed] 原地弹出已
    # 消费前缀，parse_message 已统一返回 bytes payload（不随缓冲区类型变化）。
    rx_buf = bytearray()
    # 第 103 条（B1）：当前未完成消息的"首字节到达时刻"。半开检测以"收到任意字节"
    # 刷新 last_rx_at，攻击者每 <idle_s 秒发 1 字节即可永久占用线程，并把 rx_buf 攒到
    # MAX_PAYLOAD(32MB)。这里给单条消息（头+体）设总时限，超时按协议错误断开——
    # 正常客户端一条控制消息是毫秒级，15 秒余量充足。
    partial_since = None
    msg_timeout = _safe_float(getattr(runtime, "msg_timeout", 15.0), 15.0, 1.0, 600.0)
    reason = "断开"
    try:
        while not runtime.stop_event.is_set():
            r, _, _ = select.select([sock], [], [], 1.0)
            if not r:
                now_mono = time.monotonic()
                if now_mono - info.last_rx_at > idle_s:
                    reason = "空闲超时（半开连接）"
                    break
                if partial_since is not None and now_mono - partial_since > msg_timeout:
                    reason = "单条消息超时（%.0f 秒内未收完整）" % msg_timeout
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
            if not rx_buf:
                partial_since = time.monotonic()  # 本段是某条新消息的第一段
            rx_buf += data
            # 逐条解析缓冲区内完整消息：CTRL 即时处理；上行帧（MSG_VIDEO/MSG_FRAME）
            # 视为共享声明：首帧登记为共享成员并路由给 peer 源订阅者
            protocol_error = False
            while True:
                consumed, kind, payload = parse_message(rx_buf)
                if consumed == 0:
                    break
                del rx_buf[:consumed]
                if kind in (MSG_VIDEO, MSG_FRAME):
                    _forward_upstream_frame(runtime, sock, info, kind, payload, addr)
                    continue
                # MSG_CTRL
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except ValueError:
                    reason = "协议错误（控制消息 JSON 损坏）"
                    protocol_error = True
                    break
                fatal = _dispatch_ctrl(sock, info, runtime, msg, addr, auth_cfg)
                if fatal:
                    reason = fatal
                    protocol_error = True
                    break
            if not rx_buf:
                partial_since = None  # 缓冲区已清空：当前没有半截消息在等
            if protocol_error:
                break  # 协议错误：立即断开，避免异常字节滞留缓冲区导致内存增长
    except Exception as e:
        reason = "异常: %s" % e
        log.debug("客户端 %s %s", addr, reason)
    return reason


def _client_teardown(sock, info, runtime, addr, username, reason):
    """统一收尾：移出集合 → 注销共享 → 停发送线程 → 关连接 → 记日志。"""
    with runtime.clients_lock:
        gone = runtime.clients.pop(sock, None)
        remaining = len(runtime.clients)
    if gone is not None:
        if gone.share_id is not None:
            runtime._unregister_sharer(gone)
        gone.stop_sender()
    try:
        sock.close()
    except OSError:
        pass
    if gone is not None:
        with gone._lock:
            total_mb = gone.bytes_sent / (1024.0 * 1024.0)
        log.info("客户端断开: %s（%s，%s，剩余 %d 个客户端，累计发送 %.1f MB）",
                 addr, username or "未登录", reason, remaining, total_mb)
    else:
        log.info("客户端断开: %s（%s，%s，剩余 %d 个客户端）",
                 addr, username or "未登录", reason, remaining)


def _handle_client_inner(sock, addr, runtime):
    """客户端连接主体：握手 → 准入 → 入册 → 接收循环 → 统一收尾。

    第 109 条（阶段 2）：原 384 行拆为 _do_handshake / _do_auth / _client_recv_loop
    （内部再走 _dispatch_ctrl 与 _forward_upstream_frame）/ _client_teardown。
    """
    host_cfg, net_cfg = _client_net_cfg(runtime)
    if not _do_handshake(sock, addr, net_cfg):
        return
    ok, username = _do_auth(sock, addr, runtime, host_cfg)
    if not ok:
        return
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
    auth_cfg = host_cfg.get("auth", {})
    if not isinstance(auth_cfg, dict):
        auth_cfg = {}
    reason = _client_recv_loop(sock, info, runtime, addr, net_cfg, auth_cfg)
    _client_teardown(sock, info, runtime, addr, username, reason)
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
            dropped = info.enqueue_frame(frame)
            # 第 7 条：视频帧因发送队列满被丢→该订阅者参考链断裂。门控到下一个关键帧
            # （丢弃中间 P 帧，避免对错误参考解出花屏），并强制编码器尽快出 IDR，把
            # 花屏窗口从「等周期关键帧约 2 秒」压到约 1 帧。JPEG 自包含、关键帧本身即可
            # 愈合（且上方门控刚清过 need_key），二者均无需处理。
            if dropped and not is_jpeg and not is_key:
                info.need_key = True
                with runtime.slot_lock:
                    runtime.force_key = True
                # 第 104 条（B3）：source 为 peer:N 时，上面置的 force_key 只影响
                # host 本地编码线程，**队友的编码器收不到任何请求**——订阅 peer 的
                # 观看端只能等对方自然 keyint（默认 60 帧 ≈ 2s）才恢复。这里补一次
                # 向该共享者转发的 req_keyframe（1 秒去重，避免丢帧风暴时刷爆控制通道）。
                if isinstance(source, str) and source.startswith("peer:"):
                    _request_peer_keyframe(runtime, source)
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
        return {c.share_id for c in runtime.clients.values()
                if getattr(c, "share_id", None)}


def _sharer_sock(runtime, share_id):
    """按共享 id 查成员连接 socket；不存在返回 None。

    用 getattr 读 share_id：route_frame 的调用方可能是测试替身（无该属性）。
    """
    with runtime.clients_lock:
        for s, c in runtime.clients.items():
            if getattr(c, "share_id", None) == share_id:
                return s
    return None


def _request_peer_keyframe(runtime, share_id, cooldown=1.0):
    """向 peer 共享者转发一次关键帧请求（带冷却去重，第 104 条 / B3）。

    返回是否真的发了。冷却表与锁在 HostRuntime 上（`_peer_key_lock` / `_peer_key_at`），
    用 getattr 兜底以便测试替身（MagicMock/桩）无需实现。
    """
    now = time.monotonic()
    lock = getattr(runtime, "_peer_key_lock", None)
    table = getattr(runtime, "_peer_key_at", None)
    if lock is not None and table is not None:
        with lock:
            if now - table.get(share_id, 0.0) < cooldown:
                return False
            table[share_id] = now
    sock = _sharer_sock(runtime, share_id)
    if sock is None:
        return False
    _send_ctrl_safe(runtime, sock, {"action": "req_keyframe"})
    return True


def _send_ctrl_safe(runtime, sock, obj):
    """锁内安全取 ClientInfo 并发一条控制消息（第 56 条）。

    直接 `runtime.clients[sock]` 与 _drop_client 存在竞态：键被弹掉时抛 KeyError，
    被调用方的 `except Exception` 吞掉后 finally 拆掉的却是**观看者自己**的连接。
    这里用 .get() 取，目标已断开则静默跳过，发送失败也只吞 OSError，绝不外抛。
    """
    if sock is None:
        return
    with runtime.clients_lock:
        info = runtime.clients.get(sock)
    if info is None:
        return
    try:
        info.send_ctrl(sock, obj)
    except OSError:
        pass


def _fwd_member_frame(runtime, sharer, kind, payload, is_key=False, is_jpeg=False):
    """把成员上行帧原样重打包后路由给该 peer 源的订阅者。

    帧 payload 格式与下行完全一致，重打包字节序列等价原消息（kind+len+payload）。
    """
    raw = bytes((kind,)) + struct.pack(">I", len(payload)) + payload
    route_frame(runtime, sharer.share_id, raw, is_key=is_key, is_jpeg=is_jpeg)


def _apply_listen_reuse_opt(sock):
    """为监听 socket 设置端口复用选项（第 20 条）。

    Windows 上 SO_REUSEADDR 语义是「允许别的进程重复绑定同一端口」——观看端握手后
    会明文发出凭据，端口被劫持即泄密。改用 SO_EXCLUSIVEADDRUSE 拒绝任何重复绑定；
    POSIX 保留 SO_REUSEADDR（其语义是复用 TIME_WAIT，正确且必要）。选项不可用时
    逐级降级，宁可正常监听也不让服务起不来。
    """
    if sys.platform == "win32":
        excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if excl is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, excl, 1)
                return
            except OSError:
                pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        pass


def _spawn_client(sock, addr, runtime):
    """为新连接启动处理线程（独立函数，便于单测替换，第 102 条）。"""
    threading.Thread(
        target=handle_client, args=(sock, addr, runtime), daemon=True
    ).start()


def _accept_loop(server, runtime, spawn=None):
    """接受循环：瞬时 OSError 不得让服务永久失去接受新连接的能力（第 102 条）。

    `accept()` 抛出的 OSError 有两类语义完全不同的情况：
      1. 监听 socket 已关闭（正常停止）→ 必须退出循环；
      2. WSAEMFILE（fd 耗尽）/ WSAENOBUFS / WSAENETDOWN / ECONNABORTED 等**可恢复**
         的瞬时错误 → 必须重试。
    旧实现一律 `break`，于是服务永久失去接受能力，而 `runtime._server` 仍非 None、
    GUI 仍显示"运行中"、已连接的客户端照常收帧、日志零输出——用户看到的现象是
    "新观众永远连不上、老观众正常"，极难排查。现在只在真正停止时退出，其余情况
    告警 + 50ms 退避重试（同一轮故障最多每 20 次记一条，避免刷爆日志）。

    spawn 仅用于单测注入（默认 `_spawn_client`）。
    """
    spawn = spawn or _spawn_client
    fails = 0
    while not runtime.stop_event.is_set():
        try:
            sock, addr = server.accept()
        except OSError as e:
            try:
                closed = server.fileno() < 0
            except Exception:
                closed = False
            if runtime.stop_event.is_set() or closed:
                break  # 真正的关闭：监听 socket 已失效
            fails += 1
            if fails == 1 or fails % 20 == 0:
                log.warning("accept 失败（已连续 %d 次，50ms 后重试）: %s", fails, e)
            time.sleep(0.05)
            continue
        fails = 0
        # 第 103 条（B1）：超限时不 spawn 线程（快速路径；权威判定在 handle_client
        # 的 acquire_conn 里，那里能看到"握手中"的连接，两者一致收敛）。
        limit = getattr(runtime, "max_clients", 32)
        try:
            current = runtime.conn_count()
        except Exception:
            current = 0
        if current >= limit:
            log.warning("连接数已达上限 %d，直接关闭来自 %s 的新连接", limit, addr)
            try:
                sock.close()
            except OSError:
                pass
            continue
        spawn(sock, addr, runtime)


class PipelineParams:
    """采集/编码/发送/性能四条流水线的派生参数（第 109 条 · 阶段 2 重构）。

    这些值原先散落在 run_server 开头的 60 余行局部变量里，被 4 个嵌套闭包共享。
    抽成对象后四条流水线成为模块级函数（可单独构造参数测试），run_server 只做编排。
    所有数值读取仍走 _safe_int/_safe_float（第 100 条），手改配置不会打死线程。
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
        self.base_fps = _safe_int(host_cfg.get("fps", 30), 30, 1, 240)
        self.base_quality = _safe_int(host_cfg.get("jpeg_quality", 80), 80, 1, 100)
        self.base_scale = _safe_float(host_cfg.get("scale", 1.0), 1.0, 0.05, 4.0)
        self.quality_min = _safe_int(perf_cfg.get("quality_min", 40), 40, 1, 100)
        self.scale_min = _safe_float(perf_cfg.get("scale_min", 0.25), 0.25, 0.05, 1.0)
        self.fps_min = _safe_int(perf_cfg.get("fps_min", 10), 10, 1, 240)
        # 静止检测（perf.still，全部可配；默认关闭——开启后小的锐利局部变化
        # 会被整屏面积比判据误判为静止，帧率被压到 probe_fps）
        still_cfg = perf_cfg.get("still") if isinstance(perf_cfg.get("still"), dict) else {}
        self.still_enabled = bool(still_cfg.get("enabled", False))
        self.still_probe_interval = 1.0 / _safe_int(
            still_cfg.get("probe_fps", 5), 5, 1, 240)
        still_frames = _safe_int(still_cfg.get("still_frames", 3), 3, 1, 3600)
        self.point_thr = _safe_int(still_cfg.get("point_thr", 10), 10, 0, 255)
        self.ratio_thr = _safe_float(still_cfg.get("ratio_thr", 0.005), 0.005, 0.0, 1.0)
        # 连续无变化满该时长才判定静止（默认 3 次探测 × 0.2s = 0.6s，仅连续累计，
        # 间歇内容不会因零星停顿跨帧累计误入静止）；单位**秒**（第 87 条）
        self.still_quiet_s = still_frames * self.still_probe_interval
        self.encoder_sel = codec_cfg.get("encoder", "auto")
        # 注：原 run_server 里的 base_bitrate 局部量自始未被使用（死代码），
        # 目标码率由 HostRuntime.__init__ 写入 runtime.target_bitrate，故不再保留。
        self.min_bitrate = _safe_int(codec_cfg.get("min_bitrate_kbps", 400), 400, 64) * 1000
        self.max_bitrate = _safe_int(codec_cfg.get("max_bitrate_kbps", 6000), 6000, 64) * 1000
        if self.max_bitrate < self.min_bitrate:
            self.min_bitrate, self.max_bitrate = self.max_bitrate, self.min_bitrate
        self.keyint = _safe_int(codec_cfg.get("keyint", 60), 60, 1, 3600)
        self.codec_preset = codec_cfg.get("preset", "") or ""
        self.target_width = _safe_int(codec_cfg.get("target_width", 854) or 0, 854, 0)


def _listen_target(cfg):
    """解析 (listen_host, port, bind_all)；非字符串地址按“全网卡”处理。

    第 19 条语义：bind_all（""/"0.0.0.0"/"::"/None）→ 双栈 socket，一个端口同时覆盖
    IPv4/IPv6（frp 本地回连在 SYSTEM 上下文优先走 ::1，必须双栈，否则公网链路握手即断）；
    指定具体地址（如 127.0.0.1）→ 只绑该地址、该地址族，真正把共享限制在本机。
    手改成数字/对象时按全网卡处理而不是抛 TypeError（第 100 条口径：坏配置只回退）。
    """
    host_cfg = cfg.get("host") if isinstance(cfg, dict) and isinstance(cfg.get("host"), dict) else {}
    listen_host = host_cfg.get("listen_host")
    if not isinstance(listen_host, str):
        listen_host = ""
    port = _safe_int(host_cfg.get("port", 5700), 5700, 1, 65535)
    return listen_host, port, listen_host in ("", "0.0.0.0", "::")


def bind_listener(runtime):
    """建监听 socket 并登记到 runtime；失败时写 startup_error 并返回 None。

    第 18 条：绑定失败（端口被占/权限不足/第二个实例撞 SO_EXCLUSIVEADDRUSE）时
    不再让线程静默消失——记一条致命错误并置 startup_error 供 GUI 显示，随后干净返回。
    """
    listen_host, port, bind_all = _listen_target(runtime.cfg)
    bound_addr = "0.0.0.0" if bind_all else listen_host   # 诚实日志用（第 19 条）
    server = None
    try:
        if bind_all:
            try:
                server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                try:
                    server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except OSError:
                    pass
                _apply_listen_reuse_opt(server)
                server.bind(("::", port))
                server.listen(5)
            except OSError:
                # IPv6 不可用（系统禁用 IPv6）时的回退：仅 IPv4 全网卡
                try:
                    if server is not None:
                        server.close()
                except OSError:
                    pass
                server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                _apply_listen_reuse_opt(server)
                server.bind(("0.0.0.0", port))
                server.listen(5)
        else:
            # 指定地址：按地址族绑定（含 ':' 视为 IPv6），绝不偷偷扩成全网卡
            family = socket.AF_INET6 if ":" in listen_host else socket.AF_INET
            server = socket.socket(family, socket.SOCK_STREAM)
            _apply_listen_reuse_opt(server)
            server.bind((listen_host, port))
            server.listen(5)
    except OSError as e:
        try:
            if server is not None:
                server.close()
        except OSError:
            pass
        runtime._server = None
        runtime.startup_error = (
            "无法监听 %s:%d（%s）；端口可能已被占用或权限不足。" % (bound_addr, port, e))
        log.error("服务启动失败：%s", runtime.startup_error)
        return None
    runtime._server = server
    runtime.startup_error = None
    runtime.bound_addr = bound_addr
    return server


def run_server(runtime):
    """绑定监听并运行 采集→编码→发送→性能 四条流水线，直到 stop_event 置位。

    第 109 条（阶段 2）：原 678 行巨型函数拆为 bind_listener + 4 个模块级流水线函数
    （_capture_loop/_encode_loop/_send_loop/_perf_loop），本函数只做编排：
    参数构造 → 绑定 → 起线程 → 等待停止 → 收尾。

    流水线（每级只保留“最新一帧”，天然丢弃中间帧，采集与发送解耦）：
      采集线程：按目标帧率把原始帧写入 raw 槽（编码不在采集线程，避免编码耗时
                拖慢采集节奏；连续采集失败自动重建采集后端实现故障恢复）
      编码线程：消费 raw 槽最新帧 -> H.264（NVENC/x264）或 JPEG 回退 -> 写共享槽
      发送线程：消费共享槽最新帧 -> 广播到各客户端独立发送队列
      性能线程：每秒输出统计 -> 自适应降级（码率/质量 -> 缩放 -> 帧率）
    """
    p = PipelineParams(runtime.cfg)
    server = bind_listener(runtime)
    if server is None:
        return
    _listen_host, port, _bind_all = _listen_target(runtime.cfg)
    log.info("服务已启动，监听 %s:%d（fps=%d 质量=%d，退出按钮/Ctrl+C 退出）",
             runtime.bound_addr, port, p.base_fps, p.base_quality)

    # 第 102 条：接受循环在模块级（_accept_loop），瞬时 OSError 只重试不退出。
    # 采集、编码、发送与性能节拍线程均为 daemon，随 stop_event 置位后退出。
    threads = [threading.Thread(target=_accept_loop, args=(server, runtime), daemon=True)]
    for target, args in ((_capture_loop, (runtime, p)),
                         (_encode_loop, (runtime, p)),
                         (_send_loop, (runtime,)),
                         (_perf_loop, (runtime, p))):
        threads.append(threading.Thread(target=target, args=args, daemon=True))
    for t in threads:
        t.start()

    try:
        # 主线程等待停止事件（Ctrl+C 或 GUI 退出时置位 stop_event 再让线程退出）
        while not runtime.stop_event.is_set():
            runtime.stop_event.wait(0.5)
    except KeyboardInterrupt:
        runtime.stop_event.set()
    finally:
        server.close()
        # 第 66 条：5 个线程已被 stop_event 并发置位、同时收尾，改用共享 deadline 而非
        # 各自 timeout=2 串行 join——否则理论最坏 8s 会超过 shutdown 给 _server_thread 的
        # 3s 预算，join 超时返回时 run_server 仍在收尾、_encode_loop 尾部的 encoder.close()
        # 来不及执行。共享 deadline 把收尾总耗时压到约 2s，落在预算内。
        deadline = time.monotonic() + 2.0
        for t in threads:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                t.join(timeout=remaining)
        log.info("服务已停止")
class _CaptureState:
    """采集线程的可变状态（连续错误计数、空帧起始时刻/告警标志）。"""

    __slots__ = ("consecutive_errors", "none_since", "none_warned")

    def __init__(self):
        self.consecutive_errors = 0
        self.none_since = None
        self.none_warned = False


def _grab_with_recovery(cap, backend, monitor, region, st):
    """抓一帧并处理两类失败，返回 (frame, cap)；frame 为 None 表示本轮回退。

    第 100 条：持续空帧要区分两种情况——后端已死（working=False，必须重建并上报，
    否则画面永久冻结且日志无痕）与“画面静止/独占全屏”（dxgi 语义上正常，只告警一次，
    不重建）。异常路径连续 5 次失败后重建采集器。
    返回的 cap 可能是重建后的新句柄，调用方必须接收。
    """
    try:
        bgr = cap.grab()
        if bgr is None:
            now_none = time.monotonic()
            if st.none_since is None:
                st.none_since = now_none
            if not getattr(cap, "working", True):
                if now_none - st.none_since >= 5.0:
                    log.error("采集后端已失效（dxgi/mss 均无句柄），重建采集器")
                    try:
                        cap.close()
                    except Exception:
                        pass
                    try:
                        cap = CaptureManager(backend, monitor, region)
                        if not getattr(cap, "working", True):
                            log.error("重建后的采集后端仍不可用，请检查采集源设置")
                    except Exception as e2:
                        log.error("重建采集后端失败: %s", e2)
                    st.none_since = None
                    st.none_warned = False
                    time.sleep(0.5)
                    return None, cap
            elif not st.none_warned and now_none - st.none_since >= 30.0:
                st.none_warned = True
                log.warning("已连续 %.0f 秒采集不到新帧（画面静止或独占全屏），"
                            "观看端将看不到更新；可切换采集后端为 mss 试试",
                            now_none - st.none_since)
            time.sleep(0.01)  # 无新帧（dxgi），短暂等待后重试
            return None, cap
        st.none_since = None
        st.none_warned = False
    except Exception as e:
        st.consecutive_errors += 1
        log.warning("采集错误: %s（连续 %d 次）", e, st.consecutive_errors)
        if st.consecutive_errors >= 5:
            log.warning("采集后端持续异常，尝试重建采集器（故障恢复）")
            try:
                cap.close()
            except Exception:
                pass
            try:
                cap = CaptureManager(backend, monitor, region)
            except Exception as e2:
                log.error("重建采集后端失败: %s", e2)
            st.consecutive_errors = 0
        time.sleep(0.5)
        return None, cap
    if st.consecutive_errors > 0:
        st.consecutive_errors = 0  # 恢复成功，清空连续错误计数
    return bgr, cap


def _still_gate_step(runtime, p, bgr, still_ref, in_still, quiet_since, was_still):
    """静止检测闸门一步：返回 (changed, still_ref, in_still, quiet_since, was_still)。

    探测异常按“有变化”处理：宁可多发一帧，不可画面冻结。静止/恢复的边沿各打一条日志
    并更新 runtime.stats["still"]（性能线程据此暂停自适应评估，避免把停发误判为空闲）。
    """
    try:
        changed, still_ref = motion_changed(
            still_ref, downsample_frame(bgr), p.point_thr, p.ratio_thr)
    except Exception:
        changed, still_ref = True, downsample_frame(bgr)
    in_still, quiet_since = still_gate_update(
        in_still, quiet_since, changed, time.perf_counter(), p.still_quiet_s)
    if in_still and not was_still:
        log.info("画面静止，暂停发送（%d fps 探测）", int(1 / p.still_probe_interval))
        with runtime.stats_lock:
            runtime.stats["still"] = True
    elif was_still and not in_still:
        log.info("画面变化，恢复发送")
        with runtime.stats_lock:
            runtime.stats["still"] = False
    return changed, still_ref, in_still, quiet_since, in_still


def _capture_loop(runtime, p):
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
    # 第 100 条：采集器构造永不抛异常（两个后端都失败时只把句柄置 None），此时
    # grab() 恒返回 None，线程会以 100Hz 空转且什么都不上报。启动即验活。
    if not getattr(cap, "working", True):
        log.error("采集后端初始化失败（dxgi/mss 均不可用），将无法共享画面；"
                  "请在控制台「采集源」切换后端后重试")
    st = _CaptureState()
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
                fps_now = _safe_int(runtime.slot.get("fps", p.base_fps),
                                    p.base_fps, 1, 240, warn=False)
            try:
                cap.set_params(backend, monitor, region)
            except Exception as e:
                st.consecutive_errors += 1
                log.warning("采集参数重建失败: %s（连续 %d 次）", e, st.consecutive_errors)
                time.sleep(0.5)
                continue
            frame_interval = 1.0 / fps_now if fps_now > 0 else 0.0
            frame_start = time.perf_counter()
            ts_micros = int(time.time() * 1_000_000)  # 采集开始时刻
            bgr, cap = _grab_with_recovery(cap, backend, monitor, region, st)
            if bgr is None:
                continue
            # ---- 静止检测闸门（画面无有效变化时不写 raw 槽，编码/发送自然停摆）----
            if p.still_enabled:
                changed, still_ref, in_still, quiet_since, was_still = _still_gate_step(
                    runtime, p, bgr, still_ref, in_still, quiet_since, was_still)
                if not changed:
                    if in_still:
                        # 静止期：按探测间隔巡检，降低采集开销
                        time.sleep(p.still_probe_interval)
                    else:
                        # 未入静止：按正常帧节奏继续巡检。不按探测间隔空等——
                        # 间歇内容（打字/低频更新）不会因零星停顿被压到探测速率
                        wait = frame_interval - (time.perf_counter() - frame_start)
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
                wait = frame_interval - (time.perf_counter() - frame_start)
                if wait > 0:
                    time.sleep(wait)
    finally:
        cap.close()
def _store_encoded(runtime, ef, raw_ts):
    """把编码结果写进共享槽（发送线程据此广播）。"""
    with runtime.slot_lock:
        if ef.is_video:
            runtime.slot["video"] = ef.data
            runtime.slot["video_ts"] = ef.ts
            runtime.slot["is_key"] = ef.is_key
            runtime.slot["video_codec"] = ef.codec_id
            runtime.slot["bitrate"] = ef.bitrate
        else:
            runtime.slot["jpeg"] = ef.data
            runtime.slot["ts"] = raw_ts   # 帧采集时间戳原样透传
            runtime.slot["bitrate"] = 0
        runtime.slot["encode_ms"] = ef.ms
        runtime.slot["out_w"] = ef.w
        runtime.slot["out_h"] = ef.h


def _fps_from_window(window, now):
    """1 秒滑动窗口帧率（window 为时刻列表，按首尾差值算平均）。"""
    while window and now - window[0] > 1.0:
        window.pop(0)
    if len(window) >= 2:
        return (len(window) - 1) / (window[-1] - window[0])
    return 1.0 if window else 0.0


def _encode_static_keyframe(runtime, fe, last_frame, error_cooldown_until):
    """静止期响应关键帧请求：用最近编码帧强制一帧 IDR 并写共享槽。

    返回新的"编码错误告警冷却时刻"（第 90 条：force_idr 返回异常而不抛，此处按 10 秒
    冷却上报，避免编码器半死时刷屏）。无请求/无缓存帧时原样返回，不做任何工作。
    """
    with runtime.slot_lock:
        need_key = runtime.force_key
        runtime.force_key = False
    if not need_key or last_frame is None:
        return error_cooldown_until
    res, err = fe.force_idr(last_frame)
    if err is not None:
        now_k = time.perf_counter()
        if now_k >= error_cooldown_until:
            log.warning("静止期关键帧编码错误: %s，10 秒内不再重复告警", err)
            error_cooldown_until = now_k + 10.0
        return error_cooldown_until
    if res is None:
        return error_cooldown_until
    nal, is_key, encode_ms, out_ts = res
    with runtime.slot_lock:
        runtime.slot["video"] = nal
        runtime.slot["video_ts"] = out_ts
        runtime.slot["is_key"] = is_key
        runtime.slot["video_codec"] = fe.encoder.codec_id
        runtime.slot["encode_ms"] = encode_ms
        runtime.slot["bitrate"] = fe.encoder.bitrate
    with runtime.stats_lock:
        runtime.stats["encode_ms"] = encode_ms
    log.info("静止期响应关键帧请求（%d 字节）", len(nal))
    return error_cooldown_until


def _encode_loop(runtime, p):
    """编码线程：消费 raw 槽最新帧 -> 编码（视频优先/JPEG 回退）-> 写共享槽。

    第 110 条（阶段 2）：编码器生命周期与回退逻辑改由 pipeline.FrameEncoder 统一提供
    （与 share.py 共用同一实现）。host 因此获得 share 早已有的第 53 条"构造失败负缓存
    退避"——此前编码器不可用时本循环每帧都会新建一次 VideoEncoder（4 次 av.Codec 查找
    + 4 次 open + 最多 4 条告警，30 帧/秒），把 CPU 与日志一起打满且永不恢复。

    时间戳透传（packet.pts=采集时刻）；关键帧标记（is_key）供发送端与观看端同步；
    force_key 强制 IDR（观看端关键帧请求 / ABR 重建后重置参考帧）。
    """
    last_raw_ts = -1
    last_frame = None   # 最近一次成功编码（缩放后）的帧：静止期强制关键帧恢复用
    enc_window = []     # 1 秒滑动窗口：记录每次编码完成时刻，用于统计编码帧率
    error_cooldown_until = 0.0

    def _on_build(enc, w, h, bitrate):
        """编码器建成：登记到 runtime（ABR 线程据此调码率）并刷新统计。"""
        with runtime.codec_lock:
            runtime.video_encoder = enc
        with runtime.stats_lock:
            runtime.stats["codec_name"] = enc.name
            runtime.stats["keyint"] = p.keyint
        log.info("启用视频编码器: %s（%dx%d，码率 %d Kbps，keyint %d）",
                 enc.name, w, h, bitrate // 1000, p.keyint)

    def _on_error(err):
        """编码异常：10 秒冷却告警（编码器半死时避免刷屏）。"""
        nonlocal error_cooldown_until
        now_e = time.perf_counter()
        if now_e >= error_cooldown_until:
            log.warning("编码错误: %s，10 秒内不再重复告警", err)
            error_cooldown_until = now_e + 10.0

    fe = pipeline.FrameEncoder(
        width=0, height=0, fps=p.base_fps, bitrate=runtime.target_bitrate,
        keyint=p.keyint, encoder_sel=p.encoder_sel, preset=p.codec_preset,
        target_width=p.target_width, quality=p.base_quality,
        on_build=_on_build, on_error=_on_error)
    while not runtime.stop_event.is_set():
        with runtime.slot_lock:
            raw = runtime.slot.get("raw")
            raw_ts = runtime.slot.get("raw_ts", 0)
            scale = runtime.slot["scale"]
            quality = runtime.slot["quality"]
            fps_now = _safe_int(runtime.slot.get("fps", p.base_fps),
                                p.base_fps, 1, 240, warn=False)
        if raw is None or raw_ts == last_raw_ts:
            # 无新帧：若画面静止且有观看端请求关键帧（新观众接入/花屏恢复），
            # 用最近一次编码帧强制出一帧 IDR（新 pts 绕过发送端同帧去重）
            if runtime.force_key:
                error_cooldown_until = _encode_static_keyframe(
                    runtime, fe, last_frame, error_cooldown_until)
            time.sleep(0.001)
            continue
        # 有新帧即将编码：原子读取并清除强制关键帧标志（仅在真正编码时消费）
        with runtime.slot_lock:
            force_key = runtime.force_key
            runtime.force_key = False
        with runtime.codec_lock:
            target_b = runtime.target_bitrate   # ABR 可能已更新
        now = time.perf_counter()
        ef = fe.encode(raw, raw_ts, force_key=force_key, scale=scale, quality=quality,
                       fps=fps_now, bitrate=target_b)
        if ef is None:
            # 编码器初始化/pre-roll 或编码失败：不消费 raw_ts，下轮重试
            time.sleep(0.001)
            continue
        last_frame = ef.frame  # 缓存缩放后帧：静止期强制关键帧恢复用
        _store_encoded(runtime, ef, raw_ts)
        last_raw_ts = raw_ts
        enc_window.append(now)
        with runtime.stats_lock:
            if ef.is_video:
                runtime.stats["keyframe_total"] = ef.keyframes
            runtime.stats["encode_ms"] = ef.ms
            runtime.stats["encode_fps"] = _fps_from_window(enc_window, now)
    fe.close()
    with runtime.codec_lock:
        runtime.video_encoder = None
def _send_loop(runtime):
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
def _set_encoder_bitrate(runtime, bps, cooldown=0.0):
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


def _perf_log_and_bandwidth(runtime, stats):
    """输出每秒性能日志与各客户端带宽汇总，返回 (effective_send_ms, recent_drop)。

    第 100 条：只采信“仍然新鲜”的发送耗时样本（recent_send_ms 内部按 last_send_at
    过滤）。旧实现直接读 last_send_ms，一个已经切走观看源/不再收帧的慢客户端会把
    拥塞判据永久钉在高位。
    """
    client_send_ms = []
    recent_drop = False
    now_mono = time.monotonic()
    with runtime.clients_lock:
        for info in runtime.clients.values():
            ms = info.recent_send_ms(now_mono)
            if ms > 0:
                client_send_ms.append(ms)
            if info.has_recent_drop(now_mono):
                recent_drop = True
        bw_lines = []
        for info in runtime.clients.values():
            rate = info.rate_kbps()
            lock = getattr(info, "_lock", None)
            if lock is not None:
                with lock:
                    total_mb = info.bytes_sent / (1024.0 * 1024.0)
            else:
                total_mb = getattr(info, "bytes_sent", 0) / (1024.0 * 1024.0)
            bw_lines.append(
                "%s%s %.0f KB/s %.1f MB（丢 %d 帧）" % (
                    info.addr,
                    ("(%s)" % info.username) if info.username else "",
                    rate, total_mb, info.dropped_frames))
    client_avg_ms = (sum(client_send_ms) / len(client_send_ms)) if client_send_ms else 0.0
    effective_send_ms = max(stats["avg_send_ms"], client_avg_ms)
    with runtime.stats_lock:
        runtime.stats["avg_send_ms"] = effective_send_ms
    log.info(
        "性能: 采集 %d fps / 编码 %d fps / 发送 %d fps / 帧率 %d / 质量 %d / 缩放 %.2f / 输出 %dx%d / 编码 %.1f ms / 发送 %.1f ms%s",
        stats["capture_fps"], stats["encode_fps"], stats["send_fps"], stats["fps_now"],
        stats["quality"], stats["scale"], stats["out_w"], stats["out_h"],
        stats["encode_ms"], effective_send_ms,
        "（画面静止，暂停发送）" if stats["still"] else
        ("（有客户端丢帧）" if recent_drop else ""),
    )
    if bw_lines:
        log.info("带宽: %s", "；".join(bw_lines))
    return effective_send_ms, recent_drop


def _apply_slot_params(runtime, quality, scale, fps_now):
    """把质量/缩放/帧率写回共享槽（帧率档变化额外打一条日志）。"""
    with runtime.slot_lock:
        runtime.slot["quality"] = quality
        runtime.slot["scale"] = scale
        if fps_now != runtime.slot["fps"]:
            runtime.slot["fps"] = fps_now
            log.info("自适应帧率已更新为 %d fps", fps_now)


def _abr_degrade(runtime, p, cur, effective_send_ms, recent_drop):
    """拥塞降级：视频模式 码率->缩放->帧率；JPEG 模式 质量->缩放->帧率。

    已到降级下限（质量/缩放/帧率均不能再降）时 level 为空：不再重复打印空的“降级”
    日志，避免持续拥塞时每秒一条噪音。
    """
    quality, scale, fps_now = cur["quality"], cur["scale"], cur["fps_now"]
    level = ""
    if cur["video_mode"] and cur["cur_bitrate"] > p.min_bitrate:
        new_b = max(p.min_bitrate, int(cur["cur_bitrate"] * 0.7))
        _set_encoder_bitrate(runtime, new_b)
        level = "码率 %d Kbps" % (new_b // 1000)
    elif not cur["video_mode"] and quality > p.quality_min:
        quality -= 10
        level = "质量 %d" % quality
    elif scale > p.scale_min:
        scale = max(p.scale_min, round(scale * 0.8, 3))
        level = "缩放 %.2f" % scale
    elif fps_now > p.fps_min:
        fps_now = max(p.fps_min, int(fps_now / 2))
        level = "帧率 %d" % fps_now
    _apply_slot_params(runtime, quality, scale, fps_now)
    if level:
        log.warning("自适应降级: %s（发送耗时 %.0f ms%s）",
                    level, effective_send_ms, "，丢帧" if recent_drop else "")


def _abr_recover(runtime, p, cur, base_fps, base_quality, base_scale):
    """链路空闲回升：视频模式 帧率->缩放->码率；JPEG 模式 帧率->缩放->质量。

    活动门限 send_fps>=5（由调用方判定）：内容为稀疏/间歇变化（打字、低频 UI 更新）时
    发送耗时≈0 会让回升误判链路空闲，每 1-2s 重建一次编码器（关键帧风暴）；发送不足时
    冻结参数，密集内容恢复后自然回升。码率回升带 2 秒冷却，避免频繁重建。
    """
    quality, scale, fps_now = cur["quality"], cur["scale"], cur["fps_now"]
    level = ""
    if fps_now < base_fps:
        fps_now = min(base_fps, fps_now * 2)
        level = "帧率 %d" % fps_now
    elif scale < base_scale:
        scale = min(base_scale, round(scale / 0.8, 3))
        level = "缩放 %.2f" % scale
    elif cur["video_mode"] and cur["cur_bitrate"] < p.max_bitrate:
        new_b = min(p.max_bitrate, int(cur["cur_bitrate"] * 1.3))
        _set_encoder_bitrate(runtime, new_b, cooldown=2.0)
        level = "码率 %d Kbps" % (new_b // 1000)
    else:
        quality = min(base_quality, quality + 10)
        level = "质量 %d" % quality
    _apply_slot_params(runtime, quality, scale, fps_now)
    log.info("自适应回升: %s", level)


def _perf_tick(runtime, p):
    """单次评估节拍（第 89 条：抽出后由外层 try 兜底，任一步异常不再静默杀死线程）。

    第 109 条（阶段 2）：从 _perf_loop 的嵌套闭包提升为模块级函数；统计输出、降级、
    回升各拆成独立函数，本函数只负责取样与判据。
    """
    with runtime.stats_lock:
        stats = {
            "capture_fps": runtime.stats["capture_fps"],
            "encode_fps": runtime.stats.get("encode_fps", 0),
            "send_fps": runtime.stats["send_fps"],
            "encode_ms": runtime.stats["encode_ms"],
            "avg_send_ms": runtime.stats["avg_send_ms"],
            "still": bool(runtime.stats.get("still", False)),
        }
    with runtime.slot_lock:
        stats["quality"] = runtime.slot["quality"]
        stats["scale"] = runtime.slot["scale"]
        stats["fps_now"] = _safe_int(runtime.slot.get("fps", p.base_fps),
                                     p.base_fps, 1, 240, warn=False)
        stats["out_w"] = runtime.slot.get("out_w", 0)
        stats["out_h"] = runtime.slot.get("out_h", 0)
    budget_ms = 1000.0 / max(stats["fps_now"], 1) * 0.8
    effective_send_ms, recent_drop = _perf_log_and_bandwidth(runtime, stats)
    # 每次评估时从 runtime.cfg 读取开关，让 GUI 修改可以即时生效
    if not bool(runtime.cfg["host"].get("perf", {}).get("adaptive", True)):
        return
    if stats["still"]:
        # 画面静止停发：发送耗时≈0 会让常规评估误入“回升”分支，反复重建编码器
        # （浪费 CPU）。静止期只统计不评估，恢复后自动继续。
        return
    # 同步读取用户最新基准值（GUI 修改 fps/质量/缩放后，回升目标随之更新，
    # 避免自适应回升“对抗”用户手动设置）
    base_fps = _safe_int(runtime.cfg["host"].get("fps", 30), 30, 1, 240, warn=False)
    base_quality = _safe_int(runtime.cfg["host"].get("jpeg_quality", 80), 80, 1, 100, warn=False)
    base_scale = _safe_float(runtime.cfg["host"].get("scale", 1.0), 1.0, 0.05, 4.0, warn=False)
    with runtime.slot_lock:
        # 最新帧时间戳：视频路径只更新 video_ts，JPEG 回退路径只更新 ts，二者同为
        # 采集时刻微秒（同一时钟基），取较新者即“最新一帧”的采集时刻。不能只在
        # video_ts>0 时优先采用它：回退 JPEG 后 video_ts 会被冻结，导致 lag_us 无限
        # 增长、congested 恒真，质量/缩放/帧率压到地板且永不回升。
        frame_ts = max(runtime.slot["video_ts"], runtime.slot["ts"])
        stats["video_mode"] = runtime.slot["bitrate"] > 0  # 有视频编码器在产出
        stats["cur_bitrate"] = runtime.slot["bitrate"]
    lag_us = int(time.time() * 1_000_000) - frame_ts if frame_ts > 0 else 0
    congested = (effective_send_ms > budget_ms or recent_drop
                 or (frame_ts > 0 and lag_us > 800_000))
    if congested:
        _abr_degrade(runtime, p, stats, effective_send_ms, recent_drop)
    elif (stats["send_fps"] >= 5 and not recent_drop
          and effective_send_ms < budget_ms * 0.5
          and (stats["fps_now"] < base_fps or stats["scale"] < base_scale
               or (stats["video_mode"] and stats["cur_bitrate"] < p.max_bitrate)
               or (not stats["video_mode"] and stats["quality"] < base_quality))):
        _abr_recover(runtime, p, stats, base_fps, base_quality, base_scale)
def _perf_loop(runtime, p):
    """性能统计 + 带宽汇总 + 动态自适应节拍：每秒评估一次。"""
    while not runtime.stop_event.is_set():
        time.sleep(1.0)
        try:
            _perf_tick(runtime, p)
        except Exception:
            # 第 89 条：循环体原无异常保护，配置里一个非数字值（手改或 GUI 写入，
            # 如 fps/quality/scale 被写成 "abc"）会让 int()/float() 抛 ValueError 直接
            # 杀死性能线程 → 此后无 1 秒统计日志、无自适应控制，而采集/编码/发送照常，
            # 界面看着正常。兜底后：异常被记录、本次评估跳过、线程继续；下一拍重读配置，
            # 用户改回有效值即自动恢复。统计日志在配置读取之前，故仍每秒输出。
            log.exception("性能节拍异常，跳过本次评估（线程继续，配置修复后自动恢复）")
def _install_excepthooks():
    """第 15 条：host.exe 以 windowed 打包（console=False，无可用 stderr）。

    默认 threading.excepthook 把未捕获异常写向不存在的 stderr，于是 accept/采集/
    编码/发送/性能线程崩溃时无任何日志；且 viewer 装了 excepthook 而 host 没装，
    两端不一致。这里统一改为写入文件日志：线程异常记线程名 + traceback，主线程
    未捕获异常额外弹一次友好提示框（与 viewer 行为一致），KeyboardInterrupt 透传。
    """
    def _thread_hook(args):
        name = getattr(getattr(args, "thread", None), "name", "thread")
        log.error("线程 %s 未捕获异常", name,
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    def _main_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.exception("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))
        try:
            import tkinter.messagebox as mb
            mb.showerror(APP_NAME, "程序遇到错误，已写入 logs 目录日志文件")
        except Exception:
            pass

    threading.excepthook = _thread_hook
    sys.excepthook = _main_hook


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
    _install_excepthooks()
    if args.port is not None:
        cfg["host"]["port"] = args.port
        save_config(cfg, "host")
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
            enable_dpi_awareness()
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
