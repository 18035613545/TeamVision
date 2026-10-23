# dist — 打包与发布（PyInstaller spec / installer.iss / build_*.bat / 版本号 / 签名 / 依赖 / 安装路径）

新条目格式：`dist-序号｜一行现象｜关联测试文件路径`（序号从 001 起，无对应测试写 `—`）。
以下"示例"仅演示新格式；历史条目以旧编号原样保留在末节，不重编号。

示例（演示新格式，关联测试暂无则写 `—`）：
- `dist-001｜onedir 打包形态与用户级安装路径探测（原第 101 条 / 第 131 条打包面）｜—`

dist-002｜安装脚本 dist 产物缺失即中止 + 产物空壳校验（不再产出「安装包已生成」的空包且 exit 0）｜—
dist-003｜splash.png 已随 common.APP_VERSION 重新生成（版本漂移消除）+ 图片版启动路径标注为遗留（现行 show_fast_splash 现绘）｜tests/test_item99_gen_assets.py
dist-004｜更新检查默认关闭（opt-in；开启会周期性向 api.github.com 暴露 IP 与在用事实）｜tests/test_b4_update_check.py
dist-005｜更新检查缓存临时名带 PID+线程号、data_dir 缺失时不检查｜tests/test_b4_update_check.py
dist-006｜checked_at 为未来时不再判新鲜（时钟回拨不再永久停检）｜tests/test_b4_update_check.py
dist-007｜更新检查失败提升到 INFO 留痕｜tests/test_b4_update_check.py
dist-008｜诊断报告「注册策略」打印归一后取值（与运行时实际策略一致）｜tests/test_diag.py
dist-009｜共享端控制台补「检查更新」复选框并即时落盘生效（dist-004 改 opt-in 后，注释承诺的勾选入口此前并不存在）｜—
dist-010｜安装包守卫补查 _internal 目录 / pythonXX.dll / av.libs，并按验收标准加体积带（<90MiB 硬失败、目标约 111MiB 的 ±10% 之外告警）｜—
dist-011｜日志清理改按「距今 N 天」筛日期组（旧实现保留「现存的后 5 组」）+ 补总文件数 120 / 总字节 64MiB 绝对上界｜tests/test_bugfix_20260911.py
dist-012｜签名清单去硬编码版本号（安装包条目改通配、bat 从 installer.iss 反解 OutputBaseFilename）；「应签文件不存在」由跳过+exit 0 改为失败；UnknownError 仅自签放行｜—
dist-013｜setup.bat 默认走官方源，国内镜像改 --mirror / TV_USE_MIRROR=1 显式开关，并去掉对 HTTPS 源无收益的 --trusted-host｜—
dist-014｜诊断包在本进程一条日志都没有时（崩溃重启后 PID 已变，正是使用场景）退化为按 mtime 取最近 6 份，并在 report.txt 显式写明收集了什么｜—（tests/test_diag.py 全篇不传 pid=，该分支仍零覆盖，见 T-08）
dist-015｜四支 .bat 改存 ANSI/GBK 并把 chcp 65001 改为 936（UTF-8 无 BOM + 65001 实测会破坏命令边界，可把一条命令静默并进上一条 echo）｜—

dist-016｜签名失败升级为失败退出（build_exe.bat goto :failed、build_installer.bat exit /b 1）｜—（构建脚本，人工核验分支可达性）
dist-017｜导出诊断包先 deepcopy 快照再交工作线程（不再有 dictionary changed size 竞态）｜—
dist-018｜logs 目录里的诊断包纳入回收（只保留最近 _KEEP_DIAGS=5 个，按 mtime）｜tests/test_bugfix_20260924_round7.py
dist-019｜日志清理第二遍改逐文件 continue（首个不可删文件不再让整轮上界失效）｜tests/test_bugfix_20260924_round7.py
dist-020｜.gitignore 补 config.json.*（覆盖 .tmp/.corrupt-*/.lock，不匹配 config.example.json）｜—
dist-021｜installer.iss 卸载清单对齐实际数据目录（{app}ps-host、{app}ps-viewer 的数据项）｜—
dist-022｜CI 单测改调 run_tests.bat（口径单一来源）+ ruff/mypy 钉版并真正执行（本机两门均绿）｜—（CI 自身）
dist-023｜删除恒真的 if exist "%SETUP_EXE%" 与不可达 else｜—（构建脚本）
dist-024｜Inno 版本检测读卸载注册表 DisplayVersion，<6.3 明确报原因｜—（构建脚本）
dist-025｜编译前清理非当前版本的 dist\TeamVision-Setup-*.exe；fps-host.spec 硬校验 dxcam/comtypes（见修复记录 §7.4）｜—（构建脚本/打包）

