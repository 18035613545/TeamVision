# -*- coding: utf-8 -*-
"""共享端图形控制台（第六轮优化）：深色主题 GUI。

展示服务状态/公网地址（一键复制）/frpc 配置与启停/客户端带宽列表/
实时性能统计/采集源设置/滚动日志区。与 host.HostRuntime 通过线程安全快照联动。
"""

import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

import mss

import logger as logger_mod
import diag
import ui_common
import update_check
from common import (
    APP_NAME, APP_VERSION, DEFAULT_CONFIG, data_dir, save_config, register_policy,
    REGISTER_POLICIES, safe_int,
)

#: ui-007：监听地址非法值兜底——与 DEFAULT_CONFIG/配置迁移/界面文案同源，避免漂移
DEFAULT_LISTEN_HOST = DEFAULT_CONFIG["host"]["listen_host"]

log = logger_mod.get_logger()

#: 与观看端面板一致的深色主题配色（第 136 条 C3：单一来源在 ui_common，此处仅重导出）
from ui_common import (
    COL_BG, COL_CARD, COL_CTRL, COL_FG, COL_DIM, COL_ACCENT,
    COL_ONLINE, COL_OFFLINE,
)

#: 注册策略 → 界面文案（顺序即下拉框顺序，首项为默认）
REGISTER_LABELS = {
    "open": "开放（任何客户端可自助注册）",
    "bootstrap": "仅首个账户可注册",
    "closed": "关闭（须由共享端添加）",
}
REGISTER_LABEL_TO_KEY = {label: key for key, label in REGISTER_LABELS.items()}


