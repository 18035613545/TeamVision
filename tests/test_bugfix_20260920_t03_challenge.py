# -*- coding: utf-8 -*-
"""T-03 / auth-002 + auth-020 回归：观看端必须以**具体文案**拒绝畸形挑战。

第 6 轮审查第十章 T-03 的判定：旧用例只加了 `PROTOCOL_VERSION == (2, 3)` 这类常量
断言，`common.py:132-156` 的 iter/salt 校验与 `viewer.py:1099-1128` 的三条拒登分支
**无用例**；`_answer_challenge` 永远复算合法参数，"中间人回 iter=1"从未模拟；
先 `pop("iter")` 再 `assertNotIn("iter")` 是自我实现断言。

本文件用真实 `viewer.Channel._auth_via_challenge` + 真实 socketpair（假服务端把
挑战报文直接写进内核缓冲），逐条钉：

  auth-020（逃逸口径）：`{"iter": Infinity}` 走 `int(inf)` → **OverflowError**；
    `{"nonce": 123}` 走 `b64d(123)` → **AttributeError**。旧捕获元组只有
    `(ValueError, TypeError)`，两类都会**逃出** `_auth_via_challenge`，落到
    `_authenticate` 的兜底分支 → 用户看到"认证通信失败"+ 无限重连，而 auth-002
    准备的三条明确文案全部被绕过。
  auth-002（参数强校验）：iter 与本地常量不一致 / nonce 过短 / salt 非 hex
    → 各自的具体文案，且**必须发生在 derive_dk 之前**（否则中间人回
    `{iter:1, salt:自选}` 就得到一个一次迭代的离线口令爆破 oracle）。

改回缺陷形态会红在哪一行（逐行论证）：
  ① 把 `viewer.py:1103` 的捕获元组改回 `(ValueError, TypeError)` →
     `test_json_infinity_iter...` 里 OverflowError 直接冒出 → 用例以 ERROR 收场（红）；
     `test_nonce_int...` 同理抛 AttributeError。
  ② 删掉 `viewer.py:1116-1136` 的三条校验（iter / nonce / salt）→
     `test_iter_downgrade_is_named_not_generic` 等三条 assertIn(具体文案) 红，
     且 `derive_dk` 调用探针（`TestDeriveIsNeverReached`）计数 >0 红。
  ③ 把 `common.py:148-152` 的 `OverflowError` 从 derive_dk 的归一里去掉 →
     `test_kdf_bounds_reject_before_any_work` 的 assertRaises(ValueError) 收到
     OverflowError → 红。
  ④ 把校验写成"静默钳制到合法区间"（auth-002 明确禁止）→ 具体文案断言红，
     `test_valid_challenge_still_completes` 的 proof 比对也红（两侧 dk 不同）。
`TestOldCatchShapeEscapes` 另在同文件复刻旧捕获元组，实证它对同两种报文抛出
OverflowError / AttributeError，即上面 (1) 的红不是编排出来的。
"""
import os
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common
import viewer

SALT = "ab" * 16                       # 32 位十六进制（KDF_SALT_HEX_LEN）
GOOD_NONCE = common.b64e(os.urandom(16))


def _channel():
    """绕开真实 Channel.__init__（不建线程、不连网），只留日志要用的 name。"""
    ch = viewer.Channel.__new__(viewer.Channel)
    ch.name = "t03"
    return ch


class _FakeServer:
    """把挑战（以及可选的 auth_result）预写进 socketpair 的服务端一侧。"""

    def __init__(self, *messages):
        self.client, self.server = socket.socketpair()
        self.client.settimeout(10.0)
        self.server.settimeout(10.0)
        for m in messages:
            if m is not None:
                common.send_msg(self.server, m)

    def recv_from_client(self):
        return common.recv_msg(self.server)

    def close(self):
        self.client.close()
        self.server.close()


def _chal(**over):
    msg = {"action": common.Act.CHAL, "nonce": GOOD_NONCE, "salt": SALT,
           "iter": common.PBKDF2_ITERATIONS}
    msg.update(over)
    return msg


