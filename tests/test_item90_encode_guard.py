# -*- coding: utf-8 -*-
"""第 90 条回归：_encode_loop 内每一处 encoder.encode(...) 调用都必须在 try 兜底之内。

旧路径：_encode_loop 的「无新帧 + 静止期强制关键帧」分支（`if runtime.force_key:` 内
`encoder.encode(last_frame, ..., force_key=True)`）整体位于主编码 try **之外**。编码器半死
（GPU 重置 / PyAV 错误）时该调用抛异常，沿编码线程传播 → 线程死亡 → slot["video"] 冻结、
发送线程空转、观看端永久定格，GUI 模式下无任何日志。

修复：把该分支的 encode + 槽写入也裹进 try/except（共用主路径的 10 秒告警冷却窗），异常被
记录、本帧跳过、线程继续。

本测试是对**真实 host.py 源码**的结构断言（非复刻）：用 ast 解析 host.py，定位 _encode_loop，
找出其中所有 `.encode(...)` 调用，逐一断言其被某个 `ast.Try` 的 **body**（而非 handlers）包裹。
若有人日后把关键帧分支移回 try 之外，本测试即失败。
"""
import ast
import os
import unittest

_HOST_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "host.py")
# 第 110 条（阶段 2）：编码器生命周期与 JPEG 回退抽到 pipeline.FrameEncoder，host 与
# share 共用；encode() 调用因此从 host._encode_loop 迁到这里，守卫随之下沉到该模块。
_PIPELINE_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline.py")


def _find_func(tree, name):
    """返回 AST 中第一个名为 name 的 FunctionDef（含嵌套），找不到返回 None。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _parent_map(tree):
    """构建 child→parent 映射（ast 默认无 parent 指针）。"""
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _encode_calls(func):
    """产出 func 内所有形如 `xxx.encode(...)` 的 Call 节点（Attribute.attr == "encode"）。"""
    for node in ast.walk(func):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "encode"):
            yield node


def _guarded_by_try_body(call, parents, func):
    """call 是否被某个 ast.Try 的 body（受保护块）包裹（在 func 范围内向上回溯）。

    逐级上溯：当某级 parent 是 Try 且当前 node 恰在 parent.body 里 → 受保护。
    若 call 落在 except handler 内，则该 Try 不保护它（node 会先经过 handler 而非 body），
    继续向上找外层 try。回溯到 func 仍未命中 → 未受保护。
    """
    node = call
    while node is not func:
        parent = parents.get(node)
        if parent is None:
            return False
        if isinstance(parent, ast.Try) and node in parent.body:
            return True
        node = parent
    return False


class TestEncodeLoopGuard(unittest.TestCase):
    # 第 109/110 条（阶段 2）重构后，编码调用分布在：
    #   pipeline.FrameEncoder.encode     —— 主编码路径（host 与 share 共用）
    #   pipeline.FrameEncoder.force_idr  —— 静止期强制 IDR
    # 两处的 encode() 都必须被 try 兜底，故一并检查。
    _TARGETS = ((_PIPELINE_PY, "encode"), (_PIPELINE_PY, "force_idr"))

    def setUp(self):
        self.parsed = {}
        for path, _ in self._TARGETS:
            if path in self.parsed:
                continue
            with open(path, "r", encoding="utf-8") as f:
                self.parsed[path] = ast.parse(f.read(), filename=path)
        self.parents = {p: _parent_map(t) for p, t in self.parsed.items()}
        self.funcs = []
        for path, name in self._TARGETS:
            func = _find_func(self.parsed[path], name)
            self.assertIsNotNone(
                func, "%s 中找不到 %s（结构已变？请更新本测试）" % (path, name))
            self.funcs.append((path, func))

    def _all_encode_calls(self):
        for path, func in self.funcs:
            for call in _encode_calls(func):
                yield path, func, call

    def test_encode_loop_has_encode_calls(self):
        # 健全性：确实找到了 encode 调用，避免因 AST 结构变动导致下面的断言空过。
        calls = list(self._all_encode_calls())
        self.assertGreaterEqual(
            len(calls), 2,
            "FrameEncoder.encode/force_idr 内应至少有 2 处 encoder.encode"
            "（主路径 + 静止期关键帧分支）；实际 %d 处——源码结构可能已变，"
            "请核对并更新本测试" % len(calls))

    def test_every_encode_call_is_inside_try_body(self):
        # 核心断言：每一处 .encode(...) 调用都必须在某个 try 的 body 内（第 90 条）。
        unguarded = []
        for path, func, call in self._all_encode_calls():
            if not _guarded_by_try_body(call, self.parents[path], func):
                unguarded.append("%s:%s" % (os.path.basename(path),
                                            getattr(call, "lineno", "?")))
        self.assertEqual(
            unguarded, [],
            "存在未被 try 兜底的 encoder.encode 调用（%s）——"
            "编码器半死时会杀死编码线程导致观看端永久定格（第 90 条）。" % unguarded)


if __name__ == "__main__":
    unittest.main()
