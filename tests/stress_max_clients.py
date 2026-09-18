# -*- coding: utf-8 -*-
"""E4 压测（实现方案 §11.4）：100 并发空连接停在握手前，断言 max_clients 拒绝生效、无 fd 泄漏。

挂 e2e 家族，不进默认回归（文件名不匹配 test*.py）。对应第 103 条的连接门控
（host.py acquire_conn：超限连接在握手回复之前就被关闭，客户端读 MAGIC_SERVER 见 EOF）。

断言面：
  1) 握手占位连接中，被服务端在册的 ≤ max_clients，其余 EOF；
  2) 超限期间新连接完整握手必须被拒（EOF/超时）；
  3) 释放部分连接后，新握手在有限重试内成功（名额可回收）；
  4) 全部关闭并静置后，host 进程内核句柄数回落至基线 + 容差（无 fd 泄漏）。

用法：
    F:/conda/envs/fps-screen/python.exe tests/stress_max_clients.py [--n 100]
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
sys.path.insert(0, ROOT)

import common  # noqa: E402  （源码运行，取 MAGIC 常量，避免硬编码协议魔数）

CONFIG = os.path.join(ROOT, "config.json")
PY = os.environ.get("E2E_PYTHON", r"F:/conda/envs/fps-screen/python.exe")
PORT = int(os.environ.get("E4_STRESS_PORT", "5799"))
CAP = 32
LOG_DIR = os.path.join(tempfile.gettempdir(), "teamvision_e4_stress")

k32 = ctypes.windll.kernel32
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def handle_count(pid):
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        cnt = ctypes.wintypes.DWORD(0)
        if not k32.GetProcessHandleCount(h, ctypes.byref(cnt)):
            return None
        return cnt.value
    finally:
        k32.CloseHandle(h)


class Proc:
    """同 soak：DETACHED_PROCESS + TEMP 日志（第 78 条约定）。"""

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


def probe_handshake(timeout=3.0):
    """完整客户端握手；返回 'ok'（被在册并回 OK02）或 'rejected'（EOF/断连）。超时也算 rejected。"""
    s = None
    try:
        s = socket.create_connection(("127.0.0.1", PORT), timeout=timeout)
        s.sendall(common.MAGIC_CLIENT)
        data = s.recv(len(common.MAGIC_SERVER))
        return "ok" if data == common.MAGIC_SERVER else "rejected"
    except OSError:
        return "rejected"
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(description="TeamVision E4 压测：max_clients 门控 + fd 泄漏")
    ap.add_argument("--n", type=int, default=100, help="握手前空连接数，默认 100")
    args = ap.parse_args()

    if not os.path.exists(CONFIG):
        print("config.json 不存在：本测试需基于既有配置运行。", file=sys.stderr)
        return 2

    os.makedirs(LOG_DIR, exist_ok=True)
    backup = os.path.join(tempfile.gettempdir(), "teamvision_e4_stress_config.bak")
    shutil.copy2(CONFIG, backup)
    failures = []
    host_p = None
    socks = []
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        host = cfg.setdefault("host", {})
        host["port"] = PORT
        host.setdefault("auth", {})["enabled"] = False
        host.setdefault("net", {})["max_clients"] = CAP
        host.setdefault("frp", {})["auto_start"] = False  # 测试期绝不拉起外部 frpc
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print("config.json 已改写：port=%d max_clients=%d auth=off（备份 %s）"
              % (PORT, CAP, backup), flush=True)

        host_p = Proc("stress_host", [PY, "host.py", "--console"])
        if not wait_port(PORT):
            print("FAIL: host 端口未就绪（见 %s）" % host_p.log_path, file=sys.stderr)
            return 1
        time.sleep(2.0)  # 让启动期线程/句柄稳定
        base_handles = handle_count(host_p.pid)

        # 1) N 条空连接：只完成 TCP，不发任何字节（握手前占位）
        connect_failed = 0
        for _ in range(args.n):
            s = None
            try:
                s = socket.create_connection(("127.0.0.1", PORT), timeout=2.0)
                s.setblocking(False)
                socks.append(s)
            except OSError:
                connect_failed += 1
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass
        print("空连接 %d 条（连接失败 %d），静置 2.5s 等待服务端在册判定"
              % (len(socks), connect_failed), flush=True)
        time.sleep(2.5)

        held, eof = 0, 0
        for s in socks:
            try:
                b = s.recv(1)  # 非阻塞：超限连接应已见 EOF
                if b == b"":
                    eof += 1
                else:
                    held += 1  # 理论上不该收到字节（客户端没发数据）
            except BlockingIOError:
                held += 1
            except OSError:
                eof += 1
        print("在册 held=%d（上限 %d）· 被拒 eof=%d · connect_failed=%d"
              % (held, CAP, eof, connect_failed), flush=True)
        if held > CAP:
            failures.append("在册连接 %d > max_clients %d，门控失效" % (held, CAP))

        # 2) 超限期间新握手必须被拒
        r = probe_handshake()
        print("超限期间新握手结果: %s" % r, flush=True)
        if r != "rejected":
            failures.append("超限期间新握手未被拒（%s）" % r)

        # 3) 释放 10 条在册连接后，新握手应在有限重试内成功
        released = 0
        for s in list(socks):
            if released >= 10:
                break
            try:
                if s.recv(1, socket.MSG_PEEK) == b"":
                    continue  # 被拒的，不占名额
            except BlockingIOError:
                pass
            except OSError:
                continue
            s.close()
            socks.remove(s)
            released += 1
        ok_after_release = False
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if probe_handshake() == "ok":
                ok_after_release = True
                break
            time.sleep(1.0)
        print("释放 %d 条后新握手成功: %s" % (released, ok_after_release), flush=True)
        if not ok_after_release:
            failures.append("释放名额后 20s 内新握手仍不成功（名额未回收或 host 异常）")

        # 4) 全关 + 静置，句柄回落
        for s in socks:
            try:
                s.close()
            except OSError:
                pass
        socks = []
        time.sleep(5.0)
        if not host_p.alive():
            failures.append("压测结束前 host 已退出（见 %s）" % host_p.log_path)
        else:
            end_handles = handle_count(host_p.pid)
            print("host 句柄数：基线 %s → 压测后 %s" % (base_handles, end_handles), flush=True)
            if base_handles is None or end_handles is None:
                failures.append("句柄数采样失败")
            elif end_handles > base_handles + 150:
                failures.append("疑似 fd/handle 泄漏：%d → %d（容差 150）"
                                % (base_handles, end_handles))
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass
        if host_p is not None:
            host_p.kill()
        try:
            if os.path.exists(backup):
                shutil.copy2(backup, CONFIG)
                os.remove(backup)
                print("config.json 已恢复原状", flush=True)
        except OSError as e:
            print("config.json 恢复失败: %s（备份在 %s）" % (e, backup), file=sys.stderr)

    if failures:
        for msg in failures:
            print("FAIL: %s" % msg, file=sys.stderr)
        return 1
    print("STRESS_OK: 门控生效（held≤%d）、名额可回收、句柄回落至基线容差内" % CAP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
