# net — 第 8 轮审查（网络与协议层）

- **负责范围**：本轮未提交的帧序号协议（net-045/net-046，协议 v2.4）两侧实现；`common.py`（协议常量/消息编解码/认证/配置读写）；`host.py` 的网络与服务端面（accept 循环、入册与收尾守卫、roster 广播、中继、令牌桶限速、message_pump、graceful shutdown、frp 隧道生命周期）。
- **审查时间**：本轮第 8 轮（工作树含 net-045/net-046 未提交改动，`git status` 见 `M common.py` / `M host.py` / `M viewer.py` / `M share.py`）。
- **基线**：`ruff check` / `python -m mypy` / `python -m unittest discover -s tests -p "test*.py"` 全绿（本轮审查期间两次全量 discover：**874 例，0 失败 0 错误 0 跳过**）。
- **证据脚本**（`_tmp_audit_net*.py`，`.gitignore` 已忽略）：
  - `_tmp_audit_net_seq_layout.py` —— 判据失效集合的穷举
  - `_tmp_audit_net_decode_impact.py` —— 前缀恒定时解码容忍度（hevc/x264 对照）
  - `_tmp_audit_net_seq_pixel_damage.py` —— 精确复现「协商未达成 v2.4」链路的端到端像素损伤
  - `_tmp_audit_net_legacy_viewer.py` —— 复现「旧观看端 + 新共享端」的端到端像素损伤（含 libx265 对照）
  - `_tmp_audit_net_wiring_spy.py` —— 全量测试下 net-045/net-046 接线的运行时覆盖判定
- **只读声明**：未修改任何生产代码、`tests/` 既有用例、`docs/audit/*.md` 既有条目。

## 结论摘要

| 编号 | 级别 | 一行现象 |
| --- | --- | --- |
| R8-NET-01 | P1 | 生产代码没有任何路径会产出 v2.3 布局：未升级的观看端（v1.4.0）收到新版共享端每一帧都带 4 字节 seq 前缀，H.264 下实测 28/520 帧完全解不出（画面周期性冻结） |
| R8-NET-02 | P1 | `parse_video_auto` 的布局判据与布局不是一一映射：257 个 seq 取值会让判据"成立"而不回退，seq 字节被当 NAL 前导交给解码器，实测与 R8-NET-01 同款损伤 |
| R8-NET-03 | P2 | `_expect_seq` 只在 `_chal_capable` 首次探测时确定；后续重连走挑战-应答分支不再读回包，共享端升级/换址后长期按旧次版本解帧 |
| R8-NET-04 | P2 | net-045 声称的"降级为丢弃到下一个可解码点"没有任何实现：`_video_seq_gaps` 全仓零读取者，且接收路径的唯一接线（`viewer.py:647`）无任何用例覆盖（实测：删掉后 874 例仍全绿） |

---

### R8-NET-01｜帧序号布局的向后兼容只存在于测试里，未升级的观看端每帧都会拿到 4 字节 seq 前缀

- **级别**：P1（功能错误：画面周期性冻结；跨版本互操作被破坏）
- **位置**：`host.py:3567-3571`、`share.py:253-258`、`share.py:298-303`；设计意图写在 `common.py:414-430`
- **代码**：
  ```python
  # common.py:414-430（设计上留了 v2.3 出口）
  def pack_video(video_bytes, ts=None, keyframe=False, codec=CODEC_H264, seq=None):
      """...`seq=None` 即保持 v2.3 布局，供与未升级对端通信。"""
      if seq is None:
          return (_VIDEO_HDR.pack(MSG_VIDEO, _VIDEO_PAYLOAD_BASE + len(video_bytes), ts, codec, flags) + video_bytes)
      return (_VIDEO_SEQ_HDR.pack(MSG_VIDEO, _VIDEO_SEQ_PAYLOAD_BASE + len(video_bytes), ts, codec, flags, seq & 0xFFFFFFFF) + video_bytes)

  # host.py:3567-3571（发送侧：无条件带 seq）
  delivered = route_frame(
      runtime, "local",
      pack_video(video, video_ts, is_key, codec=video_codec,
                 seq=local_seq.next()),
      is_key=is_key, send_timeout=2.0)
  ```
