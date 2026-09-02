@echo off
chcp 65001 >nul
echo ============================================
echo   安装包生成脚本 (Inno Setup 6)
echo ============================================
echo.

rem ===== [1/3] 前置检查：提醒先打包 exe =====
echo [1/3] 检查 dist 目录下的编译产物...
if not exist "dist\fps-host.exe" (
    echo [提示] 未找到 dist\fps-host.exe，请先运行 build_exe.bat 生成共享端。
)
if not exist "dist\fps-viewer.exe" (
    echo [提示] 未找到 dist\fps-viewer.exe，请先运行 build_exe.bat 生成观看端。
)
if not exist "dist\frpc.exe" (
    echo [提示] 未找到 dist\frpc.exe，请先从樱花管理面板下载并放入 dist\ 目录。
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
    pause
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
echo   安装包已生成：dist\SakuraVision-Setup-1.0.0.exe
echo ============================================
pause
exit /b 0

:failed
echo.
echo [错误] 安装包编译失败，请检查上方日志后重试。
pause
exit /b 1
