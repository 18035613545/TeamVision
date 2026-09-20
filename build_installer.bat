@echo off
chcp 936 >nul
rem dist-015（2026-09-20）：本文件按 **ANSI/GBK（代码页 936）** 保存，行首 chcp 也已改成 936。
rem 不要再改回 UTF-8 + chcp 65001：那种组合下 cmd 按字节读脚本、却按码页解析行边界，实测会把
rem 某行的尾部片段当成命令执行、同时吞掉相邻命令行（守卫/签名那几行首当其冲，且退出码碰巧还
rem 是对的，属侥幸）；改存"带 BOM 的 UTF-8"则会让首行 @echo off 变成命令名而报错。
rem 第 72 条：切到脚本自身目录，确保 installer.iss / dist\ 等相对路径不随
rem 调用方当前目录（或 CI 工作目录）漂移而从错误的目录树取文件。
cd /d "%~dp0"
echo ============================================
echo   安装包生成脚本 (Inno Setup 6)
echo ============================================
echo.

rem ===== [1/4] 前置检查：dist 产物缺失即中止 =====
rem dist-002（2026-09-18）：旧实现缺 exe 只打印「提示」继续走——Inno 的 dist\fps-host\*
rem 通配在目录不存在时仅 warning，于是能编译出"安装包已生成"的**空壳包**且脚本 exit 0。
rem 两个端的 exe 是硬前提，缺失直接失败（CI 才能正确拦截）。
echo [1/4] 检查 dist 目录下的编译产物...
set "HAS_FRPC=1"
set "MISSING=0"
rem dist-012（2026-09-20）：安装包文件名此前在本文件里写死了四处，与 installer.iss 的
rem OutputBaseFilename 各写一遍；版本 bump 时漏改任一处，下面的守卫与签名就会去查一个
rem 不存在的旧名字（而 sign_exe.ps1 对"文件不存在"只记跳过并 exit 0）。改为从 iss 现读，
rem 两处从此不可能漂移。
set "SETUP_BASE="
for /f "usebackq tokens=1,* delims==" %%A in ("installer.iss") do (
    if /i "%%A"=="OutputBaseFilename" set "SETUP_BASE=%%B"
)
if not defined SETUP_BASE (
    echo [错误] 未能从 installer.iss 解析出 OutputBaseFilename，无法确定安装包文件名，已中止。
    if not defined CI if not defined NOPAUSE pause
    exit /b 1
)
set "SETUP_EXE=dist\%SETUP_BASE%.exe"
echo 安装包目标文件（取自 installer.iss）：%SETUP_EXE%
if not exist "dist\fps-host\fps-host.exe" (
    echo [错误] 未找到 dist\fps-host\fps-host.exe，请先运行 build_exe.bat 生成共享端。
    set "MISSING=1"
)
if not exist "dist\fps-viewer\fps-viewer.exe" (
    echo [错误] 未找到 dist\fps-viewer\fps-viewer.exe，请先运行 build_exe.bat 生成观看端。
    set "MISSING=1"
)
rem dist-010（2026-09-20）：两个 exe 都在 ≠ 产物完整。真正承载 python3xx.dll / av.libs /
rem numpy 的是 _internal 目录，Inno 的 dist\fps-host\* 通配对"目录在、_internal 空了"
rem 也不报 warning，于是照样能编出"安装包已生成"的空壳包，用户装完启动即
rem "Failed to load Python DLL"（第 68 条 spec 被覆盖那一类事故的现成出口）。
for %%D in (fps-host fps-viewer) do (
    if not exist "dist\%%D\_internal\" (
        echo [错误] 未找到 dist\%%D\_internal 目录，PyInstaller onedir 产物不完整。
        set "MISSING=1"
    )
    if not exist "dist\%%D\_internal\python*.dll" (
        echo [错误] dist\%%D\_internal 内没有 pythonXX.dll —— 装完启动即 "Failed to load Python DLL"。
        set "MISSING=1"
    )
    if not exist "dist\%%D\_internal\av.libs" (
        echo [错误] dist\%%D\_internal 内没有 av.libs —— FFmpeg DLL 未收集，视频路径会静默退回 JPEG。
        set "MISSING=1"
    )
)
if "%MISSING%"=="1" (
    echo.
    echo [错误] dist 产物不完整，已中止（避免生成空壳安装包）。
    if not defined CI if not defined NOPAUSE pause
    exit /b 1
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

rem ===== [2/4] 检测 Inno Setup 6 编译器 =====
echo [2/4] 检测 Inno Setup 6 编译器 (ISCC.exe)...
set "ISCC="
where iscc >nul 2>nul
if not errorlevel 1 set "ISCC=iscc"
if not defined ISCC if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "C:\Program Files\Inno Setup 6\ISCC.exe" set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
rem 第 101 条：Inno Setup 6 默认是「仅为我安装」→ 装在 %LOCALAPPDATA%\Programs\Inno Setup 6，
rem 上面两个 Program Files 路径都不存在。旧脚本因此误报「未检测到 Inno Setup 6」，
rem 即使用户已装好也打包不出安装包。补上用户级安装目录。
if not defined ISCC if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"

if not defined ISCC (
    echo.
    echo [错误] 未检测到 Inno Setup 6，请先安装（https://jrsoftware.org/isdl.php）后重试。
    echo.
    if not defined CI if not defined NOPAUSE pause
    exit /b 1
)
echo 检测到 ISCC：%ISCC%
echo.

rem ===== [3/4] 编译安装包 =====
echo [3/4] 编译安装包 installer.iss ...
"%ISCC%" installer.iss
if errorlevel 1 goto :failed
echo.

rem ===== [4/4] 校验产物 + Authenticode 签名 =====
echo [4/4] 校验并签名安装包 ...
rem dist-002：ISCC 返回 0 仍可能是"空壳包"（见 [1/4]），这里再校验产物存在且非空壳
if not exist "%SETUP_EXE%" (
    echo [错误] 未找到 %SETUP_EXE%，安装包未生成。
    if not defined CI if not defined NOPAUSE pause
    exit /b 1
)
for %%A in ("%SETUP_EXE%") do set "SETUP_SIZE=%%~zA"
if %SETUP_SIZE% LSS 1048576 (
    echo [错误] 安装包体积异常小（%SETUP_SIZE% 字节），疑似空壳包，请检查 [1/4] 的产物。
    if not defined CI if not defined NOPAUSE pause
    exit /b 1
)
rem dist-010：>1MiB 只能挡住"完全空壳"，挡不住 _internal 只剩零星文件。实现方案 §1.1
rem 自己写的验收标准是"安装包 ≈111MiB、偏差 >10% 说明 spec 剔除失效"，这里落地：
rem 低于硬下限（90MiB）判定为产物不完整直接中止；落在目标带（约 100~122MiB）之外只告警，
rem 因为 LAN-only 包（不含 frpc）与后续依赖增减都会让体积合法地偏移。
set "SETUP_FLOOR=94371840"
set "SETUP_LO=104752819"
set "SETUP_HI=128018278"
if %SETUP_SIZE% LSS %SETUP_FLOOR% (
    echo [错误] 安装包体积 %SETUP_SIZE% 字节低于完整产物下限 %SETUP_FLOOR% 字节（基线约 111MiB）。
    echo        疑似 _internal 缺 DLL 或依赖漏装，已中止——不要把这个包发出去。
    if not defined CI if not defined NOPAUSE pause
    exit /b 1
)
if %SETUP_SIZE% LSS %SETUP_LO% (
    echo [警告] 安装包体积 %SETUP_SIZE% 字节低于目标带下限 %SETUP_LO% 字节（111MiB 基线 -10 个百分点）。
)
if %SETUP_SIZE% GTR %SETUP_HI% (
    echo [警告] 安装包体积 %SETUP_SIZE% 字节高于目标带上限 %SETUP_HI% 字节（111MiB 基线 +10 个百分点）。
)
if exist "%SETUP_EXE%" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sign_exe.ps1" ^
        -Files "%SETUP_EXE%"
    if errorlevel 1 (
        echo [警告] 安装包签名失败，产物仍可用。
    )
) else (
    echo [警告] 未找到 %SETUP_EXE%，跳过签名。
)
echo.
echo ============================================
if "%HAS_FRPC%"=="1" (
    echo   安装包已生成：%SETUP_EXE%
    echo   已包含 frpc.exe —— 支持公网/樱花 frp 隧道共享。
) else (
    echo   安装包已生成：%SETUP_EXE%
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
