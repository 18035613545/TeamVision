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
