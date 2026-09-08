<div align="center">

# 队友视野 TeamVision

**FPS 队友实时画面传输工具 · 让你的队友「看见你所见」**

采集屏幕 → H.264 / HEVC 硬件编码 → TCP 广播 → 悬浮窗实时观看

`by 西琳` · v1.3.0 · Windows 10/11 64 位

**本项目禁止用于任何商业用途**

</div>

---

## 这是什么

**队友视野（TeamVision）** 是一款面向 FPS 玩家的实时画面共享工具：共享端把自己的游戏画面采集、编码后通过网络发送给队友，队友端用一个置顶、鼠标穿透的悬浮窗实时观看，做到「边打边看队友视角」。

- 低延迟：DXGI 硬件采集 + NVENC 硬件编码，默认 30 FPS、码率自适应。
- 跨网络：同一局域网直连，或通过樱花内网穿透（SakuraFrp）跨公网连接。
- 多人观看：一个共享端可同时广播给多个观看端，慢客户端各自丢帧、互不拖累。
- 不打扰操作：悬浮窗默认置顶 + 鼠标穿透，配合全局热键随时切换频道与画面源。

---

## 架构

```
┌──────────────┐    TCP 直连（同一局域网）     ┌──────────────┐
│   共享端      │◄──────────────────────────────│   观看端      │
│ fps-host.exe │   host 监听 0.0.0.0:5700      │ fps-viewer   │
└──────┬───────┘                               └──────────────┘
       │ frpc 隧道（跨公网）
       ▼
┌──────────────────────────────┐
│   樱花内网穿透节点 (SakuraFrp) │
│   远端端口 ──映射──► 本地 5700 │
└──────────────────────────────┘
       ▲
┌──────┴───────┐
│   观看端      │   server_addr = 节点IP/域名:远端端口
│ fps-viewer   │
└──────────────┘
```

- **共享端（fps-host.exe）**：采集屏幕 → H.264/H.265(HEVC) 编码（NVENC 硬件优先，软件 x264 / JPEG 自动回退）→ TCP 广播给所有观看端，默认监听 `0.0.0.0:5700`。
- **观看端（fps-viewer.exe）**：连接 `server_addr`，按流内 codec 标记自动选择 H.264/HEVC 解码，悬浮显示画面。

---

## 功能特性

| 分类 | 说明 |
|---|---|
| 视频编码 | HEVC / H.264，NVENC 硬件编码优先；不可用时自动回退软件 x264，再回退 JPEG |
| 屏幕采集 | DXGI 硬件采集（低延迟、可解决独占全屏黑屏），不可用时自动回退 mss(GDI) |
| 带宽自适应 (ABR) | 网络差时自动下调质量 / 缩放 / 码率，码率上下限可配 |
| 多人观看 | 每客户端独立发送线程 + 最新帧队列，慢客户端只丢自己的帧 |
| 悬浮窗 | 置顶、鼠标穿透、不透明度可调、显示宽度可调，不挡游戏操作 |
| 多画面源 (MultiView) | 观看组内多人互看，热键循环切换画面源 |
| 全局热键 | 见下方「快捷键」 |
| 内网穿透 | 内置 frpc 管理，一键启动樱花隧道；自动识别已安装的 SakuraFrp |
| 日志诊断 | `logs\app_*.log` 滚动日志；GUI 内实时性能区与客户端列表 |

### 快捷键（观看端）

| 热键 | 功能 |
|---|---|
| `Ctrl + Alt + X` | 切换鼠标穿透 |
| `Ctrl + Alt + ←/→` | 上一个 / 下一个频道 |
| `Ctrl + Alt + ↑/↓` | 上一个 / 下一个画面源 |
| `Alt + 1…9` | 直接切到第 N 个频道 |

---

## 系统要求

- Windows 10 / 11 64 位
- 共享端建议有 NVIDIA 显卡（NVENC 硬件编码）；无独显会自动回退软件 / JPEG 编码
- 观看端无需任何运行环境