class _LogQueueHandler(logging.Handler):
    """把日志记录放入队列，由 GUI 主线程轮询显示（线程安全，避免跨线程操作 Tk）。"""

    def __init__(self):
        super().__init__()
        # ui-017：队列有界（UI 退出/卡顿时丢弃最旧日志，不再无限堆积）
        self.queue = queue.Queue(maxsize=2000)
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
        # ui-017（2026-09-18）：先摘掉可能残留的同类 handler（二次构造面板会双份
        # 挂载 → 日志区重复行）
        _root_logger = logging.getLogger(logger_mod.LOGGER_NAME)
        for _h in list(_root_logger.handlers):
            if isinstance(_h, _LogQueueHandler):
                _root_logger.removeHandler(_h)
        self._log_handler = _LogQueueHandler()
        _root_logger.addHandler(self._log_handler)
        self.root.after(250, self._drain_log)
        # 第 37 条：_poll() 自身会 after(500) 重排，这里不再额外排一次，避免双刷新链
        # （旧实现客户端表格每 500ms 被全删全建两次 → 可见闪烁 + 与推流线程抢锁翻倍）。
        self._poll()
        # B4（实现方案 §6）：启动后台查一次更新，仅写状态栏，失败静默
        update_check.start_background_check(
            getattr(self.runtime, "cfg", None), self._show_update_hint)

    def _show_update_hint(self, msg):
        # ui-019（2026-09-18）：与其余跨线程回调统一走 _post_ui 收口——旧实现直接
        # root.after 从更新检查线程投递，退出瞬间可能撞正在销毁的 Tcl 解释器
        self._post_ui(lambda: self._set_status(msg))

    # ---------- 界面搭建 ----------

    @staticmethod
    def _section(host_cfg, name):
        """取 host 配置里的子节；非 dict（手改成标量）时返回空 dict。

        ui-006（2026-09-18）：旧写法 `host_cfg.get("perf", {}).get(...)` 在
        `"perf": 1` 时抛 AttributeError → GUI 构造失败静默回落命令行模式、
        设置不保存、面板冻结。所有子节读取经此收口。
        """
        value = host_cfg.get(name) if isinstance(host_cfg, dict) else None
        return value if isinstance(value, dict) else {}

    def _build_style(self):
        # 第 136 条（C3）：风格配置收拢至 ui_common.build_style（含原基础控件
        # Treeview/Heading/TCombobox 三项，逐字节一致；额外应用的 Panel.* 命名
        # 风格来自观看端，本窗口未使用这些名字，不影响外观）。
        ui_common.build_style(self.root)

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
        # B2（第 134 条）：公网自检结论上面板（只展示，不改 frpc 行为）
        self._net_status_label = tk.Label(header, text="", bg=COL_BG, fg=COL_DIM,
                                          font=("Microsoft YaHei", 9))
        self._net_status_label.pack(anchor="w")
        addr_row = tk.Frame(header, bg=COL_BG)
        addr_row.pack(fill="x", pady=(6, 0))
        tk.Label(addr_row, text="公网地址", bg=COL_BG, fg=COL_DIM,
                 font=("Microsoft YaHei", 9)).pack(side="left")
        self._addr_var = tk.StringVar(
            value=self._frp_cfg().get("public_addr", ""))
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
        frp = self._frp_cfg()
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
        host_cfg = self._host_cfg()
        # D-12（2026-09-20）：界面兜底值必须与 DEFAULT_CONFIG 同源，不能各写一份字面量。
        # 旧写法 fps 兜底 60（默认 30）、auth.enabled 兜底 False（默认 True），
        # 缺键时面板显示 60fps/准入关，用户点一次"应用传输设置"就把这两个兜底值
        # 固化写回配置——README.md 承诺的"DEFAULT_CONFIG 为单一真源"在此不成立。
        _DEF_HOST = DEFAULT_CONFIG["host"]
        self._fps_var = tk.StringVar(value=str(host_cfg.get("fps", _DEF_HOST.get("fps", 30))))
        self._quality_var = tk.StringVar(
            value=str(host_cfg.get("jpeg_quality", _DEF_HOST.get("jpeg_quality", 80))))
        self._scale_var = tk.StringVar(
            value=str(host_cfg.get("scale", _DEF_HOST.get("scale", 1.0))))
        self._adaptive_var = tk.BooleanVar(
            value=bool(self._section(host_cfg, "perf").get("adaptive", True)))
        # auth-022 同族：安全开关的兜底取向 fail-closed（与 host._do_auth 一致）
        self._auth_var = tk.BooleanVar(
            value=bool(self._section(host_cfg, "auth").get("enabled", True)))
        # dist-009（2026-09-20）：更新检查开关（默认关闭，勾选才向 GitHub 外连）
        uc = (self.runtime.cfg.get("update_check")
              if isinstance(self.runtime.cfg.get("update_check"), dict) else {})
        self._update_check_var = tk.BooleanVar(value=bool(uc.get("enabled", False)))
        # 监听地址：空/0.0.0.0/:: 同为「全网卡」；默认 127.0.0.1 只允许本机与 frp 隧道
        # ui-007：非法值兜底必须先与 DEFAULT_CONFIG / 配置迁移 / 界面文案（127.0.0.1=默认）
        # 一致——旧实现回退 "0.0.0.0"，用户一点"应用"就把全网卡监听固化进配置，
        # 与"默认只监听本机"的安全口径相反
        raw_listen = host_cfg.get("listen_host")
        if not isinstance(raw_listen, str) or not raw_listen.strip():
            raw_listen = DEFAULT_LISTEN_HOST
        self._listen_var = tk.StringVar(value=raw_listen.strip())
        self._reg_var = tk.StringVar(
            value=REGISTER_LABELS[register_policy(self._section(host_cfg, "auth"))])
        self._entry(trans_card, "监听地址", self._listen_var)
        tk.Label(trans_card,
                 text="127.0.0.1=仅本机与 frp 隧道可连（默认）；0.0.0.0=允许局域网直连",
                 bg=COL_CARD, fg=COL_DIM,
                 font=("Microsoft YaHei", 8)).pack(anchor="w", padx=10)
        reg_row = tk.Frame(trans_card, bg=COL_CARD)
        reg_row.pack(fill="x", padx=10, pady=2)
        tk.Label(reg_row, text="账户注册", width=14, bg=COL_CARD, fg=COL_DIM,
                 font=("Microsoft YaHei", 9)).pack(side="left")
        ttk.Combobox(reg_row, textvariable=self._reg_var,
                     values=[REGISTER_LABELS[k] for k in REGISTER_POLICIES],
                     state="readonly", width=30).pack(side="left")
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
        # dist-009（2026-09-20）：dist-004 把更新检查改成 opt-in，注释与审计都写着
        # "需要更新提示时在共享端控制台勾选"，但 host_ui 全文此前**没有任何
        # update_check 相关控件**（Checkbutton 只有隧道自启、自适应、账户准入三处）——
        # opt-in 这条路对普通用户不可达，更新提示静默失效，注释在误导下一个读代码的人。
        # 默认仍为关闭（不外连）；勾选即 opt-in，取消即停止后续外连。
        tk.Checkbutton(row, text="检查更新", variable=self._update_check_var,
                       command=self._update_check_toggle,
                       bg=COL_CARD, fg=COL_FG, selectcolor=COL_CTRL,
                       activebackground=COL_CARD, activeforeground=COL_FG,
                       font=("Microsoft YaHei", 9)).pack(side="left", padx=(18, 0))
        btn_row = tk.Frame(trans_card, bg=COL_CARD)
        btn_row.pack(fill="x", padx=10, pady=(4, 8))
        self._button(btn_row, "应用传输设置", self._trans_save, accent=True).pack(side="left")
        # B3（实现方案 §5）：closed/bootstrap 策略下无需改配置即可本地增删账户
        self._button(btn_row, "账户管理", self._open_accounts).pack(side="left", padx=(8, 0))

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
        cap = self._host_cfg().get("capture")
        cap = cap if isinstance(cap, dict) else {}
        row = tk.Frame(cap_card, bg=COL_CARD)
        row.pack(fill="x", padx=10, pady=2)
        tk.Label(row, text="显示器", width=14, bg=COL_CARD, fg=COL_DIM,
                 font=("Microsoft YaHei", 9)).pack(side="left")
        self._monitor_var = tk.StringVar(
            value=self._monitor_label(
                # ui-030（2026-09-20）：`"monitor": "1"` 这类字符串配置真实存在
                # （share.py 当年就是为它改用 safe_int 的）。旧写法把原值直接传给
                # _monitor_label，其中 `s.startswith("%d:" % idx)` 对字符串抛
                # TypeError → HostConsole.__init__ 无兜底 → host.py 的 main 捕获后
                # gui=None，而 fps-host.spec 是 console=False，回退分支不留任何窗口：
                # 用户看到"双击 exe 之后完全没有界面、进程在后台静默跑"，真因只在日志里。
                safe_int(cap.get("monitor", 1), 1, lo=1, warn=False)))
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
        self._button(footer, "导出诊断包", self._on_export_diag).pack(side="right", padx=(0, 8))

    # ---------- 工具 ----------

    def _host_cfg(self):
        """安全取 host 配置对象（C10，2026-09-11）。

        `self.runtime.cfg["host"].get(...)` 在 host 被手改成非对象时抛 AttributeError，
        `HostConsole.__init__` 失败 → `host.main()` 捕获后**静默回退命令行模式**，
        用户看到的是"双击 exe 没有界面/没有反应"，真实原因却是配置格式。
        """
        cfg = getattr(self.runtime, "cfg", None)
        host = cfg.get("host") if isinstance(cfg, dict) else None
        return host if isinstance(host, dict) else {}

    def _frp_cfg(self):
        """安全取 host.frp 配置对象，必要时就地补一个空对象（C10）。"""
        host = self._host_cfg()
        frp = host.get("frp")
        if not isinstance(frp, dict):
            frp = {}
            try:
                host["frp"] = frp
            except Exception:
                pass
        return frp

    def _post_ui(self, fn):
        """把回调调度回 Tk 主线程（C17：隧道启停不得在主线程内同步阻塞）。

        第 136 条（C3）：实现收拢至 ui_common.post_to_ui（观看端因第 108 条的
        退出竞态走独立队列，不共用此函数）。
        """
        ui_common.post_to_ui(self.root, fn)

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
        frp = self._frp_cfg()
        frp["public_addr"] = addr
        save_config(self.runtime.cfg, "host")
        self._frp_addr_var.set(addr)
        self.root.clipboard_clear()
        self.root.clipboard_append(addr)
        self._set_status("公网地址已复制到剪贴板并保存")

    def _frp_start(self):
        """保存隧道配置并启动 frpc。

        C17（2026-09-11）：`FrpManager.start()` 会阻塞到"轮询 1 秒确认 frpc 没有秒退"，
        在 Tk 回调里同步执行会让控制台冻结约 1 秒（日志区/客户端列表/性能区全部停更），
        与项目自己在共享端/观看端反复强调并已修复的"不在主线程做阻塞操作"约定不一致。
        这里放进工作线程，结果经 `_post_ui` 回主线程更新状态栏。
        """
        frp = self._frp_cfg()
        frp["auto_start"] = bool(self._frp_auto_var.get())
        frp["token"] = self._frp_token_var.get().strip()
        frp["tunnel_ids"] = self._frp_tunnel_var.get().strip()
        frp["public_addr"] = self._frp_addr_var.get().strip()
        save_config(self.runtime.cfg, "host")
        self._set_status("正在启动隧道…")
        frp_mgr = self.runtime.frp

        def _work():
            try:
                ok, msg = frp_mgr.start()
            except Exception as e:          # 兜底：工作线程异常也不得静默丢失原因
                ok, msg = False, "启动 frpc 失败: %s" % e
            self._post_ui(lambda: self._set_status(msg, error=not ok))

        threading.Thread(target=_work, name="frp-start", daemon=True).start()

    def _frp_stop(self):
        """停止 frpc（C17：`stop()` 最长阻塞 5 秒，同样移出主线程）。"""
        self._set_status("正在停止隧道…")
        frp_mgr = self.runtime.frp

        def _work():
            try:
                msg = frp_mgr.stop()
            except Exception as e:
                msg = "停止 frpc 失败: %s" % e
            self._post_ui(lambda: self._set_status(msg or "frpc 已停止"))

        threading.Thread(target=_work, name="frp-stop", daemon=True).start()

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

    def _update_check_toggle(self):
        """dist-009：勾选/取消「检查更新」即刻落盘并生效。

        开启才允许向 api.github.com 外连（默认关闭 = 不外连）；取消后
        `update_check.enabled()` 立即为 False，后台检查与启动检查都随之停住。
        """
        on = bool(self._update_check_var.get())
        cfg = self.runtime.cfg
        sec = cfg.get("update_check")
        if not isinstance(sec, dict):
            sec = {}
            cfg["update_check"] = sec
        sec["enabled"] = on
        save_config(cfg, section="update_check")
        self._set_status("更新检查已%s%s" % (
            "开启（每 24 小时向 GitHub 查询一次）" if on else "关闭（不再外连）", ""))

    def _trans_save(self):
        """保存并即时应用传输参数：监听地址 / 注册策略 / fps / JPEG 质量 / 缩放 / 自适应 / 准入。

        监听地址只落盘，需重启共享端才生效（监听 socket 在启动时绑定一次）；重启前
        在状态栏明确说明，避免用户以为「改了没用」。
        """
        host = self._host_cfg()
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
        listen = self._listen_var.get().strip()
        if not listen or any(ch.isspace() for ch in listen):
            self._set_status(
                "监听地址格式错误：仅本机填 127.0.0.1，允许局域网直连填 0.0.0.0", error=True)
            return
        policy = REGISTER_LABEL_TO_KEY.get(self._reg_var.get(), "open")
        host["fps"] = fps
        host["jpeg_quality"] = quality
        host["scale"] = scale
        host["listen_host"] = listen
        # ui-006：setdefault 在键存在但为标量时会把标量原样返回（赋值即 TypeError），
        # 这里显式判型重建
        perf = host.get("perf")
        if not isinstance(perf, dict):
            perf = {}
            host["perf"] = perf
        perf["adaptive"] = bool(self._adaptive_var.get())
        auth = host.get("auth")
        if not isinstance(auth, dict):
            auth = {}
            host["auth"] = auth
        auth["enabled"] = bool(self._auth_var.get())
        auth["allow_register"] = policy
        save_config(self.runtime.cfg, "host")
        with self.runtime.slot_lock:
            self.runtime.slot["fps"] = fps
            self.runtime.slot["quality"] = quality
            self.runtime.slot["scale"] = scale
        bound = getattr(self.runtime, "bound_addr", None) or "0.0.0.0"
        # A0：回环双栈时 bound_addr 会带 "（+[::1]）" 后缀（如实反映两个监听地址），
        # 比较监听地址是否变化时必须取基础地址，否则每次保存都会误报"需重启生效"。
        bound_base = str(bound).split("（")[0].strip()
        restart_hint = ""
        if listen != bound_base:
            restart_hint = "；监听地址改动需重启共享端生效"
        self._set_status(
            "传输设置已应用：监听=%s，注册=%s，fps=%d，质量=%d，缩放=%.2f，自适应=%s，准入=%s%s" % (
                listen, policy, fps, quality, scale,
                "开" if self._adaptive_var.get() else "关",
                "开" if self._auth_var.get() else "关", restart_hint))

    def _open_accounts(self):
        """B3（实现方案 §5）：账户管理窗——本地列表/添加/删除/改密，不动协议与注册策略。

        数据来自共享端进程内的 AccountManager（与观看端注册同一份 accounts.json），
        因此 closed/bootstrap 策略下也能加账户；PBKDF2 在本地一次性计算（约 0.2s），
        期间面板短暂卡顿可接受。

        ui-005（2026-09-18）：单例守卫——连点会叠多个 Toplevel，旧窗列表陈旧、两边
        操作互相覆盖；已开着则前置聚焦并直接返回。
        """
        win = getattr(self, "_accounts_win", None)
        if win is not None:
            try:
                if win.winfo_exists():
                    win.deiconify()
                    win.lift()
                    win.focus_force()
                    return
            except Exception:
                pass
            self._accounts_win = None
        accounts = getattr(self.runtime, "accounts", None)
        if accounts is None:
            messagebox.showwarning(APP_NAME, "共享端尚未初始化账户表，稍后再试")
            return
        win = tk.Toplevel(self.root)
        self._accounts_win = win   # ui-005：单例引用（关闭后自动失效）
        win.title("账户管理")
        win.configure(bg=COL_BG)
        win.geometry("460x300")
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        tree = ttk.Treeview(win, columns=("created", "last"),
                            show="tree headings", height=10)
        tree.heading("#0", text="用户名")
        tree.heading("created", text="注册时间")
        tree.heading("last", text="最近登录")
        tree.column("#0", width=130, stretch=False)
        tree.column("created", width=150, stretch=False)
        tree.column("last", width=150, stretch=False)
        tree.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        hint = tk.Label(win, text="", bg=COL_BG, fg=COL_DIM,
                        font=("Microsoft YaHei", 8), justify="left")
        hint.pack(anchor="w", padx=10)

        def refresh():
            tree.delete(*tree.get_children())
            rows = accounts.list_users()
            for user, created, last in rows:
                tree.insert("", "end", text=user,
                            values=(created or "-", last or "-"))
            if not rows:
                hint.configure(text="（无账户；准入开启时观看端将无法登录）")
            else:
                # ui-016（2026-09-18）：加回账户后必须擦掉"无账户"提示——
                # 旧实现只写不擦，提示与列表相互矛盾
                hint.configure(text="")
            return rows

        def selected_user():
            sel = tree.selection()
            if not sel:
                messagebox.showwarning("账户管理", "请先在列表中选择一个账户", parent=win)
                return None
            return tree.item(sel[0])["text"]

        def add_account():
            user = simpledialog.askstring("添加账户", "用户名：", parent=win)
            if not user:
                return
            pw = simpledialog.askstring("添加账户", "密码：", show="*", parent=win)
            if not pw:
                return
            pw2 = simpledialog.askstring("添加账户", "再次输入密码确认：",
                                         show="*", parent=win)
            if pw2 is None:
                return
            if pw != pw2:
                messagebox.showerror("添加账户", "两次输入的密码不一致", parent=win)
                return
            ok, msg = accounts.register(user.strip(), pw)
            (messagebox.showinfo if ok else messagebox.showerror)(
                "添加账户", msg, parent=win)
            refresh()

        def del_account():
            user = selected_user()
            if user is None:
                return
            if accounts.count() <= 1:
                # 防自锁：删掉唯一账户后若准入开着，没人能再登录
                # auth-010（2026-09-18）：旧文案与实际后果相反——bootstrap 策略下旧
                # 实现会静默重开"任意人可注册"窗口。现在删除不再重开窗口，文案补一句
                # 说明（策略读取失败时仅显示原提示）。
                extra = ""
                try:
                    if register_policy(self._section(self._host_cfg(), "auth")) == "bootstrap":
                        extra = "\n（bootstrap 策略下自助注册窗口不会重开，之后需在共享端添加账户）"
                except Exception:
                    extra = ""
                if not messagebox.askyesno(
                        "删除账户",
                        "这是当前唯一账户。删除后若「账户准入」开启，"
                        "所有观看端都将无法登录。确定删除？%s" % extra, parent=win):
                    return
                if not messagebox.askyesno(
                        "删除账户", "再次确认：真的要删除账户 %s 吗？" % user,
                        parent=win):
                    return
            elif not messagebox.askyesno("删除账户", "确定删除账户 %s？" % user,
                                        parent=win):
                return
            ok, msg = accounts.remove(user)
            (messagebox.showinfo if ok else messagebox.showerror)(
                "删除账户", msg, parent=win)
            refresh()

        def chg_password():
            user = selected_user()
            if user is None:
                return
            pw = simpledialog.askstring("修改密码", "为 %s 设置新密码：" % user,
                                        show="*", parent=win)
            if not pw:
                return
            pw2 = simpledialog.askstring("修改密码", "再次输入新密码确认：",
                                         show="*", parent=win)
            if pw2 is None:
                return
            if pw != pw2:
                messagebox.showerror("修改密码", "两次输入的密码不一致", parent=win)
                return
            ok, msg = accounts.set_password(user, pw)
            (messagebox.showinfo if ok else messagebox.showerror)(
                "修改密码", msg, parent=win)
            refresh()

        btns = tk.Frame(win, bg=COL_BG)
        btns.pack(fill="x", padx=10, pady=(2, 10))
        for text, fn in (("添加", add_account), ("删除", del_account),
                         ("改密", chg_password)):
            self._button(btns, text, fn).pack(side="left", padx=(0, 8))
        self._button(btns, "关闭", win.destroy).pack(side="right")
        refresh()

    def _on_export_diag(self):
        """B2（第 134 条）：一键导出诊断包（脱敏 config + 近两日日志 + 运行时概况）。

        ui-008（2026-09-18）：压缩（最多 ~60MB 日志）改到工作线程——旧实现在 Tk
        主线程同步压缩，界面冻结数秒；`data_dir()` 为 None（无可写数据目录）时旧
        实现拼接路径抛 TypeError，这里给明确提示；加可重入守卫防连点叠导出。
        """
        if getattr(self, "_diag_exporting", False):
            messagebox.showinfo(APP_NAME, "上一次诊断包仍在导出中，请稍候", parent=self.root)
            return
        extra = []
        try:
            snap = self.runtime.get_snapshot()
        except Exception:
            snap = {}
        clients = snap.get("clients")
        if clients is not None:
            extra.append("在线观众数: %d" % len(clients))
        ns = snap.get("net_status") or {}
        state = ns.get("state", "unknown")
        if state == "ok":
            extra.append("最近公网自检: 通过（%s）" % ns.get("detail", ""))
        elif state == "fail":
            extra.append("最近公网自检: 失败（%s）" % ns.get("detail", ""))
        else:
            extra.append("最近公网自检: 未运行")
        base_dir = data_dir()
        if not base_dir:
            messagebox.showwarning(APP_NAME, "找不到可写的数据目录，无法导出诊断包",
                                   parent=self.root)
            return
        logs_dir = os.path.join(base_dir, "logs")
        cfg = self.runtime.cfg
        extra_text = "\n".join(extra)
        self._diag_exporting = True
        try:
            self._set_status("正在导出诊断包…")
        except Exception:
            pass

        def _work():
            try:
                report = diag.build_report_text(cfg, extra_text=extra_text)
                # 计划 §4.2.2：zip 落到 %LOCALAPPDATA%\TeamVision\logs\
                path = diag.build_report_zip(logs_dir, config=cfg,
                                             logs_dir=logs_dir,
                                             extra_text=extra_text,
                                             pid=os.getpid())
                self._post_ui(lambda: self._finish_export(path, report, None))
            except Exception as e:      # 工作线程异常也必须回到主线程反馈
                self._post_ui(lambda: self._finish_export(None, "", e))

        threading.Thread(target=_work, name="diag-export", daemon=True).start()

    def _finish_export(self, path, report, error):
        """diag 导出完成（主线程）：弹结果并把报告文本复制到剪贴板。"""
        self._diag_exporting = False
        if error is not None:
            log.warning("诊断包导出失败: %s", error)
            messagebox.showerror("导出失败", "诊断包导出失败：%s" % error,
                                 parent=self.root)
            return
        copied = ""
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(report)
            copied = "\n\n报告文本已同时复制到剪贴板。"
        except Exception:
            pass
        messagebox.showinfo(
            "导出成功",
            "诊断包已保存到：\n%s%s\n\n配置中的凭据/用户名已自动脱敏；"
            "日志仍可能包含 IP、地址等信息，请只发给信任的支持者。"
            % (path, copied), parent=self.root)

    def _on_quit(self):
        # ui-017：退出时摘除日志 handler（并关闭队列），避免全局 logger 长期持有
        try:
            logging.getLogger(logger_mod.LOGGER_NAME).removeHandler(self._log_handler)
            self._log_handler.close()
        except Exception:
            pass
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
        # B2（第 134 条）：公网自检结论行。ts==0 表示尚未跑过
        # （未配置 public_addr 或 frpc 未启动时 _start_tunnel_selfcheck 直接跳过）。
        ns = snap.get("net_status") or {}
        ts = ns.get("ts") or 0.0
        # net-023（2026-09-18）：结论带时效——隧道重启/掉线后旧结论不可信；
        # 自检每 10 分钟自动复检，超过 15 分钟的结论额外标注
        _stale = bool(ts) and (time.time() - float(ts)) > 900
        if ts and ns.get("state") == "ok":
            self._net_status_label.configure(
                text="公网自检：通过 ✅ %s%s" % (
                    time.strftime("%H:%M", time.localtime(ts)),
                    "（结论已超 15 分钟，隧道可能已变化）" if _stale else ""),
                fg=COL_ONLINE)
        elif ts:
            self._net_status_label.configure(
                text="公网自检：失败 — %s（详见运行日志）" % ns.get("detail", ""),
                fg=COL_OFFLINE)
        else:
            self._net_status_label.configure(
                text="公网自检：未运行（配置 public_addr 并启动 frpc 后自动执行）",
                fg=COL_DIM)
        # 客户端列表（C18，2026-09-11：增量刷新）
        # 原实现每 500ms `tree.delete(*tree.get_children())` 后整表重插——行选中丢失、
        # 滚动位置被拽回顶部、客户端多时长期可见闪烁。第 115 条/C4 已把**观看端**的两棵树
        # 改成增量刷新，这张共享端客户端表当时没同步。现在行数与各行值都没变时完全不动，
        # 只有值变化的行走 tree.item 更新。
        tree = self._client_tree
        rows = []
        for c in snap["clients"]:
            rows.append((
                c.get("username") or "—",
                c["addr"],
                self._fmt_uptime(c["uptime_s"]),
                "%.0f KB/s" % c["rate_kbps"],
                "%.1f MB" % c["total_mb"],
            ))
        cached = getattr(self, "_client_rows_cache", None)
        if cached is None or len(cached) != len(rows):
            try:
                y_top = tree.yview()[0]
            except Exception:
                y_top = 0.0
            tree.delete(*tree.get_children())
            for i, values in enumerate(rows):
                tree.insert("", "end", iid=str(i), values=values)
            try:
                if y_top:
                    tree.yview_moveto(y_top)
            except Exception:
                pass
        else:
            for i, values in enumerate(rows):
                if values != cached[i]:
                    try:
                        tree.item(str(i), values=values)
                    except Exception:
                        pass
        self._client_rows_cache = rows
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
