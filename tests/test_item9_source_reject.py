# -*- coding: utf-8 -*-
"""第 9 条回归：切源发包失败 / host 回绝时，watch_source 不得谎报未生效的新源。

缺陷：switch_source 先提交本地状态（watch_source=新源、重建解码器、清 last_frame）再
发包，发包失败（sock 为 None / OSError）只 return False、**不回滚** → 状态栏读
watch_source 写「源:队友·X」，而 host 从未收到请求、仍在转发本地画面。host 侧遇无效源
/订阅自身只写 debug 日志、**无拒绝回包**，观看端永不自纠（自身订阅时离线回落也不触发）。

修复：(viewer) switch_source 失败路径调 _revert_source 回滚到原源（保留 last_frame 不
黑屏、不递增 frame_serial，但重置 _got_key 并重建解码器以门控到原源下一关键帧）；
_handle_ctrl 新增 watch_reject 分支，被拒源仍等于当前源时回落 local（host 永不拒 local）。
(host) 两个拒绝分支回 watch_reject——见 test_multiview_integration.py 的真实 socket 用例。

hermetic：__new__ 构造 Channel，patch viewer.send_msg 与 codec 模块的 VideoDecoder，
stub _notify_share_error，绕开真实 socket/解码器/Tk。
"""
import json
import threading
import unittest
from unittest import mock

import viewer


class _Recorder:
    """记录 send_msg 调用与 VideoDecoder 构造，可注入发送异常。"""

    def __init__(self, send_exc=None):
        self.sent = []           # [(sock, obj)]
        self.decoders = []       # 每次 VideoDecoder(codec=...) 的 codec
        self.send_exc = send_exc

    def send_msg(self, sock, obj):
        if self.send_exc is not None:
            raise self.send_exc
        self.sent.append((sock, obj))

    def VideoDecoder(self, codec=None):
        self.decoders.append(codec)
        return "DECODER(%s)" % codec


def _channel(rec, peers, watch_source="local", sock=object()):
    ch = viewer.Channel.__new__(viewer.Channel)
    ch.name = "test"
    ch.lock = threading.Lock()
    ch._tx_lock = threading.Lock()
    ch.peers = peers
    ch.watch_source = watch_source
    ch._got_key = True
    ch.video_mode = True
    ch.cur_codec = viewer.CODEC_HEVC   # 故意非 H264：验证提交/回滚都重置为 H264
    ch._decoder = "OLD_DECODER"
    ch.last_frame = "LASTFRAME"
    ch.frame_serial = 5
    ch._active_sock = sock
    ch._test_notify = []
    ch._notify_share_error = lambda text: ch._test_notify.append(text)
    return ch


def _patched(rec):
    """同时 patch send_msg 与 codec_mod.VideoDecoder，返回两个 context manager。"""
    return (mock.patch.object(viewer, "send_msg", rec.send_msg),
            mock.patch.object(viewer.codec_mod, "VideoDecoder", rec.VideoDecoder))


class TestSwitchSourceRollbackOnFailure(unittest.TestCase):

    def test_send_oserror_rolls_back_to_prev_source(self):
        rec = _Recorder(send_exc=OSError("boom"))
        ch = _channel(rec, peers=[{"id": "peer:1"}], watch_source="local")
        p1, p2 = _patched(rec)
        with p1, p2:
            ok = ch.switch_source("peer:1")
        self.assertFalse(ok)
        self.assertEqual(ch.watch_source, "local")   # 回滚，不停在未生效的 peer:1

    def test_sock_none_rolls_back_to_prev_source(self):
        rec = _Recorder()
        ch = _channel(rec, peers=[{"id": "peer:1"}], watch_source="local", sock=None)
        p1, p2 = _patched(rec)
        with p1, p2:
            ok = ch.switch_source("peer:1")
        self.assertFalse(ok)
        self.assertEqual(ch.watch_source, "local")
        self.assertEqual(rec.sent, [])               # 从未发包

    def test_rollback_restores_label_and_leaves_honest_blank(self):
        # 回滚只恢复 watch_source（状态栏读它）；提交时的清帧/递增序号保持不变——
        # 发包失败=连接已断、无流到达，空帧才是诚实状态（保留陈旧帧反而误导）。
        rec = _Recorder(send_exc=OSError("boom"))
        ch = _channel(rec, peers=[{"id": "peer:1"}], watch_source="local")
        p1, p2 = _patched(rec)
        with p1, p2:
            ch.switch_source("peer:1")
        self.assertEqual(ch.watch_source, "local")    # 标签恢复真实
        self.assertIsNone(ch.last_frame)              # 空帧（连接已断，诚实）
        self.assertEqual(ch.frame_serial, 6)          # 提交时已递增，不回退

    def test_rollback_resets_decode_gating(self):
        # 解码器已在提交时重建一次并门控；回滚不再二次重建，_decode_video_batch
        # 会在原源下一帧到达时按 codec 自行纠正。
        rec = _Recorder(send_exc=OSError("boom"))
        ch = _channel(rec, peers=[{"id": "peer:1"}], watch_source="local")
        p1, p2 = _patched(rec)
        with p1, p2:
            ch.switch_source("peer:1")
        self.assertFalse(ch._got_key)
        self.assertFalse(ch.video_mode)
        self.assertEqual(ch.cur_codec, viewer.CODEC_H264)
        self.assertEqual(rec.decoders, [viewer.CODEC_H264])  # 仅提交时重建一次