- **触发**：任何"共享端已升级到 v1.5.0（v2.4）、观看端仍是 v1.4.0（v2.3）"的部署。旧观看端的解析是 v1.4.0 的 `common.parse_video(payload)`：`nal = payload[10:]`，**没有起始码判据也没有回退**，因此每一帧都会把 seq 的 4 字节当作 NAL 的一部分交给解码器。
- **后果**：seq 为 32 位大端，其前 3 字节在 `seq ∈ [256, 511]` 时恰好构成 Annex-B 起始码 `00 00 01`，第 4 字节被解码器当作 nal_unit_type。其中 `(seq & 0xFF) & 0x1F ∈ [1,5]` 的 40 个取值会解析成**切片 NAL 头**（0x01/0x05 等），使参考链被破坏；后续帧在下一个 IDR 之前全部解不出，`viewer._decode_video_batch` 拿到空帧列表后 `last_frame is None` → 不调 `_store_frame` → **悬浮窗画面冻结**。
  实测（libx264/H.264，520 帧 160×120，keyint=30）：基准 520/520，旧观看端 492/520，**28 帧完全解不出**，集中在 `261-270 / 293-300 / 325-330 / 357-360`，即每个 GOP 末段约 6~10 帧（0.2~0.33 秒），共约 0.93 秒，落在新流开始后第 8.5~17 秒（seq 单调递增必经过 `[256,511]`）；`seq` 回绕（约 4.9 天）后重现。
- **证据**：`_tmp_audit_net_legacy_viewer.py` 实跑输出：
  ```
  编码器 libx264（codec_id=1）  帧数 520  —— 模拟 v1.4.0 观看端看 v1.5.0 共享端
    基准（seq 被剥掉）解出 520/520
    旧观看端（seq 留在 NAL 前）解出 492/520（差 -28）
    缺失帧号: [261,262,...,270, 293,...,300, 325,...,330, 357,358,359,360]
  编码器 hevc_nvenc（codec_id=2）  基准 520/520   旧观看端 520/520（差 +0）
  编码器 libx265（codec_id=2）     基准 520/520   旧观看端 520/520（差 +0）
  ```
  生产调用点的穷举（`_tmp_audit_net_seq_layout.py` 末段）：`host.py:3569`、`share.py:257`、`share.py:302` 三处**全部传真实序号**；`seq=None` 分支只在测试与 `share.py` 的 `getattr(self,"_seq",None)` 兜底里可达（该兜底只在 `ScreenShareSession.__new__` 的测试替身上成立）。
- **同族入口**：
  - `host.py:3578`（JPEG 回退 `pack_frame`）→ 不适用（MSG_FRAME 无 seq 字段，旧端布局未变）
  - `share.py:257` / `share.py:302` → 同缺陷（peer 流同样无条件带 seq；`_tmp_audit_net_legacy_viewer.py` 的模拟对 peer 流同样成立）
  - `common.py:404` `pack_frame` → 不适用
- **去重**：新。与 net-045 描述（README/`docs/audit/net.md:40`）直接冲突：那条声明"载荷布局不自描述故按 proto 次版本协商"，但**协商结果从未作用于发送侧**——发送侧没有任何读取对端次版本的路径：`_auth_required_phase` 读过 up 对端 `proto`（`host.py:1970-1980`）却只用于拒绝旧客户端，未存入 `ClientInfo`；`route_frame` 又对多个订阅者复用同一份 `frame`（`host.py:2515-2527`），结构上无法按订阅者定制布局。
- **建议修复**：二选一。(a) 让发送侧真正按订阅者版本出包：把认证期读到的次版本存入 `ClientInfo`，`route_frame` 对"次版本 < `VIDEO_SEQ_MINOR`"的订阅者单独 `pack_video(..., seq=None)`（注意 `route_frame` 的零重打包优化会因此对这类订阅者失效，需按目标分桶打包）。(b) 若不想付出 (a) 的成本，就不要把 `seq=None` 写成"供与未升级对端通信"的承诺，改为在握手/probe 阶段对"次版本 < 4"的客户端明确拒绝并提示升级（与 `PROTO_CHAL_MIN_MINOR` 的既有口径一致），把它变成一次可排查的拒绝而不是静默花屏。

---

### R8-NET-02｜`parse_video_auto` 的布局判据与布局不是一一映射：seq 自己就能让判据"成立"