class TestMalformedChallengeShapes(unittest.TestCase):
    """auth-020：两类"合法 JSON、非法类型"的挑战必须被吞成明确文案，不得逃逸。"""

    def _answer(self, chal):
        srv = _FakeServer(chal)
        self.addCleanup(srv.close)
        return _channel()._auth_via_challenge(srv.client, common.Act.LOGIN,
                                              "bob", "pw")

    def test_json_infinity_iter_is_rejected_as_malformed(self):
        """`{"iter": Infinity}` 是**合法 JSON**：int(inf) 抛 OverflowError。

        旧捕获元组漏它 → 异常逃出 → 落到"认证通信失败"兜底。
        """
        result = self._answer(_chal(iter=float("inf")))
        self.assertFalse(result.get("ok", True))
        self.assertEqual(result.get("msg"), "挑战消息格式非法",
                         "Infinity 未被归一：文案=%r（逃逸后会变成「认证通信失败」）"
                         % result.get("msg"))

    def test_int_nonce_is_rejected_as_malformed(self):
        """`{"nonce": 123}`：b64d 里 `123.encode` 抛 AttributeError（common.py:176-178）。"""
        result = self._answer(_chal(nonce=123))
        self.assertEqual(result.get("msg"), "挑战消息格式非法",
                         "整型 nonce 未被归一：文案=%r" % (result.get("msg"),))

    def test_unusable_iter_types_are_rejected_as_malformed(self):
        """类型层面就无法转成 int 的 iter：一律归到"格式非法"，不得逃逸。"""
        for bad in ("abc", float("nan"), [1, 2], {"x": 1}, "100000abc"):
            with self.subTest(bad=bad):
                result = self._answer(_chal(iter=bad))
                self.assertEqual(result.get("msg"), "挑战消息格式非法",
                                 "iter=%r 的文案不是「挑战消息格式非法」" % (bad,))

    def test_absent_iter_falls_back_to_local_constant(self):
        """iter 缺失/为 null 时用**本地常量**（不是对端可控值）→ 仍是安全路径。

        这一条挡住"把缺省也判成畸形"的过度修正，同时钉住"缺省值来自本端"。
        """
        for absent in (None, "", [], {}):
            with self.subTest(absent=absent):
                srv = _FakeServer(_chal(iter=absent),
                                  {"action": common.Act.AUTH_RESULT, "ok": True})
                try:
                    out = _channel()._auth_via_challenge(srv.client, common.Act.LOGIN,
                                                         "bob", "pw")
                finally:
                    srv.close()
                self.assertNotEqual(out.get("msg"), "挑战消息格式非法")
                self.assertTrue(out.get("ok"), "缺省 iter 的合法挑战被误拒：%r" % (out,))

    def test_finite_but_wrong_iter_is_named_not_malformed(self):
        """能转成 int 但值不对（1e30 / -1 / 1.5 取整）→ 走 auth-002 的 iter 分支。"""
        for bad in (1e30, -1, 1.5, 10 ** 12):
            with self.subTest(bad=bad):
                out = self._answer(_chal(iter=bad))
                self.assertIn("iter", out.get("msg", ""),
                              "iter=%r 未走具名分支：%r" % (bad, out.get("msg")))

    def test_invalid_base64_nonce_is_rejected_as_malformed(self):
        result = self._answer(_chal(nonce="!!!not-base64!!!"))
        self.assertEqual(result.get("msg"), "挑战消息格式非法")

    def test_non_dict_and_wrong_action_pass_through(self):
        """账号不存在/服务端直接拒绝时 chal 本身就是 auth_result：必须原样返回。"""
        srv = _FakeServer({"action": common.Act.AUTH_RESULT, "ok": False,
                           "msg": "用户不存在"})
        self.addCleanup(srv.close)
        out = _channel()._auth_via_challenge(srv.client, common.Act.LOGIN, "bob", "pw")
        self.assertEqual(out.get("msg"), "用户不存在")


