@echo off
chcp 65001 >nul
rem 第 72 条：切到脚本自身目录，确保 fps-host.spec / fps-viewer.spec / frpc.exe /
rem dist\（及 spec 引用的 version_info.txt、assets\app.ico）等相对路径不随调用方
rem 当前目录（或 CI 工作目录）漂移而从错误的目录树取文件。
cd /d "%~dp0"
echo ============================================
echo   共享端/观看端 一键打包脚本 (PyInstaller)
echo ============================================
echo.

rem ===== [1/4] 检查 conda 并激活环境 =====
echo [1/4] 检查 conda 环境...
where conda >nul 2>nul
if errorlevel 1 (
    echo.
    echo [错误] 未检测到 conda，请先安装 Anaconda 或 Miniconda 并加入 PATH。
    echo.
    if not defined CI if not defined NOPAUSE pause
    exit /b 1
)
echo 检测到 conda，正在激活环境 fps-screen ...
call conda activate fps-screen
if errorlevel 1 goto :failed
rem 第 73 条：activate 可能「返回 0 却没真正切换」（conda 未对 cmd 初始化时），
rem 二次确认 CONDA_DEFAULT_ENV，避免在 base/系统环境里打包出缺依赖的 exe。
if /i not "%CONDA_DEFAULT_ENV%"=="fps-screen" (
    echo [错误] conda 环境未切到 fps-screen（当前 CONDA_DEFAULT_ENV=%CONDA_DEFAULT_ENV%）。
    echo        为避免在 base/系统环境打包出缺 cv2/av/numpy 的 exe，已中止。
    echo        请先执行：conda init cmd.exe ，重开终端后重试。
    goto :failed
)
echo 环境 fps-screen 激活成功。
echo.

rem ===== [2/4] 安装 PyInstaller（阿里云镜像仅本次命令生效，不改全局 pip 配置）=====
echo [2/4] 从阿里源安装 PyInstaller 到当前环境 ...
rem 第 73 条：原 `pip config set global.index-url ...` 会永久修改本机全局 pip 配置
rem （影响所有项目，超出本项目范围）；改用 -i/--trusted-host 只对这一条命令生效。
rem python -m pip 确保装进上方已 conda activate 并校验过的 fps-screen 环境。
python -m pip install -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com pyinstaller
if errorlevel 1 goto :failed
echo PyInstaller 安装完成。
echo.

rem ===== [3/4] 打包共享端（windowed 无黑窗 + 可选 dxcam 采集后端）=====
echo [3/4] 打包共享端 fps-host.exe ...
rem 第 68 条：直接用仓库已提交的 fps-host.spec 打包，不再用 pyinstaller CLI 传参。
rem CLI 传参会重新生成并覆盖 .spec，把其中的 collect_all('av') 抹掉——av.libs 的
rem FFmpeg DLL 能否打进去就全看未固定版本的 pyinstaller-hooks-contrib；一旦漏打，
rem import av 失败被 codec.py 静默吞掉，用户端无声退回 JPEG（带宽 5-20 倍回退）。
rem fps-host.spec 内已含 pathex=['vendor']、collect_all('av')、hiddenimports=['dxcam']、
rem console=False、version_info.txt、icon、onefile，等价于原 CLI 的全部参数。
pyinstaller --noconfirm --clean fps-host.spec
if errorlevel 1 goto :failed
echo 共享端打包完成。
echo.

rem ===== [4/4] 打包观看端（windowed 无黑窗）并处理 frpc =====
echo [4/4] 打包观看端 fps-viewer.exe（windowed 无黑窗）...
rem 第 68 条：同上，用已提交的 fps-viewer.spec（含 collect_all('av') 供 H.264 解码、
rem console=False、version_info.txt、icon、onefile），避免 CLI 覆盖 spec 抹掉 pyav 收集。
pyinstaller --noconfirm --clean fps-viewer.spec
if errorlevel 1 goto :failed
echo 观看端打包完成。
echo.

if exist frpc.exe (
    copy /y frpc.exe dist\frpc.exe >nul
    if errorlevel 1 (
        echo [警告] frpc.exe 复制失败，请手动将其放入 dist\ 目录。
    ) else (
        echo 已复制 frpc.exe 到 dist\ 目录。
    )
) else (
    echo frpc.exe 不存在，可稍后从樱花管理面板下载并放入 dist\ 目录。
)
echo.

echo ============================================
echo   打包完成！产物位于 dist\ 目录：
echo     dist\fps-host.exe      共享端
echo     dist\fps-viewer.exe    观看端
echo ============================================
if not defined CI if not defined NOPAUSE pause
exit /b 0

:failed
echo.
echo [错误] 打包过程中出现错误，请检查上方日志后重试。
if not defined CI if not defined NOPAUSE pause
exit /b 1