---

## 下载与安装

### 方式一：安装包（推荐）

运行 `TeamVision-Setup-1.3.0.exe`（Inno Setup 产物），自动放置 `fps-host.exe` 与 `fps-viewer.exe`。

> 安装包为**用户级安装**（默认装到 `%LOCALAPPDATA%\Programs\TeamVision`，不需要管理员权限）。
> `config.json`、`accounts.json` 与 `logs\` 都写在程序所在目录；若该目录不可写（例如手动装到
> `C:\Program Files`），会自动回退到 `%LOCALAPPDATA%\TeamVision\`，并在首次运行时迁移已有配置。

### 方式二：绿色版

将 `fps-host.exe`、`fps-viewer.exe` 放到同一目录即可运行；`config.json` 首次运行自动生成。

> 如需跨公网使用，还要自行从樱花内网穿透下载 `frpc.exe` 放到共享端同目录（见下文）。

---

## 快速开始

### 路径一：局域网直连（建议先验证）

1. 共享端双击 `fps-host.exe`，等待出现「服务已启动，监听 0.0.0.0:5700」。
2. 确认共享端内网 IP：`ipconfig` 查看 IPv4 地址（如 `192.168.1.100`）。
3. 观看端双击 `fps-viewer.exe`，在频道管理中把地址改为 `192.168.1.100:5700` 并连接。
4. 约 1~2 秒后看到悬浮画面。

### 路径二：公网穿透（樱花 SakuraFrp）

1. 在 [樱花控制台](https://console.natfrp.com) 注册、创建 **TCP** 隧道（本地端口填 `5700`），记下隧道 ID 与访问令牌 token。
2. 从控制台下载 Windows amd64 版 `frpc.exe`，放到与 `fps-host.exe` 相同的目录。
3. 共享端 GUI 的「frpc 区」填写 `token` 与 `tunnel_ids`，点「启动」（或勾选自动启动）。
4. 观看端把 `server_addr` 设为 `节点IP或域名:远端端口` 并连接。

> 映射关系：`公网节点IP:远端端口 ──► 共享端本地 127.0.0.1:5700`。一个 TCP 隧道即可承载多个观看端。

完整部署、frpc 配置与排障详见 [`docs/部署操作文档.md`](docs/部署操作文档.md)。

---

## 配置参考（config.json）

配置文件优先放在 exe 同目录（绿色版），该目录不可写时自动改用 `%LOCALAPPDATA%\TeamVision\`
（并迁移已有配置）；缺失时自动生成，旧配置升级时自动补全新字段（不覆盖已有值）。
`accounts.json` 与 `logs\` 同此规则。

### 共享端 `host`

| 字段 | 默认 | 说明 |
|---|---|---|
| `listen_host` | `0.0.0.0` | 监听地址 |
| `port` | `5700` | 服务端口，需与 frp 隧道本地端口一致 |
| `fps` | `30` | 目标帧率 |
| `jpeg_quality` | `80` | JPEG 质量 1~100（回退编码时使用） |
| `scale` | `1.0` | 画面缩放系数（0.5=半分辨率，省带宽） |
| `capture.monitor` | `1` | 采集显示器编号（从 1 开始） |
| `capture.region` | `null` | 自定义采集区域；null=全屏 |
| `capture.backend` | `dxgi` | 采集后端：`dxgi`（硬件）/ `mss`（GDI，兼容性最好） |
| `perf.adaptive` | `true` | 带宽自适应开关 |
| `perf.quality_min` | `40` | 自适应最低质量 |
| `perf.scale_min` | `0.25` | 自适应最低缩放 |
| `codec.encoder` | `auto` | 编码选择：`auto`/`hevc`/`nvenc`/`x264`/`jpeg` |
| `codec.bitrate_kbps` | `2500` | 编码码率（Kbps） |
| `codec.keyint` | `60` | 关键帧间隔（帧） |
| `codec.min_bitrate_kbps` | `400` | ABR 码率下限 |
| `codec.max_bitrate_kbps` | `6000` | ABR 码率上限 |
| `codec.preset` | `""` | 编码器预设，空=默认低延迟预设 |
| `frp.*` | — | 内网穿透配置，见部署文档 |

### 观看端 `viewer`

| 字段 | 默认 | 说明 |
|---|---|---|
| `server_addr` | `127.0.0.1:5700` | 默认连接地址（无频道时使用） |
| `display_width` | `480` | 悬浮窗显示宽度（像素），高度按原比例 |
| `alpha` | `0.9` | 窗口不透明度 0~1 |
| `click_through` | `true` | 鼠标穿透 |
| `panel_topmost` | `true` | 悬浮窗置顶 |
| `channels` | `[]` | 频道列表 `[{"name":"默认频道","addr":"IP:端口"}]` |

> 配置项以代码 `common.py` 的 `DEFAULT_CONFIG` 为单一真源；若文档与代码不符，以代码为准。

---

## 从源码构建

需要 Python 3.10 环境与 [conda](https://docs.conda.io/)（项目脚本默认使用名为 `fps-screen` 的环境）。

```bash
# 1. 安装依赖（首次）
setup.bat