class TestAuth002ParameterChecks(unittest.TestCase):
    """auth-002：三条拒登分支各有一条**具体**文案，不得与兜底文案混淆。"""

    def _answer(self, chal):
        srv = _FakeServer(chal)
        self.addCleanup(srv.close)
        return _channel()._auth_via_challenge(srv.client, common.Act.LOGIN,
                                              "bob", "pw")

    def test_iter_downgrade_is_named_not_generic(self):
        """中间人回 `{iter: 1}`：必须报"iter 参数异常"，而不是"认证通信失败"。"""
        for bad_iter in (1, 999, common.KDF_ITER_MAX + 1,
                         common.PBKDF2_ITERATIONS + 1):
            with self.subTest(it=bad_iter):
                out = self._answer(_chal(iter=bad_iter))
                self.assertIn("iter", out.get("msg", ""))
                self.assertIn("拒绝登录", out.get("msg", ""))
                self.assertNotEqual(out.get("msg"), "挑战消息格式非法")

    def test_short_nonce_is_named(self):
        """空/短 nonce 会让 proof 退化成常量 HMAC(dk,"")，可永久重放 → 必须拒。"""
        for n in ("", common.b64e(b"1234567"), common.b64e(b"")):
            with self.subTest(nonce=n):
                out = self._answer(_chal(nonce=n))
                self.assertIn("nonce", out.get("msg", ""))
                self.assertIn("拒绝登录", out.get("msg", ""))

    def test_salt_forms_are_named(self):
        out = self._answer(_chal(salt=""))
        self.assertEqual(out.get("msg"), "挑战消息缺少 salt")
        out = self._answer(_chal(salt="zz" * 16))
        self.assertIn("salt", out.get("msg", ""))
        self.assertEqual(out.get("msg"), "挑战消息格式非法（salt 须为 32 位十六进制）",
                         "salt 非法的文案与「格式非法」兜底混在一起，面板无法归因")
        out = self._answer(_chal(salt=123))
        self.assertIn("salt", out.get("msg", ""))

    def test_no_generic_escape_message_on_any_shape(self):
        """上述所有畸形形态都不得退到 `_authenticate` 的兜底"认证通信失败"。"""
        for chal in (_chal(iter=float("inf")), _chal(nonce=123), _chal(iter=1),
                     _chal(nonce=""), _chal(salt=""), _chal(salt="zz" * 16)):
            with self.subTest(chal=sorted(chal)):
                out = self._answer(chal)
                self.assertNotIn("通信失败", out.get("msg", ""))
                self.assertTrue(out.get("msg"), "返回了空文案（面板将显示套话）")


class TestDeriveIsNeverReached(unittest.TestCase):
    """auth-002 的关键前提：拒登必须发生在**调用 PBKDF2 之前**。"""

    def _answer_with_spy(self, chal, then=None):
        srv = _FakeServer(chal, then)
        self.addCleanup(srv.close)
        calls = []
        real = common.derive_dk

        def spy(password, salt_hex, iterations=common.PBKDF2_ITERATIONS):
            calls.append((salt_hex, iterations))
            return real(password, salt_hex, 1000)     # 快速实现，避免真的跑 10 万次

        with mock.patch.object(viewer, "derive_dk", spy):
            out = _channel()._auth_via_challenge(srv.client, common.Act.LOGIN,
                                                 "bob", "pw")
        return out, calls

    def test_malformed_shapes_never_call_derive(self):
        for chal in (_chal(iter=float("inf")), _chal(nonce=123), _chal(iter=1),
                     _chal(nonce=""), _chal(salt=""), _chal(salt="zz" * 16)):
            with self.subTest(chal=sorted(chal)):
                _out, calls = self._answer_with_spy(chal)
                self.assertEqual(calls, [],
                                 "校验漏在 derive_dk 之后：对端可控参数已进 KDF（爆破 oracle）")

    def test_valid_shape_calls_derive_with_local_iterations(self):
        out, calls = self._answer_with_spy(
            _chal(), {"action": common.Act.AUTH_RESULT, "ok": True})
        self.assertEqual(len(calls), 1, "合法挑战应恰好派生一次 dk")
        self.assertEqual(calls[0], (SALT, common.PBKDF2_ITERATIONS),
                         "派生参数不是本地常量（iter 被对端左右的形态回来了）")
        self.assertNotIn("格式非法", str(out.get("msg", "")))
        self.assertTrue(out.get("ok"))


