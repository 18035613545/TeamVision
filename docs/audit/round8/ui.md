# ui — 第 8 轮（round8）观看端与界面审查（viewer / host_ui / splash / ui_common）

- **负责范围**：`viewer.py`（含本轮未提交的 +178 行）、`host_ui.py`、`splash.py`、`ui_common.py`。
- **判定口径**：`docs/audit/round8/PROTOCOL.md`。
- **审查时间**：2026-10-10；基线沿用协议 §0（ruff OK / mypy OK / 174 tests OK），本轮未重复跑全量套件。
- **工作树状态**：审的是脏工作树（`viewer.py` 未提交 +178 行，含 net-045/net-046）。未做任何 `stash/checkout/restore`，生产代码全程只读。
- **验证脚本**：`_tmp_audit_viewer_probe1.py`（协商矩阵 / 序号往返 / 缺口可达性）、`_tmp_audit_viewer_probe2.py`（竞态 / UI 队列 / 不可达性）、`_tmp_audit_viewer_probe3.py`（认证链路可达性与用户可见后果）。三份输出已落盘为同名 `_out.txt`。
- **结论概览**：3 条成立（P0 0 条 / P1 0 条 / P2 3 条）。三条全部落在**本轮未提交改动**上，共同根因是：`net-045` 的接收侧只做了「协商 + 记账」两半，第三半（消费）没落地，而第二半（记账）的字段无人读取。

---

### R8-UI-01｜`_apply_probe_proto` 对 `proto[1]` 缺类型校验：同一次调用里一半代码按 `int` 防御、另一半直接比大小，非整数次版本把异常抛穿 `_authenticate`

- **级别**：P2（可自愈的功能故障 + 诊断失真；失败后每次重连都复现，直到链路对端修正为止）
- **位置**：`viewer.py:407-431`（`_apply_probe_proto`），调用点 `viewer.py:1092`、`viewer.py:1129`；下游消费 `viewer.py:642-644`
- **代码**：
  ```python
  valid = (isinstance(proto, list | tuple) and len(proto) >= 2
           and proto[0] == PROTOCOL_VERSION[0])
  host_minor = None
  if valid and isinstance(proto[1], int) and not isinstance(proto[1], bool):
      host_minor = proto[1]
  self._host_minor = host_minor
  self._expect_seq = bool(
      host_minor is not None and host_minor >= VIDEO_SEQ_MINOR)
  self._chal_capable = bool(valid and proto[1] >= PROTO_CHAL_MIN_MINOR)   # ← 这里
  ```
- **触发**：服务端任意一条认证回包的 `proto` 写法为 `[2, "<次版本>"]`（JSON 里次版本被引号包住，例如第三方/中间件拼装回包时 `str(4)`）或 `[2, null]`。第一条探针回包即命中：
  - 首次连接、无凭据 → `Action=Act.PROBE`（`viewer.py:1053`）→ 回包 `viewer.py:1092` 调用；
  - 重连且 `_anon_ok`（准入关闭部署）→ 同一 PROBE 分支 `viewer.py:1119-1129` 调用；
  - 用户在登录框按「注册」时若 `_chal_capable` 被复位（`viewer.py:1184`），下一轮经 PROBE 再次命中。
- **后果**：`proto[1] >= PROTO_CHAL_MIN_MINOR` 抛 `TypeError`，逃出 `_apply_probe_proto` → 被 `_authenticate` 的 `except Exception`（`viewer.py:1130-1133`）接住 → 状态置为「认证通信失败: '>=' not supported between instances of 'str' and 'int'」→ 返回 `False` → `_invalidate_media()` 清帧 → 悬浮窗常驻该文案（不是「需要登录」，用户看不到登录框）。每次重连都会重发探针并重现，频道在该链路修正前持续不可用。同时该次调用**已写入一半状态**：`_host_minor`/`_expect_seq` 被覆盖、`_chal_capable` 保持旧值（见证据 C），三种状态不再自洽。
- **证据**（`_tmp_audit_viewer_probe3.py`，走真实 `Channel._authenticate`，stub socket 精确投喂回包；全程离线）：
  ```
  proto=[2,'4']   返回=False  auth_msg="认证通信失败: '>=' not supported between instances of 'str' and 'int'"
  proto=[2,None]  返回=False  auth_msg="认证通信失败: '>=' not supported between instances of 'NoneType' and 'int'"
  proto=[2,4]     返回=True   _host_minor=4 _expect_seq=True _chal_capable=True
  C 段：抛出后 _host_minor=None _expect_seq=False _chal_capable='SENTINEL'（前两个已被覆盖、第三个未写）
  ```
  推演链：`proto[1]` 非 int → 第 426 行 `isinstance` 分支被刻意跳过（说明作者已识别该风险）→ 第 431 行仍无条件参与 `>=` → `TypeError` → 1130 行 `except` → `auth_msg` 被写成"认证通信失败"。
