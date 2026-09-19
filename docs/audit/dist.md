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