- **级别**：P1（功能错误：同款画面冻结，触发面是协商未达成 v2.4 的所有部署）
- **位置**：`common.py:499-521`（判据本体）、`common.py:479-481`（`_looks_like_annexb`）、受害点 `viewer.py:636-648`
- **代码**：
  ```python
  # common.py:510-521
  order = (True, False) if expect_seq else (False, True)
  last_err = None
  for use_seq in order:
      try:
          ts, codec, flags, nal, seq = parse_video(payload, expect_seq=use_seq)
      except ValueError as e:      # 载荷长度不够该布局的最小头
          last_err = e
          continue
      if _looks_like_annexb(nal):
          return ts, codec, flags, nal, seq
      last_err = ValueError("NAL 缺 Annex-B 起始码（疑似布局不符）")
  raise last_err or ValueError("视频帧 payload 无法解出 NAL")
  ```
- **触发**：`expect_seq=False` 时收到 v2.4 载荷。此时第一轮按 v2.3 解，`nal = payload[10:]` = **seq(4) + 真 NAL**，判据检查的正是 seq 的前 4 字节：
  - `seq = 1` → 前缀 `00 00 00 01`，本身就是合法的 4 字节起始码 → 判据成立，**不回退**；
  - `seq ∈ [256, 511]` → 前缀 `00 00 01 xx` → 判据成立，**不回退**。
  可达路径有两类：(a) 混合版本 MultiView——观看端 v1.5.0 连到旧共享端（回包 `proto=[2,3]` → `_expect_seq=False`），同时观看另一位 v1.5.0 队友的屏幕，而中继链路上的旧 host 只做原始字节直通（`host.py:1685` 用布局无关的 `peek_video_header`），帧仍带 seq；(b) R8-NET-03 的协商缓存失效路径。
- **后果**：与 R8-NET-01 同款损伤。`_tmp_audit_net_seq_pixel_damage.py` 在**只对失效 seq 加前缀、其余帧走正常回退**的精确模拟下，libx264 复现出完全相同的 28 帧丢失（`261-270 / 293-300 / 325-330 / 357-360`）——即这不是"概率很小的边界"，而是**每次新流开始后必然经过**的连续 256 帧区间。
- **证据**：
  ```
  # _tmp_audit_net_seq_layout.py
  B. 判据失效取值集合（扫描 seq 0..2^20）
  失效取值个数: 257
  失效取值: [1, 256, 257, ..., 511]
  C. 单调流（seq 从 1 递增）前 600 帧里被静默错解的帧: 命中帧号区间 1..511，共 257 帧
     seq=257 -> NAL 头 0x01 -> nal_unit_type=1（slice！会被当真实载荷解析）
     seq=261 -> NAL 头 0x05 -> nal_unit_type=5（slice！）
  # _tmp_audit_net_seq_pixel_damage.py（libx264）
  基准解出 520/520   实验解出 492/520（差 -28）
  ```
- **同族入口**：
  - `common.py:484-496` `peek_video_header`（中继路径）→ 已正确：**刻意不做 NAL 内容判断**，其 docstring 明确写了"靠'NAL 是否像 Annex-B'猜布局，会把非 Annex-B 的合法载荷误判成损坏帧直接丢弃"。同一个判据在中继侧被回避，却在接收侧被采用，两处口径不一致。
  - `viewer.py:642-644` → 同缺陷（唯一调用点）
  - `host.py:1685` → 不适用（不判 NAL）
- **去重**：net-046 的未收口残留。`viewer.py:1126-1128` 的注释已经点出了这 257 个取值，但把它定性为"丢掉序号"（遥测失效）；实测表明后果是**整段帧解不出**（数据面损坏），量级被低估了一个档。
- **建议修复**：把判据从"猜内容"改成"用协商结果 + 结构校验"。最小改法：`expect_seq=False` 时不做自动回退，而是**用 seq 字段的语义做二次校验**——按 v2.3 解出的 `nal` 若前 4 字节落在"构成起始码"的窄集合内，则改按 v2.4 解并要求 `_looks_like_annexb(payload[14:])` 成立（真 NAL 一定成立），两者都成立时**优先采信 v2.4**（因为 v2.4 布局下 `payload[14:]` 是完整 NAL，而 v2.3 布局下 `payload[10:]` 在真 NAL 之前没有任何合法前导字节）；再退一步，至少把"两种布局都成立"的歧义情形记一条 warning，不要静默选错。

---

### R8-NET-03｜`_expect_seq` 只在首次探测时确定，共享端升级/换址后不会重新协商