第 8 轮（2026-10-10，`docs/audit/round8/`）：

dist-026｜**ruff 门禁不覆盖 `tests/`**：`ruff check *.py` 只扫根目录，实测 `ruff check tests` 积 **89 处**违规，其中 `F821 Undefined name`（就是 T-21 的 `fy2`）与 `F823` **正是本轮把 ruff 从"三个核心文件"扩到"全部根模块"时要防的那两类**——见 `pyproject.toml` 顶部那段"窄范围换来的全绿没有防护力"的自述。也就是说同一类失误在扩范围之后又发生了一次，只是换到了 `tests/`。现把口径改为 `ruff check *.py tests`（CI 与本地同一条命令），89 处清零｜—（CI 自身）

第 9 轮（2026-10-10 后，"部署与更新"增量）：

dist-027｜**"最新标签"不等于"本机这一端有新版本"**：一旦共享端/观看端拆成两个安装包分别发版，`releases/latest` 只会是其中一端的新版本，另一端的 `latest` 停在更旧的 tag 上——按标签判定的 `check_update` 从此对该端**永不提示**。新增 `update_check.check_release(role, ...)`：在第一页发布里按资产文件名（Host/Viewer，按独立词匹配）挑出本机角色的最高版本；一条角色都认不出时退回标签口径，认得出却没有本端包时返回"无更新"（不把对端的包装给用户）；API 失败返回 `source="none"`、预发布标签跳过、24h 缓存按角色区分。**当前不接线**（现发布形态仍是单包），拆包落地时由它接管文案生成｜tests/test_round10_release_check.py

dist-028｜安装链两处口径缺口：(a) `installer.iss` 的 AppVersion / OutputBaseFilename / 文件头是手抄版本，漏改一处就产出"陈旧版本号"的安装包（第 67 条同族），且整条链在 CI 之外；(b) `[UninstallDelete]` 用 `filesandordirs` 整目录删除 `%LOCALAPPDATA%\TeamVision`——那是同机两端**共用**的数据目录，卸掉一端会静默清空另一端（拆包后必现，与"卸载要清凭据"的要求也不是同一件事）。现 (a) 三处纳入单测与 `common.APP_VERSION` 逐条比对；(b) 共用数据目录完全不进卸载清单（用户数据原样保留）。dist-028 初稿写的 `UninstallablePathsDataFiles` 经 Inno 6.7.3 实编译证明**不存在**（Unrecognized [Setup] section directive），已撤回，并加反向单测钉住「iss 里不得再出现它」｜tests/test_round9_installer.py

dist-029｜**mypy 门禁的存量红登记**：`pyproject.toml` 的 4 模块清单（第 8 轮未提交改动）带来 5 条存量错误——`codec.py` 的 av 可选导入把模块/类型名赋 None（3 条）、`_WARNED_ONCE: set` 与 `_PICK_WARNED: set` 缺类型实参（2 条）；按 HEAD 的 pyproject（只查 common.py）为绿，说明这是清单扩面时没跑过的红。本轮写范围不动这些行，改为在 `pyproject.toml` 里按错误类别 `disable_error_code` 显式放行并写明修法——**代价是 `assignment`/`misc`/`var-annotated` 三类在 CI 里整体不可见**，修掉 5 处后必须删掉这三行｜—（CI 自身）

第 12 轮（2026-10-23，工程收口）：