# 2. 生成图标与启动画面（assets/app.ico、assets/splash.png）
python gen_assets.py

# 3. 打包 exe（PyInstaller onefile，输出到 dist/）
build_exe.bat

# 4. 生成安装包（需先安装 Inno Setup 6）
build_installer.bat

# 5. 运行测试（327 项单元测试，约 111 秒）
run_tests.bat
```

主要依赖（见 `requirements.txt`）：`mss`、`opencv-python`、`numpy`、`pillow`、`pywin32`、`av`。
采集后端 `dxcam` 及其依赖 `comtypes` 以 vendored 方式提供（取自 GitHub `ra1nty/DXcam`，**非** PyPI 上过时的 0.0.5）。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| 观看端一直「连接中/无画面」 | 先用局域网直连验证：观看端填 `共享端IP:5700`，`ping` 确认可达 |
| 局域网能连、公网连不上 | 确认共享端日志出现 `frpc 已启动`，核对隧道为 TCP、本地端口=5700 |
| 连上但黑屏 | 独占全屏采集黑屏：把 `capture.backend` 设为 `dxgi`，或改为窗口化/无边框全屏 |
| 画面卡顿、延迟高 | 降 `fps`/`scale`/`bitrate_kbps`，开启 `perf.adaptive`，或用 `capture.region` 限定区域 |
| 双击 exe 无反应 | 查看 `logs\` 当日日志；临时退出杀软后重试（PyInstaller 单文件偶有误报） |
| 提示「未找到 frpc 程序」 | 把自行下载的 `frpc.exe` 放到与 `fps-host.exe` 同一目录 |

更多排障见 [`docs/部署操作文档.md`](docs/部署操作文档.md)。

---

## 说明与免责声明

- **禁止商业用途**：本项目及其所有产物（含 exe、安装包、源码）仅限个人学习与非商业使用，未经作者书面许可，不得用于任何商业用途、二次分发牟利或集成到收费产品中。完整条款见 [`LICENSE`](LICENSE)。
- `frpc.exe` 属第三方（樱花内网穿透）程序，需自行从其官网下载，本项目不附带。
- 内网穿透请遵守樱花及相关服务的使用条款；公网共享画面请注意隐私与账号安全。
- PyInstaller 单文件打包不是加密手段，请勿用于保护敏感逻辑。

---

<div align="center">

**队友视野 TeamVision** · `by 西琳` · Copyright © 2026 TeamVision Team

仅限个人学习与非商业使用 · 禁止用于任何商业用途

</div>
