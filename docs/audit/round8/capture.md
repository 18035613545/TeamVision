# 第 8 轮审查 · 域 A：采集与编码链路（screen / pipeline / share / codec）

- **负责范围**：`screen.py`（含本轮 +355 行未提交改动：`probe_geometry` / `geometry_changed` /
  `_refresh_geometry` / `_fit_region_to_desktop` / `_monitor_rect` / `_grab_mss` 全部分支）、
  `pipeline.py`、`share.py`、`codec.py` 的编解码本体（`VideoEncoder` / `VideoDecoder` /
  关键帧门控 / 切源重建 / 码率控制）。
- **审查时间**：2026-10-10（本回合）。
- **判定口径**：`docs/audit/round8/PROTOCOL.md`。
- **本机基线（自跑复现，非引用）**：
  - `ruff check *.py` → **exit 1，17 errors**（全部落在 `_tmp_audit_*.py` 临时脚本上；
    本审查员自己的 3 个脚本已按本机 ruff 口径清零，另有多条属其他队友/上一回合残留。
    `.gitignore` 忽略 `_tmp_*.py`，但它们**确实存在于工作树**。清理后
    `ruff check common.py codec.py screen.py pipeline.py share.py` → **All checks passed
    （exit 0）**，即生产文件零告警——见「跨域移交」C）。
  - `python -m mypy` → `Success: no issues found in 4 source files`（exit 0）。
  - `python -m unittest discover -s tests -p "test*.py"` → **Ran 874 tests, OK**，140.6s。
  - `tests.test_capture011_region_geom_missing` + `tests.test_capture012_geometry_drift`
    → **Ran 67 tests, OK**（两个新文件确实被 discover 收集）。
- **结论**：**2 条发现（P1×1、P2×1）**。以下「已排除」一节记录了两条被实测否定的强假设，
  以及覆盖到但未发现真阳性的范围，供后续轮次直接复用结论、不必重跑。

---

### R8-capture-01｜`FrameEncoder` 对「就地重建失败」每帧重试一次编码器构造，不落负缓存退避；同文件的构造失败路径却有限流

- **级别**：P1（功能错误 + 延迟劣化：30fps 的采集→编码链路退化成"每帧一次编码器拆装"，
  且该状态下**永不回退 JPEG**）
- **位置**：`pipeline.py:114-153`（`FrameEncoder._ensure_encoder`）、`codec.py:310-343`
  （`VideoEncoder.set_fps`）、`codec.py:360-372`（`_encode_locked` 的尺寸变化分支）
- **代码**：

  ```python
  # pipeline.py:118-133
  if self._encoder is not None:
      if fps is not None and fps != self._enc_fps:
          if self._encoder.set_fps(fps):      # ← 失败时返回 False，_enc_fps 不落账
              self._enc_fps = fps
      if bitrate is not None:
          ...
      return                                  # ← 直接返回：不设 _retry_at、不加 _backoff
  ...

  # pipeline.py:136-150（同文件、同函数的另一条失败路径，有限流）
  now = time.monotonic()
  if now < self._retry_at:
      return                     # 第 53 条：负缓存退避期，期间只走 JPEG 回退
  ...
  if enc is None or not enc.available:
      self._retry_at = now + self._backoff
      self._backoff = min(30.0, self._backoff * 2)
      return
  ```

  ```python
  # codec.py:334-337 —— codec-012 有意设计的"失败即回滚，让下一帧仍会重试"
  if ctx is None:
      with self._lock:
          self._fps = prev_fps    # codec-012：回滚，让下一帧仍会重试这一档
      return False                # 保留旧上下文继续出帧；帧率未生效
  ```

- **触发**：编码器实例存在且 `available=True`，但**就地重建**反复失败——即
  `FrameEncoder.encode()` 传进来的 `fps` 与 `self._enc_fps` 不同，而
  `_create_ctx_with()` 本轮全部候选编码器都打不开。此时每帧都满足
  `fps != self._enc_fps`，于是每帧重试一次完整重建。
  真值可达的成因：显存/内存紧张、NVENC 并发会话耗尽、驱动重置后 NVENC 暂时拒绝新会话——
  即 `set_fps` 失败的那些成因。
