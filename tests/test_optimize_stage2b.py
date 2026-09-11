# -*- coding: utf-8 -*-
"""阶段 2 续（第 113–116 条）：C2 Event 唤醒、C3 零拷贝打包、C4 面板增量刷新、C6 解码器复用。"""
import struct
import threading
import time
import unittest
from unittest import mock

import codec as codec_mod
import common
import host


class TestC2EventWake(unittest.TestCase):
    """编码/发送线程用 Event 等待新帧，替代 1ms/0.5ms sleep 轮询。"""

    def test_runtime_has_pipeline_events(self):
        rt = host.HostRuntime({"host": {}})
        self.assertIsInstance(rt.raw_evt, threading.Event)
        self.assertIsInstance(rt.enc_evt, threading.Event)

    def test_store_encoded_sets_enc_event(self):
        rt = host.HostRuntime({"host": {}})

        class _EF:
            is_video = True
            data = b"nal"
            ts = 1
            is_key = True
            codec_id = common.CODEC_H264
            bitrate = 1000
            ms = 1.0
            w = 64
            h = 48

        host._store_encoded(rt, _EF(), 1)
        self.assertTrue(rt.enc_evt.is_set())

    def test_capture_wake_raw_via_helper(self):
        rt = host.HostRuntime({"host": {}})
        host._wake_raw(rt)
        self.assertTrue(rt.raw_evt.is_set())

    def test_force_key_request_wakes_encode(self):
        # 路由丢帧置 force_key 后必须唤醒编码线程（静止期 otherwise 空等）
        rt = host.HostRuntime({"host": {}})
        rt.clients = {}
        rt.clients_lock = threading.Lock()

        class _Info:
            watch_source = "local"
            need_key = False  # 非 need_key 门控中，帧才能到达 enqueue
            _sender_active = True

            def enqueue_frame(self, frame):
                return True

            def record_sent(self, n):
                pass

            _write_lock = threading.Lock()

        sock = object()
        rt.clients[sock] = _Info()
        rt.raw_evt.clear()
        host.route_frame(rt, "local", b"x" * 8, is_key=False, is_jpeg=False)
        self.assertTrue(rt.force_key)
        self.assertTrue(rt.raw_evt.is_set())

    def test_encode_loop_idle_waits_on_raw_event(self):
        """无新帧时应 wait(raw_evt) 而非 sleep(0.001)。"""
        rt = host.HostRuntime({"host": {}})
        rt.raw_evt.clear()
        waited = []

        class _Evt:
            def wait(self, timeout=None):
                waited.append(timeout)
                return False

            def clear(self):
                pass

            def is_set(self):
                return False

        rt.raw_evt = _Evt()
        rt.stop_event = threading.Event()
        p = mock.Mock()
        p.base_fps = 30
        p.keyint = 30
        p.encoder_sel = "jpeg"
        p.codec_preset = "default"
        p.target_width = 0
        p.base_quality = 80
        rt.slot["raw"] = None

        with mock.patch.object(host.pipeline, "FrameEncoder") as Fe:
            fe = Fe.return_value
            fe.encode.return_value = None
            fe.close = mock.Mock()
            # 第一圈：raw 为 None → 进入 idle wait；随后 stop 退出
            def _stop_after_first_wait(timeout=None):
                waited.append(timeout)
                rt.stop_event.set()
                return False

            rt.raw_evt.wait = _stop_after_first_wait
            host._encode_loop(rt, p)
        self.assertTrue(waited)
        self.assertLessEqual(waited[0], 0.1)  # 有界等待，不是长 sleep


