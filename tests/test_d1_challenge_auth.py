# -*- coding: utf-8 -*-
"""D1 挑战-应答认证回归测试（实现方案-2026-09-18 §10.1–10.2）。

覆盖：
  - 状态机正控：register→chal→reg_proof 与 login→chal→auth_proof 全往返；
  - 拒绝路径：错口令、账号不存在、无进行中挑战（nonce 一次性/用户绑定）、挑战过期；
  - 兼容开关：require 默认拒绝旧版直发口令并给「观看端版本过旧」；allow_legacy 放行；
  - 旧账户兼容：第 131 条前用 legacy register 存的 {salt,hash} 记录可直接走挑战-应答；
  - 口令不过网：捕获观看端在 require 模式下发出的全部 socket 字节，断言无口令子串；
  - viewer._authenticate 集成：能力探测（probe 回包 proto）→ 挑战路径 → DPAPI 前语义不变。

hermetic：AccountManager 的 accounts.json 指到临时目录；服务端用 socketpair 直接驱动
host._auth_required_phase（握手后的阶段函数），不经真实监听端口。
"""
import copy
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

import common
import host
import viewer
from common import PBKDF2_ITERATIONS, Act, b64d, b64e, challenge_mac, derive_dk

PW = "s3cret-pw-d1"   # 含固定子串，供「口令不过网」断言反查


def _runtime(accounts_file, chal_mode=None, allow_register="open"):
    cfg = copy.deepcopy(common.DEFAULT_CONFIG)
    cfg["host"]["auth"]["enabled"] = True
    if chal_mode is not None:
        cfg["host"]["auth"]["chal_response"] = chal_mode
    cfg["host"]["auth"]["allow_register"] = allow_register
    with mock.patch.object(host, "accounts_path", return_value=accounts_file):
        return host.HostRuntime(cfg)


class _Tap:
    """socket 代理：记录本端发出的全部字节（socket 实例属性不可直接覆盖 sendall）。"""

    def __init__(self, sock):
        self._sock = sock
        self.data = []

    def sendall(self, data):
        self.data.append(bytes(data))
        return self._sock.sendall(data)

    def __getattr__(self, name):
        return getattr(self._sock, name)


class _Client:
    """把 _auth_required_phase 跑在线程里，本端扮演观看端。"""

    def __init__(self, rt, auth_cfg):
        self.a, b = socket.socketpair()
        self.b = _Tap(b)
        self.b.settimeout(20.0)
        self.out = {}
        cfg = dict({"auth_timeout": 60}, **auth_cfg)
        self.th = threading.Thread(
            target=self._serve,
            args=(rt, cfg), daemon=True)
        self.th.start()

    def _serve(self, rt, cfg):
        try:
            self.out["result"] = host._auth_required_phase(
                self.a, ("127.0.0.1", 1), rt, cfg)
        except Exception as e:      # pragma: no cover - 由主线程断言兜底
            self.out["error"] = e

    def send(self, msg):
        common.send_msg(self.b, msg)

    def recv(self):
        return common.recv_msg(self.b)

    def wire(self):
        return b"".join(self.b.data)

    def wait_closed(self):
        self.b.settimeout(5.0)
        return self.b.recv(100) == b""

    def close(self):
        for s in (self.b, self.a):
            try:
                s.close()
            except OSError:
                pass
        self.th.join(timeout=5.0)


def _answer_challenge(client, user, password, action_proof, extra=None, chal=None):
    """读（或复用已读的）chal → 现算 dk/proof → 提交应答；返回服务端的 auth_result。"""
    if chal is None:
        chal = client.recv()
    assert chal.get("action") == Act.CHAL, chal
    nonce = b64d(chal["nonce"])
    salt = (extra or {}).get("salt") or chal.get("salt") or ""
    dk = derive_dk(password, salt, int(chal.get("iter") or PBKDF2_ITERATIONS))
    reply = {"action": action_proof, "user": user,
             # auth-002（协议 v2.3）：proof 绑定用户名
             "proof": b64e(challenge_mac(dk, nonce, user)),
             "proto": list(common.PROTOCOL_VERSION)}
    if extra:
        reply.update(extra)
    if action_proof == Act.REG_PROOF:
        reply.setdefault("salt", salt)
        reply.setdefault("dk", dk.hex())
    client.send(reply)
    return client.recv()


