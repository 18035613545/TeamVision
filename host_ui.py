# -*- coding: utf-8 -*-
"""共享端图形控制台（第六轮优化）：深色主题 GUI。

展示服务状态/公网地址（一键复制）/frpc 配置与启停/客户端带宽列表/
实时性能统计/采集源设置/滚动日志区。与 host.HostRuntime 通过线程安全快照联动。
"""

import logging
import queue
import tkinter as tk
from tkinter import ttk

import mss

import logger as logger_mod
from common import APP_NAME, APP_VERSION, save_config

log = logger_mod.get_logger()

#: 与观看端面板一致的深色主题配色
COL_BG = "#14141f"
COL_CARD = "#1e1e2e"
COL_CTRL = "#2a2a3c"
COL_FG = "#e6e6ef"
COL_DIM = "#9a9ab0"
COL_ACCENT = "#7c5cff"
COL_ONLINE = "#4ade80"
COL_OFFLINE = "#f87171"


class _LogQueueHandler(logging.Handler):
    """把日志记录放入队列，由 GUI 主线程轮询显示（线程安全，避免跨线程操作 Tk）。"""

    def __init__(self):
        super().__init__()
        self.queue = queue.Queue()
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    def emit(self, record):
        try:
            self.queue.put_nowait(self.format(record))
        except Exception:
            pass


def list_monitor_rects():
    """枚举本机显示器矩形（mss 语义），返回 [{"left","top","width","height"}, ...]。

    索引 0 是"全部屏幕"的并集，1..N 为各物理显示器。枚举失败返回 []（调用方按
    "无法校验"处理，绝不因此拦住用户设置）。
    """
    rects = []
    try:
        with mss.MSS() as sct:
            for m in sct.monitors:
                rects.append({"left": int(m["left"]), "top": int(m["top"]),
                              "width": int(m["width"]), "height": int(m["height"])})
    except Exception as e:
        log.warning("枚举显示器矩形失败: %s", e)
    return rects


def _monitor_labels(rects):
    """把显示器矩形列表转成下拉框标签（索引 0 为"全部屏幕"）。"""
    if not rects:
        return ["1: 主显示器"]
    return ["%d: %s (%dx%d)" % (i, "全部屏幕" if i == 0 else "显示器 %d" % i,
                               m["width"], m["height"])
            for i, m in enumerate(rects)]


def list_monitors():
    """枚举本机显示器（mss 语义），返回 ["0: 全部屏幕 (WxH)", "1: 显示器 1 (WxH)", ...]。"""
    return _monitor_labels(list_monitor_rects())


