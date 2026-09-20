# -*- coding: utf-8 -*-
"""FPS 画面共享主机端：屏幕采集（mss/dxgi）+ H.264 视频编码（NVENC 硬件/x264 回退）+
TCP 广播 + 樱花 frpc 集成 + 图形控制台。

第七轮优化（pro-video-codec）：
- 用 PyAV/FFmpeg 做 H.264 视频编码替代逐帧 JPEG：NVENC 硬件（GPU）优先、libx264 软件回退，
  带宽相比 JPEG 降低 5-20 倍；编码器不可用自动回退逐帧 JPEG
- 低延迟编码参数：无 B 帧、NVENC tune=ull/zerolatency 或 x264 tune=zerolatency、VBR 码率
- 码率自适应（ABR）：按发送耗时/积压动态调节编码码率（超出预设范围后回落原有的
  缩放→帧率降级），带宽恢复自动回升
- 关键帧请求：观看端请求时强制出 IDR 快速恢复（含 ABR 重建后的参考帧重置）
- 时间戳透传：H.264 帧携带采集时刻，端到端延迟精确测量

依赖：mss、opencv-python（cv2）、numpy；可选 dxcam（host.capture.backend="dxgi"）、
av/PyAV（host.codec.encoder!="jpeg" 时用于 H.264 编码）。
用法示例：python host.py [--port 5700] [--console]
"""

import argparse
import contextlib
import hmac
import json
import os
import queue
import select
import secrets
import socket
import subprocess
import sys
import threading
import time

import logger
import ui_common

# A3（第 133 条）：pipeline（→screen→cv2/numpy/mss）是 host 的导入大头，改为
# main() 极简 splash 上屏后由 _load_heavy() 惰性加载（HostRuntime.__init__ 也
# 会兜底触发，库用/测试路径 `import host; HostRuntime(cfg)` 行为不变）。
# screen 公共件按 test_multiview.TestScreenExtraction 锁定的"host 转发 screen
# 公共件"契约在加载后绑定为同名全局（外部/测试按 host.encode_bgr、
# host.CaptureManager 等取用）。
pipeline = None
CaptureManager = STILL_STEP = downsample_frame = motion_changed = None
still_gate_update = calc_output_size = encode_bgr = None


def _load_heavy():
    """惰性加载采集/编码重依赖并绑定 screen 转发名（幂等）。"""
    global pipeline, CaptureManager, STILL_STEP, downsample_frame
    global motion_changed, still_gate_update, calc_output_size, encode_bgr
    if pipeline is not None:
        return
    import pipeline as _pipeline
    import screen as _screen
    pipeline = _pipeline
    CaptureManager = _screen.CaptureManager
    STILL_STEP = _screen.STILL_STEP
    downsample_frame = _screen.downsample_frame
    motion_changed = _screen.motion_changed
    still_gate_update = _screen.still_gate_update
    calc_output_size = _screen.calc_output_size
    encode_bgr = _screen.encode_bgr

# 注（C9）：MSG_CTRL 属协议常量，本模块只在注释里提到它；保留导入以便
# `host.MSG_CTRL` 这类既有引用不失效（pyflakes F401 已知无害）。
from common import (  # noqa: F401
    load_config, save_config, server_handshake, pack_frame, pack_video, exe_dir, app_dirs,
    APP_NAME, enable_dpi_awareness, accounts_path, register_policy,
    recv_msg, send_msg, tune_socket, parse_message, parse_message_raw, message_pump, MSG_CTRL, MSG_FRAME, MSG_VIDEO,
    VIDEO_FLAG_KEY, CODEC_H264, parse_video, _MSG_HDR, MAGIC_CLIENT, MAGIC_SERVER,
    AUTH_TIMEOUT_DEFAULT_S, AUTH_TIMEOUT_MIN_S,
    # C2（实现方案 §7）：action 常量表单点来源
    Act,
    # C2 §7 第 3 步：握手版本字段（先只加不启用）
    PROTOCOL_VERSION, PROTO_ENFORCE, proto_compatible,
    # auth-016（2026-09-20）：host 侧也要看次版本
    PROTO_CHAL_MIN_MINOR,
    # D1（实现方案 §10）：挑战-应答认证公共件（PBKDF2/HMAC/base64 单一真源）
    PBKDF2_ITERATIONS, derive_dk, challenge_mac, b64e, b64d,
    # auth-002/auth-006（2026-09-18）：KDF 参数与凭据长度的校验口径
    KDF_ITER_MIN, KDF_ITER_MAX, KDF_SALT_HEX_LEN, _is_hex_str,
    # C1（优化方案 2026-09-17）：配置工具单点定义在 common（原先 host/viewer/share
    # 各有一套 clamp），share.py 不再反向 import host。
    normalize_region, safe_float, safe_int,
    # ui-022 / auth-022 / ui-030（2026-09-20）：配置子节判型的单点收口
    cfg_section,
)

# 采集/静止检测公共件（CaptureManager、encode_bgr 等 screen 转发名）由
# _load_heavy() 绑定——见上方 A3（第 133 条）注释。

#: 全局日志器（未初始化时按默认配置惰性初始化）
log = logger.get_logger()

#: 第 100 条：握手阶段（等客户端魔数）的总时限。
#: accept() 出来的 socket 超时是 None，server_handshake → recv_exact(deadline=None)
#: 会永久阻塞；不发魔数的连接每个永久占用一个线程 + 一个 fd，而 accept 循环没有
#: 并发上限，一次端口扫描即可把服务耗死。
HANDSHAKE_TIMEOUT_S = 10.0

#: auth-006（2026-09-18）：用户名长度上界——注册/登录链路上没有上界时，一条超长
#: 用户名的记录会撑爆 accounts.json（且每次写盘全量重写放大量级）。
MAX_USERNAME_LEN = 64

#: 第 112 条（C1）：客户端发送速率滑动窗口的**保留**时长（秒）。
#: 带宽日志按 1 秒窗口展示（灵敏），ABR 按 3 秒窗口取平均（跨过一个 GOP，见 rate_kbps）。
_RATE_KEEP_S = 3.0

#: 第 112 条（C1）：ABR 估算实测吞吐所用的窗口（秒）。
#: keyint=60 / 30fps 时每 2 秒一个 ~70KB 关键帧，1 秒窗口会在关键帧窗口与纯 P 帧窗口
#: 之间来回跳（实测 651 Kbps ↔ 66 Kbps），据此把码率一步砸到地板是错误的。
_ABR_EGRESS_WINDOW_S = 3.0

#: 第 100 条：单个客户端"发送耗时"样本的有效期（秒）。超过该时长没有再发送过的
#: 客户端，其 last_send_ms 不再计入 ABR 的拥塞判据——否则一个已经切走/不再收帧的
#: 慢客户端会把 effective_send_ms 永久钉在高位，使码率/缩放/帧率一路降到地板且永不回升。
SEND_MS_WINDOW_S = 2.0

#: B8：共享成员超过该时长没有任何上行帧，就在 roster 里标注为"无信号"（幽灵源）。
#: 成员侧共享会话自行终止时既不发 unshare 也不产帧，观众选中它只会停在最后一帧。
_MEMBER_STALE_S = 10.0


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


def _hash_password(password, salt_hex):
    """pbkdf2_hmac(sha256) 加盐哈希，迭代 10 万次，返回十六进制摘要。"""
    return derive_dk(password, salt_hex, PBKDF2_ITERATIONS).hex()