class D1HostStateMachine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tv_d1_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.accounts_file = os.path.join(self.tmp, "accounts.json")

    def _rt(self, chal_mode=None, allow_register="open"):
        return _runtime(self.accounts_file, chal_mode, allow_register)

    def _cfg(self, chal_mode=None):
        return {"chal_response": chal_mode} if chal_mode else {}

    # ---------------- 正控往返 ----------------

    def test_register_then_login_roundtrip(self):
        rt = self._rt()
        c = _Client(rt, self._cfg())
        self.addCleanup(c.close)
        c.send({"action": Act.REGISTER, "user": "alice", "pass": "",
                "proto": list(common.PROTOCOL_VERSION)})
        res = _answer_challenge(c, "alice", PW, Act.REG_PROOF,
                                extra={"salt": os.urandom(16).hex()})
        self.assertTrue(res.get("ok"), res)
        c.th.join(5.0)
        self.assertEqual(c.out.get("result"), (True, "alice"))

        c2 = _Client(rt, self._cfg())
        self.addCleanup(c2.close)
        c2.send({"action": Act.LOGIN, "user": "alice", "pass": "",
                 "proto": list(common.PROTOCOL_VERSION)})
        chal = c2.recv()
        self.assertEqual(chal.get("action"), Act.CHAL)
        # 登录挑战必须带回存库的 salt，否则观看端无法复算 dk
        self.assertTrue(chal.get("salt"))
        self.assertEqual(chal.get("iter"), PBKDF2_ITERATIONS)
        res = _answer_challenge(c2, "alice", PW, Act.AUTH_PROOF, chal=chal)
        self.assertTrue(res.get("ok"), res)
        c2.th.join(5.0)
        self.assertEqual(c2.out.get("result"), (True, "alice"))

    def test_legacy_account_works_with_challenge(self):
        # 第 131 条前的记录只有 {salt,hash}（无 iter）：挑战流须回填迭代数并保持可登录
        rt = self._rt()
        ok, msg = rt.accounts.register("bob", PW)
        self.assertTrue(ok, msg)
        record = rt.accounts._users["bob"]
        record.pop("iter", None)          # 模拟第 131 条前的记录（无 iter 字段）
        self.assertNotIn("iter", record)
        c = _Client(rt, self._cfg())
        self.addCleanup(c.close)
        c.send({"action": Act.LOGIN, "user": "bob", "pass": "",
                "proto": list(common.PROTOCOL_VERSION)})
        res = _answer_challenge(c, "bob", PW, Act.AUTH_PROOF)
        self.assertTrue(res.get("ok"), res)

    # ---------------- 拒绝路径 ----------------

    def test_wrong_password_rejected_and_closed(self):
        rt = self._rt()
        self.assertTrue(rt.accounts.register("alice", PW)[0])
        c = _Client(rt, self._cfg())
        self.addCleanup(c.close)
        c.send({"action": Act.LOGIN, "user": "alice", "pass": "",
                "proto": list(common.PROTOCOL_VERSION)})
        res = _answer_challenge(c, "alice", "wrong-" + PW, Act.AUTH_PROOF)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("msg"), "用户名或密码错误")
        self.assertTrue(c.wait_closed(), "凭据校验失败后 host 必须断开")
        c.th.join(5.0)
        self.assertEqual(c.out.get("result"), (False, None))

    def test_unknown_account_gets_no_challenge(self):
        rt = self._rt()
        c = _Client(rt, self._cfg())
        self.addCleanup(c.close)
        c.send({"action": Act.LOGIN, "user": "ghost", "pass": "",
                "proto": list(common.PROTOCOL_VERSION)})
        chal = c.recv()
        # auth-008（2026-09-18）：账号不存在也照常下发挑战，响应形态与真实账号一致
        # ——不能据此枚举用户名
        self.assertEqual(chal.get("action"), Act.CHAL)
        self.assertRegex(chal.get("salt") or "", r"^[0-9a-f]{32}$")
        # auth-014 / T-04（2026-09-20）：旧实现下发 hmac(b"tv-nouser", user) 的
        # **确定性**伪盐，密钥硬编码在源码里 → 对端本地重算即可离线批量枚举用户名。
        # 形态合规不等于不可预测，所以这里钉的是"同一用户名两次挑战的盐必须不同"
        # （把实现改回确定性伪盐，这一条必须转红）。
        salt1 = chal.get("salt")
        c2 = _Client(rt, self._cfg())
        self.addCleanup(c2.close)
        c2.send({"action": Act.LOGIN, "user": "ghost", "pass": "",
                 "proto": list(common.PROTOCOL_VERSION)})
        chal2 = c2.recv()
        self.assertEqual(chal2.get("action"), Act.CHAL)
        self.assertNotEqual(salt1, chal2.get("salt"),
                            "不存在账号的挑战盐可复现 → 可离线枚举用户名（auth-014）")
        res = _answer_challenge(c, "ghost", PW, Act.AUTH_PROOF, chal=chal)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("msg"), "用户名或密码错误")
        self.assertTrue(c.wait_closed())

    def test_proof_without_pending_challenge_rejected(self):
        # nonce 一次性 + 挑战绑定：校验前先摘除 pending，任何「无挑战」应答都被拒
        rt = self._rt()
        self.assertTrue(rt.accounts.register("alice", PW)[0])
        c = _Client(rt, self._cfg())
        self.addCleanup(c.close)
        c.send({"action": Act.AUTH_PROOF, "user": "alice", "proof": b64e(b"x" * 32),
                "proto": list(common.PROTOCOL_VERSION)})
        res = c.recv()
        self.assertFalse(res.get("ok"))
        self.assertIn("没有进行中的挑战", res.get("msg", ""))
        self.assertTrue(c.wait_closed())

    def test_proof_user_must_match_challenge_user(self):
        rt = self._rt()
        self.assertTrue(rt.accounts.register("alice", PW)[0])
        self.assertTrue(rt.accounts.register("bob", PW)[0])
        c = _Client(rt, self._cfg())
        self.addCleanup(c.close)
        c.send({"action": Act.LOGIN, "user": "alice", "pass": "",
                "proto": list(common.PROTOCOL_VERSION)})
        chal = c.recv()
        self.assertEqual(chal.get("action"), Act.CHAL)
        c.send({"action": Act.AUTH_PROOF, "user": "bob",
                "proof": b64e(challenge_mac(b"k" * 32, b64d(chal["nonce"]), "bob")),
                "proto": list(common.PROTOCOL_VERSION)})
        res = c.recv()
        self.assertFalse(res.get("ok"))
        self.assertIn("没有进行中的挑战", res.get("msg", ""))
        self.assertTrue(c.wait_closed())

    def test_expired_challenge_rejected(self):
        rt = self._rt()
        self.assertTrue(rt.accounts.register("alice", PW)[0])
        with mock.patch.object(host, "CHALLENGE_TTL_S", 0.0):
            c = _Client(rt, self._cfg())
            self.addCleanup(c.close)
            c.send({"action": Act.LOGIN, "user": "alice", "pass": "",
                    "proto": list(common.PROTOCOL_VERSION)})
            chal = c.recv()
            time.sleep(0.05)
            res = _answer_challenge(c, "alice", PW, Act.AUTH_PROOF, chal=chal)
        self.assertFalse(res.get("ok"))
        self.assertIn("挑战已过期", res.get("msg", ""))

    # ---------------- 兼容开关（§10.2） ----------------

    def test_require_mode_rejects_plaintext_password(self):
        rt = self._rt()
        self.assertTrue(rt.accounts.register("alice", PW)[0])
        for action in (Act.LOGIN, Act.REGISTER):
            c = _Client(rt, self._cfg())     # 不传 chal_mode → 跟随 DEFAULT（require）
            self.addCleanup(c.close)
            self.assertEqual(
                rt.cfg["host"]["auth"].get("chal_response", "require"), "require")
            # net-047（2026-10-10）：刻意**不带** proto 字段，而不是原来的
            # `proto: [2, 2]`。本用例验的是认证层（require 拒直发口令），而
            # `PROTO_CHAL_MIN_MINOR=3` 这道门验的是"声明了 proto 的老客户端"；
            # net-047 新增的 `VIDEO_SEQ_MINOR=4` 门在认证层**之前**就把 `[2,2]`
            # 拦掉并给出更精确的文案（"请更新到 1.5.0"），于是原写法再也走不到
            # 这里，测的就不是它自己声称的东西了。
            # 不带 proto 才是 `allow_legacy` 真正服务的对象：1.3.x 及更早的观看端
            # 早于 C2 的 proto 字段，根本不会带这个字段（见 install.md §5.2）。
            # 顺带把这条边界钉住——**没有 proto 的老观看端不会被 net-047 误杀**。
            c.send({"action": action, "user": "alice", "pass": PW})
            res = c.recv()
            self.assertFalse(res.get("ok"))
            self.assertEqual(res.get("msg"), "观看端版本过旧，请更新后重试")
            self.assertTrue(c.wait_closed())

    def test_old_proto_client_gets_seq_message_not_chal_message(self):
        """net-047：带 proto 的老客户端先撞帧序号门，且文案比挑战-应答门更精确。

        这是两条门控的**先后与分流**契约。`[2,2]` 同时低于 `PROTO_CHAL_MIN_MINOR`
        (3) 与 `VIDEO_SEQ_MINOR` (4)，若顺序反了，用户会被告知"升级到挑战-应答
        版本"（1.4.0），而 1.4.0 观看端恰恰**仍解不了** v2.4 帧——按提示升级完
        还是会花屏。
        """
        rt = self._rt(chal_mode="allow_legacy")
        self.assertTrue(rt.accounts.register("alice", PW)[0])
        c = _Client(rt, self._cfg("allow_legacy"))
        self.addCleanup(c.close)
        c.send({"action": Act.LOGIN, "user": "alice", "pass": PW, "proto": [2, 2]})
        res = c.recv()
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("msg"), common.PROTO_SEQ_REJECT_MSG)
        # allow_legacy 只放宽"直发口令"这一层，不再放宽版本门控
        self.assertNotEqual(common.PROTO_SEQ_REJECT_MSG,
                            "观看端版本过旧，请更新后重试")
        self.assertTrue(c.wait_closed())

    def test_allow_legacy_mode_accepts_plaintext_with_warning(self):
        rt = self._rt(chal_mode="allow_legacy")
        self.assertTrue(rt.accounts.register("alice", PW)[0])
        c = _Client(rt, self._cfg("allow_legacy"))
        self.addCleanup(c.close)
        warned = []
        with mock.patch.object(host.log, "warning",
                               side_effect=lambda *a, **kw: warned.append(a)):
            # 同上：1.3.x 形态（无 proto 字段），net-047 不得拦它
            c.send({"action": Act.LOGIN, "user": "alice", "pass": PW})
            res = c.recv()
        self.assertTrue(res.get("ok"), res)
        text = " ".join(str(x) for w in warned for x in w)
        self.assertIn("allow_legacy", text)
        self.assertIn("alice", text)

    def test_unknown_chal_mode_falls_back_to_require(self):
        rt = self._rt()
        c = _Client(rt, {"chal_response": "nonsense"})
        self.addCleanup(c.close)
        self.assertTrue(rt.accounts.register("alice", PW)[0])
        c.send({"action": Act.LOGIN, "user": "alice", "pass": PW})
        res = c.recv()
        self.assertFalse(res.get("ok"))
        self.assertIn("版本过旧", res.get("msg", ""))

    # ---------------- 探测与能力广告 ----------------

    def test_probe_advertises_proto_and_keeps_connection(self):
        rt = self._rt()
        c = _Client(rt, self._cfg())
        self.addCleanup(c.close)
        c.send({"action": Act.PROBE, "user": "", "pass": "",
                "proto": list(common.PROTOCOL_VERSION)})
        res = c.recv()
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("msg"), "需要登录")
        self.assertEqual(res.get("proto"), list(common.PROTOCOL_VERSION))
        # 第 30 条：probe 后同一条连接仍可提交凭据（未注册账号 → 明确拒绝并断开，
        # 证明状态机仍在同一会话内而不是挂死）
        c.send({"action": Act.LOGIN, "user": "alice", "pass": "",
                "proto": list(common.PROTOCOL_VERSION)})
        res = c.recv()
        # auth-008：未注册账号同样收到挑战（伪盐），形态与真实账号一致
        self.assertEqual(res.get("action"), Act.CHAL)

    # ---------------- wire：口令不过网 ----------------

    def test_password_never_appears_on_wire(self):
        rt = self._rt()
        c = _Client(rt, self._cfg())
        self.addCleanup(c.close)
        c.send({"action": Act.REGISTER, "user": "dave", "pass": "",
                "proto": list(common.PROTOCOL_VERSION)})
        res = _answer_challenge(c, "dave", PW, Act.REG_PROOF,
                                extra={"salt": os.urandom(16).hex()})
        self.assertTrue(res.get("ok"), res)
        c2 = _Client(rt, self._cfg())
        self.addCleanup(c2.close)
        c2.send({"action": Act.LOGIN, "user": "dave", "pass": "",
                 "proto": list(common.PROTOCOL_VERSION)})
        res = _answer_challenge(c2, "dave", PW, Act.AUTH_PROOF)
        self.assertTrue(res.get("ok"), res)
        for client in (c, c2):
            self.assertNotIn(PW.encode("utf-8"), client.wire(),
                             "require 模式下观看端发出的任何字节都不得含口令")
        # 全量 JSON 复查：登录/注册消息里 pass 恒为空串
        for blob in c.wire(), c2.wire():
            self.assertNotIn(b'"pass": "%s"' % PW.encode(), blob)

    def test_probe_replies_carry_proto_in_optional_phase(self):
        # 准入关闭时也应带 proto（观看端能力探测路径依赖字段存在与否）
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        out = {}
        th = threading.Thread(
            target=lambda: out.update(
                result=host._auth_optional_phase(a, ("127.0.0.1", 1))),
            daemon=True)
        th.start()
        common.send_msg(b, {"action": Act.PROBE, "user": "", "pass": "",
                            "proto": list(common.PROTOCOL_VERSION)})
        res = common.recv_msg(b)
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("proto"), list(common.PROTOCOL_VERSION))
        th.join(5.0)


