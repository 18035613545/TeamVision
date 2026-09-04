# -*- coding: utf-8 -*-
"""启动画面（Splash）模块。

提供 show_splash(root=None)：
- root 为 None 时：创建独立无边框置顶窗口，居中显示启动图/产品名文字，
  进入 mainloop，约 2 秒后自动销毁并退出（阻塞约 2 秒）；
- root 非 None 时：在既有 root 上创建置顶无边框 Toplevel 启动画面，
  用 root.after 定时销毁，不阻塞主程序。

任何异常静默降级，不影响主程序。
"""

import os
import tkinter as tk

import common
from PIL import Image, ImageTk

#: 启动画面尺寸
SPLASH_SIZE = (480, 300)
#: 显示时长（毫秒）
SPLASH_MS = 2000
#: 深色主题配色
_BG = "#14141f"
_FG = "#e6e6ef"
_DIM = "#8a8a9a"


def _splash_path():
    """返回启动画面图片路径（程序资源目录 assets/splash.png）。"""
    return os.path.join(common.exe_dir(), "assets", "splash.png")


def _brand_text():
    """返回产品名文字（common 常量缺失时回退默认值）。"""
    name = getattr(common, "APP_NAME", "队友视野")
    ver = getattr(common, "APP_VERSION", "1.0.0")
    return "%s v%s" % (name, ver)


def _author_text():
    """返回作者署名文字（common.APP_AUTHOR 缺失时回退默认值）。"""
    return getattr(common, "APP_AUTHOR", "by 西琳")


def _center(win):
    """将窗口居中显示（指定尺寸）。"""
    win.update_idletasks()
    w, h = SPLASH_SIZE
    x = max(0, (win.winfo_screenwidth() - w) // 2)
    y = max(0, (win.winfo_screenheight() - h) // 2)
    win.geometry("%dx%d+%d+%d" % (w, h, x, y))


def _build(win):
    """填充启动画面内容：图片存在则等比缩放进窗口，否则显示产品名文字。"""
    win.configure(bg=_BG)
    path = _splash_path()
    if os.path.exists(path):
        try:
            img = Image.open(path)
            img.thumbnail(SPLASH_SIZE, Image.BILINEAR)
            photo = ImageTk.PhotoImage(img)
            label = tk.Label(win, image=photo, bg=_BG)
            label.image = photo  # 保持引用，防止被垃圾回收
            label.pack(fill="both", expand=True)
            return
        except Exception:
            pass
    tk.Label(win, text=_brand_text(), bg=_BG, fg=_FG,
             font=("Microsoft YaHei", 22, "bold")).pack(expand=True, pady=(0, 4))
    tk.Label(win, text=_author_text(), bg=_BG, fg=_DIM,
             font=("Microsoft YaHei", 11)).pack(pady=(0, 24))


def _show_on_root(root):
    """在既有 root 上创建置顶无边框 Toplevel，2 秒后自动销毁，不阻塞。"""
    win = None
    try:
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        _build(win)
        _center(win)
        root.after(SPLASH_MS, win.destroy)
    except Exception:
        # 第 86 条：配置中途抛异常时，已建好的无边框置顶 Toplevel 既未销毁也没排定销毁，
        # 会留下一个关不掉（无关闭按钮、不在任务栏、置顶）的幽灵窗口，只能杀进程。
        # 与 show_splash(root=None) 路径一致：失败即销毁，静默降级不留残窗。
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass


def show_splash(root=None):
    """显示启动画面。

    root 为 None 时：创建独立无边框置顶窗口，居中显示启动图/产品名，
    进入 mainloop，约 2 秒后自动销毁并退出（阻塞约 2 秒）；
    root 非 None 时：在既有 root 上创建 Toplevel 启动画面，不阻塞。
    任何异常静默降级，不影响主程序。
    """
    if root is not None:
        _show_on_root(root)
        return
    win = None
    try:
        win = tk.Tk()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        _build(win)
        _center(win)
        win.after(SPLASH_MS, win.destroy)
        win.mainloop()
    except Exception:
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