class HostConsole:
    """共享端图形控制台窗口。"""

    #: 日志区最大保留行数（第 58 条）：超出从顶部删除，避免多小时会话 Tcl text 内存无上限增长
    _MAX_LOG_LINES = 2000

    def __init__(self, runtime):
        self.runtime = runtime
        self.monitor_rects = list_monitor_rects()
        self.monitors = _monitor_labels(self.monitor_rects)
        self.root = tk.Tk()
        self.root.title("%s v%s · 共享端控制台" % (APP_NAME, APP_VERSION))
        self.root.configure(bg=COL_BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_quit)
        self._build_style()
        self._build_ui()
        # 日志转发：把全局 logger 输出接入 GUI 日志区（线程安全队列 + 轮询）
        self._log_handler = _LogQueueHandler()
        logging.getLogger(logger_mod.LOGGER_NAME).addHandler(self._log_handler)
        self.root.after(250, self._drain_log)
        # 第 37 条：_poll() 自身会 after(500) 重排，这里不再额外排一次，避免双刷新链
        # （旧实现客户端表格每 500ms 被全删全建两次 → 可见闪烁 + 与推流线程抢锁翻倍）。
        self._poll()

    # ---------- 界面搭建 ----------

    def _build_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Treeview",
                        background=COL_CTRL, foreground=COL_FG, fieldbackground=COL_CTRL,
                        bordercolor=COL_CARD, lightcolor=COL_CARD, darkcolor=COL_CARD)
        style.configure("Treeview.Heading",
                        background=COL_CARD, foreground=COL_DIM, relief="flat")
        style.map("Treeview", background=[("selected", COL_ACCENT)])
        style.map("Treeview.Heading", background=[("active", COL_CARD)])
        style.configure("TCombobox",
                        fieldbackground=COL_CTRL, background=COL_CTRL, foreground=COL_FG,
                        arrowcolor=COL_FG, bordercolor=COL_CARD, lightcolor=COL_CARD,
                        darkcolor=COL_CARD, selectbackground=COL_CTRL, selectforeground=COL_FG)

    def _card(self, parent, title):
        card = tk.Frame(parent, bg=COL_CARD)
        card.pack(fill="x", padx=14, pady=6)
        tk.Label(card, text=title, bg=COL_CARD, fg=COL_DIM,
                 font=("Microsoft YaHei", 9, "bold")).pack(anchor="w", padx=10, pady=(8, 2))
        return card

    @staticmethod
    def _entry(parent, label, variable, row_font=("Consolas", 9)):
        row = tk.Frame(parent, bg=COL_CARD)
        row.pack(fill="x", padx=10, pady=2)
        tk.Label(row, text=label, width=14, bg=COL_CARD, fg=COL_DIM,
                 font=("Microsoft YaHei", 9)).pack(side="left")
        tk.Entry(row, textvariable=variable, bg=COL_CTRL, fg=COL_FG,
                 insertbackground=COL_FG, relief="flat", font=row_font).pack(
                 side="left", fill="x", expand=True)
        return row

    def _button(self, parent, text, command, accent=False):
        return tk.Button(parent, text=text,
                         bg=COL_ACCENT if accent else COL_CTRL,
                         fg="#ffffff" if accent else COL_FG, relief="flat",
                         activebackground="#8a70ff" if accent else "#3a3a52",
                         activeforeground="#ffffff", padx=12, pady=2,
                         command=command)

    def _build_ui(self):
        # ---------- 顶部：品牌/状态/公网地址 ----------
        header = tk.Frame(self.root, bg=COL_BG)
        header.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(header, text="%s v%s · 共享端" % (APP_NAME, APP_VERSION),
                 bg=COL_BG, fg=COL_FG,
                 font=("Microsoft YaHei", 14, "bold")).pack(anchor="w")
        self._srv_status = tk.Label(header, text="服务未启动", bg=COL_BG, fg=COL_DIM,
                                    font=("Microsoft YaHei", 9))
        self._srv_status.pack(anchor="w", pady=(2, 0))
        addr_row = tk.Frame(header, bg=COL_BG)
        addr_row.pack(fill="x", pady=(6, 0))
        tk.Label(addr_row, text="公网地址", bg=COL_BG, fg=COL_DIM,
                 font=("Microsoft YaHei", 9)).pack(side="left")
        self._addr_var = tk.StringVar(
            value=self.runtime.cfg["host"].get("frp", {}).get("public_addr", ""))
        self._addr_entry = tk.Entry(addr_row, textvariable=self._addr_var, bg=COL_CTRL,
                                    fg=COL_FG, insertbackground=COL_FG, relief="flat",
                                    font=("Consolas", 10))
        self._addr_entry.pack(side="left", fill="x", expand=True, padx=8)
        self._button(addr_row, "复制", self._copy_addr, accent=True).pack(side="left")

        # ---------- frpc 区 ----------
        frp_card = self._card(self.root, "frpc 隧道")
        self._frp_status = tk.Label(frp_card, text="已停止", bg=COL_CARD, fg=COL_DIM,
                                    font=("Microsoft YaHei", 9))
        self._frp_status.pack(anchor="w", padx=10)
        frp = self.runtime.cfg["host"].get("frp", {})
        self._frp_auto_var = tk.BooleanVar(value=bool(frp.get("auto_start", False)))
        self._frp_token_var = tk.StringVar(value=frp.get("token", ""))
        self._frp_tunnel_var = tk.StringVar(value=frp.get("tunnel_ids", ""))
        self._frp_addr_var = tk.StringVar(value=frp.get("public_addr", ""))
        f = tk.Frame(frp_card, bg=COL_CARD)
        f.pack(fill="x", padx=10, pady=4)
        tk.Checkbutton(f, text="程序启动时自动开启隧道", variable=self._frp_auto_var,
                       bg=COL_CARD, fg=COL_FG, selectcolor=COL_CTRL,
                       activebackground=COL_CARD, activeforeground=COL_FG,
                       font=("Microsoft YaHei", 9)).pack(anchor="w")
        self._entry(frp_card, "访问密钥 token", self._frp_token_var)
        self._entry(frp_card, "隧道 ID", self._frp_tunnel_var)
        self._entry(frp_card, "公网地址", self._frp_addr_var)
        btns = tk.Frame(frp_card, bg=COL_CARD)
        btns.pack(fill="x", padx=10, pady=(4, 8))
        self._button(btns, "启动隧道", self._frp_start, accent=True).pack(side="left")
        self._button(btns, "停止隧道", self._frp_stop).pack(side="left", padx=(8, 0))

        # ---------- 传输参数区 ----------
        trans_card = self._card(self.root, "传输参数")
        host_cfg = self.runtime.cfg["host"]
        self._fps_var = tk.StringVar(value=str(host_cfg.get("fps", 60)))
        self._quality_var = tk.StringVar(value=str(host_cfg.get("jpeg_quality", 80)))
        self._scale_var = tk.StringVar(value=str(host_cfg.get("scale", 1.0)))
        self._adaptive_var = tk.BooleanVar(
            value=bool(host_cfg.get("perf", {}).get("adaptive", True)))
        self._auth_var = tk.BooleanVar(
            value=bool(host_cfg.get("auth", {}).get("enabled", False)))
        self._entry(trans_card, "目标帧率 fps", self._fps_var)
        self._entry(trans_card, "JPEG 质量", self._quality_var)
        self._entry(trans_card, "缩放系数", self._scale_var)
        row = tk.Frame(trans_card, bg=COL_CARD)
        row.pack(fill="x", padx=10, pady=2)
        tk.Checkbutton(row, text="自适应画质", variable=self._adaptive_var,
                       bg=COL_CARD, fg=COL_FG, selectcolor=COL_CTRL,
                       activebackground=COL_CARD, activeforeground=COL_FG,
                       font=("Microsoft YaHei", 9)).pack(side="left", padx=(0, 18))
        tk.Checkbutton(row, text="账户准入", variable=self._auth_var,
                       bg=COL_CARD, fg=COL_FG, selectcolor=COL_CTRL,
                       activebackground=COL_CARD, activeforeground=COL_FG,
                       font=("Microsoft YaHei", 9)).pack(side="left")
        self._button(trans_card, "应用传输设置", self._trans_save, accent=True).pack(
            anchor="w", padx=10, pady=(4, 8))

        # ---------- 客户端区 ----------
        client_card = self._card(self.root, "已连接观看端")
        tree = ttk.Treeview(client_card, columns=("user", "addr", "uptime", "rate", "total"),
                            show="headings", height=4)
        tree.heading("user", text="用户")
        tree.heading("addr", text="地址")
        tree.heading("uptime", text="已连接")
        tree.heading("rate", text="速率")
        tree.heading("total", text="累计流量")
        tree.column("user", width=80, minwidth=60, stretch=False, anchor="center")
        tree.column("addr", width=150, stretch=True)
        tree.column("uptime", width=70, stretch=False, anchor="center")
        tree.column("rate", width=80, stretch=False, anchor="center")
        tree.column("total", width=90, stretch=False, anchor="center")
        tree.pack(fill="x", padx=10, pady=(2, 8))
        self._client_tree = tree

        # ---------- 性能区 ----------
        perf_card = self._card(self.root, "实时性能")
        self._perf_var = tk.StringVar(value="-")
        tk.Label(perf_card, textvariable=self._perf_var, bg=COL_CARD, fg=COL_FG,
                 font=("Consolas", 9), justify="left").pack(anchor="w", padx=10, pady=(0, 8))

        # ---------- 采集源区 ----------
        cap_card = self._card(self.root, "采集源")
        cap = self.runtime.cfg["host"].get("capture", {})
        row = tk.Frame(cap_card, bg=COL_CARD)
        row.pack(fill="x", padx=10, pady=2)
        tk.Label(row, text="显示器", width=14, bg=COL_CARD, fg=COL_DIM,
                 font=("Microsoft YaHei", 9)).pack(side="left")
        self._monitor_var = tk.StringVar(value=self._monitor_label(cap.get("monitor", 1)))
        ttk.Combobox(row, textvariable=self._monitor_var, values=self.monitors,
                     state="readonly", width=34).pack(side="left")
        self._region_var = tk.StringVar(value=self._region_text(cap.get("region")))
        self._entry(cap_card, "区域(左,上,宽,高)", self._region_var)
        row3 = tk.Frame(cap_card, bg=COL_CARD)
        row3.pack(fill="x", padx=10, pady=2)
        tk.Label(row3, text="采集后端", width=14, bg=COL_CARD, fg=COL_DIM,
                 font=("Microsoft YaHei", 9)).pack(side="left")
        self._backend_var = tk.StringVar(value=cap.get("backend", "mss"))
        ttk.Combobox(row3, textvariable=self._backend_var, values=("mss", "dxgi"),
                     state="readonly", width=12).pack(side="left")
        cap_btns = tk.Frame(cap_card, bg=COL_CARD)
        cap_btns.pack(fill="x", padx=10, pady=(4, 8))
        self._button(cap_btns, "应用采集设置", self._capture_save, accent=True).pack(side="left")
        self._button(cap_btns, "恢复全屏", self._reset_fullscreen).pack(side="left", padx=(8, 0))

        # ---------- 日志区 ----------
        log_card = tk.Frame(self.root, bg=COL_CARD)
        log_card.pack(fill="both", expand=True, padx=14, pady=6)
        tk.Label(log_card, text="运行日志", bg=COL_CARD, fg=COL_DIM,
                 font=("Microsoft YaHei", 9, "bold")).pack(anchor="w", padx=10, pady=(8, 2))
        self._log_text = tk.Text(log_card, bg="#0f0f16", fg="#c8c8d6",
                                 insertbackground=COL_FG, relief="flat",
                                 font=("Consolas", 8), state="disabled", wrap="word",
                                 padx=8, pady=4, height=8)
        self._log_text.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        # ---------- 底部 ----------
        footer = tk.Frame(self.root, bg=COL_BG)
        footer.pack(fill="x", padx=14, pady=(4, 12))
        self._status_var = tk.StringVar(value="就绪")
        self._status_label = tk.Label(footer, textvariable=self._status_var, bg=COL_BG,
                                      fg=COL_DIM, font=("Microsoft YaHei", 9))
        self._status_label.pack(side="left")
        self._button(footer, "退出", self._on_quit, accent=True).pack(side="right")

    # ---------- 工具 ----------

    def _monitor_label(self, idx):
        for s in self.monitors:
            if s.startswith("%d:" % idx):
                return s
        if len(self.monitors) > 1:
            return self.monitors[1]
        return self.monitors[0] if self.monitors else "1: 主显示器"

    @staticmethod
    def _region_text(region):
        # 第 100 条：缺键/非数字的 region（手改 config.json）不得抛 KeyError——
        # 那会让整个图形控制台构造失败、静默回落到命令行模式（"双击没反应"）。
        if not isinstance(region, dict):
            return ""
        try:
            return "%d,%d,%d,%d" % (region["left"], region["top"],
                                    region["width"], region["height"])
        except (KeyError, TypeError, ValueError):
            log.warning("采集区域配置非法（%r），输入框按空（全屏）显示", region)
            return ""

    def _region_out_of_bounds(self, region):
        """区域是否明显越界；越界返回 (可用宽, 可用高)，否则 None。

        第 100 条：mss 对越界区域**不报错**，只是返回全黑帧（实测 left/top=99999
        时 mean=0），于是观看端全黑、GUI 却显示"采集源已更新"、帧率正常。这里用
        所有显示器的最大宽高做保守校验（不按单个显示器原点比对，避免多屏偏移误判）。
        枚举失败（monitor_rects 为空）时不做校验。
        """
        max_w = max((m["width"] for m in self.monitor_rects), default=0)
        max_h = max((m["height"] for m in self.monitor_rects), default=0)
        if max_w <= 0 or max_h <= 0:
            return None
        if region["left"] + region["width"] > max_w or region["top"] + region["height"] > max_h:
            return max_w, max_h
        return None

    def run(self):
        self.root.mainloop()

    # ---------- 事件 ----------

    def _copy_addr(self):
        addr = self._addr_var.get().strip()
        if not addr:
            self._set_status("暂无公网地址，请先在 frpc 区填写", error=True)
            return
        # 顶部地址编辑后也同步到配置与 frpc 区，避免只复制不保存
        frp = self.runtime.cfg["host"].setdefault("frp", {})
        frp["public_addr"] = addr
        save_config(self.runtime.cfg, "host")
        self._frp_addr_var.set(addr)
        self.root.clipboard_clear()
        self.root.clipboard_append(addr)
        self._set_status("公网地址已复制到剪贴板并保存")

    def _frp_start(self):
        frp = self.runtime.cfg["host"].setdefault("frp", {})
        frp["auto_start"] = bool(self._frp_auto_var.get())
        frp["token"] = self._frp_token_var.get().strip()
        frp["tunnel_ids"] = self._frp_tunnel_var.get().strip()
        frp["public_addr"] = self._frp_addr_var.get().strip()
        save_config(self.runtime.cfg, "host")
        ok, msg = self.runtime.frp.start()
        self._set_status(msg, error=not ok)

    def _frp_stop(self):
        msg = self.runtime.frp.stop()
        self._set_status(msg or "frpc 已停止")

    def _capture_save(self):
        monitor = 1
        try:
            monitor = int(self._monitor_var.get().split(":")[0].strip())
        except Exception:
            pass
        region = None
        txt = self._region_var.get().strip()
        if txt:
            try:
                parts = [int(x.strip()) for x in txt.replace("，", ",").split(",")]
                # 第 33 条：宽/高必须为正，否则 0,0,0,0 之类会让 mss 每帧抛错、画面冻结
                if (len(parts) == 4 and parts[0] >= 0 and parts[1] >= 0
                        and parts[2] > 0 and parts[3] > 0):
                    region = {"left": parts[0], "top": parts[1],
                              "width": parts[2], "height": parts[3]}
                else:
                    self._set_status(
                        "区域格式错误：应为 左,上,宽,高（左/上≥0，宽/高须为正整数）", error=True)
                    return
            except Exception:
                self._set_status(
                    "区域格式错误：应为 左,上,宽,高（左/上≥0，宽/高须为正整数）", error=True)
                return
            # 第 100 条：越界区域 mss 不报错、只给全黑帧 → 观看端全黑而状态栏仍说
            # "已更新"。这里提前拦下并说明原因。
            bounds = self._region_out_of_bounds(region)
            if bounds is not None:
                self._set_status(
                    "区域超出显示器范围：%d,%d,%d,%d（最大可用 %dx%d）" % (
                        region["left"], region["top"], region["width"],
                        region["height"], bounds[0], bounds[1]), error=True)
                return
        backend = self._backend_var.get().strip().lower() or "mss"
        self.runtime.update_capture(monitor=monitor, region=region, backend=backend)
        self._set_status("采集源已更新（显示器 %d / 后端 %s）" % (monitor, backend))

    def _reset_fullscreen(self):
        """一键恢复全屏采集：清空区域配置并立即应用。"""
        self._region_var.set("")
        self._capture_save()
        self._set_status("已恢复全屏采集")

    def _trans_save(self):
        """保存并即时应用传输参数：fps / JPEG 质量 / 缩放 / 自适应 / 账户准入。"""
        host = self.runtime.cfg["host"]
        try:
            fps = int(self._fps_var.get().strip())
            quality = int(self._quality_var.get().strip())
            scale = float(self._scale_var.get().strip())
            if not (1 <= fps <= 240):
                raise ValueError("fps 应在 1~240")
            if not (1 <= quality <= 100):
                raise ValueError("JPEG 质量应在 1~100")
            if not (0.1 <= scale <= 2.0):
                raise ValueError("缩放系数应在 0.1~2.0")
        except ValueError as exc:
            self._set_status("传输参数格式错误（%s）" % exc, error=True)
            return
        host["fps"] = fps
        host["jpeg_quality"] = quality
        host["scale"] = scale
        host.setdefault("perf", {})["adaptive"] = bool(self._adaptive_var.get())
        host.setdefault("auth", {})["enabled"] = bool(self._auth_var.get())
        save_config(self.runtime.cfg, "host")
        with self.runtime.slot_lock:
            self.runtime.slot["fps"] = fps
            self.runtime.slot["quality"] = quality
            self.runtime.slot["scale"] = scale
        self._set_status("传输设置已应用：fps=%d，质量=%d，缩放=%.2f，自适应=%s，准入=%s" % (
            fps, quality, scale,
            "开" if self._adaptive_var.get() else "关",
            "开" if self._auth_var.get() else "关"))

    def _on_quit(self):
        self._set_status("正在退出…")
        try:
            self.root.destroy()
        except Exception:
            pass

    def _set_status(self, text, error=False):
        self._status_var.set(text)
        # 第 34 条：error=True 时状态文字标红，让校验失败与成功提示在外观上可区分
        try:
            self._status_label.configure(fg=COL_OFFLINE if error else COL_DIM)
        except Exception:
            pass

    # ---------- 轮询刷新 ----------

    def _drain_log(self):
        # 第 38 条：insert/see 的 TclError 不再逃逸中断日志链（旧实现只捕 queue.Empty）；
        # 第 58 条：超过 _MAX_LOG_LINES 行从顶部删除，防多小时会话 Tcl text 内存无上限增长。
        try:
            while True:
                try:
                    line = self._log_handler.queue.get_nowait()
                except queue.Empty:
                    break
                self._log_text.configure(state="normal")
                self._log_text.insert("end", line + "\n")
                line_count = int(self._log_text.index("end-1c").split(".")[0])
                if line_count > self._MAX_LOG_LINES:
                    self._log_text.delete(
                        "1.0", "%d.0" % (line_count - self._MAX_LOG_LINES + 1))
                self._log_text.see("end")
                self._log_text.configure(state="disabled")
        except Exception as e:
            log.debug("日志区刷新忽略: %s", e)
        finally:
            try:
                self.root.after(250, self._drain_log)
            except Exception:
                pass

    def _poll(self):
        # 第 38 条：_refresh 移入 try、重排放入 finally——一次刷新异常不再让轮询链永久停摆
        # （旧实现 _refresh 在 try 外抛错后 after 不执行，控制台从此停在旧值，看似卡死）。
        try:
            snap = self.runtime.get_snapshot()
            if snap is not None:
                self._refresh(snap)
        except Exception as e:
            log.warning("状态刷新失败: %s", e)
        finally:
            try:
                self.root.after(500, self._poll)
            except Exception:
                pass

    def _refresh(self, snap):
        # 服务状态（第 18 条：绑定失败时显示真实错误，不再无条件报“已启动”）
        if snap.get("startup_error"):
            self._srv_status.configure(
                text="服务启动失败：%s" % snap["startup_error"], fg=COL_OFFLINE)
        elif snap.get("server_up"):
            self._srv_status.configure(
                text="监听 %s:%d · 已启动 · 准入%s" % (
                    snap.get("bound_addr", "0.0.0.0"),
                    snap["port"], "开启" if snap["auth_enabled"] else "关闭"),
                fg=COL_ONLINE)
        else:
            self._srv_status.configure(text="服务未启动", fg=COL_DIM)
        # frpc 状态
        if snap["frp_running"]:
            self._frp_status.configure(text="运行中", fg=COL_ONLINE)
        else:
            self._frp_status.configure(text="已停止", fg=COL_OFFLINE)
        # 客户端列表
        tree = self._client_tree
        tree.delete(*tree.get_children())
        for i, c in enumerate(snap["clients"]):
            tree.insert("", "end", iid=str(i), values=(
                c.get("username") or "—",
                c["addr"],
                self._fmt_uptime(c["uptime_s"]),
                "%.0f KB/s" % c["rate_kbps"],
                "%.1f MB" % c["total_mb"],
            ))
        # 性能
        p = snap["perf"]
        codec_txt = ""
        if p.get("codec_name"):
            codec_txt = " · 编码器 %s · 码率 %d Kbps" % (
                p["codec_name"], p.get("bitrate_kbps", 0))
        elif p.get("bitrate_kbps", 0) <= 0:
            codec_txt = " · JPEG"
        self._perf_var.set(
            "采集 %d fps · 编码 %d fps · 发送 %d fps · 帧率档 %d · 质量 %d · 缩放 %.2f · 编码 %.1f ms · 发送 %.1f ms%s"
            % (p["capture_fps"], p.get("encode_fps", 0), p["send_fps"], p["fps_now"],
               p["quality"], p["scale"], p["encode_ms"], p["avg_send_ms"], codec_txt))
        # 公网地址同步（frpc 区可能修改过）；正在编辑顶部地址时不覆盖用户输入
        try:
            if self.root.focus_get() is not self._addr_entry:
                self._addr_var.set(snap["public_addr"])
        except Exception:
            self._addr_var.set(snap["public_addr"])

    @staticmethod
    def _fmt_uptime(seconds):
        seconds = int(seconds)
        if seconds < 60:
            return "%ds" % seconds
        if seconds < 3600:
            return "%dm%02ds" % (seconds // 60, seconds % 60)
        return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)
