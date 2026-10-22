# 第 8 轮 · Lead 横向核查（跨域接缝）

Lead 独立于四个审查域，只查**跨文件接缝**：同一语义在多个入口各写一份、协议改动
在三端是否对称、门禁与声明的口径是否一致。条目编号沿用 `R8-LEAD-NN`。

## 基线与环境（Lead 实测，2026 本轮）

| 门禁 | 命令 | 结果 |
|---|---|---|
| Lint | `F:\conda\envs\fps-screen\python.exe -m ruff check *.py` | All checks passed（exit 0），ruff 0.16.8 |
| 类型 | `F:\conda\python.exe -m mypy` | Success: no issues found in 4 source files，mypy 1.17.1 |
| 单测 | `python -m unittest discover -s tests -p "test*.py"` | **Ran 874 tests, OK**，139.7s |

基线全绿，且工作树含第 8 轮未提交改动（`git diff --stat`：30 文件、+1144/−170 行，
含协议 v2.4 帧序号与 capture-011/012）。**门禁绿不等于无缺陷**——本目录的条目证明这一点。

---

### R8-LEAD-01｜`parse_video_auto` 在「协商布局 ≠ 实际布局」的一个序号子区间上静默返回错位 NAL，而非按 docstring 承诺报「损坏」

- **级别**：P2（正确性边界；实际危害低，但判据与文档承诺不符，且是 net-045 引入的新面）
- **位置**：`common.py:499-521`（`parse_video_auto`）、`common.py:479-481`（`_looks_like_annexb`）
- **代码**：

  ```python
  order = (True, False) if expect_seq else (False, True)
  for use_seq in order:
      ts, codec, flags, nal, seq = parse_video(payload, expect_seq=use_seq)
      if _looks_like_annexb(nal):
          return ts, codec, flags, nal, seq      # ← 提前成功
  ```

- **触发**：协商结果 `expect_seq=False`（对端声明次版本 < 2.4），但该路流实际发的是
  v2.4 布局（带 seq）。**可达组合**：viewer 1.5.0 + host 1.4.0（回包 `proto=[2,3]`）
  + 该 host 上挂着 peer share.py 1.5.0 —— 中继路径 `raw` 直通（`host.py:1696-1697`
  → `_fwd_member_frame` → `route_frame`），host 不重打包，于是 viewer 按 host 的
  proto 选了 v2.3 布局，收到的却是 peer 的 v2.4 帧。
  此时按 v2.3 解出的 `nal = seq(4B) + 真 NAL`，只要 `seq` 前 3 字节恰是 `00 00 01`
  （**seq == 1**，或 **seq ∈ [0x100, 0x1FF]**），`_looks_like_annexb` 就把它当成
  合法起始码、提前返回——错位的 NAL 与 `seq=None` 一起被当作正确结果。
- **后果**：帧 NAL 前多 4 字节。按 ffmpeg 的 Annex-B 扫描语义，多出的 `00 00 00 01`
  会被当成空 NAL 忽略（seq==1 时），`00 00 01 XX` 则注入一个零长度伪 NAL（XX 是
  seq 低位，逐帧变化）——多数解码器容忍，但会在严格解码器上产生告警/丢帧，
  且 `seq=None` 让该帧退出丢帧遥测。**危害低**，定级 P2 的依据是"判据承诺与实际不符"，
  不是"必现花屏"。
- **证据**：`_tmp_audit_lead_seqprobe.py` 实跑（fps-screen 环境）：

  ```
  === A. 实际 v2.4 / 协商 expect_seq=False ===
    seq=1        **错位** seq=None nal[:6]=000000010000 len=33
    seq=2        OK  seq=2 nal[:6]=000000016511  len=29
    seq=255      OK
    seq=256      **错位** seq=None nal[:6]=000001000000 len=33
    seq=511      **错位** seq=None nal[:6]=000001ff0000 len=33
    seq=512      OK
    错位序号: ['0x1', '0x100', '0x1FF']
  === B. 实际 v2.3 / 协商 expect_seq=True （回退方向）===
    NAL4 OK / NAL3 OK / NAL_MULTI OK        ← 回退方向无此问题
  === C. 协商一致（expect_seq=True 收 v2.4）===
    1 / 65536 / 131071 / 200001 / 0xFFFFFFFF 全 OK
  ```

  同样序列下 `parse_video(payload, expect_seq=False)` 的 NAL 长度 33 vs 正确 29，
  差额正是被吃掉的 seq 4 字节。