- **级别**：P2
- **位置**：`viewer.py:1086-1092`（首次探测）、`viewer.py:1109-1110`（缓存命中分支）、`viewer.py:429-430`（受影响的赋值）
- **代码**：
  ```python
  # viewer.py:1086-1092
  if action in (Act.LOGIN, Act.REGISTER) and self._chal_capable is None:
      send_msg(sock, {"action": Act.PROBE, "user": "", "pass": "",
                      "proto": list(PROTOCOL_VERSION)})
      probe = recv_msg(sock)
      self._apply_probe_proto(probe)          # ← 只有这条路会刷新 _expect_seq
  # viewer.py:1109-1110
  elif action in (Act.LOGIN, Act.REGISTER) and self._chal_capable:
      reply = self._auth_via_challenge(sock, action, user, password)   # 不读 reply 的 proto
  ```
- **触发**：`_chal_capable` 是**跨连接持久**的频道级缓存。一旦首次探测置位，后续重连（同一频道、有保存凭据）一律走 1109 分支，`_apply_probe_proto` 不再被调用。于是：共享端从 v1.4.0 升级到 v1.5.0（或用户把频道地址改指向另一台共享端）后，观看端只要不重启、不换频道，`_expect_seq` 就**长期停在首次探测时的值**。
- **后果**：首次探测时共享端是 v2.3 → `_expect_seq=False`；共享端升级到 v2.4 后，观看端在**全 v1.5.0** 的链路上仍按 v2.3 解帧，命中 R8-NET-02 的错解窗口。此时序空间来自共享端 `local_seq`——它随 `_send_loop` 线程存活，共享端重启后从 1 起，因此观看端在共享端重启后的 8.5 秒内完成重连即可命中（指数退避常在秒级）。
  准入关闭（`auth.enabled=false`）部署不受影响：那条路每次都发 `Act.PROBE`（`viewer.py:1046-1053`）并落到 `else` 分支重新协商——net-046 的第②处修复在这条路上是完整有效的。
- **证据**：静态推演链（1086→1092 只在 `_chal_capable is None` 时执行 → 1109 分支无 `_apply_probe_proto`）。运行时覆盖证据见 R8-NET-04 的 spy 输出：全量测试中 `_apply_probe_proto` 的 viewer 侧触发点只有 `viewer.py:_authenticate:1092`（3 次）与 `viewer.py:_authenticate:1129`（1 次，准入关闭分支），**没有一次落在 1109 分支**（即"缓存命中后不重协商"这件事没有任何用例描述过）。既有 `tests/test_d1_challenge_auth.py` 的 4 个用例全部是首次探测。
- **同族入口**：
  - `viewer.py:429-431` `_expect_seq` / `_chal_capable` 一并写入 → 同缺陷（同一次探测的两个产物，但只有后者被用作"是否还需探测"的门）
  - `viewer.py:1046-1053`（`_anon_ok` / `_auth_probed` 路径）→ 已正确（每次重连都探测）
- **去重**：新（与 net-046 无重叠：net-046 修的是"哪条分支读回包"，本条是"读回包的时机被缓存吞掉"）。
- **建议修复**：把"是否已协商"和"是否支持挑战-应答"拆开——`_expect_seq` 依赖的次版本号应当每次连接都刷新（1109 分支拿到 `reply` 后同样调一次 `_apply_probe_proto(reply)`；`auth_result` 若要复用同一解析，先确认 host 在挑战-应答回包里也带 `proto`，否则退回"每次连接先发一条 `Act.PROBE`"）。或在 1109 分支加一次"`_host_minor` 为空则先 probe"的补探测，代价是一条额外往返。

---

### R8-NET-04｜net-045 的"选择性恢复"没有落地，且接收路径的唯一接线无用例覆盖

- **级别**：P2（未闭环 + 假绿判定）
- **位置**：`viewer.py:433-451`（`_note_video_seq`）、`viewer.py:647`（唯一接线点）、`viewer.py:404-405`/`1613-1614`（复位点）
- **代码**：
  ```python
  # viewer.py:647（接收循环内唯一调用点）
  self._note_video_seq(seq)
  # viewer.py:443-451
  if seq is None:
      return
  last = getattr(self, "_last_video_seq", None)
  if last is not None:
      delta = (seq - last) & 0xFFFFFFFF
      if 1 < delta < 0x80000000:
          self._video_seq_gaps = getattr(self, "_video_seq_gaps", 0) + delta - 1
  self._last_video_seq = seq
  ```
