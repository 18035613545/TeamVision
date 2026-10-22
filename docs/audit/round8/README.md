# 第 8 轮审查总览（2026-10-10）

本目录是**第 8 轮代码审查**的完整交付：四个并行审查域 + Lead 的跨域横向核查，
以及随后落地的三批修复。审查对象是**工作树里未提交的第 8 轮改动**（协议 v2.4 帧序号
net-045/net-046、capture-011/012、`screen.py` +355 行等 30 文件 / +1144 行）加上各模块的存量代码。

各模块的正式条目已回填到 [`docs/audit/`](../)：`capture.md` / `net.md` / `ui.md` /
`dist.md` / `test.md`（编号规则见 [`docs/audit/README.md`](../README.md)）。

> **后续轮次**：第 9 轮（部署与更新 / 观看端体验 / 画质延迟增量，2026-10-10 之后）
> 没有独立目录，条目直接回填到各模块文件：ui-049~051、dist-027~028、codec-020。

## 0. 为什么还要审第 8 轮

仓库此前已有 7 轮自审（冻结对照表 136 条 + 各模块 `模块-序号` 条目），且三门禁全绿。
本轮的价值不在"再读一遍代码"，而在**换判据**：

- 历史高频缺陷形态是「**修一处漏一处**」——第 7 轮 22/57 条属此类。所以本轮把
  「同族入口 grep 并逐条标注」当成硬性动作，而不是可选项。
- 门禁绿没有防护力：本轮证明 `ruff check *.py` **一个字都没扫过 `tests/`**，那片区域
  积了 89 处违规（含未定义名）。这正好复现了 `pyproject.toml` 顶部那段"窄范围换来的
  全绿没有防护力"的自述——**同一类失误在扩大 ruff 范围之后又发生了一次，只是换到了 `tests/`**。
- 测试"通过"不等于缺陷被钉住：本轮用反汇编、spy、类级注入等手段找到 4 处**假绿**用例
  （改回缺陷形态仍绿 / 无条件绿 / 断言项从未求值 / 单跑必红靠别人副作用吃饭）。

## 1. 基线（审查开始时，Lead 实测）

| 门禁 | 命令 | 结果 |
|---|---|---|
| Lint | `python -m ruff check *.py` | All checks passed（ruff 0.16.8） |
| 类型 | `python -m mypy` | Success: no issues found in 4 source files（mypy 1.17.1） |
| 单测 | `python -m unittest discover -s tests -p "test*.py"` | **Ran 874 tests, OK**（139.7s） |

解释器 `F:\conda\envs\fps-screen\python.exe`（3.10.20）；mypy 在 base 环境。

## 2. 分工与产出

| 域 | 队友 | 负责范围 | 报告 | 发现 |
|---|---|---|---|---|
| A 采集/编码 | `audit-capture` | `screen.py` / `pipeline.py` / `share.py` / `codec.py` | [`capture.md`](capture.md) | 2（P1×1） |
| B 网络/协议 | `audit-net` | `common.py` / `host.py` 服务端面 / 帧序号协议 | [`net.md`](net.md) | 4（P1×2） |
| C 观看端/界面 | `audit-viewer` | `viewer.py` / `host_ui.py` / `ui_common.py` / `splash.py` | [`ui.md`](ui.md) | 3（P2×3） |
| D 发布/基建/测试 | `audit-dist-test` | 打包链 / `logger.py` 等 / **测试有效性** / CI 门禁 | [`dist-test.md`](dist-test.md) | 5（P1×3） |
| — 跨域横向 | Lead | 协议三端对称性、版本口径、接缝 | [`LEAD.md`](LEAD.md) | 1（与 net 域重合，已合并） |

审查统一按 [`PROTOCOL.md`](PROTOCOL.md) 的判据：每条发现必须给出**精确行号 + 触发条件 +
可观测后果 + 证据（实跑输出 / 到行的推演链 / 假绿判定）+ 同族入口 + 与既有条目的去重声明**，
并强制在报告末尾写「本轮未发现真阳性的范围」，避免"绿"被误读成"覆盖完整"。

**去重后共 13 条独立发现**（NET-04 与 UI-02 同源、LEAD-01 与 NET-02 同源，均已合并登记）。

## 3. 修复与处置

三批修复并行落地，写范围按文件切分、互不重叠（Task 5/6/7 的 `write_scopes` 见共享任务板）。

| 批次 | 范围 | 内容 |
|---|---|---|
| 1 | `common.py` / `viewer.py` | 帧布局判据加固、`_chal_capable` 类型健壮性、seq 缺口的真实消费者 + 接线用例、注释订正 |
| 2 | `pipeline.py` / `screen.py` | 编码器就地重建失败的独立退避、删掉 `_refresh_geometry` 兑现不了的降级承诺 |
| 3 | `tests/` / `pyproject.toml` / `test.yml` / `AGENTS.md` | 假绿用例转真断言、未定义名修复、**ruff 门禁扩到 `tests/`**、文档口径对齐 |

