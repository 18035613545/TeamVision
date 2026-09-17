# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

# 收集 PyAV（FFmpeg DLL 位于 av.libs）：H.264 视频解码
_av_datas, _av_binaries, _av_hiddenimports = collect_all('av')

# 启动图 splash.png 内嵌进 exe：splash.py 冻结时从 sys._MEIPASS/assets 读取，
# 程序自带启动图，不再依赖 exe 同级的 assets 目录。
_splash_datas = [('assets/splash.png', 'assets')]

a = Analysis(
    ['viewer.py'],
    pathex=[],
    binaries=_av_binaries,
    datas=_av_datas + _splash_datas,
    hiddenimports=_av_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 阶段 B1（优化方案 2026-09-17）：同 fps-host.spec——排除用不到的重模块做防御；
    # viewer.py 的 UI 同样依赖 tkinter，不得排除。
    excludes=[
        'unittest', 'doctest', 'pydoc', 'test', 'lib2to3',
        'distutils', 'setuptools', 'pkg_resources', 'pip', 'wheel',
        'IPython', 'jupyter', 'pytest',
        'matplotlib', 'scipy', 'pandas', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'wx',
    ],
    noarchive=False,
    optimize=0,
)

# 阶段 B1（优化方案 2026-09-17）：剔除明确用不到的大二进制，理由见 fps-host.spec——
# cv2 的 FFMPEG videoio 后端（~31MB）与 Pillow 的 AVIF/WebP 插件（~8MB）。
_B1_PRUNE = ('cv2\\opencv_videoio_ffmpeg', 'pil\\_avif', 'pil\\_webp')
a.binaries = [b for b in a.binaries
              if not any(p in b[0].lower().replace('/', '\\') for p in _B1_PRUNE)]

pyz = PYZ(a.pure)

# 第 131 条：由 onefile 改为 onedir（COLLECT），理由见 fps-host.spec——
# 去掉"解压到 %TEMP% 再从临时目录执行"的启发式误报链，并显著缩短冷启动。
# 仍然禁止 UPX。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='fps-viewer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='version_info.txt',
    icon=['assets\\app.ico'],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='fps-viewer',
)