dist-030｜**打包链完全在 CI 之外**：AGENTS.md 早已登记此事，但一直没补。第 9 轮的实际代价很具体——`installer.iss` 里被写进一条 Inno **不存在**的指令（`UninstallablePathsDataFiles`），单测与 lint 全绿，直到真正编译安装包才炸（6.7.3 报 `Unrecognized [Setup] section directive`）。现补 `package` job（`workflow_dispatch` 或 `v*` tag 触发，**不挂 PR**——一次完整打包约 10 分钟）：装依赖 → 补 vendor（dxcam 钉在 DXcam 的 `v0.3.0` tag 上；PyPI 上的 0.0.5 是滞后的旧版，且钉 tag 是为了上游 HEAD 变动不把 CI 弄红）→ PyInstaller 出两个 exe → 校验产物（**av 的 FFmpeg DLL 必须在**：漏了 `import av` 失败被 codec.py 静默吞掉、用户端无声退回 JPEG，带宽 5-20 倍回退——这正是 dist-068 的成因）→ choco 装 Inno Setup 6 → ISCC 编译安装包 → 校验产物。**刻意不含签名**（需要证书，CI 里没有；无证书时 sign_exe.ps1 会自签，那是本机行为，验不了"对外分发"）与 `frpc.exe`（第三方产物，installer.iss 对它是 skipifsourcedoesntexist）｜—（CI 自身）

dist-031｜**CI 无法手动触发**（`tests` workflow 没有 `workflow_dispatch`）：想验一次得推分支或改点无关代码——这是"门禁之外的事"长期没被跑起来的原因之一。已补 `workflow_dispatch`，并让 `v*` tag 也触发（发版前自动验一次）｜—（CI 自身）

dist-032｜`update_check.check_release`（第 10 轮写好、dist-027 登记"当前不接线"）**是一段没人调的死代码**——死代码会烂，而它烂掉时没人会发现（没有调用方就没有覆盖）。现接线：`check_update` 就是 `check_release` 的文案出口（传 role：共享端 "host"、观看端 "viewer"）。两处实现要点：① **缓存里存原始发布列表而不是判定结果**——判定要按角色来，同机两端的共享端与观看端看到的是同一份列表、结论不同，把判定结果存进缓存会让两端互相把对方的缓存顶掉（每次都判定为"缓存过期"而重新打网络）；② 取数从 `/releases/latest` 改成 `/releases?per_page=10`——拆包后要在一页里找出"本机角色最高的那次发布"，只取 latest 一条找不出来。旧格式缓存（只有 tag/page）仍可读，升级后不会白白多打一次网络｜tests/test_round10_release_check.py + tests/test_b4_update_check.py + tests/test_round9_update_links.py

dist-033｜`.archify/`（本机 AI 工具的输出目录）此前一直靠"没 `git add` 就没提交"侥幸挡住——这类"靠人不犯错"的防线迟早会被一次 `git add -A` 破掉。已进 `.gitignore`｜—

dist-034｜**mypy 存量红已清零**（dist-029 的收口）：那三行 `disable_error_code` 让 `assignment`/`misc`/`var-annotated` 三类错误在 CI 里**整体不可见**——为 5 条存量红长期关掉三道检查，代价远大于收益。修法与当初 pyproject 里写下的方案一致：`codec.py` 的 `av = None` → `av = cast(Any, None)`；`PictureType` 不再作为模块级类型名单独导入（把类型名赋 None 会被判 "Cannot assign to a type"），改从 `av.video.frame.PictureType` 取；`_WARNED_ONCE: set` / `_PICK_WARNED: set` → `set[str]`。三行放行随之删除｜—（CI 自身）


## 历史（旧编号，冻结）

> 逐条来自 `docs/audit/README.md` 冻结对照表，归属为 dist 的条目。一行一条 `第 N 条｜简述`。

- 第 13 条｜非管理员装 Program Files，import 期建 logs 即 PermissionError
- 第 67 条｜版本号四处漂移（common/version_info/iss/bat/文档），会发陈旧产物
- 第 68 条｜build_exe.bat 重生成 spec 抹掉 pyav collect_all，静默退 JPEG
- 第 69 条｜vendor pathex 污染打包，两个 exe 打进的 numpy 版本不同
- 第 70 条｜requirements 全 `>=` 无上限，dxcam 装到的非实测 vendored 版本
- 第 71 条｜缺 frpc.exe 仍能编出"成功"安装包，用户装完自启失败
- 第 72 条｜三个 .bat 无 cd /d %~dp0 且每条退出以 pause 结尾，CI 挂死
- 第 73 条｜setup.bat 不查 conda activate 返回值就 pip install，装进 base
- 第 74 条｜installer.iss 未装 splash.png，安装版静默降级为纯文字启动画面
- 第 101 条｜Inno Setup 用户级安装路径探测
