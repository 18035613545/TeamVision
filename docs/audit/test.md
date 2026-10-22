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
T-06｜ui-020 登记的 test_startup_order.py 只锁"顶层不 import 重依赖"，与 DPI 声明顺序无关；全仓 tests 无 enable_dpi_awareness 引用｜tests/test_bugfix_20260920_t06_selfcheck.py（含 enable_dpi_awareness 先于首个 tk.Tk() 的 AST 顺序锁 + 自检线程真跑一次）
T-07｜auth-011 的用例从 challenge_for 取回 iter 再派生，实现若仍继承旧 iter 两侧同值 → 改前改后都绿｜tests/test_bugfix_20260920_t07_t09_t10.py（直接断言改密后落库 iter == PBKDF2_ITERATIONS + 缺陷形态负向对照）
T-08｜auth-012/ui-008 的测试配置只含精确键、test_logs_included 不带 pid 调用、_post_ui 被改成同步执行，绕开被修代码｜tests/test_bugfix_20260920_dist014.py（替身不再静默吞 pid + 显式钉两个生产调用点传 os.getpid() + 包含式脱敏键断言）
T-09｜dist-005/006/007 的用例全部显式传 cache_path，临时名与 data_dir 为 None 分支永不执行；失败留痕只 assertIsNone｜tests/test_bugfix_20260920_t07_t09_t10.py（checked_at 在未来仍须重取、data_dir 为 None 不触网、INFO 留痕、写失败不留 .tmp）
T-10｜codec-002~008 登记为「—」且 set_fps 失败路径/多包合并/is_key 门禁/码率迟滞/键帧回话零回归｜tests/test_bugfix_20260920_t07_t09_t10.py（codec-012 回滚与重试、codec-013 host 侧 is_key 门禁）；codec-009 的登记失真已随 cfg-009 更正为 tests/test_item97_stop_race.py；多包合并/码率迟滞仍属第 5 轮遗留的 —
T-11｜auth-009 的跨进程锁与 cfg-001~003、net-016/017、ui-003~019 整体缺覆盖；_file_lock/_read_disk 在 tests/ 内零引用｜tests/test_bugfix_20260920_cfg004_lock.py（两个真实进程竞争同一 config.json，按报告要求不用 mock）；net-016/017 与 ui-003~019 的其余部分仍为 —，属元条目、随后续批次补
T-12｜初稿"ui-011 的桩钉不住修复"方向写反，报告已整条撤销；仅保留成立部分——_prompt_blocked_until 的**赋值侧**用哪个时钟无断言｜—（不计为待修项）
T-13｜test_d1_challenge_auth.py 的 last_login 与秒级墙钟字符串相等比较，跨秒偶发红（flaky）｜tests/test_d1_challenge_auth.py（已改为把记录值解析成时间戳、只要求落在本次调用前后的合理窗口内）

T-14｜面板不重叠不变量用例只取宽屏、刻意避开已知失效组合：参数组补窄屏 1366/1280/1024，断言改为矩形不相交，并补收窄/纵向钳制/下移/拖动同步四例｜tests/test_panel_float_no_overlap.py
T-15｜assertTrue(app.running is False or ... is True) 恒真：换成结论侧真断言（_build_ui + mainloop + running 复位）并补「子控件销毁失败仍要清尺寸」｜tests/test_viewer_splash_root_reset.py
T-16｜自造 deque(maxlen=900) 从未调生产代码：上限提为 viewer._LATENCY_RING_MAX 供引用，封顶用例改经 _store_frame 灌满验证｜tests/test_latency_p95.py
T-17｜约 17 个文件、39 处 inspect.getsource：已删 2 处与行为用例重叠的字面串断言、把 1 处字面串改为行为断言（spawn 炸弹）；残留为与行为用例配对的 AST/结构断言（如"还原在 finally 内""先 _load_heavy 后 import dxcam"），见修复记录 §7.1｜tests/test_item100_fixes.py + tests/test_item96_sock_timeout.py + tests/test_item131_hardening.py（本次改动的三处）
T-18｜pytest/unittest 两套 runner 无交叉：新增守卫用例扫描模块顶层 test_* 函数并显式失败；CI 单测统一走 run_tests.bat｜tests/test_bugfix_20260924_round7.py
T-19｜3 个 MSG_FRAME 用例依赖真实屏幕采集：host._capture_loop 加 capture_factory 注入缝，Hub 注入伪屏幕｜tests/test_multiview_integration.py
T-20｜D1 挑战-应答三个用例在断言 `out["result"]` 前**不等后台认证线程**：`out["result"] = host._auth_required_phase(...)` 是先完整求值右侧再赋值，而 socket 回包在函数**内部**已写出，观看端 `_authenticate` 读到回包即可返回——两者只隔一次 GIL 切换。全量跑（线程多、切换频繁）时断言偶尔先跑到，`out.get("result")` 得 None，CI 随机变红；单独跑 20 次（子进程）+ 80 次（进程内）都不复现。`_serve_phase` 改为返回线程，断言前经 `_await_served` join（并把线程内异常翻出来，否则断言只显示 `None != (True, 'carol')`）｜tests/test_d1_challenge_auth.py

