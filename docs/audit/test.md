# test — 测试有效性（回归保护是否真的钉住修复：T 系列）

新条目格式：`T-序号｜一行现象｜关联测试文件路径`。
本模块承载第 6 轮审查（2026-09-20）**T 系列**（报告第十章的 13 条"测试有效性"判定）。

- 编号沿用报告的 `T-01`~`T-13` 原样，不另起 `test-001` 序列——报告与修复记录都按该编号互查。
- **测试位的含义在本模块略有不同**：写的是"**现在**把这个修复改回缺陷形态会让它转红"的用例文件；
  补测仍在并行批次进行中，**未落盘的一律写 `—`，不预填文件名**。
- 判定标准取自报告第十章：判绿的正确问法是"把被修复的代码改回缺陷形态，测试会不会红"。
- 逐条落地记录见 [`docs/修复记录-2026-09-20-BUG审查报告.md`](../修复记录-2026-09-20-BUG审查报告.md) §8 末段。

T-01｜capture-002 的旧用例把 pipeline.CaptureManager 整体替换，真实 grab 的「计数→上抛」从未执行（假绿）｜tests/test_bugfix_20260920_t01_capture.py
T-02｜net-015/net-030 只锁了泵侧契约，真实 host._client_recv_loop 的记账与评估时机零覆盖｜tests/test_bugfix_20260920_t02_recvloop.py
T-03｜auth-002 只有常量断言，iter/salt 校验与观看端三条拒登分支无用例（"中间人回 iter=1"从未模拟）｜tests/test_bugfix_20260920_t03_challenge.py
T-04｜auth-010 的替身只提供 count()，命中生产代码专为替身保留的 except AttributeError——测试反向要求实现保留漏洞｜tests/test_item131_hardening.py（退化分支已删、替身显式提供 bootstrap_used()，并补"删空账户后窗口不重开"用例）
T-05｜ui-002 的 _invalidate_media 与三处状态跃迁在 tests/ 中零调用，_show_text 被 lambda 吃掉｜tests/test_bugfix_20260920_t05_viewer.py
T-06｜ui-020 登记的 test_startup_order.py 只锁"顶层不 import 重依赖"，与 DPI 声明顺序无关；全仓 tests 无 enable_dpi_awareness 引用｜—（补测在途）
T-07｜auth-011 的用例从 challenge_for 取回 iter 再派生，实现若仍继承旧 iter 两侧同值 → 改前改后都绿｜—
T-08｜auth-012/ui-008 的测试配置只含精确键、test_logs_included 不带 pid 调用、_post_ui 被改成同步执行，绕开被修代码｜—（补测在途）
T-09｜dist-005/006/007 的用例全部显式传 cache_path，临时名与 data_dir 为 None 分支永不执行；失败留痕只 assertIsNone｜—
T-10｜codec-002~008 登记为「—」且 set_fps 失败路径/多包合并/is_key 门禁/码率迟滞/键帧回话零回归｜—（codec-009 的登记失真已随 cfg-009 更正为 tests/test_item97_stop_race.py）
T-11｜auth-009 的跨进程锁与 cfg-001~003、net-016/017、ui-003~019 整体缺覆盖；_file_lock/_read_disk 在 tests/ 内零引用｜—（cfg-004 重做后的双真进程竞争用例在补测批次中，报告明确警告用 mock 替换会再次假绿）
T-12｜初稿"ui-011 的桩钉不住修复"方向写反，报告已整条撤销；仅保留成立部分——_prompt_blocked_until 的**赋值侧**用哪个时钟无断言｜—（不计为待修项）
T-13｜test_d1_challenge_auth.py 的 last_login 与秒级墙钟字符串相等比较，跨秒偶发红（flaky）｜tests/test_d1_challenge_auth.py（已改为把记录值解析成时间戳、只要求落在本次调用前后的合理窗口内）
