# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

# 收集 PyAV（FFmpeg DLL 位于 av.libs）：H.264 硬件/软件视频编码
_av_datas, _av_binaries, _av_hiddenimports = collect_all('av')

# 启动图 splash.png 内嵌进 exe：splash.py 冻结时从 sys._MEIPASS/assets 读取，
# 程序自带启动图，不再依赖 exe 同级的 assets 目录。
_splash_datas = [('assets/splash.png', 'assets')]

a = Analysis(
    ['host.py'],
    pathex=['vendor'],
    binaries=_av_binaries,
    datas=_av_datas + _splash_datas,
    hiddenimports=['dxcam'] + _av_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='fps-host',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='version_info.txt',
    icon=['assets\\app.ico'],
)