- **同族入口**：
  - `viewer.py:638-647` —— 唯一的 `parse_video_auto` 调用点（已走本函数，同缺陷）；
  - `host.py:1685` 中继校验 —— 用 `peek_video_header`，**布局无关、不受影响**（net-045 已正确裁决）；
  - `common.py:433-448` `parse_video`（严格版）—— 被 `test_item57_buffer.py` 等既有用例
    直接调用，签名不变、行为不变，本条不涉及；
  - `share.py:256-258`、`share.py:301-304`、`host.py:3569-3570` 三个打包点 —— 均已带 seq，
    同族入口已收口。
- **去重**：新条目。与 net-045/net-046 同批代码但非同一问题（net-046 修的是"准入关闭分支
  不协商 proto"与"切源不清基线"，都不触及探测判据本身）。
- **建议修复（最小）**：把 `_looks_like_annexb` 的匹配**分级**——4 字节起始码
  （`00 00 00 01`）优先级高于 3 字节（`00 00 01`）；`parse_video_auto` 先收集两种布局的
  候选，返回"起始码更强"的那个，而不是"第一个成立"的那个。误判形态下 v2.4 布局解出的
  NAL 必为 4 字节起始码、v2.3 误解出的是 3 字节，分级即可纠正；而正常形态（v2.3 帧的
  NAL 以 3 字节起始码开头）只有一种候选成立，行为不变。
  备选（更彻底、动作更大）：给 v2.4 帧用**独立的 `kind` 值**，让布局自描述，彻底去掉猜测。

---

## 已核查且**未发现**问题的横向面（避免后来者重复投入）

1. **中继路径的 v2.4 兼容**：`_forward_upstream_frame` → `_fwd_member_frame` →
   `route_frame` 全程 `raw` 直通、不重打包（`host.py:1679-1680`、`2656-2658`、`2513-2556`），
   因此 peer 的 v2.4 帧原样到达 viewer，布局协商由 viewer 侧负责——分工正确。
   `_allow_upstream` 的限速按 `payload` 长度统计，v2.4 的 +4 字节不影响判据量级。
2. **seq 生成点全覆盖**：`host.py:3552/3570`（本地流，独立 `SeqCounter`）、
   `share.py:256-258`（成员流主路径）、`share.py:301-304`（静止期强制 IDR 路径）三处
   均已取号；`share.py` 用 `getattr(self, "_seq", None)` 兜底测试直构实例，未漏。
3. **seq 基线复位点**：`viewer.py:404`（换连接）与 `viewer.py:1613`（切源）
   两处均已清；`_receiver_loop` 的每轮重连在协商之前就调 `_invalidate_media()`
   （`viewer.py:463`），因此 `_apply_probe_proto` 改写 `_expect_seq` 时基线必已为 None，
   不存在"改布局但留旧基线"的窗口。
4. **版本号字面量**：`common.APP_VERSION`、`version_info.txt`、`installer.iss`
   （注释 + `AppVersion` + `OutputBaseFilename`）、`README`、`docs/install.md`、
   `docs/changelog.md` 当前**均为 1.5.0**；`fps-*.spec` 不硬编码（走 `version_info.txt`）；
   `build_installer.bat` 从 `installer.iss` 反解产物名。第 67 条的本轮残留面见 D 域报告。
5. **`PROTO_ENFORCE=False` 的后果面**：`proto_compatible` 当前只用于留痕
   （`host.py:1931-1937`、`2132-2134`），主判仍是 FS02 MAGIC。这与 `PROTO_CHAL_MIN_MINOR`
   的硬门（`host.py:1967-1977`）不冲突：前者是"兼容性提示"，后者是"旧 proof 安全性拒绝"。

---

## 待裁决（等四个域的发现汇总后回填）

- 各域 P0/P1 条目的独立复核结论（Lead 逐条实跑或推演确认/驳回）。
- 修复批次与写范围划分（避免多写入方改同一文件）。
- 最终三门禁复跑结果。
