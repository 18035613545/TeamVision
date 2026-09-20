# 更新日志（Changelog）

> 本文件自 2026-09-18 起维护；更早版本的"逐条"细节见
> [`audit/`](audit/) 各模块文件与冻结对照表。版本号真源：`common.py` 的 `APP_VERSION`。

## [1.4.0] - 2026-09-18

**安全**
- 修复公网共享整体不可用：默认回环改为 **IPv4/IPv6 双栈**（frpc 以 `localhost` 回连走 `::1` 曾被拒），并新增启动后**公网回连自检**，结论直显共享端面板与日志（net/auth 系列，见 audit）。
- **D1 挑战-应答认证（协议 v2.2）**：登录/注册的口令不再过网——观看端就 nonce 提交
  `HMAC-SHA256(dk, nonce)`（dk=PBKDF2 派生，与服务端账户表同一把密钥），一次性 nonce、
  60 秒过期、绑定连接（**注**：这是 D1 落地当时 v2.2 的输入串；同日 auth-002 改为
  `HMAC-SHA256(dk, "tv-auth-v2|" + user + "|" + nonce)` 并把协议升到 v2.3，见下方协议条目；
  本条保留原表述以记当时变更）。默认 `auth.chal_response="require"`：1.3.x 及更早观看端会被拒
  （提示更新）；过渡期可临时设 `allow_legacy`（直发口令记 WARNING，一个版本周期后移除）。
  已有账户无需重建。详见 install.md §5.1/§5.2。
- 反作弊误报收敛 + 默认安全加固：关 UPX、onedir 化收尾、`auth_timeout` 提到 240s 下界（旧配置自动迁移）。
- 上行限速修正：令牌桶按名义速率补充（600 Kbps 档曾实测跑到 ~1312 Kbps）。

**性能**
- 上屏提速：中继整帧物化 2 次 → 1 次；viewer poll 空闲退避（A4）。
- 启动提速：splash 提前到重依赖导入之前（A3）；包体瘦身（安装包 ~208MB → ~111MB）。
- 采集门控统一 + ABR 闭环（阶段 2 第 1/3 项）；采集后端启动即失败走重建自愈、告警 10s 去重。

**功能**
- 共享端「导出诊断包」按钮导出诊断包（token/密码自动打码）+ 自检结论上面板（B2）。
- 观看端延迟统计升级：30 秒窗口 **P50/P95**（悬浮窗状态行后缀 + 诊断包逐频道行，E4）。
- 共享端「账户管理」面板（B3）：本地列出/添加/删除账户、重置口令，`closed`/`bootstrap`
  策略下无需再临时放开自助注册；删除唯一账户二次确认防自锁。
- 更新提示（B4）：两端启动后查一次 GitHub Releases 最新 tag，比当前版本新时仅状态栏
  提示（缓存 24h、失败静默、不弹窗）；纯提示器不做自动更新，`update_check.enabled`
  可关闭外连。

**工程**
- 协议层单点：`Act` 常量表 + 共享接收泵 + 握手 `proto` 字段（C2）；随后 D1 以 `chal/auth_proof/reg_proof`
  把线上协议升到 **v2.2**（次版本向后兼容：2.1 旧观看端仅在 `allow_legacy` 下可用）；同日 auth-002
  把 proof 绑定 user + 域分隔串，协议再升到 **v2.3**（`common.py` `PROTOCOL_VERSION=(2,3)`、
  `PROTO_CHAL_MIN_MINOR=3`，挑战-应答只与次版本 ≥3 的对端互通）。
- UI 基建收拢到 `ui_common.py`（异常钩子/色板/ttk 风格单一来源，C3）。
- CI（GitHub Actions 单测回归）+ ruff/mypy 基线（核心三文件），测试 700+ 项（精确项数以 run_tests.bat 输出为准，避免这类计数漂移）。
- 审计编号体系切换：新条目改用 `模块-序号`（docs/audit/，C5）。

## [1.3.0] - 2026-09-11（tag v1.3.0 = dab6e5a）

- **MultiView 多人互看 v1**：观看组 hub 内核、上行帧路由、画面源卡片与共享按钮、
  全局热键切换画面源、停止共享自动注销并回落 local。
- 默认省流参数 + 静止检测（因误判默认关闭）+ NVENC 转 VBR（静止画面不再付满码率）。
- 四轮 BUG 审查：99 条缺陷全部修复并逐条标注（2026-09-03 前后）。
- 优化方案阶段 0/1/2 落地（第 102–110 条）：连接上限门控、消息接收总时限、慢客户端淘汰修正等。
- 品牌改名 **TeamVision**（队友视野），LICENSE 非商业条款、README 上线。
- 打包转为 PyInstaller **onedir** + Inno Setup 安装包（`TeamVision-Setup-1.3.0.exe`）。

## [1.0 – 1.2] - 2026-09 早期（SakuraVision 时代）

- 初始版本（02fa4af）：host（DXGI/mss 采集 + H.264/HEVC NVENC 编码 + JPEG 兜底 + TCP 多客户端广播）
  + viewer（PyAV 解码 + Tk 悬浮窗、鼠标穿透、频道管理）。
- 樱花 frpc 内置管理卡片、`config.json` 体系、`--addr` 临时频道、登录框交互。
- 逐条演进见 [`audit/`](audit/) 冻结对照表第 1–101 条。
