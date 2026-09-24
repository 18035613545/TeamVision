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
