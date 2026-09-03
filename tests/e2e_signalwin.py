# -*- coding: utf-8 -*-
"""E2E 信号窗口：全屏纯色 + 缓慢移动白色方块（overrideredirect，独立进程）。

由 tests/e2e_multiview.py 以子进程启动：`python e2e_signalwin.py <name> [<color>]`
窗口覆盖逻辑屏幕（DPI-unaware，与 win32 逻辑坐标一致），白色方块按 (2,1)
px/frame 移动并在边界环绕，保证画面持续变化（规避静止门控）。
"""
import sys
import tkinter as tk

COLORS = {
    "green": "#00e000",
    "red": "#e00000",
    "blue": "#0040e0",
}

BLOCK = 260  # 白色方块边长（逻辑 px）


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "green"
    fill = COLORS.get(name, COLORS["green"])

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", False)
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry("%dx%d+0+0" % (sw, sh))
    root.configure(bg=fill)

    cv = tk.Canvas(root, width=sw, height=sh, bg=fill,
                   highlightthickness=0, bd=0)
    cv.pack()
    bx, by = 40, 60
    blk = cv.create_rectangle(bx, by, bx + BLOCK, by + BLOCK,
                              fill="white", outline="white")

    def tick():
        nonlocal bx, by
        bx = (bx + 2) % (sw + BLOCK) - BLOCK
        by = (by + 1) % (sh + BLOCK) - BLOCK
        cv.coords(blk, bx, by, bx + BLOCK, by + BLOCK)
        root.after(16, tick)

    root.after(16, tick)
    root.mainloop()


if __name__ == "__main__":
    main()
