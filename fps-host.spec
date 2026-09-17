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
    # 阶段 B1（优化方案 2026-09-17）：显式排除用不到的重模块，防止在"非干净"构建机上
    # 被依赖图意外吸入（当前实测环境中它们本就不在图中，收益≈0，属防御性配置）。
    # tkinter 绝不能出现在这里——host_ui.py 的 UI 全靠它。
    excludes=[
        'unittest', 'doctest', 'pydoc', 'test', 'lib2to3',
        'distutils', 'setuptools', 'pkg_resources', 'pip', 'wheel',
        'IPython', 'jupyter', 'pytest',
        'matplotlib', 'scipy', 'pandas', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'wx',
    ],
    noarchive=False,
    optimize=0,
)

# 阶段 B1（优化方案 2026-09-17）：剔除明确用不到的大二进制（单端省约 39MB，实测已验证）。
# - cv2\opencv_videoio_ffmpeg*.dll（~31MB）：视频编解码走 PyAV，图像路径只用
#   imencode/imdecode/resize/cvtColor，从不创建 VideoCapture/VideoWriter；OpenCV 对该
#   DLL 是首次使用 FFMPEG 视频后端时才惰性加载（删除后 imencode/imdecode 全链路实测可用）。
# - PIL\_avif*.pyd / _webp*.pyd（~8MB）：不处理 AVIF/WebP 图片；Pillow 插件机制允许对应
#   pyd 缺失（仅该格式不可用，Image.init() 全插件注册实测不受影响）。
_B1_PRUNE = ('cv2\\opencv_videoio_ffmpeg', 'pil\\_avif', 'pil\\_webp')
a.binaries = [b for b in a.binaries
              if not any(p in b[0].lower().replace('/', '\\') for p in _B1_PRUNE)]

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