- **同族入口**：
  - `viewer.py:1092` → 同缺陷（同一函数的另一调用点，首次探针路径）；
  - `viewer.py:1129` → 同缺陷（net-046 新增的 PROBE 分支调用点）；
  - `viewer.py:642-644`（`expect_seq=getattr(self, "_expect_seq", False)`）→ 已正确（有 `False` 兜底，且 `parse_video_auto` 双向回退）；
  - `common.py:499-521`（`parse_video_auto`）→ 已正确（异常形态被收敛为 `ValueError` 并带原因）；
  - `host.py:2130`（`proto_compatible` 判 `present and not compatible`）→ 不适用（host 侧只做告警/拒绝，不比大小）。
- **去重**：**新**。`docs/audit/ui.md` 无此条；`net-046` 虽然新增了 `_apply_probe_proto`，但登记口径只覆盖"漏清基线 / 该分支不读 proto"，未涉及该方法的健壮性。方法自身 docstring 已声明 `_expect_seq` 要"真 int 且非 bool"，本条即该策略在 `_chal_capable` 上的漏配。
- **建议修复**：把第 431 行改为与第 426 行同一判据 —— `self._chal_capable = bool(valid and host_minor is not None and host_minor >= PROTO_CHAL_MIN_MINOR)`（`host_minor` 已是"真 int 且非 bool 的 `proto[1]`"）；非 int 的次版本一律视为「能力未知」，既不抛异常也不误判为「不支持挑战-应答」。改完顺手确认 `tests/test_net045_frame_seq.py` 新增一例 `proto=[2, "4"]` 断言不抛且 `_chal_capable is False`。

---

### R8-UI-02｜`_video_seq_gaps` 是只写字段：`net-045` 的接收侧序号消费整条缺失，缺口头寸全仓零读取点（顺带一处无锁读改写）

- **级别**：P2（新增能力的落地缺口；帧丢失、错序对上层完全不可观测，`net-045` 承诺的"用序号把整段重置降级为选择性恢复"没有实现）
- **位置**：写入 `viewer.py:450`；初始化 `viewer.py:311`、复位 `viewer.py:405`、`viewer.py:1614`
- **代码**：
  ```python
  if 1 < delta < 0x80000000:
      self._video_seq_gaps = getattr(self, "_video_seq_gaps", 0) + delta - 1
  self._last_video_seq = seq
  ```
- **触发**：任何一次视频收帧。`_note_video_seq(seq)` 在 `viewer.py:647` 被逐帧调用（本地流与中继流都算）。
- **后果**：丢帧/错序只能累积进一个**没有任何读取点**的整数；面板、悬浮窗、诊断报告（`diag.py`）、关键帧请求（`_request_keyframe`）与积压追新（`_check_backlog`）都不看它。用户侧表现是：`net-045` 与 `net-046` 登记的"可识别丢帧/错序"能力在观看端不可观测，序号信息在解包后即被丢弃（`video_batch.append((ts, codec, flags, nal))` 只带四个字段，`seq` 未进批次）。此外该字段的读改写不受任何锁保护：`switch_source`（`viewer.py:1613-1614`，持 `self.lock`）与 `_invalidate_media`（`viewer.py:404-405`，不持锁，由接收线程在 `finally` 调用）会与接收线程的 `+delta-1` 并发。
- **证据**：
  - `_tmp_audit_viewer_probe1.py` B 段（全仓 `.py` 扫描）：`_video_seq_gaps` 出现点仅 `viewer.py:311/405/450/1614` 四处写入 + `tests/test_net045_frame_seq.py` 的断言，**无任何读取点**；C 段实跑 `_note_video_seq(1,2,3,7)` → `gaps=3`，值确实在累积。
  - `_tmp_audit_viewer_probe2.py` B 段（3 次 × 3000 轮解锁交错，模拟接收线程记账 vs 主线程 `switch_source` 置 0）：`_video_seq_gaps = [0, 0, 10401, 10012, 1]`。发射侧每帧跳一号，理论上主线程复位后仍应继续累加；实测出现 0 与 10401 并存 → 丢失更新真实可现。竞态本身无用户可见后果（正因为无人消费），故不另计一条。