class TestC3PackStructs(unittest.TestCase):
    """pack_frame/pack_video/pack_msg 字节序列与旧实现完全一致。"""

    def test_pack_frame_layout(self):
        jpeg = b"\xff\xd8JPEG\xff\xd9"
        raw = common.pack_frame(jpeg, ts=1234567890)
        self.assertEqual(raw[0], common.MSG_FRAME)
        (length,) = struct.unpack(">I", raw[1:5])
        self.assertEqual(length, 8 + len(jpeg))
        (ts,) = struct.unpack(">Q", raw[5:13])
        self.assertEqual(ts, 1234567890)
        self.assertEqual(raw[13:], jpeg)

    def test_pack_video_layout(self):
        raw = common.pack_video(b"\x00\x00\x00\x01KEY", ts=42, keyframe=True,
                                codec=common.CODEC_HEVC)
        self.assertEqual(raw[0], common.MSG_VIDEO)
        (length,) = struct.unpack(">I", raw[1:5])
        self.assertEqual(length, 10 + 7)
        (ts,) = struct.unpack(">Q", raw[5:13])
        self.assertEqual(ts, 42)
        self.assertEqual(raw[13], common.CODEC_HEVC)
        self.assertEqual(raw[14], common.VIDEO_FLAG_KEY)
        self.assertEqual(raw[15:], b"\x00\x00\x00\x01KEY")

    def test_pack_msg_layout(self):
        raw = common.pack_msg({"action": "ping", "t": 1})
        self.assertEqual(raw[0], common.MSG_CTRL)
        (length,) = struct.unpack(">I", raw[1:5])
        self.assertEqual(length, len(raw) - 5)
        self.assertIn(b'"ping"', raw[5:])

    def test_repack_roundtrip(self):
        vid = common.pack_video(b"NAL", ts=7, keyframe=False)
        consumed, kind, payload = common.parse_message(bytearray(vid))
        self.assertEqual(kind, common.MSG_VIDEO)
        ts, codec, flags, nal = common.parse_video(payload)
        repacked = common.pack_video(nal, ts=ts, keyframe=bool(flags & 1), codec=codec)
        self.assertEqual(repacked, vid)

    def test_fwd_member_frame_uses_msg_hdr(self):
        payload = b"\x00" * 8 + b"\xff\xd8xx"
        captured = []

        def _route(runtime, source, frame, **kw):
            captured.append((source, frame, kw))

        class _Info:
            share_id = "peer:1"

        with mock.patch.object(host, "route_frame", _route):
            host._fwd_member_frame(object(), _Info(), common.MSG_FRAME, payload, is_jpeg=True)
        self.assertEqual(len(captured), 1)
        frame = captured[0][1]
        self.assertEqual(frame[0], common.MSG_FRAME)
        self.assertEqual(frame[5:], payload)
        (length,) = struct.unpack(">I", frame[1:5])
        self.assertEqual(length, len(payload))


class _FakeTree:
    """极简 Treeview 替身：记录 delete/insert/item 调用。"""

    def __init__(self, n=0):
        self.rows = {str(i): {"values": (), "tags": ()} for i in range(n)}
        self.deleted = 0
        self.inserted = 0
        self.item_sets = 0

    def get_children(self):
        return tuple(self.rows.keys())

    def delete(self, *iids):
        self.deleted += 1
        for iid in iids:
            self.rows.pop(iid, None)
        if not iids:
            self.rows.clear()

    def insert(self, parent, index, iid=None, values=(), tags=()):
        self.inserted += 1
        self.rows[iid] = {"values": tuple(values), "tags": tuple(tags)}

    def item(self, iid, option=None, **kw):
        if kw:
            self.item_sets += 1
            self.rows.setdefault(iid, {"values": (), "tags": ()})
            if "values" in kw:
                self.rows[iid]["values"] = tuple(kw["values"])
            if "tags" in kw:
                self.rows[iid]["tags"] = tuple(kw["tags"])
            return
        if option == "values":
            return self.rows[iid]["values"]
        if option == "tags":
            return self.rows[iid]["tags"]
        return self.rows[iid]

    def selection_set(self, *a):
        pass

    def focus(self, *a):
        pass

    def yview(self, *a):
        return (0.0, 1.0)


