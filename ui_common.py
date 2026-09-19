# -*- coding: utf-8 -*-
"""UI 基建单点（第 136 条，实现方案 §8 C3）：excepthook / 色板 / ttk 风格 / 跨线程调度。

host.py、viewer.py、host_ui.py 共用，消除历史上"两份语义各跑各的"的漂移：
- install_excepthooks()：主线程 + 工作线程全局兜底（合并 viewer 第 79/100 条与
  host 第 15 条语义：写文件日志、不弹死、KeyboardInterrupt 透传、可选父窗口）；
- post_to_ui(root, fn)：工作线程回调调度回 Tk 主线程；
- COL_*：深色主题色板**单一来源**（viewer 多出的连接黄/活动行紫已并入；两端同名色
  历史上取值逐字节一致，无需仲裁）；
- build_style(root)：clam 深色 ttk 风格统一配置（host 基础控件 + viewer Panel.* 并集）。

依赖仅 tkinter + logger。tkinter 一律在函数体内延迟导入：host --console 模式即便
top-level import 本模块也零 tkinter 开销（延续第 133 条 splash 提前原则）。
"""

import sys
import threading

import logger

log = logger.get_logger()

#: 崩溃兜底弹窗文案（两端统一）
APP_ERROR_TEXT = "程序遇到错误，已写入 logs 目录日志文件"

# -------------------------------- 色板（单一来源） --------------------------------

COL_BG = "#14141f"          # 窗口背景
COL_CARD = "#1e1e2e"        # 卡片/输入区背景
COL_CTRL = "#2a2a3c"        # 控件背景
COL_FG = "#e6e6ef"          # 主要前景文字
COL_DIM = "#9a9ab0"         # 次要文字
COL_ACCENT = "#7c5cff"      # 强调色（按钮/选中高亮）
COL_ONLINE = "#4ade80"      # 在线绿
COL_CONNECTING = "#fbbf24"  # 连接中黄（观看端专属，第 136 条并入）
COL_OFFLINE = "#f87171"     # 断开红
COL_ACTIVE_ROW = "#2a2440"  # 活动频道行高亮背景（观看端专属，第 136 条并入）

#: build_style 的 palette 键 → 常量
PALETTE = {
    "bg": COL_BG,
    "card": COL_CARD,
    "ctrl": COL_CTRL,
    "fg": COL_FG,
    "dim": COL_DIM,
    "accent": COL_ACCENT,
    "online": COL_ONLINE,
    "connecting": COL_CONNECTING,
    "offline": COL_OFFLINE,
    "active_row": COL_ACTIVE_ROW,
}

# --------------------------- 全局未捕获异常兜底 ---------------------------