### 第 8 轮（2026-10-10，`docs/audit/round8/`）

本轮的 T 条目集中在**同一件事的四个面**：测试"通过"不等于缺陷被钉住。判定手段也不再靠读代码猜，
而是（a）对断言做反汇编看**哪一项真的被求值**；（b）用 spy 给生产方法挂调用记录跑**全量 discover**，
看"删掉这行测试还会不会绿"；（c）把旧实现做**类级运行时注入**，确认新用例在缺陷形态下变红。

T-21｜`tests/test_panel_float_no_overlap.py:192-193`（T-14 的核心不变量）里 `fy2` 是**未定义名**：`_float_rect` 返回 4 元组、第 192 行用 `_` 丢掉了悬浮窗底缘，第 193 行却引用它；而 7 个组合里项 1 `px2 <= fx1` 全部为真（面板右缘按定义停在 `float_x - _PANEL_GAP`）→ `JUMP_IF_TRUE_OR_POP` 直接跳过，第 4 项**一次都不求值**（`LOAD_GLOBAL fy2` 存在但永不执行）。补上第 4 个值后**仍绿**——纵向两项是"恰好成立"而非被掩盖，故未指向生产缺陷；另补负向对照 `test_overlap_judgement_is_not_masked_by_shortcircuit` 防短路复活｜tests/test_panel_float_no_overlap.py

T-22｜**4 个测试文件只在全量 discover 的收集顺序帮忙时才通过**（单跑必红）：`test_item9_source_reject.py`（11 errors）、`test_bugfix_20260920_t05_viewer.py`（12 errors）、`test_viewer_hotkey.py`（7 errors）、`test_multiview.py`（1 failure）。根因是 `viewer.codec_mod` / `host` 侧 `CaptureManager` 这类**惰性全局**只由 `_load_heavy()` 绑定，而全量跑时靠 `pipeline.py`/`share.py` 的模块级 `from screen import …` 在**收集期**顺带触发——即"这类依赖只在收集顺序帮忙时存在"。四个文件已加模块级显式加载（**不用 try/except 吞失败**：环境缺重依赖时必须明确报错，不能退化成静默跳过）。逐模块单跑体检 **67/67 OK**。体检命令按**退出码**判定：用 `-match 'FAILED'` 会被生产日志里的 `getaddrinfo failed` / `BitBlt failed` / `frpc: login to server failed` 误报 3 个假阳性，用"取输出末行"会被 `test_item100_fixes` 打在生产 traceback 之后的输出骗到｜tests/test_item9_source_reject.py + tests/test_bugfix_20260920_t05_viewer.py + tests/test_viewer_hotkey.py + tests/test_multiview.py

T-23｜`tests/test_capture011_region_geom_missing.py` 的 `test_never_leaks_absolute_coordinates` **无条件通过**：`if reg is not None:` 把 `assertRaises` 整个包住——缺陷形态（返回越界矩形 `(1920,100,2720,700)`）进入断言并通过，修复形态（`reg is None`）整段不执行也通过。改为结论侧断言（`assertIsNone(reg)` + `assertTrue(out_of_range)`），并补一条直接断言旧返回值必被校验器拒的负向对照。注意 capture-011 的功能修复本身**没有失守**（`test_returns_out_of_range_flag` 与 `TestGrabEndToEnd` 三条会红），本条只说明它不该被算作判定力来源｜tests/test_capture011_region_geom_missing.py

T-24｜`tests/test_bugfix_20260924_round7.py` 的日志回收用例**断言过松**：`assertGreaterEqual(deleted, 2)` 而夹具确定值是 **4**（created=125 / remaining=121），"删到 2 个就停"也会绿。改为按夹具公式断言（`created - 1 - _KEEP_FILES`，不硬编码 4，抗 `_KEEP_FILES` 变更）｜tests/test_bugfix_20260924_round7.py

T-25｜`tests/test_item93_cred_dialog.py` 是 `tests/` 下**唯一带 UTF-8 BOM** 的文件（与 `AGENTS.md` 的"UTF-8 无 BOM"约定不符；当前无功能后果，CPython tokenizer 认 BOM）。已剥离 BOM，行尾逐字节核对未动（CRLF 155 / LF 155 前后一致）｜tests/test_item93_cred_dialog.py