class TestOldCatchShapeEscapes(unittest.TestCase):
    """负向对照：旧捕获元组 `(ValueError, TypeError)` 对同两种报文确实拦不住。"""

    @staticmethod
    def _old_head(chal):
        nonce = common.b64d(chal.get("nonce") or "")
        iter_ = int(chal.get("iter") or common.PBKDF2_ITERATIONS)
        return nonce, iter_

    def test_old_shape_raises_out(self):
        for chal, exc in ((_chal(iter=float("inf")), OverflowError),
                          (_chal(nonce=123), AttributeError)):
            with self.subTest(chal=sorted(chal)):
                try:
                    self._old_head(chal)
                except exc:
                    pass                            # 预期：旧形态把异常抛给调用方
                else:
                    self.fail("对照失效：旧形态本应在此抛出 %s" % exc.__name__)
                # 真实实现（同一输入）不得抛出任何异常
                srv = _FakeServer(chal)
                try:
                    out = _channel()._auth_via_challenge(
                        srv.client, common.Act.LOGIN, "bob", "pw")
                except Exception as e:              # noqa: BLE001
                    self.fail("真实实现逃逸了 %s：%s" % (type(e).__name__, e))
                finally:
                    srv.close()
                self.assertEqual(out.get("msg"), "挑战消息格式非法")

    def test_old_shape_is_fine_for_plain_bad_types(self):
        """对照的另一半：`iter:"abc"` 在旧元组下已被 ValueError 接住 →
        说明本文件的红/绿确实由 OverflowError、AttributeError 两类决定。"""
        with self.assertRaises(ValueError):
            self._old_head(_chal(iter="abc"))


class TestKdfBoundsInCommon(unittest.TestCase):
    """auth-002/auth-020 在 common.py 的那半边：derive_dk 只承诺抛 ValueError。"""

    def test_kdf_bounds_reject_before_any_work(self):
        for it in (1, 999, common.KDF_ITER_MAX + 1, 10 ** 9,
                   float("inf"), float("nan"), "abc", None, [], {}):
            with self.subTest(it=it):
                with self.assertRaises(ValueError):
                    common.derive_dk("pw", SALT, it)

    def test_salt_shape_rejected(self):
        for salt in ("", "abc", "zz" * 16, "ab" * 40, "abc"):
            with self.subTest(salt=salt):
                with self.assertRaises(ValueError):
                    common.derive_dk("pw", salt, common.PBKDF2_ITERATIONS)

    def test_valid_derivation_is_deterministic(self):
        a = common.derive_dk("pw", SALT, 1000)
        b = common.derive_dk("pw", SALT, 1000)
        self.assertEqual(a, b)
        self.assertNotEqual(a, common.derive_dk("pw2", SALT, 1000))


class TestHappyPathProofBinding(unittest.TestCase):
    """正向对照（挡住"实现被改成一律拒绝"的假绿）：合法挑战仍要完成认证。"""

    def test_valid_challenge_still_completes(self):
        password = "correct horse"
        nonce = os.urandom(16)
        result = {"action": common.Act.AUTH_RESULT, "ok": True, "user": "bob"}
        srv = _FakeServer(_chal(nonce=common.b64e(nonce)), result)
        self.addCleanup(srv.close)
        out = _channel()._auth_via_challenge(srv.client, common.Act.LOGIN,
                                             "bob", password)
        self.assertEqual(out, result)
        login = srv.recv_from_client()
        proof = srv.recv_from_client()
        self.assertEqual(login.get("action"), common.Act.LOGIN)
        self.assertEqual(login.get("pass"), "", "口令不得过网（D1 的基本承诺）")
        self.assertEqual(proof.get("action"), common.Act.AUTH_PROOF)
        # proof 必须绑定 user 与域分隔串（auth-002 / 协议 v2.3）
        dk = common.derive_dk(password, SALT, common.PBKDF2_ITERATIONS)
        self.assertEqual(proof.get("proof"),
                         common.b64e(common.challenge_mac(dk, nonce, "bob")))
        self.assertNotEqual(proof.get("proof"),
                            common.b64e(common.challenge_mac(dk, nonce, "mallory")),
                            "proof 没绑定用户名：可跨账号搬运")
        self.assertEqual(proof.get("proto"), list(common.PROTOCOL_VERSION))


if __name__ == "__main__":
    unittest.main()
