# -*- coding: utf-8 -*-
"""cfg-004 回归：跨进程配置锁必须由**两个真实进程**竞争同一个 config.json。

第 6 轮审查第十章 T-11 的最后一句（`docs/BUG审查报告-2026-09-20.md`）：
  "cfg-004 的回归测试必须起两个真实进程竞争同一 `config.json`，
   用 mock 替换 `_config_file_lock` 会再次假绿。"
本文件因此不 patch 任何锁函数：子进程用**同一个解释器**（sys.executable）起，
在 `%TEMP%` 下造一份独立数据目录（`common._DATA_DIR` 指向它，config.json 与日志
都落在那里，绝不写仓库工作树），真跑 `common.acquire_file_lock` /
`common.release_file_lock` / `common.save_config`。

钉住的缺陷形态（cfg-004/005，`common.py:1321-1373` + `common.py:1401-1448`）：
  ① 旧 `acquire_file_lock` 用 `O_CREAT|O_EXCL` 排他创建 + "锁文件年龄 > 10 秒"判定
     陈旧再 `os.remove` 接管；而 Windows 上 `os.remove` 作用于**仍被他人以句柄打开**
     的文件抛 `PermissionError [WinError 32]`，旧代码 `except OSError: pass` 吞掉
     → 活着的竞争者根本无法被接管；
  ② 接管阈值 10 秒 > 等待上限 2 秒 → 持有者崩溃后的整个 10 秒窗口里，每个写入者
     都等满 2 秒然后**无保护地写**（正是 cfg-001 要根治的 host/viewer 互相陈旧覆盖）；
  ③ 拿不到锁时旧 `save_config` **照常完成整份写盘**（fail-open）。
现在：改用 `msvcrt.locking` 字节范围锁（进程退出/被强杀 → 内核关句柄 → 锁即释放，
不需要陈旧判定），且拿不到锁就放弃本次写并告警（fail-closed）。

改回缺陷形态会红在哪一行（逐行论证 + 负向对照同文件实测）：
  · 回退 ③（`if fd is None:` 改成继续写）→ `test_blocked_writer_does_not_touch_file`
    的子进程回报"写了"，且 config.json 内容被覆盖 → assertEqual(内容, 原始种子) 红。
  · 回退 ① ②（换回 O_EXCL + 年龄判定）→
    `test_kill_of_holder_releases_lock_without_stale_window`：持有者被 TerminateProcess
    后锁文件仍在（年龄 < 10 秒），第二个进程在 1.5 秒内拿不到 → ACQ=False 红；
    `TestOldLockShape` 的同形态复刻用例则实证"竞争者被活锁挡住后仍会写盘"。
  · 锁语义若被改成进程内 threading 锁（跨进程形同虚设）→
    `test_holder_process_blocks_second_writer_fail_closed` 红（B 会立刻写成功）。

注：`save_config` 成功路径目前**没有** `return True`（返回 None），失败返回 False；
所以本文件对"是否放弃写盘"的判定一律以**磁盘内容**为准，不对返回值做正向断言
（见本轮报告：这是 cfg-004 落地时留下的三态口径，调用方一旦 `if not save_config(...)`
就会把成功当失败）。
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CHILD = r'''
# -*- coding: utf-8 -*-
"""cfg-004 回归的子进程：在指定临时数据目录里真跑 common 的锁与 save_config。"""
import json
import os
import sys
import time

repo, tmp, mode = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, repo)
import common
common._DATA_DIR = tmp                      # config.json 与日志都改落到临时目录
path = common._config_path()
arg = sys.argv[4] if len(sys.argv) > 4 else ""


def mark(name):
    with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
        f.write("1")


if mode == "hold":
    fd = common.acquire_file_lock(path, timeout=3.0)
    print("HELD=%s" % (fd is not None), flush=True)
    mark("READY")
    time.sleep(float(arg))                  # 一直持锁（模拟"另一个实例正在写/卡住"）
    common.release_file_lock(path, fd)
    print("RELEASED", flush=True)
elif mode == "acquire":
    t0 = time.time()
    fd = common.acquire_file_lock(path, timeout=float(arg))
    print("ACQ=%s COST=%.3f" % (fd is not None, time.time() - t0), flush=True)
    common.release_file_lock(path, fd)
elif mode == "save":
    section = arg
    payload = {section: {"marker": "child-%d" % os.getpid(), "seq": 1}}
    ok = common.save_config(payload, section)
    print("SAVE=%r" % (ok,), flush=True)
elif mode == "loop":
    section, seconds = arg.split(",")
    end = time.time() + float(seconds)
    n = wrote = 0
    while time.time() < end:
        n += 1
        payload = {section: {"marker": "child-%d" % os.getpid(), "seq": n}}
        if common.save_config(payload, section):
            wrote += 1
        time.sleep(0.005)
    print("LOOPS=%d WROTE=%d" % (n, wrote), flush=True)
else:
    raise SystemExit(3)
'''


def _wait_until(fn, timeout=6.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False


class _Harness(unittest.TestCase):
    """公共脚手架：临时数据目录 + 子进程启动器。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tv-cfg004-")
        self.script = os.path.join(self.tmp, "child.py")
        with open(self.script, "w", encoding="utf-8") as f:
            f.write(_CHILD)
        self.cfg_path = os.path.join(self.tmp, "config.json")
        self.seed()

    def seed(self, host_marker="seed"):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"host": {"marker": host_marker, "seq": 0}}, f)

    def read_cfg(self):
        with open(self.cfg_path, encoding="utf-8") as f:
            return json.load(f)

    def spawn(self, mode, arg="", wait=True, timeout=20):
        proc = subprocess.Popen(
            [sys.executable, self.script, REPO, self.tmp, mode, arg],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace")
        if not wait:
            return proc
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            self.fail("子进程 %s 超时未退出；stderr=%s" % (mode, err[-400:]))
        return out or "", err or "", proc.returncode

    def wait_ready(self, proc, name="READY", timeout=15.0):
        ok = _wait_until(lambda: os.path.exists(os.path.join(self.tmp, name))
                         or proc.poll() is not None, timeout)
        self.assertTrue(ok, "子进程没有进入持锁状态（acquire_file_lock 起不来？）")

    def leftover_tmp_files(self):
        return [n for n in os.listdir(self.tmp) if n.endswith(".tmp")]


class TestRealProcessContention(_Harness):
    """两条真实进程竞争同一个 config.json。"""

    def test_holder_process_blocks_second_writer_fail_closed(self):
        """cfg-004 主断言：别人持锁时，本次写盘必须**放弃**（fail-closed）。"""
        holder = self.spawn("hold", "3", wait=False)
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        self.wait_ready(holder)

        out, err, rc = self.spawn("save", "viewer", timeout=15)
        self.assertEqual(rc, 0, "子进程异常退出：%s" % err[-400:])
        self.assertIn("SAVE=False", out,
                      "拿不到跨进程锁却没返回失败标记：%s" % out)
        cfg = self.read_cfg()
        self.assertNotIn("viewer", cfg,
                         "竞争期间仍然写盘（fail-open）：另一进程的陈旧整份配置会踩掉刚改的设置")
        self.assertEqual(cfg.get("host", {}).get("marker"), "seed",
                         "被挡住的那次写把整份文件换了内容")
        self.assertEqual(self.leftover_tmp_files(), [], "临时 .tmp 文件未清理（cfg-006）")

    def test_blocked_writer_can_proceed_after_holder_releases(self):
        """fail-closed 不等于"永久写不进"：锁释放后必须能正常落盘。"""
        holder = self.spawn("hold", "1.2", wait=False)
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        self.wait_ready(holder)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and holder.poll() is None:
            time.sleep(0.05)
        out, err, rc = self.spawn("save", "viewer", timeout=15)
        self.assertEqual(rc, 0, err[-400:])
        cfg = self.read_cfg()
        self.assertIn("viewer", cfg, "锁释放后仍写不进去（锁没被真正释放）")
        self.assertEqual(cfg["host"]["marker"], "seed",
                         "读-改-写丢了另一进程的节（cfg-001/第 17 条）")

    def test_kill_of_holder_releases_lock_without_stale_window(self):
        """cfg-005：持有者被强杀后锁必须**立刻**可接管（旧实现要等 10 秒年龄判定）。"""
        holder = self.spawn("hold", "120", wait=False)
        self.wait_ready(holder)
        time.sleep(0.2)
        holder.kill()                        # TerminateProcess：内核关句柄 → 字节锁自动释放
        holder.wait(10)
        out, err, rc = self.spawn("acquire", "1.5", timeout=20)
        self.assertIn("ACQ=True", out,
                      "持有者被强杀后 1.5 秒内拿不到锁：又在用「锁文件年龄」判定陈旧"
                      "（cfg-004/005 的接管阈值回退）%s %s" % (out, err[-300:]))
        self.assertTrue(os.path.exists(self.cfg_path + ".lock"),
                        "锁文件被删除：回到「创建/删除排他文件」的老竞争面（cfg-004 明令不删）")

    def test_two_writers_compete_and_both_sections_survive(self):
        """真并发：两个进程各自 section 反复读-改-写，最终文件必须双方俱在且可解析。"""
        a = self.spawn("loop", "host,1.5", wait=False)
        b = self.spawn("loop", "viewer,1.5", wait=False)
        self.addCleanup(lambda: a.poll() is None and a.kill())
        self.addCleanup(lambda: b.poll() is None and b.kill())
        for p in (a, b):
            try:
                p.wait(25)
            except subprocess.TimeoutExpired:
                p.kill()
                self.fail("并发写子进程未退出")
        out_a = a.stdout.read() if a.stdout else ""
        out_b = b.stdout.read() if b.stdout else ""
        cfg = self.read_cfg()                # 抛异常即"写坏了 config.json"
        self.assertIn("marker", cfg.get("host", {}),
                      "host 节被并发写丢：整份覆盖又回来了 %r / %r" % (out_a, out_b))
        self.assertIn("marker", cfg.get("viewer", {}),
                      "viewer 节被并发写丢（锁内重读没生效）%r / %r" % (out_a, out_b))
        self.assertEqual(self.leftover_tmp_files(), [], "残留 .tmp（cfg-006 的清理路径失效）")

    def test_lock_is_per_path_and_released_on_close(self):
        """同一进程内：acquire → release → 再 acquire 必须都能成功（幂等释放）。"""
        out, err, rc = self.spawn("acquire", "1.0", timeout=15)
        self.assertIn("ACQ=True", out)
        self.assertEqual(rc, 0)
        out2, _e2, _r2 = self.spawn("acquire", "1.0", timeout=15)
        self.assertIn("ACQ=True", out2, "上一次释放没生效（锁泄漏 → 之后所有写盘都被拒）")


class TestOldLockShape(_Harness):
    """负向对照：复刻 cfg-004 之前的"排他文件锁 + fail-open 写盘"，实证它会写。

    这里刻意**不**用 subprocess：对照的目标是"拿不到锁还继续写"这个形状，
    用同机两进程反而会让失败原因不纯。真实竞争的判定力由上面那组用例提供。
    """

    @staticmethod
    def _old_try_lock(path, timeout=0.05, stale_s=10.0):
        """旧 acquire：O_CREAT|O_EXCL；存在则看年龄判陈旧（>10 秒才敢 os.remove）。"""
        lock_path = path + ".lock"
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                return fd
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(lock_path)
                except OSError:
                    age = 0.0
                if age > stale_s:
                    try:
                        os.remove(lock_path)     # Windows 上被活句柄挡住 → PermissionError
                    except OSError:
                        pass
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.01)

    @classmethod
    def _old_save(cls, path, payload, section):
        """旧 save_config：拿不到锁也照样整份写盘（fail-open）。"""
        fd = cls._old_try_lock(path)
        try:
            base = {}
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        base = json.load(f)
                except Exception:
                    base = {}
            base[section] = payload.get(section)
            tmp = "%s.%d.old.tmp" % (path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(base, f)
            os.replace(tmp, path)
            return True
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.remove(path + ".lock")
            except OSError:
                pass

    def test_old_shape_writes_anyway_while_holder_is_alive(self):
        """旧形态在同一局面（他人持锁）下把盘写了；真实形态见上面的子进程用例。

        这里用**独立的一份文件**跑对照，免得把本文件其它用例的种子改掉。
        """
        path = os.path.join(self.tmp, "old_shape_config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"host": {"marker": "seed"}}, f)
        holder_fd = common.acquire_file_lock(path)
        self.assertIsNotNone(holder_fd, "对照失效：真实锁没拿到，谈不上「挡住」")
        try:
            self._old_save(path, {"viewer": {"marker": "old"}}, "viewer")
        finally:
            common.release_file_lock(path, holder_fd)
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertIn("viewer", cfg,
                      "对照失效：旧形态本应**无视活锁**继续写盘（cfg-004 的 fail-open）")
        # 真实实现对同一局面的结论是「放弃本次写盘」——由
        # test_holder_process_blocks_second_writer_fail_closed 用两个真进程证明。

    def test_old_takeover_ignores_a_live_holder(self):
        """旧判定的另一半：锁文件年龄 < 10 秒时，活着的竞争者根本不会被接管。"""
        path = os.path.join(self.tmp, "config.json")
        fd = common.acquire_file_lock(path)         # 真锁（字节范围）
        self.assertIsNotNone(fd)
        try:
            # 旧 try_lock 只会看锁文件年龄；真锁持有者对它毫无"续期"能力
            self.assertIsNone(self._old_try_lock(path),
                              "旧 try_lock 竟然接管了活持有者的锁文件")
        finally:
            common.release_file_lock(path, fd)


class TestApiShape(_Harness):
    """锁 API 自身的最小契约（真实文件，不 mock）。"""

    def test_acquire_returns_fd_and_release_is_idempotent(self):
        path = os.path.join(self.tmp, "config.json")
        fd = common.acquire_file_lock(path, timeout=1.0)
        self.assertIsNotNone(fd, "空闲文件拿不到锁（cfg-004 把 fail-closed 做成了永远失败）")
        common.release_file_lock(path, fd)
        common.release_file_lock(path, None)         # 幂等：None 不得抛
        fd2 = common.acquire_file_lock(path, timeout=1.0)
        self.assertIsNotNone(fd2, "释放后不可重取")
        common.release_file_lock(path, fd2)

    def test_second_handle_is_blocked_while_held(self):
        """锁是 OS 级字节范围锁：另一条持锁句柄存在时，重新 acquire 拿不到（返回 None）。

        Windows 的 LockFile 对**同一进程的另一句柄**同样互斥（实测），所以这条既
        挡住了"退化成进程内 threading 锁之外什么都不做"的实现，也挡住了
        "每次 acquire 都取同一把可重入句柄"的写法。
        """
        path = os.path.join(self.tmp, "config.json")
        box = {}
        gate = threading.Event()

        def hold():
            box["fd"] = common.acquire_file_lock(path, timeout=1.0)
            gate.set()
            box["release"].wait(5.0)
            common.release_file_lock(path, box["fd"])

        box["release"] = threading.Event()
        th = threading.Thread(target=hold, daemon=True)
        th.start()
        self.assertTrue(gate.wait(3.0), "持锁线程没起来")
        self.assertIsNotNone(box["fd"], "持锁线程自己没拿到锁")
        t0 = time.monotonic()
        fd = common.acquire_file_lock(path, timeout=0.0)
        self.assertIsNone(fd, "另一条句柄持锁时仍能取得锁：跨进程互斥形同虚设")
        self.assertLess(time.monotonic() - t0, 1.0, "timeout=0 却等了 %.2fs" %
                        (time.monotonic() - t0))
        box["release"].set()
        th.join(3.0)
        fd2 = common.acquire_file_lock(path, timeout=1.0)
        self.assertIsNotNone(fd2, "释放后不可重取（锁泄漏）")
        common.release_file_lock(path, fd2)


if __name__ == "__main__":
    unittest.main()