- **后果**：
  1. **编码线程/上传线程每帧被阻塞一次重建时长**。`_create_ctx_with`（`codec.py:239-244`）
     逐条 `av.CodecContext.create(...)` + `ctx.open()`，最多 4 条候选。源码内自有描述为
     "NVENC 拆装一次约 93–110ms"（`codec.py:235`）。30fps 下每帧预算 33ms，这一笔直接
     吃掉 3 帧以上；`host._encode_loop`（`host.py:3488-3536`）与 `share._run`
     （`share.py:177-209`）都在该调用栈上，卡住即掉帧/抬高端到端延迟。
  2. **这个状态不会回退 JPEG，也不会停**。负缓存退避（第 53 条）只在**构造新实例**失败时
     置位（`pipeline.py:146-150`）；`available=False` 触发 `_drop_encoder()`
     （`pipeline.py:116-117`）之后才轮到退避。就地重建失败既不改 `available`（旧上下文
     被有意保留，`codec.py:337`），也不经过 `_retry_at`，所以 `_backoff` 与 `_retry_at`
     一直是初值。
  3. 同一形态在 `codec.py:368-372` 的尺寸变化分支也存在：尺寸不符时 `_recreate(...)` 失败
     会走 `keep_old_on_failure=False` 把 `_ctx` 置空（`codec.py:266-270`），此后
     `available` 变 False → 一旦被 `_ensure_encoder` 看到就会 `_drop_encoder`，
     所以那条**有**兜底；缺兜底的正是 `set_fps` 这条。
- **证据**：`_tmp_audit_capture_cost.py` 实跑（`F:\conda\envs\fps-screen\python.exe`）：

  ```
  == A) set_fps 持续失败时 _ensure_encoder 的每帧行为 ==
     5 帧 encode() 后 set_fps 被调用次数 = 5 （=1 次/帧则说明持续重试）
     _enc_fps = 30  _backoff = 1.0  _retry_at = 0.0        ← 退避从未启动

  == B) 对照：编码失败 -> available=False -> _drop_encoder -> 退避 ==
     5 帧里尝试构造新编码器次数 = 1 （退避生效时应为 1）
     耗时 5 ms；_retry_at 已设 = True；_backoff = 2.0      ← 同一文件的有兜底路径

  == C) 真编码器重建的价格（本机实测）==
     VideoEncoder(...) x264 构造耗时 3.6 ms，available=True
     尺寸变化 _recreate(1280x720) = True，耗时 4.6 ms
  ```

  即：**A 组 5 帧 5 次重试、退避字段全为初值；B 组同文件同函数的另一条失败路径 5 帧
  只构造 1 次、退避正常启动**。C 组说明本机 x264 重建只要 4.6ms，所以这条缺陷的代价
  在"能建的编码器上测不出来"，只有真落到硬件编码器（源码自述 93–110ms）或
  4 条候选全失败时才显形——这也正是它至今没被发现的可能原因。
- **同族入口**（`grep -n "set_fps\|_ensure_encoder\|_drop_encoder\|_recreate("` 全仓）：

  | 路径:行 | 判定 |
  |---|---|
  | `pipeline.py:123` `self._encoder.set_fps(fps)` | **同缺陷**（本条主角） |
  | `pipeline.py:140-141` 构造 `codec_mod.VideoEncoder(...)` | 已正确（`_retry_at`/`_backoff` 限流） |
  | `pipeline.py:116-117` `available` 为假时 `_drop_encoder()` | 已正确（借道上面那条限流） |
  | `codec.py:370` `_encode_locked` 尺寸变化 `_recreate` 失败 | 已正确（`_ctx=None` → `available=False` → 走 `_drop_encoder`） |
  | `codec.py:393` 编码异常路径 `_recreate` | 已正确（连续 10 次后 `_permafailed`，`codec.py:386-391`） |
  | `codec.py:428-454` `set_bitrate` 找不到上下文 | 不适用（调用点 `pipeline.py:128-132` 整体在 `try` 内、只记 debug，`set_bitrate` 失败不阻塞编码线程） |
  | `codec.py:116` `__init__` 里的 `_recreate` | 不适用（构造期一次性） |

- **去重**：新。与 `docs/audit/codec.md` 的 codec-002/codec-012 **不是同一件事**：
  codec-012 的修复目标是"失败时**回滚** `_fps`，让这一档仍会被重试"（`codec.py:318-323`），
  本条说的是**这次重试没有限流**——把 `_fps` 回滚掉是对的，缺的是"重试频率"的约束。
  既有 audit 里没有该形态条目（`docs/audit/capture.md` / `codec.md` 逐条读毕）。
