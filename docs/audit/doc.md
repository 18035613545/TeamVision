# doc — 文档与代码口径（README / docs/install.md / docs/faq.md / docs/changelog.md 与实现不一致）

新条目格式：`D-序号｜一行现象｜关联测试文件路径`。
本模块承载第 6 轮审查（2026-09-20）**D 系列**（报告第十一章的 13 条文档口径不一致）。

- 编号沿用报告的 `D-01`~`D-13` 原样，不另起 `doc-001` 序列——修复记录与报告都按该编号互查；
  这与 README「历史条目以旧编号原样保留、不重编号」的处理方式一致。
- **测试位的含义在本模块略有不同**：文档本身没有测试，写的是"钉住该口径所对应的**代码行为**"的用例；
  纯文字更正（改一个名字、一个默认值描述）一律写 `—`。
- 逐条落地记录见 [`docs/修复记录-2026-09-20-BUG审查报告.md`](../修复记录-2026-09-20-BUG审查报告.md) §6。

D-01｜迁移条件文档写"0.0.0.0 **且** auth.enabled false"，代码是两个各自独立的 if（任一命中即改对应字段）——文档改"或"｜tests/test_item131_hardening.py
D-02｜文档称"默认/兜底只允许本机"，非法或空 listen_host 却兜底 ""→绑 0.0.0.0：代码侧统一收紧为 127.0.0.1（与 ui-007 同向）｜—
D-03｜install.md 的 `update_check.enabled` 默认值写 true，代码是 false（dist-004 改 opt-in）——文档改 false 并说明动机｜tests/test_b4_update_check.py
D-04｜changelog 两处写协议 v2.2，漏记 auth-002 带来的 v2.3——已补记；同文件"安全"段的 proof 公式仍是 v2.2 旧写法，属残留｜—
D-05｜文档写日志名 `app_YYYYMMDD.log`，实际带 PID：三处改 `app_YYYYMMDD_pid<N>.log`｜—
D-06｜文档写「一键诊断」按钮，实际按钮文字是「导出诊断包」：两处改名｜—
D-07｜文档称登录框显示"账号不存在"，require 模式下该文案已被统一为"用户名或密码错误"（防枚举）：两处删过期举例｜tests/test_d1_challenge_auth.py
D-08｜文档称 splash.png 由安装包自带，实际 iss 只装 app.ico、splash 由 spec 的 datas 内嵌进 exe：改口径｜—
D-09｜测试数量三处不一致（README 480 / changelog 593 / 实测 595）：两处统一为实测值；精确计数属易漂移项｜—
D-10｜README 主要依赖写 opencv-python，requirements.txt 是 opencv-python-headless（照 README 手装会带入 Qt 链）｜—
D-11｜README 写上行自动值 "0=自动 2×bitrate_kbps"，实际是 max(600, 2×bitrate)：补 600 Kbps 地板｜—
D-12｜README 承诺 DEFAULT_CONFIG 为单一真源，面板三处兜底值（fps/enabled/注册策略）与之漂移：代码侧改同源；「注册策略」一子项经报告复核撤销｜—
D-13｜faq/changelog 引用未跟踪文档「实现方案 §11.3」，克隆后引用落空：两处删悬空引用（文件本身仍未跟踪，属仓库决策）｜—

D-14｜四处注释/docstring 与代码行为相反：screen._pick_output_idx、host_ui 自检文案、host UNSHARE 分支、host._listen_target —— 全部按修复后的实现改写｜—
