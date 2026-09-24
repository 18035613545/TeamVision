# cfg — 进程级基建（config.json 读写 / 可写性探测 / 日志初始化）

新条目格式：`cfg-序号｜一行现象｜关联测试文件路径`（序号从 001 起，无对应测试写 `—`）。
本模块为第 5 轮审查（2026-09-18）新增归属（原 cfg 类条目散落在 common/logger）。

- `cfg-001｜config.json 原子写升级为跨进程文件锁 + 唯一临时名 + os.replace 重试（第 17 条口径补齐；锁语义已于 cfg-004 整体重做）｜—`
- `cfg-002｜可写性探针文件名带线程号，删除失败不算失败（多线程首调不再互删误判）｜—`
- `cfg-003｜setup_logger 初始化加锁（双线程首调不再各挂一套 handler）｜—`
- `cfg-004｜跨进程锁改 OS 级字节范围锁（msvcrt.locking，进程死即内核释放）+ 拿不到锁 fail-closed 放弃本次写并告警；旧实现两个洞：接管阈值 10s > 等待上限 2s、Windows 下活锁根本无法 remove 接管｜—`
- `cfg-005｜陈旧锁判定的墙钟年龄随 cfg-004 重做整体消失（不再有「锁龄」概念，时钟回拨无从影响接管）｜—`
- `cfg-006｜_persist_upgrade 与更新检查缓存补 finally 删唯一临时名，与 save_config 清理口径对齐（此前写失败永久留下含明文 frp token 的残渣）｜—`
- `cfg-007｜config_version 解析补 OverflowError：一个 Infinity 不再让整份用户配置被 _recover_corrupt_config 重置为默认｜—`
- `cfg-008｜safe_int docstring 与实现相符（越界是钳制），并补 clamp=False 模式；监听端口按「要么合法要么回退默认 + WARNING」处理｜—`
- `cfg-009｜docs/audit 的「关联测试」三处与事实相反（auth-009 登记落空 / capture-002 假绿未标 / codec-009 有效用例被记为无），已按 tests/ 实测逐行更正｜—（本条本身是索引准确性条目，无代码测试；核验证据见 docs/修复记录-2026-09-20-BUG审查报告.md §7）`

cfg-010｜save_config 读基线失败即放弃本次写（不再把整份配置塌缩成「只含本进程那一节」）｜tests/test_bugfix_20260924_round7.py
cfg-011｜新增 _atomic_write_json：load_config 首建与 _recover_corrupt_config 也走锁 + tmp + os.replace｜tests/test_bugfix_20260924_round7.py
cfg-012｜save_config 写盘失败分支改 except Exception：非 OSError 也清 tmp 并记 ERROR｜tests/test_bugfix_20260924_round7.py
cfg-013｜save_config 的 False 返回值在 11 个调用点接线（GUI 状态栏标红 / 面板提示 / 日志告警）｜—
cfg-014｜listen_host 归一化口径统一（_upgrade_config / _listen_target / diag 三处都 strip().lower()）｜tests/test_bugfix_20260924_round7.py
cfg-015｜_upgrade_config 返回 (changes, notes)：纯说明只记 INFO，不再冒充「配置已升级」并落盘｜tests/test_bugfix_20260924_round7.py

## 历史（旧编号，冻结）

> 第 17 条｜config.json 原子写 + section 读改写（本次升级为跨进程锁，见 cfg-001）
> 第 14 条｜日志 handler 初始化竞态（本次加锁，见 cfg-003）
