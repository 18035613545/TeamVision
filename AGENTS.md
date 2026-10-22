# AGENTS.md

队友视野 TeamVision —— Windows 平台的 FPS 游戏低延迟屏幕共享工具(队友端采集并做 H.264/HEVC 编码,主控端解码播放)。v1.5.0。

## 环境与命令

- 安装依赖: `setup.bat`(建 conda 环境 `fps-screen` / Python 3.10 + `pip install -r requirements.txt`);国内镜像用 `setup.bat --mirror`
- 运行: `python host.py`(共享端,默认监听 127.0.0.1:5700)/ `python viewer.py`(主控端)
- 打包: `build_exe.bat` → `build_installer.bat` → `sign_exe.ps1`(代码签名)
- 测试: `run_tests.bat`
- Lint: `ruff check *.py tests`(口径**只在 CI 的调用清单里**,本地照抄它;范围与豁免理由见 `pyproject.toml` 顶部)
- 类型检查: `python -m mypy`(覆盖 `pyproject.toml` 的 `files = ["common.py","codec.py","pipeline.py","ui_common.py"]`,另把 `screen.py` 作为 codec 的依赖做符号解析)
  **注意实际保护面**:这 5 个模块的 143 个 `def` **没有一个带返回注解**(本轮实测:common 55 / codec 29 / pipeline 25 / ui_common 7 / screen 27),mypy 默认不开 `--check-untyped-defs`,未注解的函数体**不进类型检查**——即当前查的是模块级语句与少量注解位置,"检查 4 个模块"的保护面比它听起来小得多。

## 目录

- `tests/` —— 67 个 `test*.py` 单测;另有 4 个不进 CI 的重负载脚本(`e2e_multiview.py`、`e2e_signalwin.py`、`soak_viewer_8h.py`、`stress_max_clients.py`)
- `docs/` —— 中文文档;`docs/audit/` 是按模块(auth / capture / cfg / codec / dist / doc / net / test / ui)分列的审计记录
- `assets/` —— 图标与启动图(`splash.png`),由 `gen_assets.py` 生成
- 根目录平铺模块,**不是 Python 包**(无 `__init__.py`):`common.py` 公共库、`host.py` 共享端、`viewer.py` 主控端、`screen.py` 采集、`codec.py` 编解码、`pipeline.py` 传输,另有 `share.py` / `logger.py` / `update_check.py` / `ui_common.py`
- `*.spec` 是 PyInstaller 配置,`installer.iss` 是 Inno Setup 安装包脚本

## 测试约定(踩过坑,别改)

- **只能** `python -m unittest discover -s tests -p "test*.py"`,且工作目录必须是仓库根。
  `tests/` 直接 `import common` / `import host`,没有 `conftest.py`、`pytest.ini`、`__init__.py`。
  因此 `pytest tests\` 会 `ModuleNotFoundError: common`;`python tests\test_x.py` 会静默收集 0 条测试却返回成功。
- 单条: `python -m unittest tests.test_item57_buffer`
- `run_tests.bat` 里的 `TV_CI_NO_CONDA=1` 让它在没有 `fps-screen` 环境时退回当前 python(CI 专用);本机不要定义它。
- `run_tests.bat` 是测试口径的**唯一来源**,CI 直接调它,不要在别处再复制一份命令。

## CI 的实际覆盖范围(比看上去窄)

`.github/workflows/test.yml` 名为 `tests`,只在 push 到 `main` 和开 PR 时跑,**没有 `workflow_dispatch`**,无法手动触发。
它只做四件事:装依赖 → `ruff check *.py tests` → `python -m mypy` → `cmd /c run_tests.bat`。

由此推出两条容易踩的推论:

1. 改了 lint/类型检查清单之外的目录,那道门不会替你查。`tests/` 曾在清单外待了很久,
   实测积了 89 处违规(含 `F821` 未定义名),2026-10-10 才纳入 `ruff`,**纳入前它一直是盲区**。
2. 打包、签名、安装包这条发布链**完全在 CI 之外**,改完只能手工验证。

## 环境陷阱

- **`time.monotonic()` 在 Windows 返回的是开机以来的秒数**,不是墙钟。任何构造"距上次收包 N 秒"的对象(`host.ClientInfo.last_rx_at`)的测试,都必须显式把该时间戳钉到确定值,否则本机 uptime 一长就永远绿、CI 那台刚开机的机器必挂。
  真实案例:`tests/test_bugfix_20260920_t02_recvloop.py::TestAccountingCallbacks::test_msg_timeout_floor_is_honoured` —— 开机不足约 16 分钟的机器上必失败。
- 依赖 Tk 的用例自带 `SkipTest`,headless / CI 上跳过、本机会跑。本机全量通过 ≠ CI 全量通过。
- `requirements.txt` 全部钉死 `>=下界,<上界`,并逐条注释了原因(尤其 opencv-python-headless 需要 numpy2 才有可用 wheel)。**不要随手升级或去掉上下界。**

## 编码

- `*.bat` 是 **ANSI / GBK(cp936)** —— 脚本头 `chcp 936`,必须保持这样。改成 UTF-8 + `chcp 65001` 会在中文版 cmd 上乱码。读写 `.bat` 一律按 cp936。
- 其余全部文件(`*.py` / `*.md` / `*.yml` / `*.toml`)是 **UTF-8 无 BOM**。
- 每个 `.py` 首行保留 `# -*- coding: utf-8 -*-`(仓库既有约定,ruff `UP009` 已就此豁免)。

## 代码风格

- 注释和文档字符串一律中文,写清"为什么"而不是"做了什么";改动沿用同文件既有风格。
- ruff 配置在 `pyproject.toml`:target py310、line-length 100、`select = [E, F, W, I, UP, B]`。
  `E501`(行宽)和 `UP031`(`%` → f-string)是**有意豁免**:中文注释普遍超长,全仓统一用 `%` 风格写日志和异常消息。
- 日志统一走 `logger.py`。

## 安全

- 以下含明文 frp token / 口令哈希 / 私钥,已被 `.gitignore` 挡住,**任何情况下不要提交**:`config.json*`、`accounts.json*`、`certs/`、`logs/`、`vendor/`、`.workbuddy/`、`frpc.exe`、`*.e2e_bak`。
- 本仓库是 **PUBLIC**。提交前确认不含真实内网地址、个人邮箱、本机路径。
- 签名证书私钥只放 `certs/`,由 `sign_exe.ps1` 在本地消费。

## 提交与分支

- 默认分支是 `main`。CI 在 push 到 `main` 和开 PR 时都会触发,走 PR 更稳妥 —— 推送前就能看到结果。
- 提交信息用 conventional commits(`feat:` / `fix:` / `docs:` / `refactor:` / `chore:`),与现有历史保持一致。
- 推送前本地跑一次 `run_tests.bat`;推送后用 `gh run list` 看结果、`gh run view <id> --log-failed` 抓报错原文。
