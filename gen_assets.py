# -*- coding: utf-8 -*-
"""品牌资源生成脚本（基于 Pillow）。

用法（cwd 为本文件所在目录，即项目根目录）：
    F:\conda\envs\fps-screen\python.exe gen_assets.py

运行后生成：
    assets\app.ico      产品图标（16/32/48/64/128/256 多尺寸 .ico）
    assets\splash.png   启动画面背景图（480x300）
"""

import os

from PIL import Image, ImageDraw, ImageFont

import common

#: 输出目录（本脚本所在目录下的 assets）
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

#: 品牌色
BRAND_COLOR_TOP = (124, 92, 255)    # #7c5cff 浅紫
BRAND_COLOR_BOTTOM = (91, 33, 182)  # #5b21b6 深紫
SPLASH_BG_TOP = (20, 20, 31)        # #14141f
SPLASH_BG_BOTTOM = (30, 30, 46)     # #1e1e2e
TEXT_PURPLE = (167, 139, 250)       # #a78bfa
WHITE = (255, 255, 255, 255)


def _diag_gradient(size, top_color, bottom_color):
    """生成 size=(w, h) 的对角线渐变 RGBA 图像（左上 -> 右下）。"""
    w, h = size
    img = Image.new("RGBA", size)
    px = img.load()
    for y in range(h):
        for x in range(w):
            t = (x / max(w - 1, 1) + y / max(h - 1, 1)) / 2.0
            r = int(top_color[0] + (bottom_color[0] - top_color[0]) * t)
            g = int(top_color[1] + (bottom_color[1] - top_color[1]) * t)
            b = int(top_color[2] + (bottom_color[2] - top_color[2]) * t)
            px[x, y] = (r, g, b, 255)
    return img


def _rounded_mask(size, radius):
    """生成圆角矩形的 alpha 蒙版（L 模式）。"""
    mask = Image.new("L", size, 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return mask


def _draw_scope(draw, cx, cy, radius, width, color=WHITE):
    """以 (cx, cy) 为中心绘制简约"准星/眼睛"瞄准镜图形（白色）。"""
    # 外圈圆环（镜头/眼睛外轮廓）
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                 outline=color, width=width)
    # 内圈小圆环（瞳孔）
    inner = radius * 0.55
    draw.ellipse([cx - inner, cy - inner, cx + inner, cy + inner],
                 outline=color, width=max(1, int(width * 0.8)))
    # 中心实心点（靶心/瞳孔中心）
    dot = radius * 0.18
    draw.ellipse([cx - dot, cy - dot, cx + dot, cy + dot], fill=color)
    # 四向准星短线（瞄准线）
    tip = radius * 1.28
    base = radius * 1.05
    w2 = max(1, int(width * 0.8))
    draw.line([cx - tip, cy, cx - base, cy], fill=color, width=w2)
    draw.line([cx + base, cy, cx + tip, cy], fill=color, width=w2)
    draw.line([cx, cy - tip, cx, cy - base], fill=color, width=w2)
    draw.line([cx, cy + base, cx, cy + tip], fill=color, width=w2)


def _make_icon(size):
    """生成单个尺寸的图标图像：深紫圆角方形渐变底 + 白色准星图形。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = _diag_gradient((size, size), BRAND_COLOR_TOP, BRAND_COLOR_BOTTOM)
    mask = _rounded_mask((size, size), radius=int(size * 0.22))
    img.paste(grad, (0, 0), mask)
    draw = ImageDraw.Draw(img)
    _draw_scope(draw, size / 2, size / 2,
                radius=size * 0.30, width=max(1, int(size * 0.05)))
    return img


def gen_icon():
    """生成 assets\app.ico，一次写出 16/32/48/64/128/256 多尺寸。"""
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    images = [_make_icon(w) for w, _ in sizes]
    path = os.path.join(ASSETS_DIR, "app.ico")
    # 主图为最大尺寸（256），其余尺寸放入 append_images，由 sizes 指定全部尺寸
    images[-1].save(path, format="ICO", sizes=sizes, append_images=images[:-1])
    return path


def gen_splash():
    """生成 assets\splash.png：480x300 深色渐变底 + 居中品牌图形 + 产品名。"""
    w, h = 480, 300
    img = _diag_gradient((w, h), SPLASH_BG_TOP, SPLASH_BG_BOTTOM)
    draw = ImageDraw.Draw(img)

    # 居中品牌图形（与图标同风格）
    _draw_scope(draw, w / 2, 92, radius=52, width=6)

    def _font(size, bold=True):
        """加载微软雅黑（粗体）字体，失败时回退默认字体。"""
        try:
            name = "msyhbd.ttc" if bold else "msyh.ttc"
            return ImageFont.truetype(os.path.join("C:/Windows/Fonts", name), size)
        except OSError:
            return ImageFont.load_default()

    def _center_text(y, text, font, fill):
        """在 y 处水平居中绘制文本。"""
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        x = (w - tw) / 2 - bbox[0]
        draw.text((x, y), text, font=font, fill=fill)

    # 主标题：队友视野（白色，约 48px 粗体）
    _center_text(170, "队友视野", _font(48), WHITE)
    # 副标题：SakuraVision v{版本}（浅紫，约 20px，版本取自 common.APP_VERSION）
    _center_text(235, "SakuraVision v%s" % common.APP_VERSION, _font(20, bold=False), TEXT_PURPLE)

    path = os.path.join(ASSETS_DIR, "splash.png")
    img.save(path, format="PNG")
    return path


def main():
    """生成全部品牌资源并打印结果。"""
    os.makedirs(ASSETS_DIR, exist_ok=True)
    icon_path = gen_icon()
    splash_path = gen_splash()
    print("品牌资源生成完成：")
    print(" ", icon_path)
    print(" ", splash_path)


if __name__ == "__main__":
    main()