- **触发**：一直成立（这不是条件触发，而是"声称的能力不存在"）。`_video_seq_gaps` 全仓只有写、没有读：`viewer.py:311`（初始化）、`405`/`1614`（复位）、`450`（累加）。它不进日志、不进状态栏、不参与 `_request_keyframe` 判据、不参与 `_resync`。
- **后果**：(a) net-045 的核心承诺（"把整体重置降级为丢弃到下一个可解码点"）没有任何代码实现——缺口统计既未用于告警，也未用于触发关键帧请求或选择性丢弃；接收侧对序号的全部使用就是给一个没人看的计数器自增。(b) 计数器只增不减且无上限（长期运行会持续增长），而它的累加还会把**非网络原因**的序号跳跃算成丢帧：`host.py:2520-2521` 的 `need_key` 门控（新连接/切源后只投关键帧）、`host.py:3589-3598` 的零投递路径（`delivered == 0` 直接 `continue`，但序号已在 `3570` 取走）都会让序号成段跳空。一旦将来有人把它接到面板或告警上，会立刻产生误报风暴。
- **证据**（假绿判定，协议 §2.4 允许的"现有用例为何钉不住它"）：`_tmp_audit_net_wiring_spy.py` 在不改生产代码的前提下给 `viewer.Channel._note_video_seq` 套记录调用来源的 spy，然后跑完整 discover：
  ```
  收集到用例数: 874
  运行结果: 失败 0，错误 0，跳过 0
  _note_video_seq：全量测试期间被调用 17 次
    调用栈里出现 viewer.py 的次数: 0
    **没有任何一次调用发生在 viewer.py 内部**
  _apply_probe_proto：被调用 8 次
    viewer.py:_authenticate:1092  ×3
    viewer.py:_authenticate:1129  ×1
  ```
  即：**删掉 `viewer.py:647` 这一行，874 个用例全部照旧通过**。`tests/test_net045_frame_seq.py:159-208` 的 7 个用例全部是直接调 `ch._note_video_seq(...)`（`Channel.__new__` 构造的替身），锁的是方法自身的算术，不是"接收路径有没有接上它"。
  另外 `tests/test_net045_frame_seq.py:41-49`（`test_v23_layout_unchanged`）保护的是 `pack_video(seq=None)` 的可选行为——而该行为在生产中不可达（见 R8-NET-01），它给人的"向后兼容已验证"是假象。
- **同族入口**：`viewer.py:404`、`viewer.py:1613`（两处复位）→ 已正确（换连接/换源都清基线，net-046 已补全）；`viewer.py:647` → 同缺陷（无用例覆盖）
- **去重**：新。与 net-046 的②不同：那条修的是"准入关闭分支不读回包"（已修且**有真覆盖**，spy 显示 `1129` 被执行过）；本条是"读到的值没有任何消费者"。
- **建议修复**：二选一。(a) 把序号真正用起来：在 `_video_seq_gaps` 超过阈值时打一条带频道名与累计值的 warning，并在 `_resync` 为空且缺口超过一个 GOP 时请求关键帧（这是 net-045 承诺的语义）。(b) 若本轮不打算接线，就把 `_note_video_seq` 的调用与 `_video_seq_gaps` 一起保留但明确标注"遥测未接线"，并**至少补一个驱动 `_on_message`/接收泵的用例**钉住 647 行的接线——否则这条接线在任何一次重构里都可以被无声删除而门禁全绿。

---

## 本轮未发现真阳性的范围

以下区域已按协议 §2 的口径检查，但**没有**达到"精确位置 + 触发条件 + 可观测后果 + 证据"的门槛，如实记录以免被误读为"已审且无问题"：