各条的编号、现象、关联测试与处置状态见下一节的登记表；`docs/audit/` 下五个模块文件是长期索引。

### 13 条独立发现的登记表

`正式条目` 一列指向已回填到 `docs/audit/` 各模块文件的长期索引编号；`关联测试` 是**现在把这个修复改回缺陷形态会让它转红**的用例（这才是判定力来源，不是"文件名看起来相关"）。

| 编号 | 域 | 级别 | 一行现象 | 正式条目 | 关联测试 |
|---|---|---|---|---|---|
| R8-capture-01 | 采集 | P1 | `FrameEncoder` 的「就地重建失败」每帧重试一次完整编码器构造、不落退避（30 帧调 30 次，单次 93–110ms，且 `available` 恒 True 故永不回退 JPEG） | capture-015 | `tests/test_round8_capture.py` |
| R8-capture-02 | 采集 | P2 | `_refresh_geometry` 的"改用重建采集器"承诺无消费者、且对成因 100% 无效 | capture-016 | `tests/test_capture012_geometry_drift.py` |
| R8-NET-01 | 协议 | P1 | 帧序号 v2.4 的向后兼容只存在于测试里；旧观看端每帧拿到 seq 前缀，libx264/H.264 下 520 帧里 28 帧解不出（画面周期性冻结）→ **已按"明确拒绝旧观看端"落地**（net-047） | net-047 | `tests/test_round8_host_compat.py` |
| R8-NET-02 | 协议 | P1 | `parse_video_auto` 的判据与布局非一一映射（257 个 seq 取值不回退，40 个破坏参考链） | net-048 | `tests/test_round8_net_ui.py` |
| R8-NET-03 | 协议 | P2 | `_expect_seq` 只在首次探测时确定，共享端升级/换址后不重新协商 | net-049 | — |
| R8-NET-04<br>= R8-UI-02 | 协议/界面 | P2 | `_video_seq_gaps` 全仓零读取者；接收路径唯一接线零覆盖（spy 证：删掉该行 874 例仍全绿） | net-050 | `tests/test_round8_net_ui.py` |
| R8-UI-01 | 界面 | P2 | `_chal_capable` 对 `proto[1]` 缺类型保护，畸形回包抛 `TypeError` 且状态半更新 | ui-047 | `tests/test_round8_net_ui.py` |
| R8-UI-03 | 界面 | P2 | net-046 新增调用点的注释把可达路径写反 | ui-048 | `tests/test_round8_net_ui.py` |
| R8-D-01 | 测试 | P1 | capture-011 的用例被 `if reg is not None` 包住，缺陷/修复两种形态都绿 | T-23 | `tests/test_capture011_region_geom_missing.py` |
| R8-D-02 | 测试 | P1 | `fy2` 是未定义名 → T-14 核心不变量第 4 项**从未被求值** | T-21 | `tests/test_panel_float_no_overlap.py` |
| R8-D-03 | 门禁 | P1 | `ruff check *.py` 不覆盖 `tests/`，那里积了 89 处违规（含 `F821`/`F823`） | dist-026 | —（CI 自身） |
| R8-D-04 | 测试 | P2 | 日志回收断言 `>= 2` 而夹具确定值是 4 | T-24 | `tests/test_bugfix_20260924_round7.py` |
| R8-D-05 | 测试 | P2 | `tests/` 下唯一带 UTF-8 BOM 的文件 | T-25 | `tests/test_item93_cred_dialog.py` |
| **R8-D-06**<br>（修复期发现） | 测试 | P1 | **4 个测试文件只在全量 discover 的收集顺序帮忙时才通过**（单跑 11/12/7 errors + 1 failure） | T-22 | 那 4 个文件各自 |
| R8-LEAD-01<br>= R8-NET-02 | — | P2 | Lead 独立探针复现了同一件事（`seq ∈ {1} ∪ [256,511]` 的两向实测），已并入 net-048 | net-048 | `tests/test_round8_net_ui.py` |

### 新增的回归用例

| 文件 | 例数 | 覆盖 |
|---|---|---|
| `tests/test_round8_net_ui.py` | 24 | 布局判据逐字节取回（seq=255/256/257/511/512/65536/200001）、`_chal_capable` 类型健壮性（含 `[2, 4.0]` → True 的反向约束）、seq 缺口接线（真实 `_receiver_loop` + 回环 socket）、注释准确性 |
| `tests/test_round8_capture.py` | 7 | 退避限流、窗口到期仍重试、成功后复位、换实例复位、两条失败路径互不污染、`set_bitrate` 不需同类保护的核对 |
| `tests/test_round8_host_compat.py` | 10 | 旧观看端告警的触发口径（含 bool/float/畸形）、两条认证路径的接线锁 |