def install_excepthooks(app_name, get_root=None):
    """安装主线程 + 工作线程未捕获异常兜底（合并 host 第 15 条与 viewer 第 79/100 条）。

    语义并集：线程钩子只写日志不弹模态框（SystemExit 跳过）；主线程钩子写日志后弹
    一次友好提示框，get_root() 提供 Tk 根窗口时以其为父窗（viewer 行为），否则无父
    （host 行为）；KeyboardInterrupt 透传给解释器默认处理。

    **必须由入口函数调用、不得在 import 期执行**（第 79 条：import 期安装会劫持
    测试套件等纯 import 进程，modal 框会卡死整套件）。
    """

    def _thread_hook(args):
        name = getattr(getattr(args, "thread", None), "name", "thread")
        if getattr(args, "exc_type", None) is SystemExit:
            return
        log.error("线程 %s 未捕获异常", name,
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    def _main_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.exception("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))
        parent = None
        if get_root is not None:
            try:
                root = get_root()
                if root is not None and root.winfo_exists():
                    parent = root
            except Exception:
                parent = None
        try:
            from tkinter import messagebox
            messagebox.showerror(app_name, APP_ERROR_TEXT, parent=parent)
        except Exception:
            try:
                print(APP_ERROR_TEXT)
            except Exception:
                pass

    threading.excepthook = _thread_hook
    sys.excepthook = _main_hook


# ------------------------------ 跨线程 UI 调度 ------------------------------


def post_to_ui(root, fn):
    """把回调调度回 Tk 主线程执行；投递失败（窗口已销毁等）只记一条告警。

    ui-004（2026-09-18）：旧实现在 root.after 失败时**就在调用线程（工作线程）里
    直接执行 fn**——fn 是控件操作，工作线程碰 Tk 属未定义行为；且 docstring 宣称的
    "主线程执行"前提不成立，退出竞态下真正的失败还会被静默吞掉。Tk 的 after 在
    mainloop 启动前也能排队，失败只代表窗口已销毁——此时不该碰控件。

    注意：**viewer._post_ui 刻意不走本函数**——第 108 条要求 viewer 工作线程只入队、
    由 poll 在主线程排空（退出瞬间 root.after 投递会撞正在销毁的 Tcl 解释器，是
    解释器级崩溃，except 兜不住）。本函数服务 host_ui 这类无退出竞态的简单场景。
    """
    try:
        root.after(0, fn)
    except Exception as e:
        try:
            import logger
            logger.get_logger().debug("UI 回调投递失败（窗口可能已销毁），已丢弃: %s", e)
        except Exception:
            pass


# ------------------------------ ttk 深色风格 ------------------------------


def build_style(root, palette=None, include_base=True):
    """按深色主题配置 ttk 全局风格（host 基础控件 + viewer Panel.* 的并集）。

    palette 为 PALETTE 的可选覆盖字典（键同名）；未覆盖项使用本模块常量。
    同名色两端历史取值完全一致，因此默认路径下两端外观逐字节不变（§8 验收：
    外观截图比对无回归）。

    ui-018（2026-09-18）：`include_base=False` 时只应用 Panel.* 命名风格——viewer
    面板收拢前没有基础 Treeview/TCombobox 配置，收拢后被动继承 host 的
    lightcolor/darkcolor，与 §8"外观逐字节不变"不符。
    """
    from tkinter import ttk

    p = dict(PALETTE)
    if palette:
        p.update(palette)

    style = ttk.Style(root)
    style.theme_use("clam")

    # ---- 基础控件深色（原 host_ui._build_style；ui-018 起可关闭）----
    if include_base:
        _apply_base_style(style, p)

    # ---- Panel.* 命名风格（原 viewer._build_panel 内联块，逐行照搬）----
    _apply_panel_style(style, p)
    return style


def _apply_base_style(style, p):
    style.configure("Treeview",
                    background=p["ctrl"], foreground=p["fg"], fieldbackground=p["ctrl"],
                    bordercolor=p["card"], lightcolor=p["card"], darkcolor=p["card"])
    style.configure("Treeview.Heading",
                    background=p["card"], foreground=p["dim"], relief="flat")
    style.map("Treeview", background=[("selected", p["accent"])])
    style.map("Treeview.Heading", background=[("active", p["card"])])
    style.configure("TCombobox",
                    fieldbackground=p["ctrl"], background=p["ctrl"], foreground=p["fg"],
                    arrowcolor=p["fg"], bordercolor=p["card"], lightcolor=p["card"],
                    darkcolor=p["card"], selectbackground=p["ctrl"],
                    selectforeground=p["fg"])


def _apply_panel_style(style, p):
    style.configure("Panel.TFrame", background=p["bg"])
    style.configure("Card.TFrame", background=p["card"])
    style.configure("Panel.TLabel", background=p["bg"], foreground=p["fg"])
    style.configure("Card.TLabel", background=p["card"], foreground=p["fg"])
    style.configure("PanelDim.TLabel", background=p["bg"], foreground=p["dim"])
    style.configure("CardDim.TLabel", background=p["card"], foreground=p["dim"])
    style.configure("Title.TLabel", background=p["bg"], foreground=p["fg"],
                    font=("Microsoft YaHei", 14, "bold"))
    style.configure("Subtitle.TLabel", background=p["bg"], foreground=p["dim"],
                    font=("Microsoft YaHei", 8))
    style.configure("Section.TLabel", background=p["card"], foreground=p["dim"],
                    font=("Microsoft YaHei", 9, "bold"))
    style.configure("Panel.TButton", background=p["ctrl"], foreground=p["fg"],
                    bordercolor=p["ctrl"], padding=(10, 4))
    style.map("Panel.TButton",
              background=[("active", "#3a3a52"), ("pressed", "#33334a")])
    style.configure("Accent.TButton", background=p["accent"],
                    foreground="#ffffff", bordercolor=p["accent"], padding=(12, 4))
    style.map("Accent.TButton",
              background=[("active", "#8a70ff"), ("pressed", "#6b4de8")])
    style.configure("Panel.TEntry", fieldbackground=p["ctrl"], foreground=p["fg"],
                    bordercolor=p["ctrl"], lightcolor=p["ctrl"],
                    darkcolor=p["ctrl"], insertcolor=p["fg"])
    style.configure("Panel.Treeview", background=p["card"], fieldbackground=p["card"],
                    foreground=p["fg"], bordercolor=p["card"], rowheight=24,
                    font=("Microsoft YaHei", 9))
    style.map("Panel.Treeview",
              background=[("selected", "#4a3f8a")],
              foreground=[("selected", "#ffffff")])
    style.configure("Panel.Treeview.Heading", background=p["ctrl"],
                    foreground=p["fg"], bordercolor=p["ctrl"], relief="flat",
                    font=("Microsoft YaHei", 9, "bold"))
    style.map("Panel.Treeview.Heading", background=[("active", p["ctrl"])])
    style.configure("Panel.Horizontal.TScale", background=p["accent"],
                    troughcolor="#3a3a52", bordercolor=p["card"],
                    lightcolor=p["accent"], darkcolor=p["accent"])
    style.configure("Panel.TCheckbutton", background=p["card"], foreground=p["fg"])
    style.map("Panel.TCheckbutton",
              background=[("active", p["card"])], foreground=[("active", p["fg"])])
    return style