1. **`host.py` 本轮改动的其余部分**：`git diff -- host.py` 的实质改动只有三处——`_forward_upstream_frame` 用 `peek_video_header` 替换 `parse_video`、`_send_loop` 引入 `local_seq`、以及 import 重排。逐条核对：(a) `peek` 的校验强度与原 `parse_video` 等价（都只要求 `len(payload) >= 10`，且原调用把 `_nal` 丢弃），`flags` 在两种布局都位于第 9 字节，取值正确；(b) `(list, tuple)` → `list | tuple`（`host.py:1029/1973/2694/2873`）——`isinstance(x, list | tuple)` 在 py3.10 合法且等价；(c) `except socket.timeout` → `except TimeoutError`（`host.py:2091`）——py3.10 起 `socket.timeout is TimeoutError`，等价；全仓生产代码已无其它 `except socket.timeout`（仅注释与测试保留）。
2. **`common.py` 的 `proto_compatible`（`common.py:77-90`）**：只比较主版本（`major == PROTOCOL_VERSION[0]`），因此 v2.3 客户端与 v2.4 服务端仍判为兼容——我原本怀疑"升级次版本会让旧端被拒"，实测读码后不成立；`PROTO_ENFORCE=False` 的现状下该路径只留痕。
3. **`_send_loop` 的序号取用位置（`host.py:3564-3572`）**：`local_seq.next()` 在 `route_frame` 的参数求值中调用，`route_frame` 抛异常时序号已消耗且 `last_sent_video_ts` 不更新（下轮重发同帧、序号跳 1）。行为可观测性仅限"缺口统计多 1"，而该统计无消费者（见 R8-NET-04），故不单列。
4. **`viewer.py` 接收泵结构**：`_on_message` 的 `except ValueError: return None`（`viewer.py:645-646`）只丢弃当前帧，不会连带丢弃 `video_batch` 里已累积的帧（批次在 `_on_batch` 统一消费），未发现"一帧坏整批丢"的问题。
5. **`_apply_probe_proto` 的类型健壮性（`viewer.py:426-431`）**：`proto=[2,"4"]` 这类"次版本非 int"的回包会让 `proto[1] >= PROTO_CHAL_MIN_MINOR` 抛 `TypeError`（该异常在 `_authenticate` 的 `try` 内被记为"认证通信失败"）。我判为**不单列**：该表达式与 net-046 之前完全一致（属既有形态，非本轮引入），且触发者只能是恶意/被篡改的服务端回包；本轮唯一新增的 `isinstance(proto[1], int)` 收窄只作用于 `_host_minor`。
6. **`host.py` 的 accept 循环 / 入册与收尾守卫 / roster 广播 / 令牌桶 / message_pump / shutdown join 预算 / frp 生命周期**：本轮无实质改动（仅 import 行重排），抽查 `_accept_loop*`（`host.py:2716-2812`）、`_client_sender_loop`（`host.py:828-874`）、`_allow_upstream`（`host.py:1652-1666`）、`_auth_required_phase` 的 proto 分支（`host.py:1970-1980`）未发现本轮新增缺陷；已登记条目（第 18/19/54/55/56/62/63/66/102/103/107/114/132 条、net-015~net-044）未发现新的未收口残留。

## 跨域移交

1. **→ capture 域（task-1）**：`share.py:253-258` 与 `share.py:298-303` 是本轮 net-045 在共享者侧的两个取号点（`ScreenShareSession._seq`）。已核对：两条路径都取号、静止期关键帧应答也带 seq、`getattr` 兜底让 `__new__` 构造的替身退回 v2.3 布局。**需要 capture 域复核的是**：`share.py` 之外是否还有第 3 条出帧路径（例如 `pipeline.py` 的 `drain`/`EncodedFrame` 直发）——我在 `host.py`/`share.py`/`viewer.py` 三个模块内确认 `pack_video` 只有上述三处调用点，但 `pipeline.py`/`screen.py` 的内部产出若被别处消费，我不在范围内核。
2. **→ ui 域（task-3，audit-viewer）**：R8-NET-01/NET-02 的用户可见形态是"画面偶发冻结 0.2~0.3 秒、新流开始后约 8.5~17 秒出现"，若 `docs/audit/round8/ui.md` 出现同形态条目，根因在本报告而不在渲染层。另外我注意到 `_tmp_audit_viewer_probe2.py` 已在查 `_video_seq_gaps` 的**跨线程竞态**（接收线程无锁自增 vs `switch_source` 持 `self.lock` 清零）——该竞态的后果受限于同一字段无消费者（R8-NET-04），我未重复论证，交由 ui 域判定是否单列。
3. **→ dist/test 域（task-4）**：R8-NET-04 的假绿结论可直接引用——`tests/test_net045_frame_seq.py` 的 `TestViewerGapAccounting`（7 例）与 `test_v23_layout_unchanged` 锁的是"方法算术"与"生产中不可达的可选参数"，不构成对 net-045 数据面行为的覆盖；本报告给出了可复现的判定方法（spy + 全量 discover，874 例）。