class TestSwitchSourceSuccessCommits(unittest.TestCase):

    def test_success_commits_and_sends_watch(self):
        rec = _Recorder()
        ch = _channel(rec, peers=[{"id": "peer:1"}], watch_source="local")
        p1, p2 = _patched(rec)
        with p1, p2:
            ok = ch.switch_source("peer:1")
        self.assertTrue(ok)
        self.assertEqual(ch.watch_source, "peer:1")   # 阳性对照：成功不回滚
        self.assertIsNone(ch.last_frame)              # 第 8 条：清显示帧
        self.assertEqual(ch.frame_serial, 6)          # 第 8 条：递增强制重绘
        self.assertEqual(rec.sent[-1][1], {"action": "watch", "source": "peer:1"})

    def test_local_success_also_requests_keyframe(self):
        rec = _Recorder()
        ch = _channel(rec, peers=[{"id": "peer:1"}], watch_source="peer:1")
        p1, p2 = _patched(rec)
        with p1, p2:
            ok = ch.switch_source("local")
        self.assertTrue(ok)
        actions = [obj.get("action") for _s, obj in rec.sent]
        self.assertEqual(actions, ["watch", "req_keyframe"])

    def test_invalid_source_rejected_before_any_commit(self):
        # 既有前置守卫：源不在 peers 列表 → 不提交、不发包、不重建解码器
        rec = _Recorder()
        ch = _channel(rec, peers=[], watch_source="local")
        p1, p2 = _patched(rec)
        with p1, p2:
            ok = ch.switch_source("peer:1")
        self.assertFalse(ok)
        self.assertEqual(ch.watch_source, "local")
        self.assertEqual(rec.sent, [])
        self.assertEqual(rec.decoders, [])            # 完全未触碰解码器
        self.assertEqual(ch.last_frame, "LASTFRAME")
        self.assertEqual(ch.frame_serial, 5)


class TestWatchRejectHandling(unittest.TestCase):

    def _ctrl(self, ch, obj):
        ch._handle_ctrl(json.dumps(obj).encode("utf-8"))

    def test_reject_current_source_falls_back_to_local(self):
        rec = _Recorder()
        ch = _channel(rec, peers=[{"id": "peer:1"}], watch_source="peer:1")
        p1, p2 = _patched(rec)
        with p1, p2:
            self._ctrl(ch, {"action": "watch_reject", "source": "peer:1",
                            "reason": "invalid"})
        self.assertEqual(ch.watch_source, "local")    # 真实 switch_source 回落
        self.assertTrue(any(obj.get("action") == "watch" and obj.get("source") == "local"
                            for _s, obj in rec.sent))
        self.assertEqual(len(ch._test_notify), 1)     # 给用户一次提示

    def test_reject_self_source_falls_back_to_local(self):
        rec = _Recorder()
        ch = _channel(rec, peers=[{"id": "peer:1"}], watch_source="peer:1")
        p1, p2 = _patched(rec)
        with p1, p2:
            self._ctrl(ch, {"action": "watch_reject", "source": "peer:1",
                            "reason": "self"})
        self.assertEqual(ch.watch_source, "local")

    def test_reject_ignored_when_source_already_moved_on(self):
        # 更晚发起的切源（peer:2）不得被 peer:1 的迟到回绝清掉
        rec = _Recorder()
        ch = _channel(rec, peers=[{"id": "peer:1"}, {"id": "peer:2"}],
                      watch_source="peer:2")
        p1, p2 = _patched(rec)
        with p1, p2:
            self._ctrl(ch, {"action": "watch_reject", "source": "peer:1",
                            "reason": "invalid"})
        self.assertEqual(ch.watch_source, "peer:2")   # 保持不动
        self.assertEqual(rec.sent, [])                # 未触发回落发包
        self.assertEqual(ch._test_notify, [])

    def test_reject_for_local_is_noop(self):
        # 防御性：即便收到 source=local 的回绝（host 实际永不拒 local），也不应动作
        rec = _Recorder()
        ch = _channel(rec, peers=[], watch_source="local")
        p1, p2 = _patched(rec)
        with p1, p2:
            self._ctrl(ch, {"action": "watch_reject", "source": "local",
                            "reason": "invalid"})
        self.assertEqual(ch.watch_source, "local")
        self.assertEqual(rec.sent, [])
        self.assertEqual(ch._test_notify, [])


if __name__ == "__main__":
    unittest.main()
