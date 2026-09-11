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

# 第 131 条：由 onefile 改为 onedir（COLLECT）。
# onefile 运行时会把自己解压到 %TEMP%\_MEIxxxxxx 再从临时目录执行子进程——「写入临时
# 目录后执行」是杀软/EDR 的经典启发式链，也是本项目 README 里自己记录的"偶有误报"来源；
# 每次启动还要解压 ~100MB，冷启动明显变慢。onedir 直接就地加载 DLL 与 PYZ，不再有
# 自解压与临时目录执行行为，代价只是产物从单文件变成目录（安装包负责摆好目录）。
# 仍然禁止 UPX：加壳后的 PE 同属常见误报特征。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='fps-host',
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
    name='fps-host',
)