- **同族入口**：
  - `viewer.py:1613`（`switch_source` 清基线）→ 已正确（与 `_invalidate_media` 同一不变式）；
  - `viewer.py:404`（`_invalidate_media` 清基线）→ 已正确，但同样是无锁写（仅与上一条一起构成竞态，不单列）；
  - `host.py:3552/3570`（`local_seq`，本地流）→ 已正确（本轮无需改，序号只是发出）；
  - `share.py:256-258`、`share.py:301-304`（每条共享流独立 `SeqCounter`）→ 已正确（`getattr` 兜底，缺 `_seq` 时退回 v2.3 布局）；
  - `viewer.py:642-648`（解包点）→ 同缺陷（`seq` 解出后只喂记账、不进 `video_batch`，消费侧缺环）。
- **去重**：**新**，但与 `net-045`/`net-046`（`docs/audit/net.md:40-41`）是登记关系而非重复 —— 那两条登记的是"协议侧加序号 / 三条回包路径协商"，本条是"接收侧没有消费点"。冻结对照表第 84 条（"多处定义但全仓零调用的死代码"）是同类形态的历史先例，但那条不含本字段。
- **建议修复**：二选一并明确写进 `net-045` 的落地范围 —— ① 真消费：把 `_note_video_seq` 的返回值/累计缺口接到"是否值得请求关键帧"（`_request_keyframe` 的冷却判据）与诊断报告的一行遥测，并让 `video_batch` 携带 `seq` 供 `_decode_video_batch` 做错序丢弃；② 若不打算消费，就删掉 `_video_seq_gaps` 与对应的测试断言，只留 `_last_video_seq` 基线，避免"看起来有丢帧遥测、实际零消费"的假状态。任一路径都把 `_video_seq_gaps`/`_last_video_seq` 的读写挪到 `self.lock` 内（接收线程与主线程都要）。

---

### R8-UI-03｜`net-046` 新增调用点的注释把可达路径写反：真正走到 `viewer.py:1129` 的是 PROBE 分支，不是"准入关闭部署的带凭据登录"

- **级别**：P2（注释与实现漂移，本轮已属"修复理由写错"的形态；本次无功能后果，但下一位维护者按注释推理会改错地方）
- **位置**：`viewer.py:1119-1129`（注释 1123-1128 行）
- **代码**：
  ```python
  else:
      send_msg(sock, {"action": action, "user": user, "pass": password,
                      "proto": list(PROTOCOL_VERSION)})
      reply = recv_msg(sock)
      # net-046（2026-10-10）：**准入关闭（`auth.enabled=false`）的部署
      # 全程走这条分支**——host 的 `_auth_optional_phase` 回包是带 proto
      # 的（host.py:2150-2152），而旧实现整段不读它，`_expect_seq` 终身
      # 停在 __init__ 的 False。
      self._apply_probe_proto(reply)
  ```
- **触发**：纯静态可达性判定（读代码即可复核，无需运行）。
- **后果**：无直接用户可见后果 —— `viewer.py:1129` 的调用本身是**必要的**（它覆盖 `Act.PROBE` 冒烟路径），但注释给出的理由不成立：
  - 带凭据登录时 `action ∈ {Act.LOGIN, Act.REGISTER}`（`viewer.py:1048/1053/1078/1081`）；
  - 若 `_chal_capable is None`（首连/刚复位），第 1086 行的条件成立 → 先在第 1092 行调用 `_apply_probe_proto(probe)` 并据回包转向挑战-应答或拒绝，**永远走不到 1119 的 else**；
  - 若 `_chal_capable` 已知为 `True`，走 1109；已知为 `False`，走 1111 直接拒绝明文口令（auth-003 口径）；
  - 唯一落到 1119 的是 `action == Act.PROBE`（`viewer.py:1048` 的 `_anon_ok` 重连、`viewer.py:1053` 的首次探测）。