class D1AccountManagerApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tv_d1_acc_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        with mock.patch.object(host, "accounts_path",
                               return_value=os.path.join(self.tmp, "accounts.json")):
            self.mgr = host.AccountManager()

    def test_register_derived_validates_format(self):
        bad_hex = self.mgr.register_derived("u1", "zz" * 16, "00" * 32)
        self.assertFalse(bad_hex[0])
        short = self.mgr.register_derived("u2", os.urandom(16).hex(), "00" * 8)
        self.assertFalse(short[0])
        salt = os.urandom(16).hex()
        dk = os.urandom(32).hex()
        ok, msg = self.mgr.register_derived("u3", salt, dk)
        self.assertTrue(ok, msg)
        rec = self.mgr._users["u3"]
        self.assertEqual(rec["hash"], dk)
        self.assertEqual(rec["iter"], PBKDF2_ITERATIONS)
        # 写盘成功：重新加载后仍在
        with mock.patch.object(host, "accounts_path",
                               return_value=os.path.join(self.tmp, "accounts.json")):
            self.assertIn("u3", host.AccountManager()._users)

    def test_challenge_for_and_verify_proof(self):
        started = time.time()
        salt = os.urandom(16).hex()
        dk = derive_dk(PW, salt, PBKDF2_ITERATIONS)
        self.assertTrue(self.mgr.register_derived("alice", salt, dk.hex())[0])
        got = self.mgr.challenge_for("alice")
        self.assertEqual(got, (salt, PBKDF2_ITERATIONS))
        self.assertIsNone(self.mgr.challenge_for("ghost"))
        nonce = os.urandom(16)
        ok, msg = self.mgr.verify_login_proof("alice", nonce,
                                              challenge_mac(dk, nonce, "alice"))
        self.assertTrue(ok, msg)
        # T-13（2026-09-20）：旧断言把 last_login 与 `time.strftime(...)` 做**秒级墙钟
        # 字符串相等**比较——写入与读取跨秒即偶发红（flaky）。改为解析成时间戳、
        # 只要求落在本次调用前后的合理窗口内。
        recorded = self.mgr._users["alice"].get("last_login")
        parsed = time.mktime(time.strptime(recorded, "%Y-%m-%d %H:%M:%S"))
        self.assertGreaterEqual(parsed, started - 2.0)
        self.assertLessEqual(parsed, time.mktime(time.localtime()) + 2.0)
        ok, msg = self.mgr.verify_login_proof("alice", nonce,
                                              challenge_mac(os.urandom(32), nonce,
                                                            "alice"))
        self.assertFalse(ok)
        self.assertEqual(msg, "用户名或密码错误")

    def test_corrupt_record_rejected_gracefully(self):
        # 第 27 条口径的延续：记录损坏 → 明确拒绝而不是异常
        self.mgr._users["broken"] = {"salt": "zz", "hash": "yy"}
        # auth-019（2026-09-20）：旧实现只判 isinstance(salt, str)，把 "zz" 原样下发，
        # 观看端用 `_is_hex_str(salt, 32)` 拒收 → 该账户永久登不进且零提示。
        # 这条断言旧口径（assertIsNotNone「字段在，格式后验」）把缺陷钉成了期望值。
        self.assertIsNone(self.mgr.challenge_for("broken"))
        ok, msg = self.mgr.verify_login_proof("broken", os.urandom(16), b"x" * 32)
        self.assertFalse(ok)
        # auth-008：记录损坏/不存在与口令错误统一文案（不可枚举）
        self.assertEqual(msg, "用户名或密码错误")


