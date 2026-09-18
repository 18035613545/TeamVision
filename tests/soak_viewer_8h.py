# -*- coding: utf-8 -*-
"""E4 长稳（实现方案 §11.4）：本机 viewer 收流 8h，RSS 每 5min 采样，波动 >20% 判失败。

挂 e2e 家族，不进默认回归（文件名不匹配 test*.py）。源码方式运行 host+viewer
（fps-screen env），这样长稳覆盖的是当前工作区代码（含 E4 的 _latency_ring），
而不是旧 dist 构建。

行为：
  1) 备份仓库 config.json → 改 host.port=E4_SOAK_PORT（默认 5798）、
     host.auth.enabled=false、viewer.channels=[]（--addr 作为唯一临时频道，第 100 条）；
  2) 起 host --console 与 viewer --addr 127.0.0.1:PORT；
  3) 预热 warmup 秒（默认 600）内不采样——启动期工作集先涨后削（实测 2.5→70→25MB），
     计入基线会把正常修剪误判为漂移；随后每 interval 秒采一次 viewer/host RSS
     （psapi GetProcessMemoryInfo），viewer 基线 = 预热后前 3 个样本中位数，
     其后任一样本 > 基线×(1+阈值) 判失败；
  4) 任一进程中途退出即失败；结束时 taskkill 两端并恢复 config.json。

用法（默认 8 小时，总时长含预热；快速自检：--minutes 3 --interval 20 --warmup 180）：
    F:/conda/envs/fps-screen/python.exe tests/soak_viewer_8h.py
    F:/conda/envs/fps-screen/python.exe tests/soak_viewer_8h.py --minutes 480 --interval 300
"""
import argparse
import ctypes
import ctypes.wintypes
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.json")
PY = os.environ.get("E2E_PYTHON", r"F:/conda/envs/fps-screen/python.exe")
PORT = int(os.environ.get("E4_SOAK_PORT", "5798"))
LOG_DIR = os.path.join(tempfile.gettempdir(), "teamvision_e4_soak")

k32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010


class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def rss_mb(pid):
    """工作集（MiB）；进程已退出返回 None。"""
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None
    try:
        counters = _PMC()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(counters), counters.cb):
            return None
        return counters.WorkingSetSize / (1024.0 * 1024.0)
    finally:
        k32.CloseHandle(h)


class Proc:
    """DETACHED_PROCESS 子进程，stdout/stderr 落 TEMP 日志（同 e2e_multiview 第 78 条约定）。"""

    def __init__(self, name, args):
        self.name = name
        self.log_path = os.path.join(LOG_DIR, name + ".log")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        self.log = open(self.log_path, "wb")
        self.p = subprocess.Popen(
            args, cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT,
            env=env, creationflags=0x00000008)
        self.pid = self.p.pid

    def alive(self):
        return self.p.poll() is None

    def kill(self):
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.pid)],
                           capture_output=True, timeout=15)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass
        self.log.close()