- **建议修复**：把 `pipeline.py:119-125` 的 `set_fps` 分支也纳入已有的负缓存退避——
  在 `self._encoder.set_fps(fps)` 返回 False 时也置 `self._retry_at = now + self._backoff`
  并 `self._backoff = min(30.0, self._backoff * 2)`，成功时复位
  `self._backoff = 1.0`。注意必须保留"退避到期后仍会重试同一档"的语义
  （`tests/test_bugfix_20260920_t07_t09_t10.py:274-286` 钉的就是这条，别把它关掉）。

---

### R8-capture-02｜`_refresh_geometry` 的返回值是死码：`True` 唯一含义「私有字段不可用 → 改用重建采集器」没有任何调用点兑现

- **级别**：P2（健壮性/可维护性：一条已文档化的降级路径永不执行）
- **位置**：`screen.py:437-478`（`_refresh_geometry`）、调用点 `screen.py:542`
  （`_monitor_rect`）与 `screen.py:692`（`_grab_mss`）
- **代码**：

  ```python
  # screen.py:470-478
  try:
      if getattr(self, "_mss", None) is not None:
          getattr(self, "_mss", None)._monitors = None
  except Exception as e:
      # 私有字段不可用（上游改名/结构变化）时退化：整套重建
      log.debug("作废 mss 显示器缓存失败（%s），改用重建采集器", e)
      return True
  return True
  ```

  ```python
  # screen.py:542（唯一形态的两个调用点都这样）
  self._refresh_geometry(force=force)      # ← 返回值被丢弃
  ```

- **触发（推演链）**：上游 mss 若把实例级几何缓存 `_monitors` 改名/重构
  （该字段是**上游私有属性**，本修复直接写它）→ `except` 分支命中 → `_refresh_geometry`
  返回 `True` 并记一条 **debug** 日志 → 第 542 行把 `True` 丢掉 → 采集器不重建 →
  继续用陈旧几何抓帧。此时若几何探针同时不可用（非 Windows / `user32` 不可用 /
  `EnumDisplayMonitors` 失败），`_monitor_rect` 会退回 mss 的陈旧 `monitors`
  （`screen.py:544-548`），回到 capture-012 要消灭的"BitBlt 成功且不写入"形态。
- **后果**：单点失败（探针坏）时无降级，画面冻结；且线索只有一条 debug 日志
  （默认级别下不可见）。注意**反向**也成立：probe 可用时这条死码不影响正确性
  （几何一律取自 probe，不读 mss 缓存），所以它不是常态故障，而是"双重降级场景下
  承诺不成立"。
- **证据**：
  1. 到行推演链如上（`screen.py:474-477` 返回 True → `screen.py:542` 丢弃 →
     `screen.py:544` 只在 probe 不可用时才用 mss 缓存）。
  2. **假绿判定**：`tests/test_capture012_geometry_drift.py:404-419`
     `test_private_field_failure_degrades_to_rebuild_signal` 只断言
     `self.assertTrue(cap._refresh_geometry(force=True))`——它钉住的是"返回 True"，
     而**没有任何用例钉住"返回 True 之后采集器真的被重建"**。把 `_monitor_rect` 里
     的丢弃改成"接住并重建"，或把 `_refresh_geometry` 的返回值删掉，本用例照样绿。
     这正是 `docs/audit/test.md` T 系列要抓的"替身/断言只锁一半"形态。
  3. 该用例的 docstring 自称"交由调用方走整套重建"——调用方并不走。
- **同族入口**（grep `_refresh_geometry`）：

  | 路径:行 | 判定 |
  |---|---|
  | `screen.py:542` `_monitor_rect` 内 | **同缺陷**（丢弃返回值） |
  | `screen.py:692` `_grab_mss` 经 `_monitor_rect` 间接调用 | **同缺陷**（同样拿不到） |
  | `tests/test_capture012_geometry_drift.py:381/392/401/419/428/481` | 用例直接调 `_refresh_geometry` 并断言返回值，属于"锁信号不锁后果"的假绿支撑 |

