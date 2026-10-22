# capture — 采集（screen.py / pipeline.CaptureSource·StillGate / 采集区域与静止门控）

新条目格式：`capture-序号｜一行现象｜关联测试文件路径`（序号从 001 起，无对应测试写 `—`）。
以下"示例"仅演示新格式；历史条目以旧编号原样保留在末节，不重编号。

示例（演示新格式）：
- `capture-001｜采集/静止门控统一到 pipeline.CaptureSource / StillGate（原第 111 条）｜tests/test_optimize_stage2.py`

capture-002｜采集异常上抛给自愈链（连续异常重建可达）、告警 10s 冷却，故障后不再永久黑屏刷日志｜tests/test_bugfix_20260920_t01_capture.py（真实 CaptureManager.grab：上抛 + 计数 + 后果链 + 旧逻辑负向对照）。原登记 tests/test_optimize_stage2.py 为**假绿**——该用例把 pipeline.CaptureManager 整体替换掉，screen.py 的 except→计数→上抛从未执行（cfg-009/T-01）
capture-003｜dxcam 区域按「显示器内相对坐标」换算（mss 绝对坐标 → 相对），越界回退 mss｜—
capture-004｜显示器序号按几何矩形匹配 DXGI 输出（0=全部屏幕回退 mss），不再错屏/IndexError 静默回退｜—
capture-005｜--console 也声明 DPI 感知（不再与「是否有窗口」绑定），mss 逻辑像素与 dxcam 物理像素不再混用｜—
capture-006｜ensure_vendor_on_path() 提到 _open() 开头：源码运行首个采集会话的 DXGI 几何匹配不再恒失败；该 except 由 debug 提为一次性 warning｜—

capture-007｜_pick_output_idx 分清「几何枚举不可用」（保留序号回退 + 一次性 WARNING）与「无几何一致项」（返回 None 回退 mss）｜tests/test_bugfix_20260924_round7.py
capture-008｜normalize_region 放行负原点（副屏在主屏左侧可框选）；host_ui 越界校验改按显示器并集四至｜tests/test_bugfix_20260924_round7.py
capture-009｜share 侧空帧重建改时间判据 5.0 秒（与 host 对齐，静止下不再每 ~1.5 秒重建 DXGI）｜tests/test_bugfix_20260924_round7.py
capture-010｜dxcam 匹配告警集提到模块级（跨重建实例只报一次）｜tests/test_bugfix_20260924_round7.py
capture-011｜`_dxgi_region` 在「显示器几何取不到」时按越界处理并回退 mss，不再把虚拟桌面绝对坐标当输出内相对坐标递给 dxcam（0.3.0 抛 ValueError → 连续 5 次触发一轮重建循环）｜tests/test_capture011_region_geom_missing.py
capture-012｜新增 `probe_geometry`/`geometry_changed` 几何漂移判据，`_grab_mss` 整屏分支改用探针矩形；显示设置变更后不再冻结在旧帧（mss 的 `monitors` 每实例只算一次，而 `BitBlt` 源矩形离屏时返回成功却不写目标 DIB → 三条判据全静默）｜tests/test_capture012_geometry_drift.py
capture-013｜`_grab_mss` 区分「序号越界」的两类成因：曾有效 → 屏被拔除，放弃本帧；从未有效 → 配置写错（`safe_int` 只有下界无上界，`monitor: 99` 可达），降级到第 1 块屏继续出图并指明 `host.capture.monitor`。判据是 `_monitor_rect()` **之前**的 `self._geometry`——它内部会刷新该字段，之后再取则恒判为配置错误｜tests/test_capture012_geometry_drift.py
capture-014｜区域采集也走几何探针（新增 `_fit_region_to_desktop`）：区域矩形在桌面内是否有效由当前几何决定，分辨率改小后旧区域越界 → `BitBlt` 离屏"成功且不写入"，实测同尺寸离屏源返回上次抓帧的**逐字节副本**、部分越界写入全零。部分越界夹到可见交集，完全离屏退回整屏（不退成丢帧，否则永久黑屏不自愈）｜tests/test_capture012_geometry_drift.py

第 8 轮（2026-10-10，`docs/audit/round8/`）：

capture-015｜`FrameEncoder._ensure_encoder` 的「就地重建失败」路径**每帧重试一次完整编码器构造、不落负缓存**：同一个函数里的构造失败路径有 `_retry_at`/`_backoff` 指数退避，而 `set_fps(fps)` 返回 False 时（`codec.py:334-337` 的 codec-012 有意回滚 `_fps` 让下一帧重试——那个决定是对的）没有任何频率约束。实测 30 帧调用 30 次 `_create_ctx_with`（源码自述单次 93–110ms），而 30fps 每帧预算只有 33ms，且该状态下 `available` 恒 True、**永不回退 JPEG**。现给该路径独立退避（`_fps_retry_at`/`_fps_backoff`，换实例即复位）——**刻意不复用构造退避**，共用会让一次帧率失败顺带推迟一次真正需要的重建｜tests/test_round8_capture.py

capture-016｜`_refresh_geometry` 的降级承诺**兑现不了**：except 分支（mss 私有字段 `_monitors` 不可用/结构变化）返回 True 并声称"改用重建采集器"，但该函数全仓只有一个调用点 `_monitor_rect`（纯查询热路径），采集器重建的唯一入口是 `CaptureSource._rebuild()`，且上游真改了 `_monitors` 时重建出来的 mss **同样没有该字段**——重建 100% 无效。现删掉承诺、订正 docstring（返回值只表示"本帧检出了几何漂移"，两个出口同义），并把原来只锁"返回 True"的用例改为钉四个可观测后果（基准已刷新 / 未触发任何重建 / 不外抛 / 之后取到探针新几何而非陈旧缓存）｜tests/test_capture012_geometry_drift.py

## 历史（旧编号，冻结）

> 逐条来自 `docs/audit/README.md` 冻结对照表，归属为 capture 的条目。一行一条 `第 N 条｜简述`。

- 第 5 条｜采集器句柄置 None 后空转，会话活着什么都不传
- 第 32 条｜"恢复全屏"是 no-op，旧采集区域被保留并回写
- 第 33 条｜采集区域校验形同虚设，非法区域致每帧抛错黑屏
- 第 61 条｜threading.Timer 非守护且不 cancel，退出后仍新开上传会话
- 第 87 条｜静止检测数逐通道元素、quiet_ms 实为秒（当前 enabled=False 不可观测）
- 第 88 条｜calc_output_size target_width=1 时返回宽 0，resize 抛错静默杀线程
- 第 97 条｜stop() join 超时 < 发送超时，在上传线程脚下关掉编码器与句柄
- 第 100 条｜dxcam output_color=BGR 红蓝互换（此号被 13 处杂项修复复用）
- 第 111 条｜采集/静止门控统一到 pipeline.CaptureSource / StillGate