class D1ViewerIntegration(unittest.TestCase):
    """viewer._authenticate 的真实协议路径：能力探测 → 挑战-应答 → 凭据缓存。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tv_d1_vw_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.rt = _runtime(os.path.join(self.tmp, "accounts.json"))

    def _channel(self, **attrs):
        ch = viewer.Channel.__new__(viewer.Channel)
        ch.name = "D1频道"
        ch.owner = None
        ch.username = ""
        ch._password = None
        ch.auth = {}
        ch._anon_ok = False
        ch._auth_probed = False
        ch._chal_capable = None
        ch._prompt_blocked_until = 0.0
        ch._retry_delay = 12.5
        ch._auth_error_text = ""
        ch._last_auth_user = ""
        ch.auth_msg = ""
        for k, v in attrs.items():
            setattr(ch, k, v)
        return ch

    def _serve_phase(self):
        """起一个后台线程跑 host 的认证阶段，返回 (tap, out, thread)。

        **调用方必须在断言前 join**（见 `_await_served`）：`out["result"]` 是在
        `_auth_required_phase` **返回之后**才写入的，而观看端的 `_authenticate`
        读到回包即可返回——两者之间只隔一次 GIL 切换。旧实现不 join，于是全量跑
        （线程多、GIL 切换频繁）时断言偶尔先跑到，`out.get("result")` 拿到 `None`，
        CI 随机变红；单独跑该用例 20 次（子进程）与 80 次（进程内）都不复现。
        """
        a, b = socket.socketpair()
        tap = _Tap(b)
        tap.settimeout(20.0)
        out = {}

        def target():
            try:
                out["result"] = host._auth_required_phase(
                    a, ("127.0.0.1", 1), self.rt, {"auth_timeout": 60})
            except Exception as e:      # pragma: no cover
                out["error"] = e

        th = threading.Thread(target=target, daemon=True)
        th.start()
        # 用例中途失败时也要收尾，别把线程漏给后面的用例
        self.addCleanup(th.join, 20.0)
        return tap, out, th

    def _await_served(self, out, th):
        """断言前必须调用：等服务端认证阶段真正收尾，并把线程里的异常翻出来。"""
        th.join(timeout=20.0)
        self.assertFalse(th.is_alive(), "host 认证阶段线程 20 秒未收尾")
        if "error" in out:
            # 不翻出来的话，断言只会显示 `None != (True, 'carol')`，真因丢失
            raise out["error"]

    def test_login_via_challenge_success_and_no_password_on_wire(self):
        self.assertTrue(self.rt.accounts.register("alice", PW)[0])
        sock, out, th = self._serve_phase()
        self.addCleanup(sock.close)
        ch = self._channel(username="alice", _password=PW)
        with mock.patch.object(viewer, "_dpapi_encrypt", return_value=None):
            self.assertTrue(ch._authenticate(sock), ch.auth_msg)
        self._await_served(out, th)
        self.assertEqual(out.get("result"), (True, "alice"))
        self.assertEqual(ch._chal_capable, True)
        self.assertNotIn(PW.encode("utf-8"), b"".join(sock.data))
        self.assertEqual(ch.auth_msg, "")

    def test_register_via_dialog_uses_challenge(self):
        sock, out, th = self._serve_phase()
        self.addCleanup(sock.close)
        ch = self._channel()
        ch._ask_credentials = lambda: ("register", "carol", PW)
        with mock.patch.object(viewer, "_dpapi_encrypt", return_value=None):
            self.assertTrue(ch._authenticate(sock), ch.auth_msg)
        self._await_served(out, th)
        self.assertEqual(out.get("result"), (True, "carol"))
        self.assertNotIn(PW.encode("utf-8"), b"".join(sock.data))
        rec = self.rt.accounts._users["carol"]
        self.assertEqual(rec["iter"], PBKDF2_ITERATIONS)

    def test_wrong_password_maps_to_item133_behaviour(self):
        self.assertTrue(self.rt.accounts.register("alice", PW)[0])
        sock, out, th = self._serve_phase()
        self.addCleanup(sock.close)
        ch = self._channel(username="alice", _password="wrong-" + PW)
        self.assertFalse(ch._authenticate(sock))
        self._await_served(out, th)
        self.assertIn("用户名或密码错误", ch.auth_msg)
        self.assertEqual(ch._retry_delay, 0.0)
        self.assertEqual(ch.username, "")          # 凭据已清除（第 133 条）
        self.assertIsNone(ch._password)
        self.assertEqual(ch._last_auth_user, "alice")

    def test_old_host_without_proto_refuses_cleartext(self):
        # auth-003（2026-09-18）：旧服务端（probe 回包无 proto）不再降级为明文口令
        # 直发——明确拒绝，且不再向对端发送任何登录消息
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.settimeout(10.0)
        seen = []

        def serve():
            try:
                # 旧服务端在 socketpair 的另一端（a）；观看端用 b
                common.recv_msg(a)   # probe（新观看端的能力探测，旧服务端也会收到）
                common.send_msg(a, {"action": Act.AUTH_RESULT, "ok": False,
                                    "msg": "需要登录"})          # 旧版：无 proto
                seen.append(common.recv_msg(a))   # 若观看端仍直发口令，这里会收到
            except Exception:
                pass

        threading.Thread(target=serve, daemon=True).start()
        ch = self._channel(username="alice", _password=PW)
        with mock.patch.object(viewer, "_dpapi_encrypt", return_value=None):
            self.assertFalse(ch._authenticate(b))
        self.assertEqual(ch._chal_capable, False)
        self.assertIn("挑战-应答", ch.auth_msg)
        b.close()
        time.sleep(0.3)
        self.assertEqual(seen, [], "auth-003：不得再降级直发口令")


class D1ProtocolSurface(unittest.TestCase):
    def test_defaults_and_constants(self):
        # auth-002（2026-09-18）：2.3 = proof 绑定 user + 域分隔串
        # net-045（2026-10-09）：2.4 = 视频帧载荷带单调递增帧序号
        self.assertEqual(common.PROTOCOL_VERSION, (2, 4))
        # 挑战-应答的准入门槛仍是 2.3（认证面未变），帧序号门槛另由 VIDEO_SEQ_MINOR 把关
        self.assertEqual(common.PROTO_CHAL_MIN_MINOR, 3)
        self.assertEqual(common.VIDEO_SEQ_MINOR, 4)
        self.assertEqual(common.DEFAULT_CONFIG["host"]["auth"]["chal_response"],
                         "require")
        self.assertEqual(Act.CHAL, "chal")
        self.assertEqual(Act.AUTH_PROOF, "auth_proof")
        self.assertEqual(Act.REG_PROOF, "reg_proof")
        # v2.2 与 2.1 客户端主版本一致 → proto_compatible 仍放行（兼容窗口成立）
        ok, compat = common.proto_compatible({"proto": [2, 1]})
        self.assertTrue(ok and compat)
        self.assertFalse(common.proto_compatible({"proto": [3, 0]})[1])

    def test_common_helpers_roundtrip(self):
        salt = os.urandom(16).hex()
        dk = derive_dk(PW, salt, 1000)
        self.assertEqual(len(dk), 32)
        nonce = os.urandom(16)
        self.assertEqual(b64d(b64e(challenge_mac(dk, nonce))), challenge_mac(dk, nonce))
        with self.assertRaises(ValueError):
            b64d("not base64!!")
        # 与 host 存储格式互逆：_hash_password 的 hex 即 verify_login_proof 的 HMAC key
        h = host._hash_password(PW, salt)
        self.assertEqual(h, derive_dk(PW, salt, PBKDF2_ITERATIONS).hex())


if __name__ == "__main__":
    unittest.main()