- **去重**：新（`docs/audit/capture.md` 的 capture-011~014 都没有这一条；
  capture-010 讲的是告警去重的模块级化，不是返回值）。与上面 R8-capture-01 组合时
  会互相放大（重建频率已经异常，重建信号又送不到）。
- **建议修复**：二选一，别留中间态——(a) 让 `_monitor_rect` 接住 `True` 并真的重建
  （对 `CaptureSource` 而言等于 `self.cap._open()` 或回传一个"几何不可信"信号）；
  或 (b) 删掉 `except` 分支里的 `return True`，把 docstring 与
  `test_private_field_failure_degrades_to_rebuild_signal` 一起改成"只记 debug、
  不改行为"，消除假承诺。

---

## 本轮已排除（实测否定，勿再重报）

以下两条在动手前是**强假设**，都用最小脚本实跑否定了，记录原始输出以免下一轮重复劳动。

### 排除 1：`_geometry_summary` 的 `%` 拼接会污染漂移告警 —— **否，格式正确**

初判依据是 `screen.py:283-288` 的 `parts = ["全部屏幕 %dx%d@(%d,%d)" % (...) for m in monitors[:1]]`
与 `screen.py:467-469` 把 summary 当**第 2 个参数**传给 `_warn_pick_once`，担心 `%` 二次替换。
实跑（`_tmp_audit_capture_probe.py`）：

```
== 1) _geometry_summary 原始返回值 ==
     '全部屏幕 2560x1440@(0,0)，#1 2560x1440@(0,0)'      残留格式符: 无
== 2) 真路径：_refresh_geometry 漂移告警的最终日志行 ==
    [sakura] WARNING: 显示器 #1 的几何已变化（全部屏幕 2560x1440@(0,0)，#1 2560x1440@(0,0)），采集区域自动跟进
    含未替换格式符的行数 = 0
```

结论：**格式正确**，`logger.py` 的 handler 是 `%(message)s`，summary 作为参数传入，
两次 `%` 替换的参数各自都对得上。
唯一残留的边界（记此备查，**当前不可达**）：summary 里若出现 **float**，
第二次替换会把 `%d` 用在 float 上抛 `TypeError`，日志行被 logging 的
`--- Logging error ---` 替代。实测该分支：

```
== 3) 可达性边界：summary 里出现 float 会怎样 ==
    %-格式化 抛出: TypeError not all arguments converted during string formatting
```

探针本身已用 `int(gsm(76))` / `int(r.left)` 强制取整（`screen.py:219-235`），本机实测
`probe_geometry` 返回值全 int（`== 4) 真实数据形态 == 全部为 int: True`），
所以不可达——除非未来把 geometry 来源换成别处（例如直接吃 mss 的 `monitors`）。
**不作为发现登记**，仅作为 `screen.py:280-291` 的边界注记。

### 排除 2：`share._send_jpeg_cached` 的 `target_width=0` 送 1080p JPEG —— **否，入参已压制**

初判依据是 `share.py:321` 的 `pipeline.encode_bgr(frame, 1.0, self._quality, 0)`
最后一个参数是 `target_width=0`（不压制），而 `share._run` 构造 `CaptureSource` 时
`scale=1.0`（`share.py:247-249`）；若缓存帧是原始 1920x1080，这条兜底就会绕过
`host.codec.target_width` 送出全尺寸 JPEG。实跑（`_tmp_audit_capture_verify.py`）：

```
A) 静止期关键帧 JPEG 兜底 vs 受 target_width 压制的视频路径
  正常 _encode_send 路径 : ef.is_video=False  ef.w x ef.h = 854x480  jpeg=142050 B
  share._send_jpeg_cached : 实际送出 854x480  jpeg=142050 B
  体积比 = 1.00x，多出 0 字节/帧
```

原因：`FrameEncoder.encode` 在两条出口上都把 `frame` 或 `bgr` 交给 `calc_output_size`
压制（`pipeline.py:174-188`），`ef.frame` 落进 `last_frame` 时已经是 854x480，
再乘 1.0 不变。**`host._static_jpeg_fallback`（`host.py:3426`）拿到的也是同一个
`ef.frame`，两条同族出口行为一致**，不存在"修一处漏一处"。不作为发现登记。

---

## 本轮未发现真阳性的范围

如实说明查过、给了判据、但没找到站得住结论的区域（避免"全绿"被误读为覆盖完整）：