def wait_port(port, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def median(vals):
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def main():
    ap = argparse.ArgumentParser(description="TeamVision E4 长稳：viewer 收流 RSS 漂移监测")
    ap.add_argument("--minutes", type=float, default=480.0, help="总时长（分钟），默认 8h")
    ap.add_argument("--interval", type=float, default=300.0, help="采样间隔（秒），默认 5min")
    ap.add_argument("--drift", type=float, default=0.20, help="RSS 相对基线的失败阈值，默认 20%%")
    ap.add_argument("--warmup", type=float, default=600.0,
                    help="预热秒数，期内只做存活检查不采样（默认 600）")
    args = ap.parse_args()

    if not os.path.exists(CONFIG):
        print("config.json 不存在：本测试需基于既有配置运行（先 python host.py 生成默认配置）。",
              file=sys.stderr)
        return 2

    os.makedirs(LOG_DIR, exist_ok=True)
    backup = os.path.join(tempfile.gettempdir(), "teamvision_e4_soak_config.bak")
    shutil.copy2(CONFIG, backup)
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        host = cfg.setdefault("host", {})
        host["port"] = PORT
        host.setdefault("auth", {})["enabled"] = False
        host.setdefault("frp", {})["auto_start"] = False  # 测试期绝不拉起外部 frpc
        cfg.setdefault("viewer", {})["channels"] = []
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print("config.json 已改写：port=%d auth=off channels=[]（备份 %s）" % (PORT, backup),
              flush=True)

        procs = []
        viewer_rss, host_rss = [], []
        failures = []
        try:
            host_p = Proc("soak_host", [PY, "host.py", "--console"])
            procs.append(host_p)
            if not wait_port(PORT):
                print("FAIL: host 端口 %d 未就绪" % PORT, file=sys.stderr)
                return 1
            viewer_p = Proc("soak_viewer", [PY, "viewer.py", "--addr", "127.0.0.1:%d" % PORT])
            procs.append(viewer_p)

            warm_deadline = time.monotonic() + args.warmup
            while time.monotonic() < warm_deadline:
                if not host_p.alive():
                    print("FAIL: host 预热期退出（见 %s）" % host_p.log_path, file=sys.stderr)
                    return 1
                if not viewer_p.alive():
                    print("FAIL: viewer 预热期退出（见 %s）" % viewer_p.log_path, file=sys.stderr)
                    return 1
                time.sleep(min(5.0, max(warm_deadline - time.monotonic(), 0.001)))
            print("预热结束（%.0fs），开始采样" % args.warmup, flush=True)

            t_end = time.monotonic() + args.minutes * 60.0
            i = 0
            while time.monotonic() < t_end:
                i += 1
                if not host_p.alive():
                    failures.append("host 进程中途退出（见 %s）" % host_p.log_path)
                    break
                if not viewer_p.alive():
                    failures.append("viewer 进程中途退出（见 %s）" % viewer_p.log_path)
                    break
                v = rss_mb(viewer_p.pid)
                h = rss_mb(host_p.pid)
                if v is None:
                    failures.append("viewer RSS 采样失败（进程可能刚退出）")
                    break
                viewer_rss.append(v)
                if h is not None:
                    host_rss.append(h)
                if len(viewer_rss) >= 3:
                    base = median(viewer_rss[:3])
                    drift = v / base - 1.0 if base > 0 else 0.0
                    print("[%d] viewer RSS %.1fMB (%+.1f%% 基线 %.1fMB) · host RSS %.1fMB"
                          % (i, v, drift * 100.0, base,
                             host_rss[-1] if host_rss else float("nan")), flush=True)
                    if v > base * (1.0 + args.drift):
                        failures.append("viewer RSS 漂移 %.1f%% > %.0f%%（%.1fMB vs 基线 %.1fMB）"
                                        % (drift * 100.0, args.drift * 100.0, v, base))
                        break
                else:
                    print("[%d] viewer RSS %.1fMB · host RSS %.1fMB（基线建立中）"
                          % (i, v, host_rss[-1] if host_rss else float("nan")), flush=True)
                remain = t_end - time.monotonic()
                if remain <= 0:
                    break
                time.sleep(min(args.interval, max(remain, 0.001)))
        finally:
            for p in procs:
                p.kill()
    finally:
        restored = False
        try:
            if os.path.exists(backup):
                shutil.copy2(backup, CONFIG)
                os.remove(backup)
                restored = True
                print("config.json 已恢复原状", flush=True)
        except OSError as e:
            print("config.json 恢复失败: %s（备份仍在 %s）" % (e, backup), file=sys.stderr)
        if not restored and os.path.exists(backup):
            print("请手动恢复：copy %s %s" % (backup, CONFIG), file=sys.stderr)

    if failures:
        for msg in failures:
            print("FAIL: %s" % msg, file=sys.stderr)
        return 1
    if len(viewer_rss) < 3:
        print("FAIL: 样本不足 3 个（%d），无法判定" % len(viewer_rss), file=sys.stderr)
        return 1
    print("SOAK_OK: %d 个样本，viewer 基线 %.1fMB、峰值 %.1fMB、末值 %.1fMB"
          % (len(viewer_rss), median(viewer_rss[:3]), max(viewer_rss), viewer_rss[-1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