class AccountManager:
    """服务端账户注册/认证与 accounts.json 持久化（线程安全，不存明文密码）。"""

    def __init__(self):
        self._lock = threading.Lock()
        # 第 102 条：账户文件与 config.json 一样落在**可写**数据目录。安装在
        # Program Files 后非管理员运行时 exe 目录只读，原实现写这里会让注册直接失败。
        self._path = accounts_path()
        #: auth-010（2026-09-18）：bootstrap 策略"已消耗"标记文件——删除唯一账户
        #: 不得静默重开"任意人可自助注册"窗口（旧判定只看当前账户数）
        self._bootstrap_marker = self._path + ".bootstrap-used"
        self._users = {}  # 用户名 -> {salt, hash, created, last_login}
        self._pending_users = None  # auth-009：_mutate → _save 之间的写盘快照
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
            # auth-015（2026-09-20）：bootstrap 标记只在 register / register_derived
            # 成功后写，`_load()` 读到非空账户表时不回填——于是所有在引入该标记之前
            # 生成的 accounts.json 都没有标记文件：升级到本版后只要把账户删空
            # （团队解散、清理测试账号），"第一个账户可自助注册"的窗口就静默重开，
            # 已按建议加固过的配置退回加固前，而面板的删除确认文案承诺"窗口不会重开"。
            # 见到非空账户表即补写标记（幂等）。
            if users and not self.bootstrap_used():
                self._mark_bootstrap_used()
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

    def _read_disk(self):
        """读磁盘上的账户表副本（None = 文件缺失/损坏/非法）；不抛异常。

        auth-009（2026-09-18）：每次写操作前在文件锁内重读，避免双开实例用旧内存
        快照整表覆盖（另一方刚注册的账户被静默抹掉、已删除的账户复活）。
        """
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            users = data.get("users") if isinstance(data, dict) else None
            return users if isinstance(users, dict) else None
        except (OSError, ValueError):
            return None

    def _write_users(self, users):
        """原子写回账户表；返回是否成功（写失败只告警，绝不改变认证结论）。

        第 102 条：原实现让 OSError 直接抛出——`authenticate` 末尾记 last_login 时
        磁盘满/目录只读会让**口令正确的用户被拒**（且提示是"认证异常"而不是"无法写入
        账户文件"），`register` 则留下"内存里已注册、磁盘上没有"的假成功。
        auth-009：临时文件名带 PID + 线程号（唯一），避免两个进程写坏同一个 .tmp。
        """
        tmp = "%s.%d.%d.tmp" % (self._path, os.getpid(), threading.get_ident())
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"users": users}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
            return True
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            log.error("accounts.json 写入失败（%s）：账户改动未持久化", e)
            return False

    @contextlib.contextmanager
    def _file_lock(self):
        """跨进程互斥（auth-009）：`<accounts>.lock` 排他创建，陈旧锁自动接管。

        双开实例时进程内锁互不排斥，没有它就会出现"账户被静默抹掉/复活"。
        等待超时（2 秒）后不再阻塞——宁可放行本次写（极小概率竞争），也不能让
        GUI 线程永久卡死。
        """
        lock_path = self._path + ".lock"
        fd = None
        deadline = time.monotonic() + 2.0
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                # 陈旧锁（持有者被强杀）：超过 10 秒未更新直接接管
                try:
                    if time.time() - os.path.getmtime(lock_path) > 10.0:
                        os.remove(lock_path)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    log.warning("accounts.json 文件锁等待超时，本次写可能与另一实例竞争")
                    break
                time.sleep(0.05)
            except OSError as e:
                log.warning("accounts.json 文件锁创建失败（%s），本次写无跨进程保护", e)
                break
        try:
            yield
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.remove(lock_path)
                except OSError:
                    pass

    def _save(self):
        """写回账户表（`_mutate` 在本方法调用前置入读-改-写快照）；返回是否成功。

        auth-009：内部写路径统一经 `_mutate`（文件锁内读磁盘→改→写），本方法只做
        实际写盘。保留无参形态，既有测试/桩 `m._save = lambda: False`（模拟磁盘
        不可写）语义不变。
        """
        users = getattr(self, "_pending_users", None)
        if users is None:
            users = self._users
        try:
            return self._write_users(users)
        finally:
            self._pending_users = None

    def _mutate(self, fn, fail_msg="无法写入账户文件（磁盘或权限问题），未生效"):
        """跨进程安全的读-改-写（auth-009）：返回 fn 的 (ok, 提示语)。

        调用方传入 `fn(users)`：收到**锁内重读的磁盘快照**并就地修改，返回 (ok, msg)。
        写盘成功才用该快照替换内存表；写失败保持内存原状（调用方无需回滚）。
        """
        with self._lock, self._file_lock():
            users = self._read_disk()
            if users is None:
                users = {u: dict(r) if isinstance(r, dict) else r
                         for u, r in self._users.items()}
            ok, msg = fn(users)
            if not ok:
                self._users = users   # 内存与磁盘对齐（本轮未改动）
                return ok, msg
            self._pending_users = users
            if not self._save():
                return False, fail_msg
            self._users = users
            return True, msg

    def _touch_last_login(self, user):
        """把 last_login 写回磁盘；账户在别处已被删除时静默跳过（auth-009）。"""
        def _apply(users):
            record = users.get(user)
            if isinstance(record, dict):
                record["last_login"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return True, ""

        try:
            ok, _msg = self._mutate(_apply, fail_msg="last_login 未落盘")
            if not ok:
                log.warning("账户 %s 最近登录时间未落盘（不影响本次登录）", user)
        except Exception as e:                     # 记一次登录不应影响认证结论
            log.warning("账户 %s last_login 写盘失败：%s", user, e)

    def count(self):
        """当前账户数（自助注册策略判定用；bootstrap 策略下 >0 即关闭注册）。"""
        with self._lock:
            return len(self._users)

    def bootstrap_used(self):
        """bootstrap 策略是否已消耗过（曾成功注册过账户）。

        auth-010（2026-09-18）：只看 `count()==0` 时，面板删掉唯一账户（或账户文件
        被清空）会静默重开"任意人可自助注册"窗口——确认文案却说"所有观看端都将无法
        登录"，与真实后果相反。这里以持久标记判定"窗口是否用过"。
        """
        try:
            return os.path.exists(self._bootstrap_marker)
        except OSError:
            return False

    def _mark_bootstrap_used(self):
        """记录 bootstrap 窗口已消耗（幂等；写标记失败降级为旧行为，不影响注册）。"""
        try:
            with open(self._bootstrap_marker, "w", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        except OSError as e:
            # auth-015：旧实现是 `except OSError: pass`，零日志——标记写不出去意味着
            # "删空账户即重开注册窗口"的保护实际没生效，却没有任何线索能查到。
            log.warning("bootstrap 已消耗标记写入失败（%s）：删空账户后自助注册窗口"
                        "可能重新打开", e)

    def register(self, user, password):
        """注册新账户，返回 (ok: bool, 提示语)。"""
        user = (user or "").strip()
        if not user or not password:
            return False, "用户名与密码不能为空"
        if len(user) > MAX_USERNAME_LEN:
            return False, "用户名过长（最多 %d 字符）" % MAX_USERNAME_LEN
        # 第 22 条：PBKDF2（10 万次迭代）在锁外计算，避免并发注册/错误口令把全局
        # 账户锁占满导致所有正常登录排队、host CPU 打满。
        salt = secrets.token_hex(16)
        pw_hash = _hash_password(password, salt)
        record = {
            "salt": salt,
            "hash": pw_hash,
            "iter": PBKDF2_ITERATIONS,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        def _apply(users):
            if user in users:
                return False, "用户名已存在"
            users[user] = record
            return True, "注册成功"

        ok, msg = self._mutate(
            _apply, fail_msg="无法写入账户文件（磁盘或权限问题），注册未生效")
        if not ok:
            return ok, msg
        self._mark_bootstrap_used()   # auth-010：bootstrap 窗口一经使用即消耗
        log.info("账户注册成功: %s", user)
        return True, msg

    def authenticate(self, user, password):
        """认证账户，返回 (ok: bool, 提示语)。

        auth-008（2026-09-18）：账号不存在与口令错误统一为"用户名或密码错误"
        （旧版明文路径同样不得暴露账号是否存在）。
        """
        user = (user or "").strip()
        # 第 22 条：锁内只取校验字段，PBKDF2（10 万次迭代）放到锁外计算，避免并发
        # 错误口令占满全局账户锁让所有正常登录排队、host CPU 打满。
        with self._lock:
            record = self._users.get(user)
        # 第 27 条：单条记录字段损坏（缺 salt/hash 或 salt 非十六进制）时优雅拒绝，
        # 不再让 bytes.fromhex 抛错被当成“认证异常”踢连接（否则该账号永远登不进）。
        salt = record.get("salt") if isinstance(record, dict) else None
        expected = record.get("hash") if isinstance(record, dict) else None
        if not isinstance(salt, str) or not isinstance(expected, str):
            if isinstance(record, dict):
                log.warning("账户 %s 记录损坏（缺 salt/hash），拒绝登录", user)
            # 同代价假比较（口令派生的 PBKDF2 仍照算，避免用耗时枚举账号）
            try:
                _hash_password(password or "", "0" * 32)
            except Exception:
                pass
            return False, "用户名或密码错误"
        try:
            matched = hmac.compare_digest(expected, _hash_password(password, salt))
        except (ValueError, TypeError) as e:
            log.warning("账户 %s 口令校验失败（记录损坏）：%s", user, e)
            return False, "账号不存在"
        if not matched:
            return False, "用户名或密码错误"
        self._touch_last_login(user)   # auth-009：读-改-写落在文件锁内
        log.info("账户登录成功: %s", user)
        return True, "登录成功"

    def challenge_for(self, user):
        """D1（实现方案 §10.1）：登录挑战用，返回 (salt_hex, iter) 或 None（账号不存在/记录损坏）。

        旧记录无 iter 字段时回填当前全局迭代数——存储的 hash 本身就是 pbkdf2 派生的
        dk，观看端用 salt+iter 重算即可得到同一把 HMAC 密钥。
        """
        user = (user or "").strip()
        with self._lock:
            record = self._users.get(user)
            if not isinstance(record, dict):
                return None
            salt = record.get("salt")
            pw_hash = record.get("hash")
            if not isinstance(salt, str) or not isinstance(pw_hash, str):
                return None
            iter_ = record.get("iter", PBKDF2_ITERATIONS)
        if not isinstance(iter_, int) or iter_ <= 0:
            iter_ = PBKDF2_ITERATIONS
        # auth-019（2026-09-20）：旧实现只判 `isinstance(salt, str)`，把形态不合规的
        # salt（手改配置、或旧版本落盘的非 32 位 hex）原样下发。观看端在
        # viewer.py:1034 用 `_is_hex_str(salt, KDF_SALT_HEX_LEN)` 拒收，返回的提示
        # 不匹配 `_authenticate` 任何已知分支 → 该账户永久登不进且界面零提示。
        # 这里按"记录损坏"处理（返回 None → 走与账号不存在同一条伪盐路径），
        # 与 auth-006 的凭据长度校验口径一致。
        if not _is_hex_str(salt, KDF_SALT_HEX_LEN):
            log.warning("账户 %s 的 salt 形态不合规（应为 %d 位十六进制），"
                        "按记录损坏处理、不下发挑战", user, KDF_SALT_HEX_LEN)
            return None
        # auth-002（2026-09-18）：记录里的 iter 必须有界——手改/损坏的记录若含
        # 1（弱化 KDF）或 10^9（让观看端挂死）都会被原样下发
        if not (KDF_ITER_MIN <= iter_ <= KDF_ITER_MAX):
            log.warning("账户 %s 记录的 iter=%r 越界，按默认 %d 处理",
                        user, iter_, PBKDF2_ITERATIONS)
            iter_ = PBKDF2_ITERATIONS
        return salt, iter_

    def verify_login_proof(self, user, nonce, proof):
        """D1：校验 auth_proof。key=存储的 dk（hash 字段即 pbkdf2 派生字节），
        期望 proof = HMAC-SHA256(dk, nonce, user)。返回 (ok, 提示语)。

        auth-008（2026-09-18）：账号不存在/记录损坏与"口令错误"统一文案，且做一次
        同代价的假比较（compare_digest）——否则响应时间/文案都能用来枚举账号名。
        """
        user = (user or "").strip()
        with self._lock:
            record = self._users.get(user)
        pw_hash = record.get("hash") if isinstance(record, dict) else None
        if not isinstance(pw_hash, str):
            if isinstance(record, dict):
                log.warning("账户 %s 记录损坏（缺 hash），拒绝登录", user)
            hmac.compare_digest(hmac.new(b"tv-dummy", nonce, "sha256").digest(), proof)
            return False, "用户名或密码错误"
        try:
            dk = bytes.fromhex(pw_hash)
            expected = challenge_mac(dk, nonce, user)   # auth-002：proof 绑定用户名
        except (ValueError, TypeError) as e:
            log.warning("账户 %s 口令校验失败（记录损坏）：%s", user, e)
            # auth-008：记录损坏与口令错误统一文案（不可枚举）
            return False, "用户名或密码错误"
        if not hmac.compare_digest(expected, proof):
            return False, "用户名或密码错误"
        self._touch_last_login(user)   # auth-009：读-改-写落在文件锁内
        log.info("账户登录成功: %s（挑战-应答）", user)
        return True, "登录成功"

    def register_derived(self, user, salt_hex, dk_hex):
        """D1：注册自助派生账户，salt/dk 由观看端计算上送，host 侧口令零接触。
        返回 (ok, 提示语)。

        auth-006（2026-09-18）：user/salt/dk 全部**上下界**校验——旧实现只有下界，
        单条 ~20MB 记录即可撑爆 accounts.json，且每次 _save 都是二次方全量重写。
        """
        user = (user or "").strip()
        if not user or not isinstance(salt_hex, str) or not isinstance(dk_hex, str):
            return False, "用户名与凭据不能为空"
        if len(user) > MAX_USERNAME_LEN:
            return False, "用户名过长（最多 %d 字符）" % MAX_USERNAME_LEN
        # salt 定长 32 位 hex（与观看端 secrets.token_hex(16) 一致）；dk 定长 64 位
        # hex（PBKDF2-SHA256 32 字节摘要）——均只放下界会让超大记录落库
        if not _is_hex_str(salt_hex, KDF_SALT_HEX_LEN) or not _is_hex_str(dk_hex, 64):
            return False, "凭据格式无效（salt 须为 32 位、dk 须为 64 位十六进制）"
        record = {
            "salt": salt_hex,
            "hash": dk_hex,
            "iter": PBKDF2_ITERATIONS,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        def _apply(users):
            if user in users:
                return False, "用户名已存在"
            users[user] = record
            return True, "注册成功"

        ok, msg = self._mutate(
            _apply, fail_msg="无法写入账户文件（磁盘或权限问题），注册未生效")
        if not ok:
            return ok, msg
        self._mark_bootstrap_used()   # auth-010：bootstrap 策略一经使用即消耗
        log.info("账户注册成功: %s（挑战-应答）", user)
        return True, msg

    # ---- B3（实现方案 §5）：账户管理面板的本地运维接口，纯本地读写、不动协议 ----

    def list_users(self):
        """返回 [(用户名, 注册时间, 最近登录)]，按注册时间排序；供面板展示，不含任何口令材料。"""
        with self._lock:
            rows = [
                (u, (r.get("created") or "") if isinstance(r, dict) else "",
                 (r.get("last_login") or "") if isinstance(r, dict) else "")
                for u, r in self._users.items()
            ]
        rows.sort(key=lambda x: (x[1], x[0]))
        return rows

    def remove(self, user):
        """删除账户，返回 (ok, 提示语)。写盘失败不落内存，避免"面板已删、重启复活"。"""
        user = (user or "").strip()

        def _apply(users):
            if not isinstance(users.get(user), dict):
                return False, "账号不存在"
            users.pop(user)
            return True, "删除成功"

        ok, msg = self._mutate(
            _apply, fail_msg="无法写入账户文件（磁盘或权限问题），删除未生效")
        if ok:
            log.info("账户已删除: %s（本地管理）", user)
        return ok, msg

    def set_password(self, user, password):
        """本地改口令：生成新盐重新哈希（第 22 条：PBKDF2 在锁外算）。返回 (ok, 提示语)。

        auth-011（2026-09-18）：改密同步写入当前 `iter`——旧实现继承旧记录字段，
        一旦 PBKDF2_ITERATIONS 常量上调，改密后的账户在挑战阶段仍按旧 iter
        派生，输对口令也永久登录失败。
        """
        user = (user or "").strip()
        if not user or not password:
            return False, "用户名与密码不能为空"
        salt = secrets.token_hex(16)
        pw_hash = _hash_password(password, salt)

        def _apply(users):
            record = users.get(user)
            if not isinstance(record, dict):
                return False, "账号不存在"
            users[user] = dict(record, salt=salt, hash=pw_hash,
                               iter=PBKDF2_ITERATIONS)
            return True, "修改成功"

        ok, msg = self._mutate(
            _apply, fail_msg="无法写入账户文件（磁盘或权限问题），改密未生效")
        if ok:
            log.info("账户口令已重置: %s（本地管理）", user)
        return ok, msg


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
        # B8：最近一次收到该成员**上行视频帧**的时刻（roster 里标注"无信号"源用）。
        # 成员侧共享会话自行终止时既不注销也不产帧，订阅它的观众会停在最后一帧且无提示；
        # 观众端已按 B8 主修（会话自行终止 → unshare + UI 提示）上报，这里是共享端侧的
        # 兜底标注，让"幽灵源"在源列表里可见。
        self.last_up_at = 0.0

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
        # A1（2026-09-11）：补充速率与容量必须分开——名义速率 = max_kbps × 1000/8 = ×125
        # 字节/秒，容量 = 2 秒额度。原实现把"2 秒额度"当成每秒补充量，持续补充速度
        # 是名义值的 2 倍（实测 600 Kbps 档跑出 1312 Kbps），限速形同放宽一倍。
        rate_bytes_s = max(1.0, max_kbps * 125.0)     # kbps -> bytes/s = ×1000/8
        burst_bytes = rate_bytes_s * 2.0              # 容量 = 2 秒额度（容忍关键帧突发）
        max_fps = max(1.0, float(max_fps))
        if self._up_at <= 0.0:
            self._up_tokens = burst_bytes             # 首次：填满，不丢首帧
            self._up_frames = max_fps
        else:
            elapsed = max(0.0, now - self._up_at)
            self._up_tokens = min(burst_bytes, self._up_tokens + elapsed * rate_bytes_s)
            self._up_frames = min(max_fps, self._up_frames + elapsed * max_fps)
        self._up_at = now
        if self._up_tokens < nbytes or self._up_frames < 1.0:
            self._up_dropped += 1
            return False
        self._up_tokens -= nbytes
        self._up_frames -= 1.0
        return True

    def allow_ctrl_broadcast(self, now=None, burst=3, window=10.0):
        """限速触发 O(N) 全量广播的控制消息（net-020，2026-09-18）。

        滑动窗口内最多 burst 条（默认 10 秒 3 条）：正常用户切源/停共享远低于此，
        滥用者则被丢弃并计数（10 秒最多告警一次）。
        """
        now_m = time.monotonic() if now is None else now
        try:
            times = self._ctrl_btimes
        except AttributeError:
            times = self._ctrl_btimes = []
        cutoff = now_m - window
        while times and times[0] < cutoff:
            times.pop(0)
        if len(times) >= burst:
            self._ctrl_bdropped = getattr(self, "_ctrl_bdropped", 0) + 1
            last = getattr(self, "_ctrl_bwarn_at", 0.0)
            if now_m - last >= 10.0:
                self._ctrl_bwarn_at = now_m
                log.warning("客户端 %s 控制消息过于频繁（已丢弃 %d 条触发广播的 "
                            "watch/unshare）", self.addr, self._ctrl_bdropped)
            return False
        times.append(now_m)
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

        C1（2026-09-11）：与 viewer._send_locked / share._send_msg 同一不变量——收紧的
        超时必须在 finally 里还原，否则接收循环的"阻塞基线"被永久改写。
        """
        with self._write_lock:
            prev = None
            saved = False
            try:
                prev = sock.gettimeout()
                saved = True
            except Exception:
                saved = False
            if saved:
                try:
                    sock.settimeout(send_timeout)
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
            cutoff = now - _RATE_KEEP_S
            while self._window and self._window[0][0] <= cutoff:
                self._window.pop(0)
            if send_ms > 0:
                self.last_send_ms = send_ms
                self.last_send_at = now

    def rate_kbps(self, now=None, window=1.0):
        """最近 window 秒的平均发送速率（KB/s）。

        第 112 条（C1）：窗口可调。ABR 用 3 秒（≥ 一个 GOP：keyint=60 时每 2 秒一个
        ~70KB 关键帧，1 秒窗口会在"关键帧窗口 651Kbps"与"纯 P 帧窗口 66Kbps"之间
        来回跳，据此定码率会一步砸到地板）；带宽日志仍用 1 秒保持灵敏。
        """
        now = now if now is not None else time.monotonic()
        win = max(0.1, float(window))
        cutoff = now - win
        total = 0.0
        with self._lock:
            for t, n in self._window:
                if t > cutoff:
                    total += n
        return total / (win * 1024.0)

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

    C1（2026-09-11）：sendall 前的超时收紧在退出时还原（与本函数外的控制消息发送
    同一不变量：接收循环的阻塞基线必须保持）。
    """
    prev = None
    saved = False
    try:
        prev = sock.gettimeout()
        saved = True
        try:
            sock.settimeout(send_timeout)
        except Exception:
            saved = False
    except Exception:
        saved = False
    try:
        while not info.send_stop.is_set() and not runtime.stop_event.is_set():
            try:
                frame = info.send_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
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
        if saved:
            try:
                sock.settimeout(prev)
            except Exception:
                pass
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


class FrpManager:
    """樱花 frpc 子进程生命周期管理器：start/stop/is_running，供 GUI 随时启停。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self._proc = None
        self._external_running = False  # 由 SakuraFrpService/Launcher 托管时不再重复拉起 frpc
        self._last_frpc_line = ""  # 第 63 条：frpc 最近一行输出，验活失败时回报原因
        self._lock = threading.Lock()

    @staticmethod
    def _iter_process_names():
        """枚举本机进程映像名（Toolhelp32 系统快照）。

        第 131 条：原实现 spawn `tasklist` 子进程做进程发现——在反作弊/EDR 眼里那是
        「创建 cmd.exe 并枚举进程」（MITRE T1057），既慢又平白多出一条可疑行为记录。
        CreateToolhelp32Snapshot 只读系统快照：不拉起子进程，也不打开目标进程的句柄。
        非 Windows 或任一 API 不可用时返回空列表（调用方按「未检测到」处理）。
        """
        if sys.platform != "win32":
            return []
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return []
        TH32CS_SNAPPROCESS = 0x00000002
        MAX_PATH = 260

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * MAX_PATH),
            ]

        names = []
        snapshot = None
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            kernel32.Process32FirstW.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
            kernel32.Process32NextW.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if not snapshot or snapshot == wintypes.HANDLE(-1).value:
                return []
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                return []
            while True:
                names.append(entry.szExeFile)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
        except Exception:
            return []
        finally:
            if snapshot:
                try:
                    ctypes.windll.kernel32.CloseHandle(snapshot)
                except Exception:
                    pass
        return names

    @staticmethod
    def _sakura_service_running():
        """检测本机是否已运行 SakuraFrpService（樱花启动器守护进程）。"""
        try:
            return any(
                (name or "").lower() == "sakurafrpservice.exe"
                for name in FrpManager._iter_process_names()
            )
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
            frp = cfg_section(cfg_section(self.cfg, "host"), "frp")   # ui-022 同族收口
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
                # app_dirs()：onedir 打包时 exe 在 <安装根>\fps-host\，frpc.exe 通常
                # 放在安装根，因此除了 exe 目录还要搜上一级
                search_dirs.extend(app_dirs())
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
        """停止自持有的 frpc。

        B3（2026-09-11）：原实现在 `terminate()` 后就无条件宣告"frpc 已终止"，且**先把
        `self._proc` 摘成 None**——若 frpc 卡死（网络中断后卡在 DNS/TLS 重试、被安全软件
        挂起）导致 5 秒内不退出，异常被 `except Exception: pass` 吞掉，GUI 显示"已终止"，
        而应用内再也点不停这个孤儿进程（再点只会得到"frpc 未在运行"）。孤儿 frpc 占着隧道
        与本地端口，下次 start() 会再拉一个（同 token 互踢）→"隧道反复上下线"。

        现在：wait 超时升级为 kill + 再 wait，**只有确认进程已退出才摘句柄并回报成功**；
        仍失败则保留句柄，如实回报"未能停止 frpc（PID x）"。
        """
        with self._lock:
            external = self._external_running
            proc = self._proc
        if external:
            log.info("SakuraFrpService 正在管理隧道，未停止外部樱花服务")
            return "樱花启动器正在托管隧道，未停止外部服务"
        if proc is None:
            return "frpc 未在运行"
        if proc.poll() is not None:
            # 已经自己退出了：只需清句柄
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            log.info("frpc 已终止")
            return "frpc 已终止"
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            # terminate 失败或 5 秒未退出：升级为强杀
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
        if proc.poll() is None:
            # 仍存活：保留句柄（下次还能再停），如实回报
            pid = getattr(proc, "pid", "?")
            log.warning("未能停止 frpc（PID %s），进程仍在运行；可稍后重试或手动结束", pid)
            return "未能停止 frpc（PID %s）" % pid
        with self._lock:
            if self._proc is proc:
                self._proc = None
        log.info("frpc 已终止")
        return "frpc 已终止"

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
        _load_heavy()  # A3（第 133 条）：main() 之外的库用/测试路径在此兜底
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
        self.max_clients = safe_int(net_cfg.get("max_clients", 32), 32, 1, 4096)
        self.msg_timeout = safe_float(net_cfg.get("msg_timeout_s", 15.0), 15.0, 1.0, 600.0)
        self._conn_lock = threading.Lock()
        self._conn_count = 0
        # auth-005（2026-09-18）：未认证（握手+登录）连接的单独名额上限。不加限制时
        # 未认证 socket 可占满全部 max_clients（每条约 7.5 秒开一条即可维持），
        # 对所有正常观众关门；这里给已认证/正常连接预留至少 3/4 名额。
        self.max_pending_auth = max(4, self.max_clients // 4)
        self._pending_count = 0
        # 第 104 条（B3）：向 peer 共享者转发关键帧请求的冷却表（share_id -> 时刻）
        self._peer_key_lock = threading.Lock()
        self._peer_key_at = {}
        # 第 55 条：串行化 roster 广播（快照 + 发送），防陈旧快照覆盖新快照。
        # 与 clients_lock 分离：clients_lock 只在快照瞬间持有，_roster_lock 只保护
        # 「谁是发送者 / 是否有未消化的脏标记」。获取顺序恒为 _roster_lock → clients_lock，
        # 无环、不死锁。
        # 阶段 A2（优化方案 2026-09-17）：单发送者 + 脏标记——同一时刻只有一个线程真正
        # 发送（发送过程不持任何锁），并发调用者置脏即返，不再排队干等最坏 N×2s。
        self._roster_lock = threading.Lock()
        self._roster_sending = False
        self._roster_dirty = False
        host = cfg["host"]
        # 第 107 条（B8）：单个共享成员的上行限额。0=自动：带宽取目标码率×2，
        # 帧率取 2×fps（share.py 按 host.fps 上传，固定 8fps 会误伤正常共享）。
        codec_sec = host.get("codec", {}) if isinstance(host.get("codec"), dict) else {}
        base_kbps = safe_int(codec_sec.get("bitrate_kbps", 2500), 2500, 64)
        self.up_max_kbps = safe_int(net_cfg.get("upstream_max_kbps", 0), 0, 0) \
            or max(600, 2 * base_kbps)
        self.up_max_fps = safe_int(net_cfg.get("upstream_max_fps", 0), 0, 0) \
            or max(8, 2 * safe_int(host.get("fps", 30), 30, 1, 240))
        capture = cfg_section(host, "capture")     # ui-022 同族收口
        self.slot = {
            "jpeg": None,
            "ts": 0,
            "video": None,       # H.264/H.265 Annex-B 帧（视频编码路径）
            "video_ts": 0,       # 该视频帧的采集时刻微秒（packet.pts 透传）
            "is_key": False,     # 视频帧是否关键帧
            "video_codec": CODEC_H264,  # 视频帧编码类型（协议 codec 字节，H.264/HEVC）
            "quality": safe_int(host.get("jpeg_quality", 80), 80, 1, 100),
            "scale": safe_float(host.get("scale", 1.0), 1.0, 0.05, 4.0),
            "fps": safe_int(host.get("fps", 30), 30, 1, 240),
            "encode_ms": 0.0,
            "bitrate": 0,        # 当前视频编码码率（bps，0=JPEG 模式）
            "out_w": 0,        # 最近一帧实际编码输出宽度（target_width 压制后）
            "out_h": 0,        # 最近一帧实际编码输出高度
            "monitor": safe_int(capture.get("monitor", 1), 1, 0),
            "region": normalize_region(capture.get("region")),
            "backend": "dxgi" if capture.get("backend") == "dxgi" else "mss",
        }
        self.slot_lock = threading.Lock()
        # 第 113 条（C2）：流水线唤醒事件。采集写 raw 槽后置 raw_evt，编码消费；
        # 编码写 video/jpeg 槽后置 enc_evt，发送消费。替代原先 1ms/0.5ms 的 sleep 轮询
        # ——静止/无新帧时 CPU 空转归零，有帧时零额外延迟。
        self.raw_evt = threading.Event()
        self.enc_evt = threading.Event()
        self.force_key = False  # 观看端请求关键帧（slot_lock 保护），编码线程消费后清空
        self.video_encoder = None  # 共享的 VideoEncoder 实例（编码线程创建，perf 线程调码率）
        self.codec_lock = threading.Lock()
        # 当前 ABR 目标码率（bps）：ABR 调整后更新，编码线程重建编码器时沿用，
        # 避免重建回到基础码率导致码率震荡
        self.target_bitrate = safe_int(
            cfg_section(host, "codec").get("bitrate_kbps", 2500), 2500, 64) * 1000
        self.stats = {"capture_fps": 0, "encode_fps": 0, "send_fps": 0,
                  "encode_ms": 0.0, "avg_send_ms": 0.0,
                  "codec_name": "", "keyint": 0,
                  "keyframe_total": 0, "abr_changes": 0, "still": False,
                  # 第 112 条（C1）：各客户端最近 1 秒**实测**发送速率汇总（bit/s）
                  "egress_bps": 0}
        self.stats_lock = threading.Lock()
        # B2（第 134 条）：公网隧道自检结论不再只写日志——锁内替换、锁外读取，
        # 供面板行与诊断报告（diag.build_report_zip）消费。
        self._net_status = {"state": "unknown",
                            "detail": "未运行自检", "ts": 0.0}
        self.accounts = AccountManager()
        self.frp = FrpManager(cfg)
        self._next_share_id = 0  # 共享成员序号（连接递增分配，断开不复用）
        self._server = None
        self._server_thread = None
        # 第 18 条：服务线程致命启动错误（如端口被占）置此，供 GUI 显示而非静默死亡
        self.startup_error = None
        # 第 19 条：服务实际绑定地址（双栈为 "0.0.0.0"，指定地址时为该地址），供 GUI 诚实显示
        self.bound_addr = ""
        # A0（2026-09-11）：全部监听 socket（回环双栈 = 127.0.0.1 + [::1]）；_server 为第一个
        self._servers = []
        self.listen_dual_loopback = False

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

    def acquire_pending(self):
        """占用一个"未认证连接"名额（auth-005）；超限返回 False。

        与 acquire_conn 的总名额叠加使用：攻击者用"连上不登录"的 socket 最多只能
        挤满 max_pending_auth（默认 1/4），其余名额留给已认证连接与正常观众。
        """
        with self._conn_lock:
            if self._pending_count >= self.max_pending_auth:
                return False
            self._pending_count += 1
            return True

    def release_pending(self):
        """归还未认证连接名额（幂等）。"""
        with self._conn_lock:
            if self._pending_count > 0:
                self._pending_count -= 1

    def shutdown(self):
        self.stop_event.set()
        # A0：回环双栈会有两个监听 socket（127.0.0.1 + [::1]），全部关闭
        for srv in list(getattr(self, "_servers", []) or []) + ([self._server] if self._server else []):
            try:
                srv.close()
            except OSError:
                pass
        self._servers = []
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
            try:
                info.last_up_at = time.monotonic()   # B8：登记即视为"刚活跃"
            except Exception:
                pass
        self.broadcast_roster()
        log.info("共享成员 %s 上线：%s（%s）", info.addr, info.share_id,
                 info.username or "-")
        return info.share_id

    def _unregister_sharer(self, info):
        """注销共享成员（连接断开时调用）并广播 roster；返回是否真的有注销。"""
        with self.clients_lock:
            if info.share_id is None:
                return False
            share_id = info.share_id
            info.share_id = None
        # C5（2026-09-11）：关键帧冷却表按 share_id 记时且 share_id 单调递增不复用，
        # 原来只增不减——每出现一个新的共享成员就留下一条永不清理的记录（长跑数月 /
        # 反复开共享场景下可观测）。注销时一并弹出。
        table = getattr(self, "_peer_key_at", None)
        lock = getattr(self, "_peer_key_lock", None)
        if table is not None and lock is not None:
            try:
                with lock:
                    table.pop(share_id, None)
            except Exception:
                pass
        self.broadcast_roster()
        return True

    def broadcast_roster(self):
        """向所有存活客户端推送一次 peers 名单（id/name/addr）。

        第 55 条：陈旧快照不得**最后**送达（注册/注销先释放 clients_lock 再广播的旧实现
        会让线程 A 的陈旧快照覆盖线程 B 已发的新名单，观看端源列表永久少一个共享者）。

        阶段 A2（优化方案 2026-09-17）：实现改为「单发送者 + 脏标记」。旧实现让每个
        调用线程各排队完整发送一轮——发送一个卡死客户端最长 2s（第 54 条有限超时），
        争用时后面的调用者要在锁外干等最坏 N×2s，期间新成员的 roster 迟迟发不出。
        现在同一时刻只有一个发送循环（发送过程不持任何锁）：并发调用者在 _roster_lock
        内看到 _roster_sending=True 就置脏并立即返回；发送者收尾时发现脏标记，就以
        最新客户端表再发一轮。第 55 条不变量仍在——最后送达的必然是最后快照的名单，
        且任何一轮都从「当前」状态重新快照（不携带陈旧行）。

        失败连接照旧出锁后经 `_drop_client` 移除：其内部 `_unregister_sharer` 会再次
        调用本函数，此时要么发送者仍是自己（只置脏，收尾时重发一轮含"已移除"的最新
        名单），要么发送者身份已让出（正常竞争）。
        """
        with self._roster_lock:
            if self._roster_sending:
                self._roster_dirty = True
                return
            self._roster_sending = True
            self._roster_dirty = False
        try:
            while True:
                failed = []
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
                    row = {"id": c.share_id,
                           "name": (c.username or "").strip() or addr_txt,
                           "addr": addr_txt}
                    # B8：超过 _MEMBER_STALE_S 没有上行帧的成员标注为"无信号"（它的共享
                    # 会话很可能已自行终止），避免观众选中"幽灵源"停在最后一帧却毫无提示。
                    # 只在真的无信号时才加这个键——roster 消息格式对旧观看端保持兼容。
                    if time.monotonic() - getattr(c, "last_up_at", 0.0) > _MEMBER_STALE_S:
                        row["stale"] = True
                    rows.append(row)
                msg = {"action": Act.PEERS, "peers": rows}
                for sock, info in snapshot:
                    try:
                        info.send_ctrl(sock, msg)
                    except OSError as e:
                        # 第 105 条（B4）：可能已写出半截消息，连接必须移除
                        log.debug("向 %s 推送 roster 失败（按连接损坏处理）: %s", info.addr, e)
                        failed.append((sock, info))
                # 出 _roster_lock 后再移除：_drop_client → _unregister_sharer →
                # broadcast_roster 会再次进入本函数，绝不能在持锁时调用。
                for sock, info in failed:
                    _drop_client(self, sock, info, "控制消息写入失败")
                with self._roster_lock:
                    if not self._roster_dirty:
                        self._roster_sending = False
                        return
                    self._roster_dirty = False
        except BaseException:
            # 异常不得让 _roster_sending 卡在 True（否则此后所有人只置脏、无人发送）
            with self._roster_lock:
                self._roster_sending = False
            raise

    def update_capture(self, monitor=_UNSET, region=_UNSET, backend=_UNSET):
        """更新采集源配置并即时生效（写配置 + 写共享槽，采集线程每帧读取）。

        第 32 条：用 _UNSET 哨兵区分「未提供」与「显式传 None」。region=None 表示
        清空采集区域回到全屏（GUI「恢复全屏」依赖此语义），而非旧实现里的「跳过不改」。
        """
        # ui-022（2026-09-20）：setdefault 只挡"键不存在"，`"capture": 1` 这类标量
        # 会被原样返回，随后的 `capture["monitor"] = …` 抛 TypeError → 采集按钮静默失效。
        capture = cfg_section(self.cfg["host"], "capture")
        if self.cfg["host"].get("capture") is not capture:
            self.cfg["host"]["capture"] = capture
        if monitor is not _UNSET:
            monitor = safe_int(monitor, 1, 0)
            capture["monitor"] = monitor
        if region is not _UNSET:
            # 第 100 条：写入前规范化，杜绝"缺键/负值区域"进配置与共享槽
            region = normalize_region(region)
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

    def set_net_status(self, state, detail):
        """B2（第 134 条）：记录一次公网自检结论（state: ok|fail|unknown）。"""
        with self.stats_lock:
            self._net_status = {"state": state, "detail": detail,
                                "ts": time.time()}

    def get_snapshot(self):
        """返回供 GUI 刷新的状态快照（线程安全）。

        阶段 A2（优化方案 2026-09-17）：clients_lock 内只做列表快照，逐个客户端的
        统计（rate_kbps 等）出锁计算——旧实现在锁内逐个统计，GUI 刷新频繁时会让
        握手入册/连接注销的线程排在后面等这些计算。
        """
        with self.clients_lock:
            infos = list(self.clients.values())
        clients = []
        for info in infos:
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
            net_status = dict(self._net_status)
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
            # ui-022（2026-09-20）：旧写法 `self.cfg["host"].get("frp", {})` 只挡了
            # "键不存在"、挡不住"值不是字典"。host.auth 被写成非 None 标量（false/0/""/[]）
            # 时这里抛 AttributeError——而它在 _poll 的 try 内，异常发生在 snap 取到之前，
            # 于是 _refresh(snap) 永不执行：控制台从此只显示旧值（在线观众数/fps/码率/
            # 监听状态全部冻结），并以约 2 条/秒刷"状态刷新失败"。
            "public_addr": cfg_section(self.cfg["host"], "frp").get("public_addr", ""),
            "auth_enabled": bool(cfg_section(self.cfg["host"], "auth").get("enabled", True)),
            "clients": clients,
            "perf": perf,
            "capture": capture,
            "frp_running": self.frp.is_running(),
            "server_up": self._server is not None,
            "bound_addr": self.bound_addr or "0.0.0.0",
            "startup_error": self.startup_error,
            "net_status": net_status,
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

    限速发生在**转发之前**，所以超额流量既不会复制给订阅者，也不会进入
    _fwd_member_frame 的中继入队（A4 §3.1 后中继为原始 bytes 零重打包）。
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


def _forward_upstream_frame(runtime, sock, info, kind, payload, addr, raw=None):
    """处理客户端上行帧（MSG_VIDEO/MSG_FRAME）：登记共享者 → 校验 → 限速 → 路由。

    第 26 条：中继前校验负载——视频帧走 parse_video 校验结构，JPEG 帧至少含
    SOI(FFD8) 标记；0 字节/损坏负载不得直达订阅端。

    A4 §3.1：调用方（_client_recv_loop）用 parse_message_raw 一次物化整条消息
    并以 `raw` 传入时，转发直接复用该 bytes（payload 只是它的只读视图），
    不再重打包；`raw=None`（如测试直接调用）则回退为按 kind/payload 重建。
    """
    if raw is None:
        raw = _MSG_HDR.pack(kind, len(payload)) + payload
    if kind == MSG_VIDEO:
        try:
            _ts, _codec, flags, _nal = parse_video(payload)
        except ValueError:
            log.warning("客户端 %s 上行视频帧损坏，忽略", addr)
            return
        # C8（2026-09-11）：**先校验负载、后登记共享者**。原实现先登记再 parse_video，
        # 一条损坏视频帧即可让该连接出现在所有观看端的源列表里并触发一次 roster 广播
        # （随后又因超限/断开消失）——表现为面板源列表抖动。
        if info.share_id is None:
            runtime._register_sharer(sock, info)
        if not _allow_upstream(runtime, info, payload, addr):   # 第 107 条（B8）
            return
        _fwd_member_frame(runtime, info, kind, payload,
                          is_key=bool(flags & VIDEO_FLAG_KEY), raw=raw)
        return
    # MSG_FRAME：payload = [8B ts][JPEG]
    if len(payload) < 10 or payload[8:10] != b"\xff\xd8":
        log.debug("客户端 %s 上行 JPEG 帧损坏（长度 %d），忽略", addr, len(payload))
        return
    if info.share_id is None:
        runtime._register_sharer(sock, info)
    if not _allow_upstream(runtime, info, payload, addr):       # 第 107 条（B8）
        return
    _fwd_member_frame(runtime, info, kind, payload, is_jpeg=True, raw=raw)


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
        sndbuf=safe_int(net_cfg.get("sndbuf_kb", 1024), 1024, 0) * 1024,
        rcvbuf=safe_int(net_cfg.get("rcvbuf_kb", 2048), 2048, 0) * 1024,
        keepalive=bool(net_cfg.get("keepalive", True)),
        keepalive_idle=safe_int(net_cfg.get("keepalive_idle_s", 60), 60, 1, 3600),
    )
    try:
        sock.settimeout(HANDSHAKE_TIMEOUT_S)
    except OSError:
        pass
    try:
        # net-031（2026-09-20）：握手阶段补**总时限**（此前只有每次 recv 的 10 秒超时，
        # 滴字节的对端最坏可把一次握手拖到 4×10=40 秒），与认证阶段 net-022 同口径。
        server_handshake(sock, time.monotonic() + HANDSHAKE_TIMEOUT_S)
    except Exception as e:
        log.warning("客户端 %s 握手失败（%.0f 秒内未完成）: %s",
                    addr, HANDSHAKE_TIMEOUT_S, e)
        try:
            sock.close()
        except OSError:
            pass
        return False
    return True


def _register_allowed(runtime, auth_cfg, addr):
    """判断本次自助注册是否放行，返回 (allowed, 拒绝原因)。

    第 131 条：开启准入但注册完全开放时，任何能连上端口的人都可以自建账号看屏，
    等于没做准入。默认仍保持 open（不改变既有使用习惯），但提供 bootstrap/closed
    两种策略，并在共享端控制台暴露开关与提示。
    """
    policy = register_policy(auth_cfg)
    if policy == "open":
        return True, ""
    if policy == "closed":
        log.warning("客户端 %s 尝试注册，但自助注册已关闭", addr)
        return False, "共享端已关闭自助注册，请向共享端索取账号"
    # bootstrap：只允许创建第一个账户——公网隧道已暴露、账户表还是空的时候，
    # 不能让人人都能自建账号，那样准入形同虚设。
    # auth-010（2026-09-18）：以"曾注册过"的持久标记判定是否消耗过该窗口，而不是
    # 只看当前账户数——否则删掉唯一账户（或清空账户文件）会静默重开"任意人可注册"。
    # auth-015（2026-09-20）：删掉旧实现为测试替身保留的 `except AttributeError`
    # 退化分支（它把"没有标记接口"直接当成"没有消耗过"，正在被测试依赖，见 T-04）；
    # 替身改为显式提供 bootstrap_used()。
    bootstrap_open = (runtime.accounts.count() == 0
                      and not runtime.accounts.bootstrap_used())
    if bootstrap_open:
        return True, ""
    log.warning("客户端 %s 尝试注册，但 bootstrap 注册窗口已关闭，已拒绝", addr)
    return False, "共享端已有账户，自助注册已关闭（请向共享端索取账号）"


def _str_field(msg, key, default=""):
    """从控制/认证消息里安全取字符串字段，返回 (value, ok)。

    B1（2026-09-11）：第 104 条（B11）只给 `_auth_required_phase` 补了 `isinstance`
    校验，同一份输入却还出现在另外两条路径上——`{"user": 123}` 在 `_auth_optional_phase`
    与 `_dispatch_ctrl` 的迟到认证分支里抛 `AttributeError`，被外层 `except` 记成"认证
    解析失败/异常"并**悄悄断开连接**（日志原因与真实原因不符，排障困难）。

    统一口径：字段缺失/为 None → 返回 (default, True)；存在但**非字符串** → 返回
    (default, False)，由调用方决定是回 `auth_result ok=false` 还是忽略该字段。
    """
    if not isinstance(msg, dict):
        return default, False
    value = msg.get(key)
    if value is None:
        return default, True
    if isinstance(value, str):
        return value, True
    return default, False


def _clip(text, n=200):
    """把**对端可控**的字符串截到 n 个字符再进日志（auth-018 / auth-021 的统一出口）。

    logger 不截断、滚动 5MB×5，一条未认证报文就能把真实日志整段冲掉；凡是准备
    `%s` / `%r` 进日志的对端字段，一律先过这里。
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    return text if len(text) <= n else text[:n] + "…(共 %d 字符)" % len(text)


CHALLENGE_TTL_S = 60.0  # D1（实现方案 §10.1）：挑战 nonce 有效期，超时须重新发起登录

#: auth-005（2026-09-18）：认证期的分档"进展"时限——总时限（auth_timeout，默认
#: 240s）单独存在时，攻击者连上后一次不发也能占满一个连接名额 240 秒，约每 7.5 秒
#: 开一条 socket 即可永久占满 max_clients。拆档后：
#:  - 首条消息 30s：新版观看端握手后立即发 probe/login（等待界面就绪在**连接之前**），
#:    正常远快于此；
#:  - 下发挑战后 60s：观看端收到挑战时用户已提交口令，正常 1 秒内回 proof；
#:  - probe 后等用户输密码：须覆盖登录框最长等待（120s），给 150s。
AUTH_FIRST_MSG_S = 30.0
AUTH_DIALOG_WINDOW_S = 150.0
#: 挑战应答的宽限：挑战 TTL 到期后仍多等这几秒，让"挑战已过期"的明确回复能发出去
#: （而不是直接在 recv 超时处断开，用户看到的只是"连接断了"）
AUTH_CHALLENGE_GRACE_S = 5.0


def _fake_login_salt(user):
    """账号不存在时下发的盐（auth-008 → auth-014 收口，2026-09-20）。

    auth-014：旧实现返回 `hmac(b"tv-nouser", user)`——密钥硬编码在源码里、无任何
    服务端随机量，对端拿到 CHAL 后用同一个函数本地重算即可 100% 判定账号是否存在，
    枚举判据从"看文案"退化成"比对本地产物"，且可完全离线批量验证用户名。

    现在直接下发**真随机盐**（与真实账号的 secrets.token_hex(16) 同分布、无指纹）。
    不存在的账号本就不需要盐参与校验：应答阶段 verify_login_proof 走的是同代价
    dummy 比较（host.py 内 `hmac.new(b"tv-dummy", nonce, …)`），盐只在 CHAL 报文里
    过网一次，因此随机化不影响任何正确性路径，代价为零。
    """
    return secrets.token_hex(16)


def _auth_required_phase(sock, addr, runtime, auth_cfg):
    """准入开启：D1 挑战-应答认证状态机 + 旧版明文凭据兼容分支；返回 (放行?, 用户名)。

    第 23 条：认证阶段强制「总时限」而非「每次 recv 超时」，防 slowloris 无限占用线程。
    第 31 条：总时限须大于观看端登录框等待（120s），否则用户还在输入 host 就先断开。
    第 104 条（B11）：user/pass 必须是字符串——旧写法在 {"user":123} 上抛
    AttributeError 被记成"认证异常"，用户看到的是"认证通信失败"而非"用户名或密码错误"。
    B10（2026-09-11）：认证窗口下界抬到 AUTH_TIMEOUT_MIN_S，见 safe_float 约束。

    D1（实现方案 §10.1–10.2）：口令不再过网。
      登录:   C→S {login, user}  → S→C {chal, nonce, salt, iter}
              C→S {auth_proof, user, proof=HMAC-SHA256(dk, nonce)}
      注册:   C→S {register, user} → S→C {chal, nonce, iter}（salt 由观看端生成）
              C→S {reg_proof, user, salt, dk, proof}
    nonce 一次性（校验前先摘除）、绑定本连接、CHALLENGE_TTL_S 过期。
    host.auth.chal_response="require"（默认）拒绝旧版直发口令；"allow_legacy" 临时
    放行并记 WARNING（含用户名与来源地址），一个发布周期后删除该分支。
    """
    auth_timeout = safe_float(auth_cfg.get("auth_timeout", AUTH_TIMEOUT_DEFAULT_S),
                               AUTH_TIMEOUT_DEFAULT_S, AUTH_TIMEOUT_MIN_S, 3600)
    mode = auth_cfg.get("chal_response", "require")
    if mode != "allow_legacy":
        if mode != "require":
            log.warning("auth.chal_response=%r 非预期值，按 require 处理", mode)
        mode = "require"
    deadline = time.monotonic() + auth_timeout
    # auth-005：当前 recv 的截止时刻。首条消息 30s；此后每收到一条完整消息（=进展）
    # 恢复到总时限；下发挑战 / probe 后按对应档位收紧（见常量注释）。
    msg_deadline = min(deadline, time.monotonic() + AUTH_FIRST_MSG_S)
    sock.settimeout(auth_timeout)
    pending = None  # 本连接当前挑战：{kind, user, nonce, salt, iter, exp}

    def _issue_challenge(kind, user, salt=None, iter_=PBKDF2_ITERATIONS):
        nonlocal pending, msg_deadline
        nonce = secrets.token_bytes(16)
        pending = {"kind": kind, "user": user, "nonce": nonce,
                   "salt": salt, "iter": iter_,
                   "exp": time.monotonic() + CHALLENGE_TTL_S}
        msg_deadline = min(deadline, time.monotonic() + CHALLENGE_TTL_S
                           + AUTH_CHALLENGE_GRACE_S)
        reply = {"action": Act.CHAL, "nonce": b64e(nonce), "iter": iter_,
                 "proto": list(PROTOCOL_VERSION)}
        if salt is not None:
            reply["salt"] = salt
        send_msg(sock, reply)

    def _reject_and_close(text, why_log=None):
        send_msg(sock, {"action": Act.AUTH_RESULT, "ok": False, "msg": text})
        if why_log:
            log.warning("客户端 %s %s", addr, why_log)
        sock.close()
        return False, None

    try:
        while True:
            msg = recv_msg(sock, msg_deadline)
            # auth-005：收到任何完整消息都算进展，后续消息回到总时限
            msg_deadline = deadline
            if not isinstance(msg, dict):
                # net-026（2026-09-18）：发 `[1,2]` 这类合法 JSON 非对象时旧实现
                # 直接在 `.get` 上抛 AttributeError，被兜底记成"认证通信失败"
                # （误导排障）；这里按协议错误明确拒绝。
                return _reject_and_close("认证消息格式非法（应为 JSON 对象）",
                                         "认证消息不是 JSON 对象（%s），已断开"
                                         % type(msg).__name__)
            action = msg.get("action")
            # C2 §7 第 3 步：认证期 CTRL 随带 proto=[2,2]。主判仍是 MAGIC，
            # 这里只留痕；PROTO_ENFORCE（当前 False）翻转后不兼容客户端被拒。
            present, compatible = proto_compatible(msg)
            if present and not compatible:
                log.warning("客户端 %s 协议版本不兼容（proto=%r，服务端=%s）",
                            addr, msg.get("proto"), PROTOCOL_VERSION)
                if PROTO_ENFORCE:
                    return _reject_and_close("协议版本不匹配，请更新观看端")
            raw_user, ok_user = _str_field(msg, "user")
            raw_pass, ok_pass = _str_field(msg, "pass")
            if not ok_user:
                return _reject_and_close("认证消息格式非法（user 必须是字符串）",
                                         "认证消息 user 类型非法（%s），已拒绝"
                                         % type(msg.get("user")).__name__)
            if not ok_pass:
                return _reject_and_close("认证消息格式非法（pass 必须是字符串）",
                                         "认证消息 pass 类型非法（%s），已拒绝"
                                         % type(msg.get("pass")).__name__)
            user = (raw_user or "").strip()
            # auth-018（2026-09-20）：MAX_USERNAME_LEN 旧实现只加在 register /
            # register_derived 的**落库处**，LOGIN / AUTH_PROOF 的 user 字段没有校验，
            # 而 recv_msg 允许 MAX_PAYLOAD=32MB、_str_field 不截断——未认证对端发一条
            # 32MB user 的 LOGIN 即可在 1853/1866/1871/1893/1900 与 _auth_optional_phase
            # 的日志行里产出 32MB 日志（recv 缓冲 + str + 格式化峰值约 130MB），
            # 把真实日志整体冲掉。限制加在读取处，所有下游回显点一次性收口。
            # 文案与"口令错误"统一，不因长度新增枚举面。
            if len(user) > MAX_USERNAME_LEN:
                return _reject_and_close(
                    "用户名或密码错误",
                    "登录用户名超长（%d 字符，上限 %d），已拒绝"
                    % (len(user), MAX_USERNAME_LEN))
            password = raw_pass or ""

            # ---------- 应答阶段：auth_proof / reg_proof ----------
            if action in (Act.AUTH_PROOF, Act.REG_PROOF):
                # auth-016（2026-09-20）：次版本门控旧实现只做在观看端
                # （viewer.py 要求 proto[1] >= PROTO_CHAL_MIN_MINOR），host 侧
                # common.proto_compatible 只判主版本 ==2，于是 v2.2 的观看端按
                # "主版本相同即兼容"发来旧口径 proof（只签 nonce，不绑 user），
                # host 校验必然失败 → 命中观看端"口令错误"分支，**清空已存凭据并写回
                # config**：老客户端既登不上、又丢了保存的口令，界面还只显示套话。
                pr = msg.get("proto")
                if (isinstance(pr, (list, tuple)) and len(pr) >= 2
                        and isinstance(pr[1], int) and pr[1] < PROTO_CHAL_MIN_MINOR):
                    return _reject_and_close(
                        "观看端版本过旧，请更新后重试",
                        "%s 来自次版本过旧的观看端（proto=%r，服务端=%s），已拒绝且"
                        "不动其对账凭据" % (action, pr, PROTOCOL_VERSION))
                kind = "login" if action == Act.AUTH_PROOF else "register"
                if pending is None or pending["kind"] != kind or user != pending["user"]:
                    return _reject_and_close("没有进行中的挑战，请重新登录",
                                             "无进行中挑战即提交 %s，已断开" % action)
                p, pending = pending, None  # nonce 一次性：校验前先摘除，防重放
                if time.monotonic() > p["exp"]:
                    return _reject_and_close("挑战已过期，请重新登录",
                                             "挑战过期（>%d 秒）后仍提交应答" % int(CHALLENGE_TTL_S))
                raw_proof, ok_proof = _str_field(msg, "proof")
                proof = None
                if ok_proof and raw_proof:
                    try:
                        proof = b64d(raw_proof)
                    except ValueError:
                        proof = None
                if proof is None:
                    # auth-021（2026-09-20）：整段 %r 会把最长 32MB 的 proof 原样写进
                    # 日志（`%r` 还会连引号与转义一起展开，体积略增）。raw_proof 与
                    # auth-018 的 user 一样来自**未认证**对端可控的报文，触发只需三步：
                    # LOGIN → 收挑战 → 乱填 proof，比 auth-018 还少一步前提。
                    # 凭据类字段一律只记长度 + 前 8 位。
                    return _reject_and_close(
                        "挑战应答格式非法",
                        "auth proof 格式非法（长度 %d，前缀 %r），已断开"
                        % (len(raw_proof or ""), _clip(raw_proof, 8)))
                if kind == "login":
                    ok, text = runtime.accounts.verify_login_proof(user, p["nonce"], proof)
                else:
                    raw_salt, ok_s = _str_field(msg, "salt")
                    raw_dk, ok_d = _str_field(msg, "dk")
                    dk_bytes = None
                    if ok_s and ok_d and raw_salt and raw_dk:
                        try:
                            dk_bytes = bytes.fromhex(raw_dk)
                        except ValueError:
                            dk_bytes = None
                    if dk_bytes is None:
                        return _reject_and_close("注册凭据格式非法",
                                                 "reg_proof salt/dk 格式非法，已断开")
                    if not hmac.compare_digest(
                            challenge_mac(dk_bytes, p["nonce"], user), proof):
                        ok, text = False, "挑战校验失败"
                    else:
                        ok, text = runtime.accounts.register_derived(user, raw_salt, raw_dk)
                send_msg(sock, {"action": Act.AUTH_RESULT, "ok": ok, "msg": text})
                if ok:
                    return True, user
                log.warning("客户端 %s 认证失败: %s（%s）", addr, text, user or "-")
                sock.close()
                return False, None

            # ---------- 发起阶段：login / register ----------
            if action == Act.LOGIN:
                if password:
                    if mode == "require":
                        return _reject_and_close("观看端版本过旧，请更新后重试",
                                                 "以旧版直发口令登录，require 模式下已拒绝（%s）"
                                                 % (user or "-"))
                    log.warning("客户端 %s（%s）走旧版认证，口令明文过网（allow_legacy），"
                                "请更新观看端后将 auth.chal_response 改回 require",
                                addr, user or "-")
                    ok, text = runtime.accounts.authenticate(user, password)
                    send_msg(sock, {"action": Act.AUTH_RESULT, "ok": ok, "msg": text})
                    if ok:
                        return True, user
                    log.warning("客户端 %s 认证失败: %s（%s）", addr, text, user or "-")
                    sock.close()
                    return False, None
                got = runtime.accounts.challenge_for(user)
                if got is None:
                    # auth-008（2026-09-18）：账号不存在不再单独回 "账号不存在"
                    # （响应有无即可枚举用户名）——照常下发挑战（确定性伪盐），
                    # 应答阶段与"口令错误"统一报"用户名或密码错误"。
                    log.info("客户端 %s 登录请求：账号不存在（已下发伪盐挑战）", addr)
                    _issue_challenge("login", user, salt=_fake_login_salt(user))
                else:
                    _issue_challenge("login", user, salt=got[0], iter_=got[1])
                continue
            if action == Act.REGISTER:
                allowed, why = _register_allowed(runtime, auth_cfg, addr)
                if not allowed:
                    return _reject_and_close(why)
                if password:
                    if mode == "require":
                        return _reject_and_close("观看端版本过旧，请更新后重试",
                                                 "以旧版直发口令注册，require 模式下已拒绝（%s）"
                                                 % (user or "-"))
                    log.warning("客户端 %s（%s）走旧版注册，口令明文过网（allow_legacy），"
                                "请更新观看端后将 auth.chal_response 改回 require",
                                addr, user or "-")
                    ok, text = runtime.accounts.register(user, password)
                    send_msg(sock, {"action": Act.AUTH_RESULT, "ok": ok, "msg": text})
                    if ok:
                        return True, user
                    log.warning("客户端 %s 注册失败: %s（%s）", addr, text, user or "-")
                    sock.close()
                    return False, None
                _issue_challenge("register", user)
                continue
            if action == Act.PROBE:
                # 观看端无凭据时的探测：告知需要登录，但不作为认证失败告警记录。
                # D1：随带 proto，供新版观看端判定服务端是否支持挑战-应答
                # （旧服务端的回复没有该字段 → 走 allow_legacy 直发路径）。
                send_msg(sock, {"action": Act.AUTH_RESULT, "ok": False,
                                "msg": "需要登录", "proto": list(PROTOCOL_VERSION)})
                # 第 30 条：probe 后保持连接，等观看端在同一 socket 提交真实凭据。
                # auth-005：此时用户可能正对着登录框输入，窗口收紧到
                # AUTH_DIALOG_WINDOW_S（覆盖观看端登录框 120s 上限），
                # 而不是让"probe 后空等"吃掉完整 auth_timeout。
                msg_deadline = min(deadline, time.monotonic() + AUTH_DIALOG_WINDOW_S)
                continue
            return _reject_and_close("不支持的认证请求")
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
        # net-022（2026-09-18）：可选 auth 阶段同样要有**总时限**——只设 per-recv
        # 超时时，攻击者每 <2 秒喂 1 字节即可让这条消息永久挂住线程 + 连接名额
        # （准入关闭时该分支无登录框等待，不存在需要放宽的合法场景）。
        deadline = time.monotonic() + 2.0
        sock.settimeout(2.0)
        msg = recv_msg(sock, deadline)
        # C2 §7 第 3 步：准入关闭路径同样只留痕不启用（与 _do_auth 一致）
        present, compatible = proto_compatible(msg)
        if present and not compatible:
            log.warning("客户端 %s 协议版本不兼容（proto=%r，服务端=%s）",
                        addr, msg.get("proto"), PROTOCOL_VERSION)
            if PROTO_ENFORCE:
                send_msg(sock, {"action": Act.AUTH_RESULT, "ok": False,
                                "msg": "协议版本不匹配，请更新观看端"})
                sock.close()
                return False, None
        # B1：非字符串 user 不再抛 AttributeError（那会被下面 except 记成"可选 auth 解析
        # 失败"并断开连接）；这里忽略该字段继续放行，只留一条明确日志。
        user, ok_user = _str_field(msg, "user")
        if not ok_user:
            log.warning("客户端 %s 可选 auth 消息 user 类型非法（%s），已忽略该字段",
                        addr, type(msg.get("user") if isinstance(msg, dict) else None).__name__)
            user = ""
        user = (user or "").strip()
        # auth-018（2026-09-20）：这条路径在未认证前读取对端可控的 user，且无长度
        # 校验（准入关闭时任何人都能走到这里）——进日志前先截断。
        user = _clip(user, MAX_USERNAME_LEN)
        send_msg(sock, {"action": Act.AUTH_RESULT, "ok": True,
                        "msg": "账户准入已关闭，无需登录",
                        "proto": list(PROTOCOL_VERSION)})
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
    auth_cfg = cfg_section(host_cfg, "auth")
    # auth-022（2026-09-20）：这里旧写法是 `auth_cfg = {}` 后
    # `auth_cfg.get("enabled", False)` → 一条把 host.auth 写成字符串/数字/数组的
    # 坏配置就让**准入静默关闭**，而同一份配置下 register_policy 按缺键取默认
    # "open" —— 一次输入错误同时关掉两道安全开关，任何能连上的人都能自建账号看屏。
    # 安全开关的兜底取向必须与 auth-007 对 allow_register 的既定口径一致：fail-closed。
    if not isinstance(host_cfg.get("auth"), dict):
        log.warning("配置 host.auth 不是对象（%r），按**开启准入**处理（fail-closed）",
                    host_cfg.get("auth"))
    if auth_cfg.get("enabled", True):
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
    if not isinstance(src, str):
        # B1：{"source": ["x"]} 会让 `src in _live_share_ids(runtime)` 抛
        # TypeError: unhashable type: 'list'，连接被静默断开。非字符串源按"无效源"回绝。
        log.debug("watch 拒绝非字符串源 %r（%s）", src, addr)
        if not _reply_ctrl(sock, info, {"action": Act.WATCH_REJECT,
                                        "source": None, "reason": "invalid"}, addr):
            return "控制消息写入失败（连接已损坏）"
        return None
    if src != "local" and src == info.share_id:
        log.debug("watch 拒绝订阅自身 %r（%s）", src, addr)
        if not _reply_ctrl(sock, info, {"action": Act.WATCH_REJECT,
                                        "source": src, "reason": "self"}, addr):
            return "控制消息写入失败（连接已损坏）"
    elif src == "local" or src in _live_share_ids(runtime):
        info.watch_source = src
        info.need_key = True  # 切换源：重新从关键帧开始收
        if src != "local":
            _send_ctrl_safe(runtime, _sharer_sock(runtime, src),
                            {"action": Act.REQ_KEYFRAME})
    else:
        log.debug("watch 无效源 %r（%s），保持原源 %r", src, addr, info.watch_source)
        if not _reply_ctrl(sock, info, {"action": Act.WATCH_REJECT,
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
    if action == Act.PING:
        if not _reply_ctrl(sock, info, {"action": Act.PONG, "t": msg.get("t", 0)}, addr):
            return "控制消息写入失败（连接已损坏）"
    elif action == Act.CAP_PROBE:
        if not _reply_ctrl(sock, info, {"action": Act.CAP, "multiview": True}, addr):
            return "控制消息写入失败（连接已损坏）"
    elif action == Act.WATCH:
        # net-020（2026-09-18）：watch/unshare 会触发 O(N) 全量 roster 广播
        # （逐个客户端上 _write_lock 同步写），且不经过上行帧限速——廉价控制消息
        # 即可把 CPU 与目标观众的 _write_lock 打满。这里加一道独立限速。
        if not info.allow_ctrl_broadcast():
            # net-029（2026-09-20）：net-020 加了配额却对被限流的请求**不回话**——
            # 观看端 switch_source 是"先本地提交、再发包"，只有收到 WATCH_REJECT 才
            # 回滚，于是被丢弃的那条让状态栏显示「队友·X」而实际是本地画面；若被丢的
            # 是"切回 local"那条，host 仍按已失效的 peer 源路由 → 永久黑屏且无自愈路径。
            # 触发面不窄：burst=3/10s 且 WATCH 与 UNSHARE 共用同一桶，重连自动补发
            # WATCH、队友离线自动切回 local、面板双击切源都能在 10 秒内叠到 4 条。
            log.debug("客户端 %s watch 被限速，回 WATCH_REJECT 让对端回滚", addr)
            if not _reply_ctrl(sock, info, {"action": Act.WATCH_REJECT,
                                            "source": msg.get("source"),
                                            "reason": "throttled"}, addr):
                return "控制消息写入失败（连接已损坏）"
            return None
        return _ctrl_watch(sock, info, runtime, msg, addr)
    elif action == Act.UNSHARE:
        if not info.allow_ctrl_broadcast():
            # net-029：停共享被限流时同样要给对端一个回话。这里不能复用 WATCH_REJECT
            # （观看端会据此切回 local，而语义上共享并未停止），改为回一条明确的
            # 错误控制消息，并把该连接的**待处理 unshare 合并**到配额窗口之后由
            # roster 变化自然收敛——成员仍在 roster 中，观看端不会误切。
            log.debug("客户端 %s unshare 被限速（合并到下一次广播）", addr)
            return None
        # 成员主动停止共享（连接保留）：注销并广播 roster，
        # 让订阅该 peer 的观看端回落 local（viewer peers 处理）
        if info.share_id is not None:
            old_id = info.share_id
            runtime._unregister_sharer(info)
            log.info("共享成员 %s 注销：%s（%s）", addr, old_id, info.username or "-")
    elif action == Act.REQ_KEYFRAME:
        # codec-014（2026-09-20）：客户端主动路径旧实现**无合并、无限速**——一条约
        # 40 字节的消息即可把本机编码器钉成帧帧 IDR（每次 IDR 的码率开销远高于 P 帧），
        # 而 net-020 的配额只挂在 WATCH/UNSHARE 上、上行令牌桶只管视频帧。
        # 内部愈合路径 _request_peer_keyframe 本来就带 1 秒冷却，两端口径不一致。
        # 现在两条路径统一走带冷却的去重。
        if info.watch_source.startswith("peer:"):
            # 转达成员端：其共享会话强制出一帧关键帧（第 56 条：安全发送）。
            # 复用同一份冷却表，避免每条客户端消息都做一次 clients 全表扫描 + 同步 sendall。
            _request_peer_keyframe(runtime, info.watch_source)
        else:
            if _request_local_keyframe(runtime):
                log.debug("客户端 %s 请求关键帧，已置位强制 IDR", addr)
    elif action in (Act.PROBE, Act.LOGIN, Act.REGISTER):
        # 第 100 条：迟到超过 2 秒的认证消息。准入关闭时上面的可选 auth 窗口（select 2s）
        # 已把本连接按"旧客户端"放行，此后到达的 probe/login 若无人应答，观看端会把
        # 下一条 roster/pong 当认证回复 → 在"无需登录"的服务端上反复弹登录框、最后报
        # "需要登录"。这里补一个明确答复（两种准入状态下都安全）。
        late_user, ok_late_user = _str_field(msg, "user")
        if not ok_late_user:
            # B1：{"user": 123} 不再抛 AttributeError 打断连接；忽略该字段即可
            log.warning("客户端 %s 迟到的认证消息 user 类型非法（%s），已忽略",
                        addr, type(msg.get("user")).__name__)
            late_user = ""
        late_user = (late_user or "").strip()
        if auth_cfg.get("enabled", False):
            if not _reply_ctrl(sock, info, {"action": Act.AUTH_RESULT, "ok": False,
                                            "msg": "需要登录"}, addr):
                return "控制消息写入失败（连接已损坏）"
        else:
            if late_user:
                info.username = late_user
            if not _reply_ctrl(sock, info, {"action": Act.AUTH_RESULT, "ok": True,
                                            "msg": "账户准入已关闭，无需登录"}, addr):
                return "控制消息写入失败（连接已损坏）"
            log.debug("客户端 %s 迟到的认证消息（%s），已按准入关闭放行", addr, action)
    return None


def _client_recv_loop(sock, info, runtime, addr, net_cfg, auth_cfg):
    """控制消息/上行帧接收循环；返回断开原因（供统一收尾记录）。

    C2（实现方案 §7 第 2 步）：select 1s + recv + bytearray 累积 + 逐条排空的
    骨架并入 common.message_pump（与 viewer Channel._receiver_loop 共用一份）；
    半开检测（keepalive_idle_s）、单条消息超时（第 103 条 slowloris 防线）、
    分发与退出语义仍全部在本函数的回调里，行为逐分支不变。
    """
    # 第 102 条：走 safe_float——手改成 "abc" 时旧写法在每个客户端接入后立刻抛
    # ValueError（表现为"连上就断"），且违反本文件"配置数值一律走 safe_*"的约定。
    idle_s = safe_float(net_cfg.get("keepalive_idle_s", 60), 60, 1, 3600)
    # 第 57 条：rx_buf 用 bytearray，+= 原地扩展（摊还 O(1)）——泵内维持同一实现。
    # 第 103 条：partial_since 记录"当前未完成消息的首字节到达时刻"。
    # net-015（2026-09-18）：半开检测与单条消息总时限都必须以"完整消息"为进度单位——
    # 旧实现由 on_rx（任意字节）刷新 last_rx_at、并由 post 回调无条件重置
    # partial_since，攻击者发 `0x02 + 长度=32MB-1` 头后每 <15s 滴 1 字节即可把
    # 计时起点无限后移，永久占用接收线程 + fd + 连接名额 + ≤32MB 缓冲
    # （max_clients 全占即对所有观众关门）。现在：仅真正消费完整消息才刷新。
    partial_since = None
    msg_timeout = safe_float(getattr(runtime, "msg_timeout", 15.0), 15.0, 1.0, 600.0)

    def _on_idle(now, buf):
        if now - info.last_rx_at > idle_s:
            return "空闲超时（半开连接）"
        if partial_since is not None and now - partial_since > msg_timeout:
            return "单条消息超时（%.0f 秒内未收完整）" % msg_timeout
        return None

    def _on_buf(event, buf, now, n_msgs=0):
        nonlocal partial_since
        if event == "pre_empty":
            partial_since = now  # 本段是某条新消息的第一段
        elif n_msgs > 0:
            # 本轮消费掉了完整消息：半截消息（如有）从"现在"起算自己的总时限；
            # 完整消息即"客户端还活着"的证据，刷新半开检测时钟（net-015）
            partial_since = now if buf else None
            info.note_rx(now)
        # n_msgs == 0 且缓冲区非空：同一截未完成消息又收到一段字节——
        # **不**刷新 partial_since / last_rx_at，否则滴字节即可无限续命

    def _on_message(kind, payload, raw):
        if kind in (MSG_VIDEO, MSG_FRAME):
            # A4 §3.1：上行帧走泵内 parse_message_raw 零拷贝通道——整条消息
            # （含 5 字节头）一次物化，中继直接入队该 bytes，不再重打包。
            _forward_upstream_frame(runtime, sock, info, kind, payload, addr,
                                    raw=raw)
            return None
        # MSG_CTRL
        try:
            msg = json.loads(payload.decode("utf-8"))
        except ValueError:
            return "协议错误（控制消息 JSON 损坏）"
        return _dispatch_ctrl(sock, info, runtime, msg, addr, auth_cfg)

    try:
        reason = message_pump(
            sock,
            should_stop=runtime.stop_event.is_set,
            on_idle=_on_idle,
            # net-030（2026-09-20）：同一套超时判定也要在每个收到数据的轮次上评估。
            # 旧实现只有 select 空转满 1 秒才调 on_idle，于是"每 0.4~0.9 秒投 1 字节
            # 且仍把下行读干净"的对端让判据一次都不执行（实测 12 秒内 on_idle 调用
            # 0 次、泵不退出）——net-015 修的是计时起点，评估点仍漏着。
            on_tick=_on_idle,
            # net-015：note_rx 改由 _on_buf 在"消费完整消息"时刷新——任意字节刷新
            # 会让滴字节攻击同时绕过 60s 半开检测
            on_recv_error=lambda exc: "接收错误",
            on_eof=lambda: "断开",
            on_message=_on_message,
            on_buf=_on_buf,
            raw_kinds=(MSG_VIDEO, MSG_FRAME),
        )
        return reason if reason is not None else "断开"
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

    auth-005（2026-09-18）：握手+认证阶段占用**未认证名额**（max_pending_auth），
    完成后立即归还——未认证 socket 挤不满全部 max_clients。
    """
    host_cfg, net_cfg = _client_net_cfg(runtime)
    acquired = None
    acquire = getattr(runtime, "acquire_pending", None)
    if callable(acquire) and not acquire():
        log.warning("未认证连接数已达上限 %d，拒绝来自 %s 的新连接（多为连上不登录）",
                    getattr(runtime, "max_pending_auth", 0), addr)
        try:
            sock.close()
        except OSError:
            pass
        return
    if callable(acquire):
        acquired = True
    try:
        if not _do_handshake(sock, addr, net_cfg):
            return
        ok, username = _do_auth(sock, addr, runtime, host_cfg)
    finally:
        if acquired:
            try:
                runtime.release_pending()
            except Exception:
                pass
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
    # net-019（2026-09-18）：入册之后任何一步异常都必须走统一收尾——旧实现
    # `_client_recv_loop`/`broadcast_roster` 抛异常时直接冒泡出 handle_client，
    # 客户端留在 runtime.clients、发送线程继续活着（删不掉的源 + 线程/fd 泄漏）。
    reason = "异常"
    try:
        reason = _client_recv_loop(sock, info, runtime, addr, net_cfg, auth_cfg)
    except Exception as e:
        reason = "异常: %s" % e
        log.warning("客户端 %s 接收循环异常，按断开收尾: %s", addr, e)
    finally:
        _client_teardown(sock, info, runtime, addr, username, reason)
def _subscriber_count(runtime, source="local"):
    """当前订阅指定源的存活客户端数（B9：ABR 判据要区分"有订阅者"与"没人看"）。"""
    try:
        with runtime.clients_lock:
            return sum(1 for info in runtime.clients.values()
                       if getattr(info, "watch_source", None) == source)
    except Exception:
        return 0


def route_frame(runtime, source, frame, is_key=False, is_jpeg=False, send_timeout=2.0):
    """把一帧消息投递给 watch_source == source 的存活客户端（订阅路由）。

    语义与旧 broadcast_frame 完全一致：_sender_active 者走 enqueue_frame（容量 1，
    发送慢丢中间帧）；否则同步 sendall fallback（测试/旧式直连）。新增关键帧门控：
    视频订阅者在 need_key 期间（新连接/切换源后）只收关键帧，收到后清除；
    JPEG 帧自包含，始终放行。

    B9（2026-09-11）：返回本次实际投递的客户端数（0 = 没有任何订阅者）——发送线程据此
    区分"真的发出去了"与"根本没人订阅"，避免零观众时把码率一路顶到上限。
    """
    if not frame:
        return 0
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
                # net-021（2026-09-18）：只有本机源才置本机 force_key——订阅 peer 源
                # 的观众丢帧与本地编码器无关，旧实现照样强制本地流出 IDR
                # （正反馈放大：慢观众 → 本机 IDR 风暴）。
                if source == "local":
                    with runtime.slot_lock:
                        runtime.force_key = True
                    _wake_raw(runtime)
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
    return len(targets)


def _live_share_ids(runtime):
    """当前在册共享成员 id 集合（watch 源校验用）。"""
    with runtime.clients_lock:
        return {c.share_id for c in runtime.clients.values()
                if getattr(c, "share_id", None)}


def _request_local_keyframe(runtime, cooldown=1.0):
    """本机共享画面的"强制下一帧 IDR"请求，带**运行级**冷却去重。

    codec-014（2026-09-20）：客户端 `req_keyframe` 旧实现无条件 `force_key = True`
    + `_wake_raw`，一条约 40 字节的消息即可把编码器钉成帧帧 IDR（IDR 的码率开销
    远高于 P 帧），而 net-020 的配额只管 WATCH/UNSHARE、上行令牌桶只管视频帧，
    控制消息整体没有配额。这里按"整个运行每秒至多一次"合并——force_key 本来就是
    全局单标志，多个观众同时等关键帧时合并成一次 IDR 正是想要的语义；正常观看端
    的重试间隔远大于 1 秒，不受影响。与内部愈合路径 `_request_peer_keyframe` 同口径。
    """
    now = time.monotonic()
    lock = getattr(runtime, "_peer_key_lock", None)
    table = getattr(runtime, "_peer_key_at", None)
    if isinstance(table, dict) and lock is not None:
        with lock:
            if now - table.get("*local*", 0.0) < cooldown:
                return False
            table["*local*"] = now
    with runtime.slot_lock:
        runtime.force_key = True
    _wake_raw(runtime)  # C2：唤醒编码线程（静止期也在等 raw_evt）
    return True


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
    if isinstance(table, dict) and lock is not None:
        with lock:
            if now - table.get(share_id, 0.0) < cooldown:
                return False
            table[share_id] = now
    sock = _sharer_sock(runtime, share_id)
    if sock is None:
        return False
    _send_ctrl_safe(runtime, sock, {"action": Act.REQ_KEYFRAME})
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


def _fwd_member_frame(runtime, sharer, kind, payload, is_key=False, is_jpeg=False,
                      raw=None):
    """把成员上行帧原样重打包后路由给该 peer 源的订阅者。

    帧 payload 格式与下行完全一致，重打包字节序列等价原消息（kind+len+payload）。
    第 114 条（C3）：header 用 common._MSG_HDR 一次 pack，避免多段临时 bytes。
    A4 §3.1：recv 循环已用 parse_message_raw 物化整条消息时直接复用 `raw`，
    跳过重打包（整帧物化次数 2→1）；转发路径不得修改 payload 任何字节。
    B8：顺带记录该成员最近一次产帧时刻（roster 的"无信号"标注依据）。
    """
    try:
        sharer.last_up_at = time.monotonic()
    except Exception:
        pass
    if raw is None:
        raw = _MSG_HDR.pack(kind, len(payload)) + payload
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


def _as_server_list(server):
    """把单个监听 socket 或 socket 列表统一成列表（A0 回环双栈会同时监听 v4/v6）。"""
    if server is None:
        return []
    if isinstance(server, (list, tuple)):
        return [s for s in server if s is not None]
    return [server]


def _admit_client(sock, addr, runtime, spawn):
    """连接上限判定 + 交给 spawn（第 103 条：快速路径，权威判定在 handle_client）。"""
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
        return
    spawn(sock, addr, runtime)


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

    A0（2026-09-11）：`server` 可以是**监听 socket 列表**（回环双栈 = 127.0.0.1 + [::1]）。
    单 socket 时保持原有直连 accept 语义（单测用假 server 注入，必须照旧）；多 socket 时
    用 select 同时等待，任一就绪即 accept。
    """
    spawn = spawn or _spawn_client
    servers = _as_server_list(server)
    if len(servers) == 1:
        _accept_loop_single(servers[0], runtime, spawn)
    elif servers:
        _accept_loop_multi(servers, runtime, spawn)


def _accept_loop_single(server, runtime, spawn):
    """单监听 socket 的接受循环（原实现，单测注入假 server 走这条）。"""
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
        _admit_client(sock, addr, runtime, spawn)


def _accept_loop_multi(servers, runtime, spawn):
    """A0：多监听 socket（回环双栈）的接受循环，用 select 同时等待。

    失效的监听 socket 只从集合里剔除，不会拖垮另一条地址族的服务；全部失效或
    stop_event 置位时退出。
    """
    fails = 0
    while servers and not runtime.stop_event.is_set():
        try:
            ready, _w, _x = select.select(list(servers), [], [], 0.2)
        except (OSError, ValueError) as e:
            alive = []
            for srv in servers:
                try:
                    if srv.fileno() >= 0:
                        alive.append(srv)
                except Exception:
                    pass
            if len(alive) == len(servers):
                # 全部仍"看起来"有效却 select 失败：按瞬时错误退避重试
                fails += 1
                if fails == 1 or fails % 20 == 0:
                    log.warning("accept 失败（已连续 %d 次，50ms 后重试）: %s", fails, e)
                time.sleep(0.05)
                continue
            servers = alive
            continue
        if not ready:
            continue
        fails = 0
        for srv in ready:
            try:
                sock, addr = srv.accept()
            except OSError as e:
                try:
                    closed = srv.fileno() < 0
                except Exception:
                    closed = False
                if runtime.stop_event.is_set() or closed:
                    servers = [s for s in servers if s is not srv]
                    continue
                fails += 1
                if fails == 1 or fails % 20 == 0:
                    log.warning("accept 失败（已连续 %d 次，50ms 后重试）: %s", fails, e)
                time.sleep(0.05)
                continue
            _admit_client(sock, addr, runtime, spawn)


def _listen_target(cfg):
    """解析 (listen_host, port, bind_all)；非字符串地址按“全网卡”处理。

    第 19 条语义：bind_all（""/"0.0.0.0"/"::"/None）→ 双栈 socket，一个端口同时覆盖
    IPv4/IPv6（frp 本地回连在 SYSTEM 上下文优先走 ::1，必须双栈，否则公网链路握手即断）；
    指定具体地址（如 127.0.0.1）→ 只绑该地址、该地址族，真正把共享限制在本机。
    手改成数字/对象时按全网卡处理而不是抛 TypeError（第 100 条口径：坏配置只回退）。

    A0（2026-09-11）：具体回环地址（127.0.0.1 / localhost / ::1）不再只绑单一地址族——
    frpc 的本地目标常写 `localhost`，Windows 解析优先 `::1`，只绑 IPv4 回环会让隧道永远
    连不上本机（公网观众端"画面无法传递"且两端无日志）。见 `_is_loopback_alias`。
    """
    host_cfg = cfg.get("host") if isinstance(cfg, dict) and isinstance(cfg.get("host"), dict) else {}
    listen_host = host_cfg.get("listen_host")
    if not isinstance(listen_host, str) or not listen_host.strip():
        # D-02（2026-09-20）：旧实现把"非字符串"归一为 ""，而 "" 属于 bind_all 集合
        # → 坏配置反而**绑 0.0.0.0 全网卡**，与文档"默认/兜底只允许本机"、以及
        # host_ui.py:237（ui-007）同一情形兜底 127.0.0.1 的口径双向相反。
        # 安全默认方向统一为回环：兜底失败时收紧而不是放开。
        log.warning("配置 host.listen_host 非字符串或为空（%r），按只允许本机与 frp 隧道接入", listen_host)
        listen_host = "127.0.0.1"
    # cfg-008（2026-09-20）：端口要的是"要么合法要么回退默认"，不是钳制——
    # 旧实现 `"port": 0` 会被 safe_int 钳成 **监听端口 1**（而不是回退 5700）且零提示。
    port = safe_int(host_cfg.get("port", 5700), 5700, 1, 65535, clamp=False)
    return listen_host, port, listen_host in ("", "0.0.0.0", "::")


#: A0：回环别名——这些值都表示"只允许本机（与 frp 隧道）接入"，但必须**双栈**监听，
#: 否则会漏掉 frpc 的 `::1` 回连（Windows 上 localhost 优先解析为 ::1）。
_LOOPBACK_ALIASES = ("127.0.0.1", "localhost", "::1")


def _is_loopback_alias(listen_host):
    """listen_host 是否是"具体回环地址"（需要 IPv4 + IPv6 回环双监听）。"""
    return isinstance(listen_host, str) and listen_host.strip().lower() in _LOOPBACK_ALIASES


def _has_frp_tunnel(cfg):
    """配置里是否声明了实际可用的樱花隧道（有 token 或 tunnel_ids）。

    A0：仅用于启动自检与文档口径提示——"监听回环 + 配了隧道"这一组合下，隧道本地
    地址若为 `localhost` 就会走 `::1`，必须有 IPv6 回环监听才可达。
    """
    if not isinstance(cfg, dict):
        return False
    frp = (cfg.get("host") or {}).get("frp") if isinstance(cfg.get("host"), dict) else None
    if not isinstance(frp, dict):
        return False
    if str(frp.get("token") or "").strip():
        return True
    tids = frp.get("tunnel_ids")
    if isinstance(tids, str):
        return bool(tids.strip())
    if isinstance(tids, (list, tuple)):
        return any(str(t).strip() for t in tids)
    return bool(str(frp.get("public_addr") or "").strip())


def _frp_public_target(runtime):
    """从配置取 (host, port) 形式的公网隧道地址；不可用时返回 None（C14/A0 自检用）。"""
    try:
        host_cfg = runtime.cfg.get("host") if isinstance(runtime.cfg, dict) else None
        host_cfg = host_cfg if isinstance(host_cfg, dict) else {}
        frp = host_cfg.get("frp") if isinstance(host_cfg.get("frp"), dict) else {}
        raw = str(frp.get("public_addr") or "").strip()
        if not raw:
            return None
        return _split_public_addr(raw)
    except Exception:
        return None


def _split_public_addr(raw):
    """把 "域名:端口" / "[v6]:端口" 解析成 (host, port)；解析不出返回 None。"""
    raw = raw.strip()
    if raw.startswith("["):
        end = raw.find("]")
        if end < 0:
            return None
        host, rest = raw[1:end], raw[end + 1:]
        if not rest.startswith(":"):
            return None
        port_s = rest[1:]
    elif raw.count(":") == 1:
        host, port_s = raw.split(":", 1)
    else:
        return None
    host = host.strip()
    try:
        port = int(port_s.strip())
    except (TypeError, ValueError):
        return None
    if not host or not (0 < port < 65536):
        return None
    return host, port


def _start_tunnel_selfcheck(runtime, port, delay=3.0):
    """A0/C14：共享端启动后异步自检"公网隧道是否真的打得到本机"。

    只回答"frpc 进程在不在"是不够的——本次故障（A0）里 frpc 明明"运行中"，隧道却因为
    只监听 IPv4 回环而永远连不上本机，两端日志都没有线索。这里做一次真实回连：
    connect(public_addr) → 发 `FS02` → 等 `OK02`；失败则把可操作的结论写进日志
    （含"隧道本地地址请填 127.0.0.1"和 frpc 日志里的关键字），成功则记一条 INFO。

    无隧道配置或 frpc 未在运行时直接跳过；整个过程在 daemon 线程里做，不阻塞启动。
    """
    target = _frp_public_target(runtime)
    if target is None:
        return
    if not runtime.frp.is_running():
        return

    def _probe():
        # net-032（2026-09-20）：旧实现在循环内写 `delay = 600.0` 却没声明 nonlocal，
        # Python 编译期就把 delay 定为 _probe 的局部名 → **首次迭代**读它即
        # UnboundLocalError，线程一启动就死、异常无人接，自检从"启动跑一次"退化为
        # "从不跑"。这里改用只属于本函数的 next_delay，外层形参只读不写。
        # net-033（2026-09-20）：旧实现只在 _start_tunnel_selfcheck 入口判一次
        # is_running()，net-032 一修好就变成"用户停掉 frpc 后仍每 10 分钟对外拨号
        # 直到进程退出"。两条必须同时落地。
        next_delay = float(delay)
        waited = 0.0
        try:
            while not runtime.stop_event.is_set():
                if not runtime.frp.is_running():
                    log.info("公网隧道自检停止：frpc 已不在运行")
                    return
                host, pub_port = target
                if runtime.stop_event.wait(max(0.0, next_delay - waited)):
                    return
                waited = 0.0
                next_delay = 600.0      # 首次 3 秒后自检，此后每 10 分钟复检一次
                try:
                    sock = socket.create_connection((host, pub_port), timeout=8)
                except Exception as e:
                    log.warning("公网隧道自检失败：无法连接 %s:%s（%s）——请确认 frpc 已在运行、"
                                "隧道已启用；若连接被拒绝，检查樱花控制台里隧道的本地地址/端口",
                                host, pub_port, e)
                    # B2（第 134 条）：四种结论除日志外同步进 _net_status（面板/诊断报告消费）
                    runtime.set_net_status("fail", "无法连接 %s:%s（隧道未就绪？）" % (host, pub_port))
                    continue
                _probe_once(runtime, sock, host, pub_port, port)
        except Exception:
            # net-032：线程体内任何意外都不能让自检静默消失——daemon 线程抛出的
            # 异常只有 stderr（windowed 打包下没有 stderr），这里兜底记一条日志。
            log.exception("公网隧道自检线程异常退出")

    def _probe_once(runtime, sock, host, pub_port, port):
        try:
            sock.settimeout(8)
            try:
                sock.sendall(MAGIC_CLIENT)
            except OSError as e:
                # net-023：sendall 也必须在 try 内——旧实现隧道半开时这里抛的
                # OSError 直接冒出线程（无结论、无日志），自检静默消失
                log.warning("公网隧道自检失败：向 %s:%s 发送握手失败（%s）",
                            host, pub_port, e)
                runtime.set_net_status("fail", "握手发送失败（隧道半开？）")
                return
            try:
                got = sock.recv(4)
            except OSError as e:
                got = b""
                log.warning("公网隧道自检失败：TCP 已连通 %s:%s 但未收到握手（%s）。"
                            "最常见原因是隧道本地地址/端口没指向共享端监听端口——樱花 frpc 日志会写"
                            "「连接映射目标 [tcp://localhost:%d] 失败，连接被拒绝」。"
                            "请把隧道本地地址显式写成 127.0.0.1，或确认共享端正在监听该端口。",
                            host, pub_port, e, port)
                runtime.set_net_status("fail", "未收到握手（隧道本地地址未指向监听端口）")
                return
            if got == MAGIC_SERVER:
                log.info("公网隧道自检通过：%s:%s 回连本机并完成握手", host, pub_port)
                runtime.set_net_status("ok", "%s:%s 回连握手成功" % (host, pub_port))
            else:
                log.warning("公网隧道自检异常：%s:%s 返回 %r（期望 %r）——"
                            "该地址可能指向了别的服务", host, pub_port, got, MAGIC_SERVER)
                runtime.set_net_status("fail", "返回了非本服务的响应（地址指向别的服务？）")
        finally:
            try:
                sock.close()
            except OSError:
                pass

    threading.Thread(target=_probe, daemon=True, name="frp-selfcheck").start()


def _log_security_notice(runtime):
    """启动时把当前暴露面写清楚：监听全网卡 / 准入关闭 / 自助注册开放。

    第 131 条：这些是"默认安全"之外的选择，用户有权这样配，但必须能从日志里一眼
    看到自己把屏幕暴露给了谁——而不是靠猜。默认组合（127.0.0.1 + 准入开启）不产生
    任何告警噪音。

    A0（2026-09-11）：默认形态（回环 + 樱花 frp）现在**真的**可达——回环别名会同时
    监听 `[::1]` 与 `127.0.0.1`，所以文案不再把"设计上可达"当成"已经可达"，并显式
    写出隧道侧要满足的条件；IPv6 回环绑定失败时才发风险告警（D3）。
    """
    host_cfg = runtime.cfg.get("host") if isinstance(runtime.cfg, dict) else None
    host_cfg = host_cfg if isinstance(host_cfg, dict) else {}
    listen_host, port, bind_all = _listen_target(runtime.cfg)
    auth_cfg = host_cfg.get("auth") if isinstance(host_cfg.get("auth"), dict) else {}
    auth_on = bool(auth_cfg.get("enabled", False))
    if not bind_all:
        dual = bool(getattr(runtime, "listen_dual_loopback", False))
        if _is_loopback_alias(listen_host) and dual:
            log.info("监听 %s:%d（仅本机与 frp 隧道可达；已同时监听 [::1] 与 127.0.0.1，"
                     "兼容 frpc 以 localhost 回连，%s）",
                     runtime.bound_addr, port, "需登录" if auth_on else "未开启账户准入")
        else:
            log.info("监听 %s:%d（仅本机与 frp 隧道可达，%s）",
                     runtime.bound_addr, port, "需登录" if auth_on else "未开启账户准入")
        if _is_loopback_alias(listen_host) and _has_frp_tunnel(runtime.cfg) and not dual:
            # A0 兜底自检：只绑了单地址族回环，而 frpc 很可能走 localhost → ::1
            log.warning(
                "已配置 frp 隧道，但本机只监听了 %s:%d（未覆盖 IPv6 回环 [::1]）："
                "樱花 frpc 的本地地址若为 localhost，Windows 会优先解析为 ::1，隧道将连不上"
                "本机（公网观众端表现为「连接提前关闭」且本端零日志）。请把樱花隧道的本地地址"
                "改成 127.0.0.1，或把 host.listen_host 设为 0.0.0.0 后重启共享端"
                "（0.0.0.0 会以双栈监听，同时覆盖两种地址族）。",
                runtime.bound_addr, port)
        return
    if not auth_on:
        log.warning(
            "当前监听 0.0.0.0:%d 且账户准入关闭：同一网络中任何人都能直接观看你的屏幕，"
            "不会要求登录。仅在对端完全可信时这样配置。", port)
        return
    policy = register_policy(auth_cfg)
    if policy == "open":
        log.warning(
            "当前监听 0.0.0.0:%d，准入已开启但允许自助注册：同一网络中任何人都能"
            "自行注册账号观看你的屏幕。如需收紧，请在「传输参数」里把账户注册改为 "
            "bootstrap（仅首个账户可注册）或 closed（完全关闭）。", port)
    else:
        log.info("监听 0.0.0.0:%d（账户准入开启，注册策略 %s）", port, policy)


def _listen_backlog(runtime):
    """监听队列长度：按 max_clients 量级取（C7）。

    原实现固定 `listen(5)`，而 `host.net.max_clients` 默认 32（含握手中）——多人同时
    进入时内核 accept 队列只有 5，多余连接可能被直接拒绝（对端表现为连接重置）。
    """
    try:
        max_clients = safe_int(getattr(runtime, "max_clients", 32), 32, 1, 4096)
    except Exception:
        max_clients = 32
    return max(16, min(128, max_clients * 2))


def _make_listener(family, addr, backlog, dual_v6only=None):
    """建一个监听 socket（绑定失败抛 OSError，由调用方决定回退/报错）。"""
    server = socket.socket(family, socket.SOCK_STREAM)
    try:
        if dual_v6only is not None:
            try:
                server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, dual_v6only)
            except OSError:
                pass
        _apply_listen_reuse_opt(server)
        server.bind(addr)
        server.listen(backlog)
    except OSError:
        try:
            server.close()
        except OSError:
            pass
        raise
    return server


def bind_listener(runtime):
    """建监听 socket 并登记到 runtime；失败时写 startup_error 并返回 None。

    第 18 条：绑定失败（端口被占/权限不足/第二个实例撞 SO_EXCLUSIVEADDRUSE）时
    不再让线程静默消失——记一条致命错误并置 startup_error 供 GUI 显示，随后干净返回。

    返回值：监听 socket 列表（成功）或 None（失败）。`runtime._server` 仍指向第一个
    （主）socket，兼容既有调用方与测试的 `_server.getsockname()`。

    A0（2026-09-11）：`listen_host` 为具体回环地址（127.0.0.1/localhost/::1）时同时监听
    `127.0.0.1:port` 与 `[::1]:port`。两者都只绑回环，**不降低安全性**，但覆盖了 frpc
    以 `localhost`（Windows 优先 ::1）回连的情形——否则公网隧道永远连不上本机。
    """
    listen_host, port, bind_all = _listen_target(runtime.cfg)
    bound_addr = "0.0.0.0" if bind_all else listen_host   # 诚实日志用（第 19 条）
    backlog = _listen_backlog(runtime)
    servers = []
    loopback_dual = False
    try:
        if bind_all:
            try:
                servers = [_make_listener(socket.AF_INET6, ("::", port), backlog, dual_v6only=0)]
            except OSError:
                # IPv6 不可用（系统禁用 IPv6）时的回退：仅 IPv4 全网卡
                servers = [_make_listener(socket.AF_INET, ("0.0.0.0", port), backlog)]
        elif _is_loopback_alias(listen_host):
            # A0 正解：回环双栈。先绑 IPv4（主 socket，保持 bound_addr/getsockname 语义），
            # 再尽力绑 IPv6 [::1]；IPv6 不可用时只保留 IPv4 并留下告警（自检在
            # _log_security_notice 里发出）。
            primary = _make_listener(socket.AF_INET, ("127.0.0.1", port), backlog)
            servers = [primary]
            try:
                servers.append(_make_listener(
                    socket.AF_INET6, ("::1", port), backlog, dual_v6only=1))
                loopback_dual = True
            except OSError as e:
                log.warning("IPv6 回环 [::1]:%d 监听失败（%s）；"
                            "frpc 若以 localhost 回连将连不上本机", port, e)
            # bound_addr 保持基础地址：GUI/日志以它为准，双栈信息写在 _log_security_notice
            bound_addr = "127.0.0.1"
        else:
            # 指定地址：按地址族绑定（含 ':' 视为 IPv6），绝不偷偷扩成全网卡
            family = socket.AF_INET6 if ":" in listen_host else socket.AF_INET
            servers = [_make_listener(family, (listen_host, port), backlog)]
    except OSError as e:
        for s in servers:
            try:
                s.close()
            except OSError:
                pass
        runtime._server = None
        runtime._servers = []
        runtime.listen_dual_loopback = False
        runtime.startup_error = (
            "无法监听 %s:%d（%s）；端口可能已被占用或权限不足。" % (bound_addr, port, e))
        log.error("服务启动失败：%s", runtime.startup_error)
        return None
    runtime._server = servers[0]
    runtime._servers = servers
    runtime.listen_dual_loopback = loopback_dual
    runtime.startup_error = None
    runtime.bound_addr = bound_addr
    return servers


def run_server(runtime):
    """绑定监听并运行 采集→编码→发送→性能 四条流水线，直到 stop_event 置位。

    第 109 条（阶段 2）：原 678 行巨型函数拆为 bind_listener + 4 个模块级流水线函数
    （_capture_loop/_encode_loop/_send_loop/_perf_loop），本函数只做编排：
    参数构造 → 绑定 → 起线程 → 等待停止 → 收尾。

    A0（2026-09-11）：`bind_listener` 现返回监听 socket **列表**（回环双栈时为两个），
    接受循环对全部监听 socket 一起 select，收尾时全部关闭。
    """
    p = pipeline.PipelineParams(runtime.cfg)
    servers = bind_listener(runtime)
    if not servers:
        return
    _listen_host, port, _bind_all = _listen_target(runtime.cfg)
    log.info("服务已启动，监听 %s:%d（fps=%d 质量=%d，退出按钮/Ctrl+C 退出）",
             runtime.bound_addr, port, p.base_fps, p.base_quality)
    _log_security_notice(runtime)
    _start_tunnel_selfcheck(runtime, port)

    # 第 102 条：接受循环在模块级（_accept_loop），瞬时 OSError 只重试不退出。
    # 采集、编码、发送与性能节拍线程均为 daemon，随 stop_event 置位后退出。
    threads = [threading.Thread(target=_accept_loop, args=(servers, runtime), daemon=True)]
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
        for s in servers:
            try:
                s.close()
            except OSError:
                pass
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
def _still_edge(runtime, p, in_still):
    """静止/恢复边沿：打日志并更新 runtime.stats["still"]。

    性能线程据 stats["still"] 暂停自适应评估，避免把"静止停发"误判成链路空闲而误降码率
    （见第 89 条的 perf guard）。
    """
    if in_still:
        log.info("画面静止，暂停发送（%d fps 探测）", int(1 / p.still_probe_interval))
    else:
        log.info("画面变化，恢复发送")
    with runtime.stats_lock:
        runtime.stats["still"] = bool(in_still)


def _capture_loop(runtime, p):
    """采集线程：按目标帧率把原始帧写入 raw 槽，只统计帧率，不负责编码与发送。

    编码移动到独立线程，避免编码耗时拉长采集间隔（采集节奏 = 1/fps 严格节流）。

    第 111 条（阶段 2）：采集句柄的故障恢复与静止检测闸门改由 pipeline.CaptureSource /
    pipeline.StillGate 统一提供（与 share.py 共用同一实现，差异做成参数）：
    - 后端已死（working=False）持续 5 秒 → 重建并上报，否则画面永久冻结且日志无痕
    - 采集异常连续 5 次 → 重建采集器（dxcam 设备丢失等场景自动恢复）
    - 长期空帧 30 秒 → 告警一次（画面静止或独占全屏属正常语义，不重建）
    """
    # 1 秒滑动窗口：记录每帧采集完成的时刻，用于统计实际采集帧率
    fps_window = []
    with runtime.slot_lock:
        backend = runtime.slot["backend"]
        monitor = runtime.slot["monitor"]
        region = runtime.slot["region"]
    # 第 100 条：采集器构造永不抛异常（两个后端都失败时只把句柄置 None），此时
    # grab() 恒返回 None，线程会以 100Hz 空转且什么都不上报。启动即验活。
    src = pipeline.CaptureSource(
        backend, monitor, region, none_rebuild_s=5.0,
        none_rebuild_requires_dead=True, none_warn_s=30.0)
    if not src.working():
        log.error("采集后端初始化失败（dxgi/mss 均不可用），将无法共享画面；"
                  "请在控制台「采集源」切换后端后重试")
    gate = pipeline.StillGate(
        p.point_thr, p.ratio_thr, p.still_quiet_s,
        on_edge=lambda in_still: _still_edge(runtime, p, in_still))
    try:
        while not runtime.stop_event.is_set():
            with runtime.slot_lock:
                backend = runtime.slot["backend"]
                monitor = runtime.slot["monitor"]
                region = runtime.slot["region"]
                fps_now = safe_int(runtime.slot.get("fps", p.base_fps),
                                    p.base_fps, 1, 240, warn=False)
            if not src.set_params(backend, monitor, region):
                continue    # 参数重建失败：内部已计数并退避等待
            frame_interval = 1.0 / fps_now if fps_now > 0 else 0.0
            frame_start = time.perf_counter()
            ts_micros = int(time.time() * 1_000_000)  # 采集开始时刻
            bgr = src.grab()
            if bgr is None:
                continue    # 重建/告警/等待已在 CaptureSource.grab 内部完成
            # ---- 静止检测闸门（画面无有效变化时不写 raw 槽，编码/发送自然停摆）----
            if p.still_enabled:
                changed = gate.check(bgr)
                if not changed:
                    if gate.in_still:
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
            _wake_raw(runtime)  # C2：唤醒编码线程
            # 1 秒滑动窗口统计实际采集帧率
            fps_window.append(now)
            with runtime.stats_lock:
                runtime.stats["capture_fps"] = _fps_from_window(fps_window, now)
            # 帧间节流：目标间隔 1/fps，按每次循环开始时刻计算剩余等待，避免累计漂移
            if frame_interval > 0:
                wait = frame_interval - (time.perf_counter() - frame_start)
                if wait > 0:
                    time.sleep(wait)
    finally:
        src.close()


def _wake_raw(runtime):
    """C2：唤醒编码线程（force_key 置位 / 采集新帧时调用）。测试替身无该属性时静默跳过。"""
    evt = getattr(runtime, "raw_evt", None)
    if evt is not None:
        evt.set()


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
    enc_evt = getattr(runtime, "enc_evt", None)
    if enc_evt is not None:
        enc_evt.set()  # C2：唤醒发送线程


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
    if not need_key:
        return error_cooldown_until
    if last_frame is None:
        # 尚无缓存帧：保留请求，等首帧编码成功后再响应（原先直接丢弃）
        with runtime.slot_lock:
            runtime.force_key = True
        return error_cooldown_until
    res, err = fe.force_idr(last_frame)
    if err is not None:
        now_k = time.perf_counter()
        if now_k >= error_cooldown_until:
            log.warning("静止期关键帧编码错误: %s，10 秒内不再重复告警", err)
            error_cooldown_until = now_k + 10.0
        # 编码失败：恢复请求，下轮/下一帧重试（原先丢弃后只能等自然 keyint ≈2s）
        with runtime.slot_lock:
            runtime.force_key = True
        return error_cooldown_until
    if res is None:
        with runtime.slot_lock:
            runtime.force_key = True
        return error_cooldown_until
    nal, is_key, encode_ms, out_ts = res
    with runtime.slot_lock:
        runtime.slot["video"] = nal
        runtime.slot["video_ts"] = out_ts
        runtime.slot["is_key"] = is_key
        runtime.slot["video_codec"] = fe.encoder.codec_id
        runtime.slot["encode_ms"] = encode_ms
        runtime.slot["bitrate"] = fe.encoder.bitrate
    enc_evt = getattr(runtime, "enc_evt", None)
    if enc_evt is not None:
        enc_evt.set()  # C2：唤醒发送线程，避免静止 IDR 再等 50ms 兜底
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
            fps_now = safe_int(runtime.slot.get("fps", p.base_fps),
                                p.base_fps, 1, 240, warn=False)
        if raw is None or raw_ts == last_raw_ts:
            # 无新帧：若画面静止且有观看端请求关键帧（新观众接入/花屏恢复），
            # 用最近一次编码帧强制出一帧 IDR（新 pts 绕过发送端同帧去重）
            if runtime.force_key:
                error_cooldown_until = _encode_static_keyframe(
                    runtime, fe, last_frame, error_cooldown_until)
            # C2：等采集置位 raw_evt（或 force_key 路径主动唤醒）；50ms 兜底覆盖
            # stop_event 与未走 _wake_raw 的测试替身。原先 1ms sleep 空转 CPU 1000 次/秒。
            runtime.raw_evt.wait(0.05)
            runtime.raw_evt.clear()
            continue
        # 有新帧即将编码：原子读取并清除强制关键帧标志（仅在真正编码时消费）
        with runtime.slot_lock:
            force_key = runtime.force_key
            runtime.force_key = False
        with runtime.codec_lock:
            target_b = runtime.target_bitrate   # ABR 可能已更新
        ef = fe.encode(raw, raw_ts, force_key=force_key, scale=scale, quality=quality,
                       fps=fps_now, bitrate=target_b)
        if ef is None:
            # 编码器初始化/pre-roll 或编码失败：恢复 force_key（请求未兑现），
            # 不消费 raw_ts，下轮重试
            if force_key:
                with runtime.slot_lock:
                    runtime.force_key = True
            runtime.raw_evt.wait(0.05)
            runtime.raw_evt.clear()
            continue
        last_frame = ef.frame  # 缓存缩放后帧：静止期强制关键帧恢复用
        _store_encoded(runtime, ef, raw_ts)
        last_raw_ts = raw_ts
        # C11（2026-09-11）：取**编码完成**后的时刻入窗。原实现在编码开始前取 now 再
        # 追加进"编码完成时刻"窗口 → encode_fps 反映的是编码启动频率而非完成频率
        # （高速编码器下差异小，但语义与注释不符）。
        now = time.perf_counter()
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
        delivered = 0   # B9：本帧实际投递的订阅者数（route_frame 抛异常时保持 0）
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
                delivered = route_frame(
                    runtime, "local",
                    pack_video(video, video_ts, is_key, codec=video_codec),
                    is_key=is_key, send_timeout=2.0)
                last_sent_video_ts = video_ts  # 仅发送成功后才视为已发送
            except Exception as e:
                log.warning("广播错误: %s", e)
        elif jpeg is not None and ts != last_sent_jpeg_ts:
            send_start = time.perf_counter()
            try:
                delivered = route_frame(runtime, "local", pack_frame(jpeg, ts),
                                        is_jpeg=True, send_timeout=2.0)
                last_sent_jpeg_ts = ts  # 仅发送成功后才视为已发送
            except Exception as e:
                log.warning("广播错误: %s", e)
        else:
            # C2：等编码线程写入新帧（enc_evt）；50ms 兜底覆盖 stop 与测试替身。
            # 原先 0.5ms sleep 空转 CPU 2000 次/秒。
            runtime.enc_evt.wait(0.05)
            runtime.enc_evt.clear()
            continue
        # B9（2026-09-11）：本帧没有投递给任何订阅者（零观众/全部在等关键帧）时不登记
        # 发送统计——旧实现统计的是"发送线程处理了多少帧"，与 route_frame 是否真的
        # 投递无关。零观众时 send_fps≈27、avg_send_ms≈0 恰好满足 _abr_recover 的
        # send_fps>=5 门槛，于是无人观看的 11 秒里码率被 2500→5492 Kbps，并反复强制
        # IDR（实测 3 个 76–99 KB 白编码关键帧），首个观众从 5.5 Mbps 起跳。
        if not delivered:
            with runtime.stats_lock:
                runtime.stats["send_fps"] = 0.0
                runtime.stats["avg_send_ms"] = 0.0
            continue
        send_ms = (time.perf_counter() - send_start) * 1000.0
        now = time.perf_counter()
        send_window.append((now, send_ms))
        while send_window and now - send_window[0][0] > 1.0:
            send_window.pop(0)
        if len(send_window) >= 2:
            span = send_window[-1][0] - send_window[0][0]
            # C11：两个样本时刻理论上可能相同（同一 perf_counter 刻度）→ ZeroDivisionError
            # 被 _perf_loop 兜住，但整拍统计会丢失。加分母保护。
            send_fps = (len(send_window) - 1) / span if span > 0 else float(len(send_window))
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
        # B9：没有订阅者时不必强制 IDR——白编码一个 76–99 KB 的关键帧既浪费 CPU/带宽，
        # 又抬高"最后一帧"的体积。码率本身照常设置，等观众到来后的正常关键帧即可。
        if _subscriber_count(runtime, "local") == 0:
            log.info("ABR 码率调整: %d Kbps（当前无订阅者，不强制关键帧）", bps // 1000)
            with runtime.stats_lock:
                runtime.stats["abr_changes"] += 1
                runtime.stats["last_abr_at"] = time.monotonic()
            return changed
        with runtime.slot_lock:
            runtime.force_key = True
        _wake_raw(runtime)
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

    第 112 条（C1）：顺带汇总各客户端最近 1 秒的**实测**发送速率（ClientInfo._window
    里已有的字节数）写入 stats["egress_bps"]，供 ABR 按链路真实能力定码率——旧实现
    只看配置的目标码率，链路跑不动也不知道。
    """
    client_send_ms = []
    recent_drop = False
    egress_bps = 0.0
    now_mono = time.monotonic()
    # B9：订阅本机源的客户端数（独立短锁，不与客户端快照锁嵌套）
    stats["subscribers"] = _subscriber_count(runtime, "local")
    with runtime.clients_lock:
        infos = list(runtime.clients.values())
    # 阶段 A2（优化方案 2026-09-17）：出锁逐个统计——旧实现在 clients_lock 内对每个
    # 客户端调用 recent_send_ms/rate_kbps/加 _lock，连接入册/注销要排在这些计算后面。
    #
    # net-016（2026-09-18）：ABR 判据只采信本机源订阅者（watch_source=="local"）。
    # MultiView 下订阅 peer:X 的慢链路观众：其发送耗时/丢帧/吞吐走的是转发路径，
    # 与本地编码器无关，但旧实现把它们并进判据持续"投毒"——降级侧被钉到
    # min_bitrate/scale_min/fps_min，且只要那观众不断开就永不回升（回升分支有
    # 订阅者门控、降级分支没有）。
    for info in infos:
        if getattr(info, "watch_source", "local") != "local":
            continue
        ms = info.recent_send_ms(now_mono)
        if ms > 0:
            client_send_ms.append(ms)
        if info.has_recent_drop(now_mono):
            recent_drop = True
    bw_lines = []
    for info in infos:
        rate = info.rate_kbps(now_mono)   # 最近 1 秒实测速率（KB/s，日志用）
        if getattr(info, "watch_source", "local") == "local":
            # ABR 用 3 秒窗口取平均：跨过一个 GOP，避开关键帧突发造成的 10 倍跳变；
            # 仅本机源订阅者计入——否则慢的 peer 订阅者把本地码率压穿（net-016）
            egress_bps += info.rate_kbps(now_mono, window=_ABR_EGRESS_WINDOW_S) * 8192.0
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
    stats["egress_bps"] = int(egress_bps)
    with runtime.stats_lock:
        runtime.stats["avg_send_ms"] = effective_send_ms
        runtime.stats["egress_bps"] = int(egress_bps)
    log.info(
        "性能: 采集 %d fps / 编码 %d fps / 发送 %d fps / 帧率 %d / 质量 %d / 缩放 %.2f / 输出 %dx%d / 编码 %.1f ms / 发送 %.1f ms / 实测 %d Kbps%s",
        stats["capture_fps"], stats["encode_fps"], stats["send_fps"], stats["fps_now"],
        stats["quality"], stats["scale"], stats["out_w"], stats["out_h"],
        stats["encode_ms"], effective_send_ms, int(egress_bps) // 1000,
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

    第 112 条（C1）：码率降级按**实测吞吐**定目标。旧实现固定 ×0.7，链路实际只跑得动
    800 Kbps 时要从 6000 Kbps 连降 8 个评估周期（≈8 秒持续拥塞）才收敛；现在直接降到
    “实测速率 ×0.9”一步到位；实测吞吐高于 ×0.7 档位时仍取 ×0.7（正常小步降级）。
    实测值来自 ClientInfo._window 的字节数，与编码器目标码率无关，故内容稀疏（VBR
    本就出得少）不会误触发——本分支只在已判定拥塞时才走到。

    `level` 只在**真的重建了编码器**时才非空：否则持续拥塞/变化落在滞后带内时会每秒
    打印一条并未发生的“降级”。已到降级下限时 level 同样为空。
    """
    quality, scale, fps_now = cur["quality"], cur["scale"], cur["fps_now"]
    level = ""
    if cur["video_mode"] and cur["cur_bitrate"] > p.min_bitrate:
        cur_b = cur["cur_bitrate"]
        step_b = int(cur_b * 0.7)
        egress_bps = int(cur.get("egress_bps", 0))
        if egress_bps > 0:
            # 实测吞吐就是链路当前真实上限（含背压下被跳过的帧），留 10% 余量
            step_b = min(step_b, int(egress_bps * 0.9))
        new_b = max(p.min_bitrate, step_b)
        if _set_encoder_bitrate(runtime, new_b):
            level = ("码率 %d Kbps（实测 %d Kbps）" % (new_b // 1000, egress_bps // 1000)
                     if egress_bps > 0 else "码率 %d Kbps" % (new_b // 1000))
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

    第 112 条（C1）：码率档未真正重建（冷却中或变化落在 ±15% 滞后带内）时直接返回，
    不再打印一条并未发生的“自适应回升: 码率 X”（旧实现每秒一条假日志）。
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
        if not _set_encoder_bitrate(runtime, new_b, cooldown=2.0):
            return          # 未真正重建：既不改动其它参数，也不打日志
        level = "码率 %d Kbps" % (new_b // 1000)
    else:
        quality = min(base_quality, quality + 10)
        level = "质量 %d" % quality
    _apply_slot_params(runtime, quality, scale, fps_now)
    if level:
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
        stats["fps_now"] = safe_int(runtime.slot.get("fps", p.base_fps),
                                     p.base_fps, 1, 240, warn=False)
        stats["out_w"] = runtime.slot.get("out_w", 0)
        stats["out_h"] = runtime.slot.get("out_h", 0)
    budget_ms = 1000.0 / max(stats["fps_now"], 1) * 0.8
    effective_send_ms, recent_drop = _perf_log_and_bandwidth(runtime, stats)
    # 每次评估时从 runtime.cfg 读取开关，让 GUI 修改可以即时生效
    # B2（2026-09-11）：`perf` / `host` 必须先做 isinstance 兜底——手改成 `"perf": 1` 时
    # `1.get("adaptive", ...)` 抛 AttributeError，被 _perf_loop 的 log.exception 兜住后
    # 每秒写一条完整堆栈（5MB 滚动日志几分钟被刷满），而 _perf_tick 在读取开关处提前退出
    # → ABR 降级/回升永久失效（界面仍正常输出"性能:"行，看起来只是"自适应没反应"）。
    # 项目其它位置均已用 isinstance 防御，只有此处遗漏。
    host_cfg = runtime.cfg.get("host") if isinstance(runtime.cfg, dict) else None
    host_cfg = host_cfg if isinstance(host_cfg, dict) else {}
    perf_cfg = host_cfg.get("perf") if isinstance(host_cfg.get("perf"), dict) else {}
    if not bool(perf_cfg.get("adaptive", True)):
        return
    if stats["still"]:
        # 画面静止停发：发送耗时≈0 会让常规评估误入“回升”分支，反复重建编码器
        # （浪费 CPU）。静止期只统计不评估，恢复后自动继续。
        return
    # 同步读取用户最新基准值（GUI 修改 fps/质量/缩放后，回升目标随之更新，
    # 避免自适应回升“对抗”用户手动设置）
    base_fps = safe_int(host_cfg.get("fps", 30), 30, 1, 240, warn=False)
    base_quality = safe_int(host_cfg.get("jpeg_quality", 80), 80, 1, 100, warn=False)
    base_scale = safe_float(host_cfg.get("scale", 1.0), 1.0, 0.05, 4.0, warn=False)
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
    # net-016：降级补与回升对称的订阅者门控——没有本机源订阅者时降级只损本机画质、
    # 无人受益（回升分支同款门控见下）
    if congested and stats.get("subscribers", 0) > 0:
        _abr_degrade(runtime, p, stats, effective_send_ms, recent_drop)
    elif (stats["send_fps"] >= 5 and not recent_drop
          # B9：没有订阅者时不回升——send_fps 已由 _send_loop 在"零投递"时归零，这里
          # 再加一道订阅者计数，双保险（例如观看端全部切到了别的 peer 源）。
          and stats.get("subscribers", 0) > 0
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
    """第 15 条 → 第 136 条（C3）：实现收拢至 ui_common.install_excepthooks。

    host.exe 以 windowed 打包（console=False，无可用 stderr），默认钩子会把未捕获
    异常写向不存在的 stderr，线程崩溃时无任何日志。统一语义：线程异常记线程名 +
    traceback；主线程未捕获异常额外弹一次友好提示框（host 无 Tk 根窗口，不传
    get_root）；KeyboardInterrupt 透传。
    """
    ui_common.install_excepthooks(APP_NAME)


def print_public_addr(cfg):
    """打印公网地址提示（日志输出，windowed 模式亦安全）。"""
    public_addr = cfg_section(cfg["host"], "frp").get("public_addr", "")   # ui-022
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

    # A3（第 133 条）：GUI 模式先上屏纯 tkinter 极简 splash，再触发重依赖加载
    # （HostRuntime.__init__ 里的 _load_heavy 兜底）——双击到可见窗口的间隔
    # 不再包含 cv2/numpy/mss 的导入时间。splash 失败静默降级为旧行为。
    #
    # ui-020（第 36 条回退修复，2026-09-18）：DPI 感知必须**在建任何窗口之前**声明——
    # Windows 在进程首个 HWND 创建时即定死 DPI 感知，第 133 条把 splash 的 tk.Tk()
    # 提到了声明之前，使 enable_dpi_awareness() 必然失败且返回值被丢弃（完全静默）。
    # 与 viewer 侧顺序对齐。
    # capture-005（2026-09-20）：DPI 声明不该与"是否有窗口"绑定。旧实现只在
    # `not args.console` 时声明，命令行模式整个进程保持"未 DPI 感知"，于是
    # mss（EnumDisplayMonitors → 逻辑像素）与 dxcam（物理像素）两套坐标口径不同：
    # 系统缩放≠100% 时区域采集/副屏采集截错位置。另一半是时序隐患——
    # `import dxcam` 自身会在模块级把进程抬成 per-monitor aware（vendor/dxcam 的
    # DXFactory/Output.__post_init__ 调 SetProcessDpiAwareness(2)），不显式声明时
    # "第一个采集会话"用一套口径、自愈重建之后换另一套，同一份配置前后不一致。
    # 一开始就声明，两套口径从第一帧起统一。
    if not enable_dpi_awareness():
        log.warning("DPI 感知声明失败，高 DPI 下界面文字可能发虚、命令行模式的采集坐标"
                    "可能与 dxcam 物理像素口径不一致（详见日志）")
    splash_root = None
    if not args.console:
        try:
            import tkinter as tk
            import splash as splash_mod
            splash_root = tk.Tk()
            splash_mod.show_fast_splash(splash_root)
        except Exception:
            if splash_root is not None:
                try:
                    splash_root.destroy()
                except Exception:
                    pass
            splash_root = None

    runtime = HostRuntime(cfg)

    if splash_root is not None:
        try:
            splash_root.destroy()
        except Exception:
            pass

    # 自动启动 frpc（配置了 auto_start 时）
    if cfg_section(cfg["host"], "frp").get("auto_start"):   # ui-022（3616 行同源）
        ok, msg = runtime.frp.start()
        (log.info if ok else log.warning)("%s", msg)

    # 图形控制台模式（默认）：GUI 主循环为前台线程，服务在后台线程运行
    gui = None
    if not args.console:
        try:
            # ui-020：DPI 已在上方（建 splash 之前）声明，此处不再重复调用——
            # 重复调用的返回值恒 False，只会制造误导。
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