1. **`probe_geometry` / `_probe_geometry_uncached` 探针本体**：缓存键 `(时刻, 列表)`、
   失败不缓存（`screen.py:198-206`）、`EnumDisplayMonitors` 失败/`found` 为空返回 None
   （`screen.py:238-241`）、异常全归"不可用"（`screen.py:244-246`）、
   回调 `proc_type` 生命周期覆盖整次调用——逐条走完未发现缺陷。本机实跑与真实
   `mss.MSS().monitors` 逐项一致（由 `tests/test_capture012_geometry_drift.py:777-795`
   交叉验证，本机通过）。
2. **`geometry_changed` 的等价判定**：长度不等判 True、任一侧空判 False、
   单条记录不可解析判 False（不误判漂移）、忽略 mss 10.x 的 `is_primary`/`name`/`unique_id`
   （`screen.py:249-272`）。语义自洽。
3. **`_fit_region_to_desktop` 的三分支**（`screen.py:480-521`）：完全在内→原样、
   部分越界→夹交集、完全离屏→退回整屏。手工覆盖了负原点双屏、越屏左/上、越屏右/下、
   区域比桌面大 5 倍、探针记录缺字段等组合，未找到能产出"既非原区域又非桌面内"
   的输入。代码顺序（先取 `rl/rt/rr/rb` 与 `virtual`，再 `max`/`min`，再 `nr>nl and nb>nt` 守卫）
   在 `rr <= rl` 或 `rb <= rt` 的退化矩形上也是安全的（两个条件同时为假即走 offscreen）。
4. **`_grab_mss` 的拔屏/配置错双判据**（`screen.py:685-722`）：`prev` 必须在
   `_monitor_rect` **之前**取（`screen.py:691`），`_monitor_rect` 内部
   `_refresh_geometry` 会刷新 `_geometry`，顺序正确；`stale_ok=True` 时不回退 mss 缓存
   （`screen.py:544`）也对；`monitor=0`（并集）时 `idx=0` 恒存在，
   `monitor>0` 越界时按 `probe[1]` 降级并与告警里的块数同口径（`screen.py:711-716`）。
   未发现缺陷。
5. **`_pick_output_idx` 的 mss 句柄前置判定**（`screen.py:575-590`）：新增
   `_mss is None` 早退后，"mss 初始化失败"仍留有线索（测试断言的 `mss-handle-missing-*`
   键），且 `_pick_output_idx` 改为不打探针（走 `_monitor_rect` 的默认 `stale_ok=False`）。
   `tests/test_bugfix_20260924_round7.py:511-519` 为适配它加了 `cap._mss = object()`——
   该改动正确，不是"为迁就实现改测试"。
6. **`CaptureSource` / `StillGate`（`pipeline.py:223-465`）**：`_should_rebuild_none` 的
   三条件、`_errors` 在成功一帧后清零、`_nones`/`_none_since`/`_none_warned` 三个 latch
   都在 `grab()` 成功分支与重建分支复位（`pipeline.py:406-422`）——**没找到
   PROTOCOL §1.3 说的"latch 不复位"**；`set_params` 的 `cap is None` 分支复用重建机制
   （`pipeline.py:334-341`）。本轮 `pipeline.py` 的未提交改动只有 import 重排
   （`git diff -- pipeline.py` 仅 `+import` 顺序与 `from screen import (...)` 展开），
   零行为变更。
7. **`share.ScreenShareSession`**：`_send_msg` 的"锁内保存/临时 5.0/`finally` 还原"
   （`share.py:338-346`）满足第 96/97 条的不变量；`stop()` 在 join 超时后**保留**
   `_thread` 引用（`share.py:123-133`，codec-009 语义）；`_send_key_response` 的四条失败
   路径都返回 False 供调用方保留请求（`share.py:265-309`）；`_command`/`_run` 的
   `finally` 都会走 `_close_resources`。未发现缺陷。唯一留有的是：
   `settimeout(5.0)` 是**进程内共享 socket 的全局状态**，与 viewer 接收线程的
   `select`/`recv` 存在理论竞态窗口——但 `share.py` 一侧已按"写锁内保存+还原"做到
   能力边界内的最好，**其余部分属 viewer 侧**，见「跨域移交」B。
