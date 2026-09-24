@echo off
chcp 936 >nul
rem dist-015（2026-09-20）：本文件按 **ANSI/GBK（代码页 936）** 保存，行首 chcp 也已改成 936。
rem 不要再改回 UTF-8 + chcp 65001：那种组合下 cmd 按字节读脚本、却按码页解析行边界，实测会把
rem 某行的尾部片段当成命令执行、同时吞掉相邻命令行（守卫/签名那几行首当其冲，且退出码碰巧还
rem 是对的，属侥幸）；改存"带 BOM 的 UTF-8"则会让首行 @echo off 变成命令名而报错。
rem 第 81 条：测试原先只能「在仓库根手动执行 python -m unittest discover -s tests」才跑得起来。
rem 原因：tests 下用例裸 import common/host/viewer（均为仓库根模块），既无 sys.path 引导，也无
rem conftest.py / pytest.ini / __init__.py，所以换当前目录、直接 python tests\test_x.py、或
rem pytest tests\ 都会 ModuleNotFoundError: common。本脚本先把当前目录钉到仓库根（cd 到脚本自身
rem 所在目录），再激活 fps-screen 环境跑发现式测试，让「全套件绿灯」成为任何人一条命令即可复现的
rem 结果，而不再依赖口口相传的调用方式。退出码 = 测试结果（0 全过 / 非 0 有失败），便于脚本判断。
rem
rem 等效的手动调用（均须先位于仓库根、且在 fps-screen 环境内）：
rem   全套件：   python -m unittest discover -s tests -p "test*.py"
rem   单个文件： python -m unittest tests.test_item57_buffer
rem 注意：不带 -m unittest 直接跑 python tests\test_x.py 仍会失败，请用上面的 -m unittest 形式。
cd /d "%~dp0"
echo ============================================
echo   单元测试一键运行 (unittest discover)
echo ============================================
echo.

rem ===== [1/2] 检查 conda 并激活 fps-screen 环境 =====
echo [1/2] 检查 conda 环境...
rem dist-022（2026-09-24）：CI（GitHub windows-latest + setup-python）没有 conda/fps-screen
rem 环境，此前 CI 只能在 workflow 里内联复制本脚本 [2/2] 的 unittest 命令，两处口径会各自漂移。
rem 现在 CI 直接调用本脚本并显式定义 TV_CI_NO_CONDA=1，conda 不可用时退回 setup-python 的
rem python 跑同一条命令；该变量不复用 CI，避免本机为免 pause 设 CI=1 时绕开下面的环境守卫。
where conda >nul 2>nul
if errorlevel 1 goto :conda_missing
echo 检测到 conda，正在激活环境 fps-screen ...
call conda activate fps-screen
if errorlevel 1 goto :activate_failed
rem 与 build_exe.bat 同源：activate 可能「返回 0 却没真正切换」，二次确认 CONDA_DEFAULT_ENV，
rem 避免在 base/系统环境里跑测试（缺 cv2/av/numpy/PIL 会 ModuleNotFoundError）。
if /i not "%CONDA_DEFAULT_ENV%"=="fps-screen" (
    if defined TV_CI_NO_CONDA (
        echo [警告] conda 环境未切到 fps-screen（TV_CI_NO_CONDA=1）：CI 上用当前 python 直接运行。
        echo.
        goto :run_tests
    )
    echo [错误] conda 环境未切到 fps-screen，当前 CONDA_DEFAULT_ENV=%CONDA_DEFAULT_ENV%。
    echo        请先执行：conda init cmd.exe ，重开终端后重试。
    goto :failed
)
echo 环境 fps-screen 激活成功。
echo.

:run_tests
rem ===== [2/2] 运行测试 =====
echo [2/2] 运行 tests 下全部单元测试 ...
echo.
python -m unittest discover -s tests -p "test*.py"
set "RC=%ERRORLEVEL%"
echo.
echo ============================================
if "%RC%"=="0" (
    echo   结果：全部通过
) else (
    echo   结果：存在失败或错误，退出码 %RC%
)
echo ============================================
if not defined CI if not defined NOPAUSE pause
exit /b %RC%

:conda_missing
if defined TV_CI_NO_CONDA (
    echo [警告] 未检测到 conda（TV_CI_NO_CONDA=1）：CI 上用当前 python 直接运行同一套测试命令。
    echo.
    goto :run_tests
)
echo.
echo [错误] 未检测到 conda，请先安装 Anaconda 或 Miniconda 并加入 PATH。
echo.
if not defined CI if not defined NOPAUSE pause
exit /b 1

:activate_failed
if not defined TV_CI_NO_CONDA goto :failed
echo [警告] conda 激活 fps-screen 失败（TV_CI_NO_CONDA=1）：CI 上用当前 python 直接运行同一套测试命令。
echo.
goto :run_tests

:failed
echo.
echo [错误] 测试无法启动：环境激活失败，请按上方提示处理后重试。
if not defined CI if not defined NOPAUSE pause
exit /b 1