## 4. 一处**未落地代码修复**的发现（如实登记，不是遗漏）

| 条目 | 状态 |
|---|---|
| **R8-NET-03** `_expect_seq` 只在首次探测时确定 | `_chal_capable` 是跨连接持久缓存，共享端升级后观看端不再重新协商。修它要让 host 的 **9 个 `AUTH_RESULT` 发送点**统一带 `proto`、并在 viewer 的两条挑战-应答分支各调一次 `_apply_probe_proto`；且**必须**先保证失败回包也带 `proto`，否则会把 `_chal_capable` 误刷成 `False` 从而永久拒绝明文口令（比现状更糟）。在 R8-NET-02 修好之后它的唯一后果已降级为"序号遥测字段不更新"，故只登记。 |

## 4b. R8-NET-01 的收尾（审查后由用户决策，已落地）

本轮初稿对 `R8-NET-01`（帧序号 v2.4 的向后兼容只存在于测试里）给的是三选一：
① 按订阅者次版本分桶打包 ② **明确拒绝旧观看端** ③ 接受并只改文档口径。用户选了 ②，
随后按此实现：

- 判据 `common.video_seq_capable(msg) -> (present, capable)` 提到**协议层**（两端共用，
  不再是 host 私有的判据）；文案常量 `common.PROTO_SEQ_REJECT_MSG` 同层。
- 拒绝文案与 `PROTO_CHAL_MIN_MINOR` 那条**刻意不同**——viewer 按字面量分流，
  前者复位能力缓存重试一次即可连上，后者是终态，必须换观看端。
- 三条认证入口全接：`_auth_required_phase`（准入开启）、`_auth_optional_phase`
  （准入关闭）、`_dispatch_ctrl` 的迟到认证分支（第 100 条的兜底）。第三条最关键：
  高延迟/隧道场景下 probe 晚于 2 秒可选 auth 窗口才到，那条路径正是慢 probe 的必经之路，
  只改前两处等于给旧观看端留了一条每 2 秒重连一次的后门。
- **刻意保留的缺口**：完全不带 `proto` 字段的对端仍然放行。host 在认证期无法区分
  "1.3.x 的旧观看端"与"只发帧、不解码的旧 `share.py` 中继 peer"，打死后者等于废掉
  MultiView；这类对端走 `parse_video_auto` 的 Annex-B 起始码回退。
- 连带更正 `tests/test_d1_challenge_auth.py` 三处：它们原用 `proto: [2,2]`/`[2,1]`
  当"老客户端"替身，而 `[2,2]` 现在先撞帧序号门，测的不再是自己声称的东西。改为
  **不带 proto**——那才是 `allow_legacy` 真正服务的对象（1.3.x 早于 C2 的 proto 字段），
  并新增一例钉住"两条门控的先后与分流"（顺序反了会让用户升到 1.4.0 后照样花屏）。

## 5. 本轮方法上新增的两件可复用资产

1. **面量化的"假绿"判定法**（D 域）：不靠读代码猜，而是
   （a）对断言做反汇编看**哪一项真的被求值**；
   （b）用 spy 给生产方法挂调用记录，跑**全量 discover**，看"删掉这行测试还会不会绿"；
   （c）把旧实现做**类级运行时注入**，确认新用例在缺陷形态下变红。
   本轮的 `fy2`（项从未求值）、`viewer.py:647` 接线（删掉仍全绿）、`set_fps` 退避（8 != 1）
   都是这套方法抓出来的。
2. **逐模块单跑体检**（T 域延伸）：`AGENTS.md` 把 `python -m unittest tests.<模块>` 写成推荐的单条跑法，
   于是"单跑必红、全量才绿"= 隐式顺序依赖。对本轮 67 个模块逐个单跑，扫出 **4 个**这类文件
   （靠 `pipeline.py`/`share.py` 模块级 `from screen import …` 在收集期顺带填上惰性全局），
   已全部显式化。命令见 `test.md` 的 T-22 条目。

## 6. 关于本目录里的证据脚本

审查与修复期用到的复现脚本以 `_tmp_*.py` 命名写在**仓库根**，`.gitignore:51` 的 `_tmp_*.py`
已将其排除在版本控制之外，**收尾时已全部删除**（它们是一次性调试产物，不值得塞进 PUBLIC 仓库）。

这不妨碍复核：每份报告都**内联了关键输出**（命令、参数、期望数字、实测行），
而长期防回归的资产是新增的用例——
`tests/test_round8_net_ui.py`（24 例）、`tests/test_round8_capture.py`（7 例）、
`tests/test_round8_host_compat.py`（10 例），以及被转成真断言的既有用例。
