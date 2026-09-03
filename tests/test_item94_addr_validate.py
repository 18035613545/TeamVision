# -*- coding: utf-8 -*-
"""第 94 条回归：新增频道必须校验地址、并按规范形去重。

旧路径：add_channel 从不调 parse_addr，去重只比精确字符串 `ch.addr == addr`：
(a) 垃圾地址（端口非数字/越界/缺端口）通过非空检查即落盘 → 接收线程无限退避重试，
    面板只显"断开"不给原因；
(b) "localhost:5700" 与 "127.0.0.1:5700" 字符串不同 → 可同时存在 → 开启准入时同一用户
    在第二个频道必收"用户名已存在" → 触发第 28 条清空并持久化凭据 → 每 30 秒弹登录框死循环。

修复：
1) 新增模块级 _canonical_addr：复用 parse_addr 校验端口（非法即抛 ValueError），并折叠为
   (host_lower, port) 规范形，回环别名 localhost/127.0.0.1/[::1] 统一为 127.0.0.1；
2) add_channel 先 _canonical_addr(新地址) 校验（ValueError 由 _panel_add 红字/_wizard_apply
   跳过显示原因），再按规范形去重；既有频道历史非法地址用内层 try 容错跳过，不阻断新增。

hermetic：_canonical_addr 直接单元测；add_channel 用 ViewerApp.__new__ + 替身 Channel +
mock _save_channels（不落盘、不建真实接收线程）。
"""
import threading
import unittest
from unittest import mock

import viewer
from viewer import _canonical_addr


class _FakeChannel:
    def __init__(self, name, addr, owner=None):
        self.name = name
        self.addr = addr


def _add_app():
    a = viewer.ViewerApp.__new__(viewer.ViewerApp)
    a.channels = []
    a._channels_lock = threading.RLock()
    a.cfg = {"viewer": {"channels": []}}
    a._save_channels = lambda: None       # 不落盘
    return a


class TestCanonicalAddr(unittest.TestCase):
    def test_folds_loopback_aliases(self):
        # 报告核心场景：localhost 与 127.0.0.1（含缺省端口/大小写）折叠为同一规范形。
        self.assertEqual(_canonical_addr("localhost:5700"), ("127.0.0.1", 5700))
        self.assertEqual(_canonical_addr("127.0.0.1:5700"), ("127.0.0.1", 5700))
        self.assertEqual(_canonical_addr("LOCALHOST"), ("127.0.0.1", 5700))   # 缺省端口 + 大小写
        self.assertEqual(_canonical_addr("127.0.0.1"), ("127.0.0.1", 5700))
        self.assertEqual(_canonical_addr("[::1]:5700"), ("127.0.0.1", 5700))

    def test_lowercases_host_and_keeps_port(self):
        self.assertEqual(_canonical_addr("Example.COM:1234"), ("example.com", 1234))

    def test_rejects_malformed(self):
        for bad in ("", "   ", "host:abc", "host:99999", "host:0", "host:"):
            with self.assertRaises(ValueError, msg="应拒绝 %r" % bad):
                _canonical_addr(bad)


class TestAddChannelValidation(unittest.TestCase):
    def test_rejects_malformed_and_persists_nothing(self):
        a = _add_app()
        with mock.patch.object(viewer, "Channel", _FakeChannel):
            for bad in ("host:abc", "host:99999", "host:", "host:0"):
                with self.assertRaises(ValueError):
                    a.add_channel("n", bad)
        self.assertEqual(a.channels, [])           # 垃圾地址未落盘（旧代码会落盘）

    def test_accepts_valid_and_stores_as_entered(self):
        a = _add_app()
        with mock.patch.object(viewer, "Channel", _FakeChannel):
            idx = a.add_channel("n", "Example.COM:1234")
        self.assertEqual(idx, 0)
        self.assertEqual(a.channels[0].addr, "Example.COM:1234")   # 原样存储，去重才用规范形

    def test_dedup_folds_loopback_case_and_default_port(self):
        # 旧精确字符串去重会放行这些；规范形去重必须判为重复 → 杜绝重复登录死循环。
        a = _add_app()
        with mock.patch.object(viewer, "Channel", _FakeChannel):
            a.add_channel("n1", "localhost:5700")
            for dup in ("127.0.0.1:5700", "LOCALHOST:5700", "localhost", "127.0.0.1"):
                with self.assertRaises(ValueError, msg="%r 应判为重复" % dup):
                    a.add_channel("nX", dup)
        self.assertEqual(len(a.channels), 1)       # 只留下第一个

    def test_distinct_hosts_and_ports_still_allowed(self):
        # 反向：不同主机/端口不应被误判为重复（规范化不能过度合并）。
        a = _add_app()
        with mock.patch.object(viewer, "Channel", _FakeChannel):
            a.add_channel("n1", "127.0.0.1:5700")
            a.add_channel("n2", "127.0.0.1:5701")     # 端口不同
            a.add_channel("n3", "example.com:5700")    # 主机不同
        self.assertEqual(len(a.channels), 3)

    def test_existing_invalid_addr_does_not_block_new_add(self):
        # 历史遗留：已落盘的非法地址不应让 _canonical_addr 抛错阻断新增（内层 try 容错）。
        a = _add_app()
        a.channels = [_FakeChannel("old", "garbage:notaport")]
        with mock.patch.object(viewer, "Channel", _FakeChannel):
            idx = a.add_channel("new", "127.0.0.1:5700")   # 不应抛
        self.assertEqual(idx, 1)
        self.assertEqual(len(a.channels), 2)


if __name__ == "__main__":
    unittest.main()
