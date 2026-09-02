@echo off
chcp 65001 >nul
set ENV_NAME=fps-screen
set PYTHON_VERSION=3.10

echo ================================================
echo FPS 画面传输程序 - conda 环境一键配置
echo ================================================

:: 检查 conda 是否可用
where conda >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 conda，请先安装 Anaconda 或 Miniconda。
    pause
    exit /b 1
)

:: 配置 conda 频道（清华 TUNA 镜像；阿里云 anaconda 镜像已下线不可用）
echo [1/4] 配置 conda 镜像频道...
call conda config --add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main/
call conda config --add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/free/
call conda config --set show_channel_urls yes

:: 创建 conda 环境
echo [2/4] 创建 conda 环境 %ENV_NAME%（Python %PYTHON_VERSION%）...
call conda create -n %ENV_NAME% python=%PYTHON_VERSION% -y
if errorlevel 1 goto :failed

:: 配置 pip 阿里云镜像源（第三方库均从阿里源下载）
echo [3/4] 配置 pip 阿里云镜像源...
call pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/
call pip config set global.trusted-host mirrors.aliyun.com

:: 激活环境并安装项目依赖
echo [4/4] 激活环境并从阿里源安装依赖...
call conda activate %ENV_NAME%
call pip install -r requirements.txt
if errorlevel 1 goto :failed

echo ================================================
echo 环境配置完成！
echo 日常使用请执行：conda activate %ENV_NAME%
echo ================================================
pause
exit /b 0

:failed
echo 执行过程中出现错误，请检查上方日志后重试。
pause
exit /b 1