- **证据**：`_tmp_audit_viewer_probe3.py` A 段逐 action 列表（含每条的判定条件与分支落点）：
  ```
  用户已缓存凭据 → Act.LOGIN     → 1109 或 1111 → 不可达 1129
  用户点登录/注册 → REGISTER|LOGIN → 1086 分支    → 不可达 1129
  首次无凭据探测 → Act.PROBE      → 1119 else    → **可达 1129**
  重连、_anon_ok 已确认准入关闭     → 1119 else    → **可达 1129**
  ```
  另与 `host.py:2078-2089`（`Act.PROBE` 时回 `{"ok": False, "msg": "需要登录", "proto": ...}` 并 `continue` 等真凭据）、`host.py:2150-2152`（准入关闭时回 `ok=True` + `proto`）两处回包形态互证：两条路径都会在第 1092 行先被消费一次。
- **同族入口**：
  - `viewer.py:1092` → 已正确（首连/能力未知路径的同一调用）；
  - `viewer.py:1046-1053` → 不适用（`action` 赋值处，无 proto 读取）；
  - `host.py:2078-2089` → 不适用（服务端侧同族分支，回包本身就带 `proto`）。
- **去重**：**新**（`net-046` 登记的是"该分支不读回包"这个功能缺口已修，本条只针对修复注释把可达路径写反；两者不冲突，但建议随 `net-046` 一并订正，避免同一段注释在后续轮次继续误导）。
- **建议修复**：把这 6 行注释改成事实 —— 「本分支只在 `action == Act.PROBE` 时可达（`_anon_ok` 重连与首次无凭据探测）：这两条路径同样要在回包上定协商；带凭据的 LOGIN/REGISTER 在 `_chal_capable is None` 时经 1086 分支的探针（1092）完成协商」。若希望"准入关闭部署也走带凭据回包"为真，那要改的是控制流而不是注释。

---

## 本轮未发现真阳性的范围

以下区域逐项查过，**未**找到满足协议 §2 五要件（精确位置 + 触发 + 后果 + 证据 + 同族入口）的问题，如实列出以免"全绿"被误读为覆盖完整：

1. **`viewer.py` 的 Tk 回调与主线程竞态（退出瞬间的 `after`）**：`poll`（`viewer.py:3735-3744`）在进入函数体**第一件事**就重排下一次 `after`，因此函数体内任一异常（`_sync_hotkey_notice_once`/`_process_cred_requests`/`_check_share_sessions`/`_update_status_overlay`）都不会打断轮询链；`quit()`（`viewer.py:2025-2043`）与 `run()` 的 `finally`（`viewer.py:2010-2023`）都是 `running=False` → `_stop_hotkeys()` → 两阶段 `request_stop`+`stop()` → 再 `destroy`，未见"销毁后再投递 Tk 命令"的可达路径；`_post_ui`（`viewer.py:2190-2207`）同时拦 `running` 与 `_mainloop_ready`。实测 100 个回调 + 1 个抛异常的回调：单轮执行 64 个上界生效、异常只记一条 warning、后续回调照常执行、队列最终排空（`_tmp_audit_viewer_probe2.py` C/D 段）。
2. **latch 不复位**：逐个核过 `user_moved`（`3957-3973` 置位，无复位入口属**设计**：拖动后不再自动贴角，属第 43 条的既定语义）、`_panel_user_moved`（`_panel_show` 复位，ui-034）、`_panel_suppress_select`（正常路径 2622 与 `finally` 2634 双复位）、`_cred_busy`（`finally` 复位）、`_share_toggle_busy`（2966 复位）、`_diag_exporting`（viewer 3419/3440 两处复位；host_ui 898 唯一复位点，失败的 `_fail` 已改为形参传值，ui-022 修复成立）、`_hotkey_notice_synced`（一次性上屏，正确）、`_cred_evicted`/`_cred_timeout`（1060/1069 消费后复位）。未发现"置真后永不复位"的新形态。
3. **面板不可见时全速刷新**：`_panel_refresh_list`（2554-2560）与 `_panel_refresh_preview`（2644-2650）都在 `_panel_visible()` 为假时只续排定时器、跳过硬活（第 39 条口径）；`_panel_refresh_preview` 的两处 `after` 都在 `finally` 内，异常也不停更。
4. **`host_ui.py` 刷新链与日志框上限**：`_drain_log`（940-963）与 `_poll`（965-978）的续排都在 `finally`；`queue.Empty` 分支不触 Tk；日志上限算式复核为"恰好保留 `_MAX_LOG_LINES` 行"（`index("end-1c")` 返回 N+1 → 删 `N-1998` 行）；Treeview 增量刷新（1024-1055）在行数变化时才全量重建并恢复 `yview`。
5. **`ui_common.py` / `splash.py`**：`install_excepthook` 对 `KeyboardInterrupt` 透传、线程钩子跳过 `SystemExit`；`post_to_ui` 失败只记一条 debug（ui-004 口径，viewer 刻意不走它，注释与实现一致）；`splash._show_on_root`/`show_splash` 的异常路径都会销毁已建窗口（第 86 条）；`build_style(include_base=False)` 的取值路径与 ui-018 一致。本轮这两个文件只有 import 顺序/空行改动，未产生新缺陷。
6. **面板几何**：`_panel_layout`（3547-3593）已登记的限制（面板请求高 > 屏高时只能贴顶、footer 可能越出）在 `tests/test_panel_float_no_overlap.py:256-262` 用例 docstring 中明确标为"尽力而为"，属已知且被测试接受的边界，不计为新发现。本轮 `screen_w` 删除（3564-3566）经复核确为死读，行为与既有用例（`test_no_negative_coords` 等）一致。