class TestC4PanelIncremental(unittest.TestCase):
    """行数不变时只更新变化行，不再每 500ms 全删全建。"""

    def _app(self, tree):
        import viewer
        app = viewer.ViewerApp.__new__(viewer.ViewerApp)
        app.running = True
        app.panel = object()
        app._tree = tree
        app._panel_selected = 0
        app._panel_known_count = 1
        app._panel_suppress_select = False
        app._panel_visible = lambda: True
        app._sync_share_button = lambda: None
        app._panel_refresh_sources = lambda: None
        app.get_channels_snapshot = lambda: [{
            "name": "A", "addr": "127.0.0.1:5700", "status": "receiving",
            "fps": 30.0, "latency": 1.0, "video": True, "codec": common.CODEC_H264,
        }]
        app.active_idx = 0
        return app

    def test_unchanged_snapshot_does_not_rebuild(self):
        import viewer
        tree = _FakeTree(n=1)
        tree.rows["0"] = {
            "values": ("●", "H264·A", "127.0.0.1:5700", "30", "1"),
            "tags": ("row_active", "st_receiving"),
        }
        app = self._app(tree)
        deleted_before, inserted_before = tree.deleted, tree.inserted
        app._panel_refresh_list()
        self.assertEqual(tree.deleted, deleted_before)
        self.assertEqual(tree.inserted, inserted_before)
        self.assertEqual(tree.item_sets, 0)

    def test_changed_fps_updates_only_values(self):
        tree = _FakeTree(n=1)
        tree.rows["0"] = {
            "values": ("●", "H264·A", "127.0.0.1:5700", "30", "1"),
            "tags": ("row_active", "st_receiving"),
        }
        app = self._app(tree)
        app.get_channels_snapshot = lambda: [{
            "name": "A", "addr": "127.0.0.1:5700", "status": "receiving",
            "fps": 29.0, "latency": 1.0, "video": True, "codec": common.CODEC_H264,
        }]
        deleted_before, inserted_before = tree.deleted, tree.inserted
        app._panel_refresh_list()
        self.assertEqual(tree.deleted, deleted_before)
        self.assertEqual(tree.inserted, inserted_before)
        self.assertEqual(tree.item_sets, 1)
        self.assertEqual(tree.rows["0"]["values"][3], "29")

    def test_count_change_rebuilds(self):
        tree = _FakeTree(n=1)
        app = self._app(tree)
        app.get_channels_snapshot = lambda: []  # 行数 1→0
        app._panel_refresh_list()
        self.assertGreater(tree.deleted, 0)


class TestC6DecoderReuse(unittest.TestCase):
    """重连/切源复用解码上下文，不再每次 new VideoDecoder。"""

    def test_reset_reuses_context_when_flush_ok(self):
        d = codec_mod.VideoDecoder.__new__(codec_mod.VideoDecoder)
        d._codec = common.CODEC_H264
        d.decoded_frames = 0
        ctx = mock.Mock()
        d._ctx = ctx
        d.reset()
        ctx.flush_buffers.assert_called_once()
        ctx.close.assert_not_called()
        self.assertIs(d._ctx, ctx)

    def test_reset_reinits_when_flush_fails(self):
        d = codec_mod.VideoDecoder.__new__(codec_mod.VideoDecoder)
        d._codec = common.CODEC_H264
        d.decoded_frames = 0
        ctx = mock.Mock()
        ctx.flush_buffers.side_effect = RuntimeError("dead")
        d._ctx = ctx
        with mock.patch.object(d, "_init_ctx", lambda: setattr(d, "_ctx", "NEW")):
            d.reset()
        ctx.close.assert_called_once()
        self.assertEqual(d._ctx, "NEW")

    def test_set_codec_closes_old_context(self):
        d = codec_mod.VideoDecoder.__new__(codec_mod.VideoDecoder)
        d._codec = common.CODEC_H264
        old = mock.Mock()
        d._ctx = old
        with mock.patch.object(d, "_init_ctx", lambda: setattr(d, "_ctx", "HEVC")):
            d.set_codec(common.CODEC_HEVC)
        old.close.assert_called_once()
        self.assertEqual(d._codec, common.CODEC_HEVC)
        self.assertEqual(d._ctx, "HEVC")

    def test_set_codec_same_codec_is_noop(self):
        d = codec_mod.VideoDecoder.__new__(codec_mod.VideoDecoder)
        d._codec = common.CODEC_H264
        ctx = mock.Mock()
        d._ctx = ctx
        d.set_codec(common.CODEC_H264)
        ctx.close.assert_not_called()
        self.assertIs(d._ctx, ctx)

    def test_close_idempotent(self):
        d = codec_mod.VideoDecoder.__new__(codec_mod.VideoDecoder)
        ctx = mock.Mock()
        d._ctx = ctx
        d.close()
        d.close()
        ctx.close.assert_called_once()
        self.assertIsNone(d._ctx)


if __name__ == "__main__":
    unittest.main()
