@echo off
chcp 65001 >nul
rem 第 72 条：切到脚本自身目录，确保 requirements.txt 等相对路径不随调用方
rem 当前目录（或 CI 工作目录）漂移而装错文件。
cd /d "%~dp0"
set ENV_NAME=fps-screen
set PYTHON_VERSION=3.10
rem 第 73 条：镜像只作为「本次命令」的参数传入，不再用 conda config / pip config 永久
rem 修改全局配置——那会影响本机所有项目，超出本项目范围。若确需全局镜像请用户自行设置。
set CONDA_MIRROR=-c https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main/ -c https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/free/
set PIP_MIRROR=-i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com

echo ================================================
echo FPS 画面传输程序 - conda 环境一键配置
echo ================================================

:: 检查 conda 是否可用
where conda >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 conda，请先安装 Anaconda 或 Miniconda。
    if not defined CI if not defined NOPAUSE pause
    exit /b 1
)

:: 创建 conda 环境（清华 TUNA 镜像，仅本次命令生效）
echo [1/3] 创建 conda 环境 %ENV_NAME%（Python %PYTHON_VERSION%）...
call conda create -n %ENV_NAME% python=%PYTHON_VERSION% -y %CONDA_MIRROR%
if errorlevel 1 goto :failed

:: 激活环境
echo [2/3] 激活 conda 环境 %ENV_NAME% ...
call conda activate %ENV_NAME%
if errorlevel 1 goto :activate_failed
rem 第 73 条：activate 可能「返回 0 却没真正切换」（conda 未对 cmd 初始化时）。
rem 用 CONDA_DEFAULT_ENV 二次确认，避免把依赖装进 base/系统 Python。
if /i not "%CONDA_DEFAULT_ENV%"=="%ENV_NAME%" goto :activate_failed

:: 安装项目依赖（阿里云镜像 + 环境自带 pip，仅本次命令生效）
echo [3/3] 从阿里源安装项目依赖到 %ENV_NAME% ...
call python -m pip install %PIP_MIRROR% -r requirements.txt
if errorlevel 1 goto :failed

echo ================================================
echo 环境配置完成！
echo 日常使用请执行：conda activate %ENV_NAME%
echo ================================================
if not defined CI if not defined NOPAUSE pause
exit /b 0

:activate_failed
echo.
echo [错误] conda 环境 %ENV_NAME% 激活失败（当前 CONDA_DEFAULT_ENV=%CONDA_DEFAULT_ENV%）。
echo        为避免把依赖装进 base/系统 Python，已中止安装。
echo        请先执行：conda init cmd.exe ，重开终端后再运行本脚本。
if not defined CI if not defined NOPAUSE pause
exit /b 1

:failed
echo 执行过程中出现错误，请检查上方日志后重试。
if not defined CI if not defined NOPAUSE pause
exit /b 1
