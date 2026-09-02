@echo off
chcp 65001 >nul
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
    pause
    exit /b 1
)
echo 检测到 conda，正在激活环境 fps-screen ...
call conda activate fps-screen
if errorlevel 1 goto :failed
echo 环境 fps-screen 激活成功。
echo.

rem ===== [2/4] 配置 pip 阿里源并安装 PyInstaller =====
echo [2/4] 配置 pip 阿里源并安装 PyInstaller ...
pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/
if errorlevel 1 goto :failed
pip install pyinstaller
if errorlevel 1 goto :failed
echo PyInstaller 安装完成。
echo.

rem ===== [3/4] 打包共享端（windowed 无黑窗 + 可选 dxcam 采集后端）=====
echo [3/4] 打包共享端 fps-host.exe ...
set EXTRA_PATHS=
if exist vendor (
    rem vendor 目录存在（如 pip install --target=.\vendor dxcam）时把 dxcam 一并打进 exe
    set EXTRA_PATHS=--paths vendor
)
pyinstaller --noconfirm --clean --onefile --windowed --name fps-host --icon assets\app.ico --version-file version_info.txt %EXTRA_PATHS% --hidden-import dxcam host.py
if errorlevel 1 goto :failed
echo 共享端打包完成。
echo.

rem ===== [4/4] 打包观看端（windowed 无黑窗）并处理 frpc =====
echo [4/4] 打包观看端 fps-viewer.exe（windowed 无黑窗）...
pyinstaller --noconfirm --clean --onefile --windowed --name fps-viewer --icon assets\app.ico --version-file version_info.txt viewer.py
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
pause
exit /b 0

:failed
echo.
echo [错误] 打包过程中出现错误，请检查上方日志后重试。
pause
exit /b 1
