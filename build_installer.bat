@echo off
chcp 65001 >nul
rem 第 72 条：切到脚本自身目录，确保 installer.iss / dist\ 等相对路径不随
rem 调用方当前目录（或 CI 工作目录）漂移而从错误的目录树取文件。
cd /d "%~dp0"
echo ============================================
echo   安装包生成脚本 (Inno Setup 6)
echo ============================================
echo.

rem ===== [1/3] 前置检查：提醒先打包 exe =====
echo [1/3] 检查 dist 目录下的编译产物...
set "HAS_FRPC=1"
if not exist "dist\fps-host.exe" (
    echo [提示] 未找到 dist\fps-host.exe，请先运行 build_exe.bat 生成共享端。
)
if not exist "dist\fps-viewer.exe" (
    echo [提示] 未找到 dist\fps-viewer.exe，请先运行 build_exe.bat 生成观看端。
)
if not exist "dist\frpc.exe" (
    set "HAS_FRPC=0"
    echo [警告] 未找到 dist\frpc.exe —— 本安装包将不包含 frpc。
    echo        后果：装好的程序只能局域网直连共享，公网/樱花 frp 隧道不可用；
    echo        用户在 frpc 区点「启动」会看到「未找到 frpc 程序」提示。
    echo        如需公网共享，请从樱花管理面板下载 frpc 放入 dist\ 后重跑本脚本。
    echo        若只做局域网共享，此警告可忽略——LAN-only 安装包是有意支持的形态。
)
echo.
echo 提醒：请确认已先运行 build_exe.bat 生成 dist\ 下的共享端/观看端 exe。
echo.

rem ===== [2/3] 检测 Inno Setup 6 编译器 =====
echo [2/3] 检测 Inno Setup 6 编译器 (ISCC.exe)...
set "ISCC="
where iscc >nul 2>nul
if not errorlevel 1 set "ISCC=iscc"
if not defined ISCC if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "C:\Program Files\Inno Setup 6\ISCC.exe" set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"

if not defined ISCC (
    echo.
    echo [错误] 未检测到 Inno Setup 6，请先安装（https://jrsoftware.org/isdl.php）后重试。
    echo.
    if not defined CI if not defined NOPAUSE pause
    exit /b 1
)
echo 检测到 ISCC：%ISCC%
echo.

rem ===== [3/3] 编译安装包 =====
echo [3/3] 编译安装包 installer.iss ...
"%ISCC%" installer.iss
if errorlevel 1 goto :failed
echo.
echo ============================================
if "%HAS_FRPC%"=="1" (
    echo   安装包已生成：dist\ 下的 TeamVision-Setup-*.exe
    echo   已包含 frpc.exe —— 支持公网/樱花 frp 隧道共享。
) else (
    echo   安装包已生成：dist\ 下的 TeamVision-Setup-*.exe
    echo   未包含 frpc.exe —— 仅局域网直连共享，公网 frp 隧道不可用。
)
echo   版本号同 installer.iss 的 OutputBaseFilename
echo ============================================
if not defined CI if not defined NOPAUSE pause
exit /b 0

:failed
echo.
echo [错误] 安装包编译失败，请检查上方日志后重试。
if not defined CI if not defined NOPAUSE pause
exit /b 1