## 跨域移交

1. → **net 域（`docs/audit/round8/net.md`）**：`R8-UI-02` 的另一半是协议侧问题 —— 序号已进载荷（`common.py:414-430`）、`parse_video_auto` 也会解出 `seq`，但**观看端没有任何消费**（`viewer.py:642-648` 解包后 `seq` 只进无人读的计数器，批次内不带序号）。建议 net 域在 `net-045` 条目下确认"接收侧消费点是否属于本轮范围"；若属，则跨 `viewer.py` 与 `net.md` 两侧一起收口。
2. → **net 域（同上）**：`parse_video_auto`（`common.py:499-521`）的回退判据只看"解出的 NAL 是否像 Annex-B"，会接受**布局误判但结果仍像 Annex-B** 的解析（`_tmp_audit_viewer_probe1.py` F 段：`expect_seq=False` 时把 `seq ∈ {1,256,257,511}` 的 v2.4 帧解成 v2.3，多带 4 字节且 `seq` 丢成 `None`）。因为真 v2.4 载荷里的 NAL 起始码恒为 `00 00 00 01`，通常会被正确的第一轮判据吃掉，目前只是**理论欠约束**而非已知病态；我没有构造出确定性病态输入，故不计为本轮 ui 发现，移交 net 域判定是否需要在 `net-045` 条目下补一条判据（例如用 `len(payload) - base` 与 `codec/flags` 组合做二次校验）。
3. → **capture/host 域（`docs/audit/round8/capture.md` / Lead）**：`share.py:256`、`share.py:301` 与 `host.py:3552` 的序号空间相互独立（本地流、每条共享流各自 `SeqCounter(start=1)`），观看端切源时只清基线、不记录"当前是哪条流"。本轮 `switch_source` 的清理（`viewer.py:1613-1614`）足以避免切源瞬间的假缺口；但若将来把 `_video_seq_gaps` 接到关键帧请求或诊断输出（见 `R8-UI-02` 建议①），就必须同时带上"流身份"，否则跨流跳号会被记成丢帧。此项属预防性提示，不构成当前缺陷。
4. → **net 域（同 2）**：真 v2.4 载荷里 NAL 起始码恒为 4 字节 `00 00 00 01`（`pack_video` 的载荷由编码器 Annex-B 输出直接拼接），请 net 域复核该不变式是否在 JPEG 回退与中继路径上也成立。

## 去重对照（与既有登记的关系）

- `R8-UI-01`：新。与 `net-046`（`docs/audit/net.md:41`）相关但不重复 —— 那条登记"三条回包路径共用协商方法"已实现，本条是其健壮性缺口。
- `R8-UI-02`：新。与 `net-045`/`net-046`（`docs/audit/net.md:40-41`）是"发送侧/协商侧已登记、接收侧消费未登记"的关系；形态上与冻结表第 84 条同类，但字段不同、且是本轮新增。
- `R8-UI-03`：新。仅针对 `net-046` 修复注释的可达路径失真，不重复其功能登记。
- 已排除（不报）：`ui-002~ui-046` 覆盖的既有条目（断线跃迁、`_store_frame` 加锁裁剪、面板让位、DPI、splash 清理、日志上限等）本轮逐条复核后**未发现未收口残留**，故不复述；冻结表中 ui 类 18 条（第 8/12/15/34-46/58/60/64/65/86/91/92/94/98/99/108/115/117/133/136 条）同理。