8. **`codec.VideoEncoder` 编码器链/preset 校验/`_install_ctx` 先建后切/permafailed**
   （`codec.py:120-230`、`246-272`、`345-416`）：未发现缺陷。`_ENCODE_FAIL_LIMIT=10`
   的 permafailed 与 `available` 判据自洽。
9. **两个新测试文件的有效性**（`tests/test_capture011_region_geom_missing.py`、
   `tests/test_capture012_geometry_drift.py`，67 例全绿）：除了 R8-capture-02 指出的
   `test_private_field_failure_degrades_to_rebuild_signal` 一处"锁信号不锁后果"外，
   其余用例都用**真实 `screen.CaptureManager`（`__new__` 绕开 `_open`）+ 忠实复刻上游
   缓存语义的伪 mss**（`FakeMss.monitors` 真的按 `_monitors is None` 门控重枚举，
   `test_capture012_geometry_drift.py:61-98`），并且有一条与真实 vendored dxcam
   `_validate_region` 的逐例交叉核验（`test_capture011...:229-264`）。
   未发现"替身比真实宽松"导致的假绿。

## 跨域移交

- **A（→ test 域，`task-4`）**：`tests/test_bugfix_20260920_t07_t09_t10.py:288-294`
  的 `test_pipeline_accounts_by_return_value` 用 `inspect.getsource` +
  `assertRegex(src, r"if\s+self\._encoder\.set_fps\(fps\)\s*:")` 断言
  `pipeline.FrameEncoder._ensure_encoder` 的**源码文本**。它会在两个方向上咬人：
  (a) 按 R8-capture-01 的建议在该分支加退避（例如 `if self._encoder.set_fps(fps): ... else: ...`）
  仍能匹配，但一旦重排（如先算 `now` 再调 `set_fps`）就会假红；
  (b) 它是 T 系列点名的"只锁结构不锁行为"形态——`assertNotRegex` 那条断言
  "不得无条件记 `_enc_fps`"用正则描述换行缩进，行为等价的重排会让它失效。
  建议转成行为断言（替身编码器 + 断言 `_enc_fps` 未被无条件写成 fps）。
- **B（→ ui/viewer 域，`task-3`）**：`share._send_msg`（`share.py:338-346`）为满足
  第 96/97 条，在**多线程共享的 client socket** 上临时 `settimeout(5.0)` 再还原。
  该 socket 同时被 viewer 的接收线程 `select`/`recv` 复用。请核 viewer 侧是否存在
  "启动时 `settimeout(None)`、之后被本函数临时改成 5.0 的窗口内恰好 `recv`"
  的路径（`viewer.py:294` 附近按注释是置回阻塞基线；`share.py:332` 的注释也这么声称）。
  若接收线程确实全程 select 门控（有数据才 recv），则是纯理论竞态，可在 report 里
  如实标注"未验证"；否则是一条真实的跨线程 sockopt 竞态。
- **C（→ dist/test 域，`task-4`）**：`ruff check *.py`（`AGENTS.md` 与
  `.github/workflows/test.yml` 的口径）现在**不是绿的**，因为工作树里存在
  `_tmp_audit_*.py`：`_tmp_audit_dist_scan_vacuous.py`、`_tmp_audit_net_seq_layout.py`、
  `_tmp_audit_viewer_probe1.py`（以及本审查员自己的三个，已在本报告写完前清理到
  只剩必要者）。它们被 `.gitignore` 忽略，所以 CI 不受影响，但**本机口径的"ruff 全绿"
  是假绿**——任何人照着 `AGENTS.md` 跑 `ruff check *.py` 都会得到 exit 1。
  建议在 `AGENTS.md` / 审查协议里写明"跑门禁前先清 `_tmp_*.py`，或用
  `ruff check $(git ls-files '*.py')`"，否则每轮审查的基线都得人工解释一遍。

## 复现脚本

| 文件 | 用途 |
|---|---|
| `_tmp_audit_capture_cost.py` | R8-capture-01 的量化：A 组（每帧重试 5/5）vs B 组（退避 1/1）vs C 组（真编码器重建耗时） |
| `_tmp_audit_capture_probe.py` | 「已排除 1」的反证：summary 无残留格式符、真路径日志行逐字、float 边界 |
| `_tmp_audit_capture_verify.py` | 「已排除 2」的反证：854x480 vs 854x480，体积比 1.00x |

三个脚本都只读生产代码（`mock.patch.object` 只作用于进程内对象），不写任何生产文件。
