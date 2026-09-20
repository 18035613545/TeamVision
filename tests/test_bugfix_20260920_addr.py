# -*- coding: utf-8 -*-
"""net-028 / net-027 回归：`common.parse_addr` 的 IPv6 形态与"空主机"判定。

对应第 6 轮审查第十章 T 系列的判定方法：「把被修复的代码改回缺陷形态，测试会不会红」。
本文件的用例全部**直接调真实函数**（无替身、无 monkeypatch），因此判定力强。

钉住的三个缺陷形态（按报告行号）：
  · net-018 的初衷（`common.py:742-753`）：裸 IPv6 `::1` 必须整体作主机、
    端口取缺省值。旧旧实现 `rsplit(":")` 把 "1" 当端口 → 连到端口 1。
  · net-028（`common.py:752-755`）：net-018 的修法一度"只要冒号 ≥2 就整串作主机"，
    把另一种本来可用的写法 `::1:5700`（裸 IPv6 + 端口，A0 双栈回环下 frpc 回连的
    常见手写形式）也吞成 ('::1:5700', 5700)——而 Windows 的 getaddrinfo 不报错，
    把它曲解成 `::0.1.87.0` 之类，比"连不上"更难查。收口形态是
    「末段是纯数字 且 去掉末段后仍是合法 IPv6」才按 host:port 拆。
  · net-027（`common.py:771-773`）：`:5700` 这类冒号前为空的输入旧实现返回
    ('', 5700)，空主机经 create_connection 解析到本机链路 IPv6（fe80::…），
    观看端每轮重连白等 10 秒再进无限退避。现在必须抛 ValueError。

mutation 实测（本轮判定标准，纯函数允许在 %TEMP% 上跑变异体）：
  1) 把 net-028 分支改回 `return addr_str, default_port`（去掉 rpartition 判定）
     → test_bare_ipv6_with_port_splits 期望 ('::1', 5700) 实得 ('::1:5700', 5700) → **红**；
  2) 删掉 net-027 的 `if not host: raise` → test_empty_host_rejected 收到 ('', 5700) → **红**；
  3) 把 net-018 分支改回无条件 rsplit → test_bare_ipv6_without_port_takes_default
     期望 ('::1', 5700) 实得 ('::', 1) → **红**。
三条变异体已在 %TEMP% 用 importlib 实跑复核，结果见本轮报告。

附带口径缺口（**不在此断言**，避免把缺陷钉成契约，交第 7 轮处理）：
`::1:99999` / `::1:0` 经 net-028 新分支时未过 `1 <= port <= 65535` 检查，
仍返回 ('::1', 99999)；其它分支都会抛 ValueError。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import parse_addr

#: 缺省端口（DEFAULT_CONFIG 里两端一致使用的 5700）
P = 5700


class TestBareIpv6Forms(unittest.TestCase):
    """无方括号的裸 IPv6：net-018（整串作主机）与 net-028（末段确为端口时拆分）共存。"""

    def test_bare_ipv6_with_port_splits(self):
        """net-028 核心：`::1:5700` 是"地址 + 端口"，不得整串当主机。

        改回缺陷形态（无条件 `return addr_str, default_port`）实得
        ('::1:5700', 5700) —— 端口被吞、主机名成为非法字符串。
        """
        self.assertEqual(parse_addr("::1:5700"), ("::1", P))
        # 更长的合法前缀同理（去掉末段后仍是合法 IPv6）
        self.assertEqual(parse_addr("2001:db8::1:5700"), ("2001:db8::1", P))

    def test_bare_ipv6_without_port_takes_default(self):
        """net-018 半边不得被 net-028 改回去：`::1` 整串都是主机，端口取缺省。

        若实现退回"末段是数字就当端口"，这里会得到 ('::', 1)——
        正是报告里"连到端口 1"的原始故障形态。
        """
        self.assertEqual(parse_addr("::1"), ("::1", P))
        self.assertEqual(parse_addr("::1", default_port=8000), ("::1", 8000))
        self.assertEqual(parse_addr("fe80::1"), ("fe80::1", P))
        # 末段不是纯数字时（形如 ::abcd:ef01）也不得拆——rsplit 判定含 isdigit 门槛
        self.assertEqual(parse_addr("2001:db8::abcd:ef01"), ("2001:db8::abcd:ef01", P))

    def test_bare_ipv6_last_segment_not_a_port(self):
        """末段虽是数字、但去掉它之后不是合法 IPv6 → 整串作主机（ipaddress 判定，非数冒号）。

        `1:2:3:4:5:6:7:8` 去掉末段 "8" 得 `1:2:3:4:5:6:7` —— 不是合法 IPv6
        （缺一段），必须整体作主机，不能被"末段是数字"这一半条件骗走。
        """
        self.assertEqual(parse_addr("1:2:3:4:5:6:7:8"), ("1:2:3:4:5:6:7:8", P))


class TestBracketedIpv6Forms(unittest.TestCase):
    """带方括号形态：net-018 起就该支持，且是 `[地址]:端口` 的规范写法。"""

    def test_bracket_forms(self):
        self.assertEqual(parse_addr("[::1]:5700"), ("::1", P))
        self.assertEqual(parse_addr("[::1]"), ("::1", P))
        self.assertEqual(parse_addr("[fe80::1%eth0]:5700"), ("fe80::1%eth0", P))
        self.assertEqual(parse_addr("[2001:db8::1]:8000", default_port=1),
                         ("2001:db8::1", 8000))

    def test_bracket_malformed_still_rejected(self):
        """括号形态的非法后缀必须抛 ValueError，不得静默降级成"整串作主机"。"""
        for bad in ("[]:5700", "[::1", "[::1]5700", "[::1]:x", "[::1]:"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_addr(bad)


class TestEmptyHostRejected(unittest.TestCase):
    """net-027：`:5700` 之类的空主机必须在解析期拒绝，而不是留到 connect。"""

    def test_empty_host_rejected(self):
        """改回缺陷形态（删 `if not host: raise`）实得 ('', 5700) → 本用例红。"""
        with self.assertRaises(ValueError):
            parse_addr(":5700")
        # 单冒号（既无主机也无端口）同样拒绝
        with self.assertRaises(ValueError):
            parse_addr(":")

    def test_ipv4_and_hostname_forms_unchanged(self):
        """IPv4/域名的既有口径不受 IPv6 改造影响（回归基线）。"""
        self.assertEqual(parse_addr("127.0.0.1:5700"), ("127.0.0.1", P))
        self.assertEqual(parse_addr("example.com:8000"), ("example.com", 8000))
        self.assertEqual(parse_addr("example.com"), ("example.com", P))
        for bad in ("", "   ", "a:", "a:x", "a:0", "a:70000"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_addr(bad)


class TestRealFunctionIsUsed(unittest.TestCase):
    """负向对照：证明上面断言打的是真实实现，而不是测试自己复刻的副本。

    范式同 test_item97_stop_race.py 的"旧逻辑负向对照"——把两代缺陷形态各复刻成
    本地函数，确认它们对同一批输入给出**与真实实现可区分**的答案；两者一致即说明
    断言没有判定力（第 6 轮 T 系列判的就是这个）。
    """

    @staticmethod
    def _pre_net018(addr_str, default_port=P):
        """net-018 之前的形态：只要含冒号就 rsplit 取末段当端口。"""
        addr_str = addr_str.strip()
        if not addr_str:
            raise ValueError("地址不能为空")
        if ":" in addr_str:
            host, port_str = addr_str.rsplit(":", 1)
            return (host.strip(), int(port_str))   # ← net-027 也一并未修（空 host 不抛）
        return addr_str, default_port

    @staticmethod
    def _net028_shape(addr_str, default_port=P):
        """net-018 的过度修正形态（net-028 报的就是它）：多冒号一律整串作主机。"""
        addr_str = addr_str.strip()
        if not addr_str:
            raise ValueError("地址不能为空")
        if addr_str.count(":") >= 2:
            return addr_str, default_port          # ← 无条件下放，吞掉 `::1:5700`
        if ":" in addr_str:
            host, port_str = addr_str.rsplit(":", 1)
            return (host.strip(), int(port_str))
        return addr_str, default_port

    def test_pre_net018_shape_differs(self):
        """对照 1：裸 `::1` 在旧口径下得到 (':', 1)——net-018 断言的判定力来源。"""
        self.assertEqual(self._pre_net018("::1"), (":", 1))
        self.assertNotEqual(self._pre_net018("::1"), parse_addr("::1"),
                            "对照失效：net-018 的判定对本用例没有区分度")
        self.assertEqual(self._pre_net018(":5700"), ("", 5700),
                         "对照形态本身应返回空主机（否则 net-027 无红可证）")
        with self.assertRaises(ValueError):
            parse_addr(":5700")

    def test_net028_shape_differs(self):
        """对照 2：`::1:5700` 在"整串作主机"口径下端口被吞——net-028 断言的判定力来源。"""
        self.assertEqual(self._net028_shape("::1:5700"), ("::1:5700", P))
        self.assertNotEqual(self._net028_shape("::1:5700"), parse_addr("::1:5700"),
                            "对照失效：net-028 的判定对本用例没有区分度")
        # 而 net-018 那半边在两种口径下同值 → 单靠 `::1` 一条断言挡不住 net-028
        self.assertEqual(self._net028_shape("::1"), parse_addr("::1"))

    def test_source_uses_ipaddress_predicate(self):
        """结构侧证据：无括号分支确实经 `ipaddress` 判合法 IPv6，而非数字启发式。"""
        import inspect
        src = inspect.getsource(parse_addr)
        self.assertRegex(src, r"_is_ipv6_literal\(",
                         "无括号 IPv6 判定未使用 ipaddress 口径（数冒号启发式正是 net-028 的成因）")
        self.assertRegex(src, r"if not host:\s*\n\s*raise ValueError",
                         "缺少 net-027 的空主机拒绝")
        self.assertNotIn("socket.getaddrinfo", src,
                         "parse_addr 不应在解析期触网（getaddrinfo 会把非法串静默曲解）")
        # 惰性导入只发生在多冒号分支：判定助手必须真实存在且口径正确
        import common
        self.assertTrue(callable(getattr(common, "_is_ipv6_literal", None)))
        self.assertFalse(common._is_ipv6_literal("127.0.0.1"))
        self.assertFalse(common._is_ipv6_literal("fe80:"))   # 拆剩的前缀不合法
        self.assertTrue(common._is_ipv6_literal("::1"))
        self.assertTrue(common._is_ipv6_literal("2001:db8::1"))


if __name__ == "__main__":
    unittest.main()
