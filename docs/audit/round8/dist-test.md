# round8 · 域 D：发布链路 / 进程级基建 / 测试有效性

- **负责范围**：`tests/`（测试有效性，最高优先）、门禁覆盖（CI / ruff / mypy / discover）、发布链路（`*.bat` / `*.spec` / `installer.iss` / `version_info.txt` / `requirements.txt`）、进程级基建（`logger.py` / `update_check.py` / `diag.py` / `gen_assets.py`）。
- **审查时间**：2026-10-10 本轮；工作树含本轮未提交改动（1144 insertions / 170 deletions，30 个文件）。
- **口径**：`docs/audit/round8/PROTOCOL.md`。只读生产代码与既有测试；本报告与 `_tmp_audit_dist*.py` 之外未写任何文件。
- **本轮实测基线（复核 Lead，与 PROTOCOL 一致）**：
  - `F:\conda\envs\fps-screen\python.exe -m unittest discover -s tests -p "test*.py"` → **Ran 874 tests, OK**，140.8s（两次独立跑，结果一致）。
  - `-v` 全量输出：**874 行结果里 0 个 skipped**（`skipped` 字样只出现在用例名 `test_non_dict_items_are_skipped` / `test_stale_request_is_skipped` 里）。`test_ui_common.TestBuildStyle` 等 Tk 用例在本机真跑通过。
  - discover 覆盖完整性实测：`tests/` 下 **64 个 `test*.py` 全部匹配**；不匹配的 4 个正是文档声明的重负载脚本（`e2e_multiview.py` / `e2e_signalwin.py` / `soak_viewer_8h.py` / `stress_max_clients.py`）。**discover 侧无遗漏**。
  - `git diff` 中 `run_tests.bat` / `build_*.bat` / `setup.bat` / `sign_exe.ps1` / 两个 `.spec` / `requirements.txt` **均未被本轮改动**（最后修改时间 2026-09-17 ~ 09-24）；本轮动的发布链文件只有 `installer.iss`、`version_info.txt`。

---

## 一、测试有效性（本轮最高优先）

### R8-D-01｜`test_never_leaks_absolute_coordinates` 无条件通过：缺陷形态下它恰好走进被自己包住的 `assertRaises`，**这条"防绝对坐标泄露"的钉子钉不住任何东西**

- **级别**：P1（测试有效性：核心回归钉子无判定力，capture-011 的对应断言位形同虚设）
- **位置**：`tests/test_capture011_region_geom_missing.py:133-140`
- **代码**：
  ```python
  def test_never_leaks_absolute_coordinates(self):
      """旧实现返回的 `(1920, 100, 2720, 700)` 会被真实 dxcam 拒绝。"""
      mgr = _manager(FakeMss(), {"left": 1920, "top": 100,
                                 "width": 800, "height": 600}, monitor=3)
      reg, _ = mgr._dxgi_region()
      if reg is not None:
          with self.assertRaises(ValueError):
              _validate_region_as_dxcam(1920, 1080, reg)
  ```
- **触发**：把 `screen.py` 的 `_dxgi_region` 改回缺陷形态即可。`git diff` 显示的旧实现（本文件同目录 diff 第 264-265 行）是：
  ```python
  if mon is None:
      return (left, top, left + w, top + h), False       # = (1920, 100, 2720, 700), False
  ```
- **后果**：
  - **缺陷形态** → `reg = (1920, 100, 2720, 700)`，`reg is not None` 为真 → 进入 `assertRaises`，`_validate_region_as_dxcam(1920, 1080, (1920,100,2720,700))` 命中 `not (1920 >= 2720 ...)` → 抛 `ValueError` → 断言**通过**。
  - **修复形态**（现行）→ `reg = None` → 整个 `if` 体不执行 → 断言**也通过**。
  这条用例在改前改后都为绿，它是本轮新增文件里**唯一**以"不得泄露绝对坐标"命名、却对该行为零判定力的断言。docstring 写的 `旧实现返回的 (1920, 100, 2720, 700) 会被真实 dxcam 拒绝` 只陈述了一个事实，没有把该事实变成断言。
- **证据**（假绿判定，逐行到指令）：
  1. 静态推演即闭环：`reg is None`（修复后被测行为）→ 不执行断言；`reg is not None`（缺陷形态）→ 断言恒真。两个分支都绿。
  2. 同文件 `TestRealDxcamCrossCheck::test_stub_matches_real_validate_region`（`:232-264`）**已经**独立证明了"`(1920,100,2720,700)` 会被真实 `DXCamera._validate_region` 拒绝"，所以本用例想表达的内容不需要它承担。
  3. capture-011 本身**并未失守**：`test_returns_out_of_range_flag`（`:122-131`，`assertIsNone(reg)` + `assertTrue(out_of_range)`）与 `TestGrabEndToEnd` 三条（`:156-187`）确实会在缺陷形态下翻红。本条是**冗余位上的假绿**，不是整条修复失守——级别按此定 P1 而非 P0。
- **同族入口**：
  - `tests/test_capture011_region_geom_missing.py:59`（`StrictCamera.grab` 内 `if region is not None:`）→ **不适用**：那是替身内部构造，`region=None` 时本就不该校验，语义正确。
  - `tests/test_capture012_geometry_drift.py` 内所有 `if ... is not None:`（`:276`、`:520`）→ **不适用**：都是夹具参数装配，非断言守卫。
  - 全仓扫描（`_tmp_audit_dist_scan_vacuous.py`，AST 扫 `if <cond>` 无 `else` 且体内含断言）共 6 处，逐条判定见 R8-D-02 与本条；其余 4 处均已核对为**已正确**（见"未发现真阳性的范围"）。
- **去重**：**新**。`docs/audit/test.md` 的 T-01~T-20 无此条（T-14/T-17 是另外两条，见下）。
- **建议修复**：把守卫换成结论侧断言，并保留对真实 dxcam 的交叉验证职责分工——
  ```python
  reg, out_of_range = mgr._dxgi_region()
  self.assertIsNone(reg, "几何取不到时返回了 region")   # 无条件
  self.assertTrue(out_of_range)
  ```
  若仍想保留"旧坐标必被 dxcam 拒"的证明，就直接断言常量：`with self.assertRaises(ValueError): _validate_region_as_dxcam(1920, 1080, (1920, 100, 2720, 700))`（它不再依赖被测行为）。

---

### R8-D-02｜T-14 的"面板与悬浮窗不相交"不变量里 `fy2` 是未定义名：四个判据项**只有 3 项真正参与运算**，第 4 项从未求值，而 ruff 的 F821 在本轮之前根本没扫过 `tests/`

- **级别**：P1（测试有效性：不变量少一个判据维度；同时暴露门禁盲区，见 R8-D-03）
- **位置**：`tests/test_panel_float_no_overlap.py:192-193`
- **代码**：
  ```python
  fx1, fy1, fx2, _ = _float_rect(app)
  overlap = not (px2 <= fx1 or px1 >= fx2 or py2 <= fy1 or py1 >= fy2)
  ```
- **触发**：`TestPanelAvoidsFloat::test_no_overlap_invariant`（`:172-197`）的 7 个组合，在**本机现行几何**下逐个求值。
- **后果**：
  - `_float_rect`（`:153-157`）返回的是 **4 元组** `(x, y, x+w, y+height)`；第 192 行用 `_` 丢掉了第 4 个值（= 悬浮窗**底缘**），第 193 行却引用 `fy2`。全仓 `fy2` 只出现在第 193 行这一处（`grep -rn fy2 E:\AI\teamview` 仅此匹配），模块 globals 里没有它 → 该名字一旦被求值就是 `NameError`。
  - 但反汇编证明它**永远不会被求值**：第 193 行的析取在短路求值下，项 1 `px2 <= fx1` 在 7 个组合里**全部为真**（面板右缘恒在悬浮窗左缘左侧，差值恰为 `_PANEL_GAP`），`JUMP_IF_TRUE_OR_POP` 直接跳到 `UNARY_NOT`，后三项一次都不执行。
  - 于是这条被 `docs/audit/test.md` T-14 登记为"**断言改为矩形不相交**"的核心不变量，实际只在**水平轴**上判了一次，纵向那一维（`py1 >= fy2` / `py2 <= fy1`）从未参与。用例名与 docstring 声称的"矩形不相交"高于它实际做的事。
- **证据**（实跑 + 反汇编，两者互相印证）：
  - `_tmp_audit_dist_panel_term4.py` 输出（本机实跑）：
    ```
    combo                  panel                  float                  第3项 py2<=fy1
    (361, 694, 2560)       (1475, 60, 2169, 1070) (2189, 10, 2550, 213)  False   (第1项=True 第2项=False)
    (960, 694, 2560)       (876, 60, 1570, 1070)  (1590, 10, 2550, 213)  False   (第1项=True 第2项=False)
    (380, 500, 1920)       (1010, 60, 1510, 1070) (1530, 10, 1910, 213)  False   (第1项=True 第2项=False)
    (160, 694, 1920)       (1036, 60, 1730, 1070) (1750, 10, 1910, 213)  False   (第1项=True 第2项=False)
    (960, 694, 1366)       (0, 60, 376, 1070)     (396, 10, 1356, 213)   False   (第1项=True 第2项=False)
    (600, 694, 1280)       (0, 60, 650, 1070)     (670, 10, 1270, 213)   False   (第1项=True 第2项=False)
    (361, 694, 1024)       (0, 60, 633, 1070)     (653, 10, 1014, 213)   False   (第1项=True 第2项=False)

    会真正求值第 4 项的组合数 = 0 / 7
    实测：模块 globals 含 fy2 = False
    ```
  - `dis.dis(TestPanelAvoidsFloat.test_no_overlap_invariant)` 关键片段（偏移取自实跑）：
    ```
    193  90 LOAD_FAST  8 (px2)
         92 LOAD_FAST 10 (fx1)
         94 COMPARE_OP 1 (<=)
         96 JUMP_IF_TRUE_OR_POP 60 (to 120)      ← 本机 7/7 组合在此跳走
         98 LOAD_FAST  6 (px1)
    ...
        114 LOAD_FAST  7 (py1)
        116 LOAD_GLOBAL 5 (fy2)                 ← 从未执行
        118 COMPARE_OP 5 (>=)
       120 UNARY_NOT
    ```
  - `co_names = ('subTest', '_app', 'position', '_panel_rect', '_float_rect', 'fy2', 'assertFalse')`、`co_varnames` 中**没有** `fy2` —— 编译器已把它当全局名，运行期只能靠 `NAME_ERROR` 收场。
  - **为什么全量跑 874 例仍 OK**：短路使它成为死操作数；这也解释了为什么单跑该用例只有 `ok`（`F:\conda\envs\fps-screen\python.exe -m unittest tests.test_panel_float_no_overlap.TestPanelAvoidsFloat.test_no_overlap_invariant -v` → `OK`）。
- **同族入口**：
  - `_float_rect` 的其余调用点：`tests/test_panel_float_no_overlap.py:151`（`test_panel_sits_left_of_float_with_gap` 内另一处解包）→ **已正确**（那里显式解成 4 名）。
  - 其余"未绑定名参与短路"形态：`_tmp_audit_dist_scan_vacuous.py` 扫出的 6 处已逐条判完，仅本条为真阳性。
  - `tests/test_bugfix_20260911.py:224`（`if first[0] == socket.AF_INET6:` 内才断言 IPv6 来源）→ **同缺陷形态、但本条不重复登记**：它在 `localhost` 优先解析为 IPv4 的机器上整段不执行，属同一族（守卫式断言无 else 兜底），已作为**同族第二例**在此列出；因其当前命中的是**平台相关**而非"恒定短路"，不单列条目。
- **去重**：**新**。T-14 登记的是"参数组补窄屏 1366/1280/1024 + 断言改为矩形不相交"，与"第 4 项恒不求值"是两件事——T-14 的修复确实做了（7 个组合都在），但它把不变量写成了 4 项、实际只有 3 项生效。**属"已登记条目的未收口残留"**（T-14 未收口），不是重复。
- **建议修复**：把 `_` 改成 `fy2`（一行）。改完该用例仍应为绿——若变红，说明它暴露了此前从未被检查的纵向重叠，那才是 T-14 真正想守的东西。

---

### R8-D-03｜门禁范围的**双重高估**：CI 的 `ruff check *.py` 一个字都没扫过 `tests/`（实测该目录 89 处违规，含 F821/F823），而 `pyproject.toml` 把"当作依赖被带入检查"当成了保护面——四个模块 141 个函数体、**0 个**被 mypy 检查

- **级别**：P1（门禁覆盖缺口：本轮扩范围的**动机**正是"窄范围漏掉真 bug"，而测试目录仍在范围外；同时 mypy 扩范围的实际保护面被配置文件注释高估）
- **位置**：`.github/workflows/test.yml:37`、`pyproject.toml:29-31`（ruff）、`.github/workflows/test.yml:38-41`、`pyproject.toml:62-63`（mypy）
- **代码**：
  ```yaml
  # .github/workflows/test.yml
  - name: Lint (全仓根模块；范围见 pyproject.toml 顶部裁决)
    run: |
      pip install ruff==0.16.8
      ruff check *.py
  ```
  ```toml
  # pyproject.toml
  # 覆盖范围在 2026-10-09 从「三个核心文件」扩到**全部根模块**（P1-4）。
  ...
  "host.py" = ["E402"]        # 仅 per-file-ignores，不含 tests/
  ```
  ```toml
  [tool.mypy]
  files = ["common.py", "codec.py", "pipeline.py", "ui_common.py"]
  # …screen.py 会作为 codec.py 的依赖被带入检查。
  ```
- **触发**：`ruff check *.py` 这一形态（`*` 不跨目录，由 shell/ruff 展开后只落根目录文件）；`python -m mypy` 常态。
- **后果（三个独立后果，一次说清）**：
  1. **`tests/` 完全在 lint 之外**：实测 `F:\conda\envs\fps-screen\python.exe -m ruff check tests --statistics` → **Found 89 errors**：`I001` 18、`E702` 16、`E741` 10、`UP041` 9、`UP015` 8、`F401` 7、`UP004` 4、`B007` 3、`F841` 3、`B905` 3，以及 **`F821` 1 处（`tests/test_panel_float_no_overlap.py:193:81: F821 Undefined name 'fy2'`）与 `F823` 1 处（`tests/test_bugfix_20260920_t06_selfcheck.py:381:42: F823 Local variable 'delay' referenced before assignment`）**。这两条恰好是本轮把 ruff 扩到全仓根模块、写明"F821 一共只出现 3 处，正是这两处"时要防住的缺陷类——**同一类在 `tests/` 里也有，且从未被拦**。R8-D-02 就是它漏掉的那一个。
     对照实验（`_tmp` 空目录 + 注入违规）：把 F821 注入 `tests/` 后，`ruff check *.py` **不报**，`ruff check *.py tests` **报** → 范围差就是 `tests/`。
  2. **mypy 扩范围的实际保护面接近零**：`F:\conda\python.exe -m mypy` → `Success: no issues found in 4 source files`。这里的 "4" 是 `files` 列表的 4 个模块——**不含 `screen.py`**（与 `files = [...]` 字面一致）。把 `screen.py` 复制到临时目录、在文件末尾追加 `_BREAK: int = "this is definitely not an int"` 后同口径跑 mypy → 报 `screen.py:744: error`，说明它确实被作为依赖读入（所以"被带入检查"这句话不是空的）；但 `screen.py:29: note: By default the bodies of untyped functions are not checked ... [annotation-unchecked]` 同时说明：**没有返回注解的函数体不被检查**。实测这 5 个文件的注解覆盖率：
     ```
     文件              def 总数  带返回注解  body 被 mypy 查
     common.py            54          0           0   ← 在 files 列表内
     codec.py             29          0           0   ← 在 files 列表内
     pipeline.py          24          0           0   ← 在 files 列表内
     ui_common.py          7          0           0   ← 在 files 列表内
     screen.py            27          0           0   ← 仅作为依赖被带入
     合计 def 141 个，其中带返回注解（= 真正被检查的函数体）0 个。
     ```
     即：本轮 `files` 从 1 个扩到 4 个、注释还额外宣称 `screen.py` 被带入，而**这 5 个模块的函数体一个都没在检查范围内**。同一口径下把 `_body_break: int = "not an int"` 注入 `screen.py` 的某个未注解函数体，mypy 仍报 `Success: no issues found in 4 source files`——本节所有结论都是实跑出来的。
- **证据**：上文两处实测输出（`ruff check tests --statistics` → 89 errors；mypy 临时副本对照 + `_tmp_audit_dist_mypy_coverage.py` 的注解统计）。
- **同族入口**：
  - `docs/audit/README.md` / `AGENTS.md` 里"`ruff check common.py codec.py screen.py`"的旧口径 → **已过期**（本轮改成 `*.py`），但 `AGENTS.md` 的 Lint 行仍在写三文件形态 —— 属文档面，交 doc 域（见「跨域移交」）。
  - `pyproject.toml` 的 per-file-ignores 只列 `host.py`/`host_ui.py`/`viewer.py`，**未用 `"*.py"` 兜底**（注释明确说明这是有意的）→ **已正确**：兜底会把核心文件一起豁免，这里避开了。
  - `tools/` 之类其它目录 → **不存在**（仓库无第二源码目录），故 `tests/` 是唯一缺口。
- **去重**：**新**。`dist-022`（"CI 单测改调 run_tests.bat + ruff/mypy 钉版并真正执行"）登记的是**是否执行**；本轮 P1-4 登记的**范围**扩到"全部根模块"；**没有任何既有条目覆盖"lint/类型检查完全不含 `tests/`"**，也没有条目记录 `files` 列表的真实注解覆盖率。
- **建议修复**（三选一，按代价排序）：
  1. 最小：CI 的 lint 步改成显式清单 `ruff check common.py codec.py screen.py host.py viewer.py pipeline.py share.py logger.py update_check.py diag.py gen_assets.py ui_common.py splash.py host_ui.py`（已实测 `All checks passed!`，见附录），**不**把 `tests/` 一次全卡（89 处会立刻翻红）。
  2. 推荐：先只对 `tests/` 开 `--select F821,F823,F401,F841`（缺陷类子集，非风格），实测仅 1+1+7+3 = 12 处，可一次收口；风格类留着不动，与本仓"E501/UP031 有意豁免"的既有裁决同向。
  3. 独立项：要么给 `screen.py` 加了返回注解并把文件列进 `files`，要么把 `pyproject.toml` 里"作为依赖被带入检查"改为"仅参与符号解析，函数体未被检查"，别让注释承诺一个不存在的保护面。

---

### R8-D-04｜`test_prune_skips_undeletable_and_continues` 的断言 `deleted >= 2` 比它钉住的确定值宽一倍：实测确定值是 **4**，任何"删到 2 个就停"的改动都会绿

- **级别**：P2（测试有效性：断言过宽，钉不住上界）
- **位置**：`tests/test_bugfix_20260924_round7.py:607-609`
- **代码**：
  ```python
  deleted = created - len(remaining)
  self.assertGreaterEqual(
      deleted, 2,
      "首个删不掉的文件让整轮清理退出了（dist-019 失效）：只删了 %d 个" % deleted)
  ```
- **触发**：把 `logger.py:162-175` 的阶段② while 循环改成"删除失败后只再删一个就停"或"把 `_KEEP_FILES` 判据整体放宽 1"之类的半截修复。
- **后果**：本夹具（`_KEEP_FILES + 5 = 125` 个当天文件、其中删文件名序最前的 1 个被 mock 成 `OSError`）下，阶段②应删除的**确定值是 4**（125 − 1 锁定 − 120 上界 = 4，实测 `remaining = 121`）。断言只要求 `>= 2`，于是"删到 2 个就提前收工"这一整类改动照样绿——而 dist-019 要守的正是"**不能提前收工**"。该用例的判定力比其 docstring 声称的弱。
- **证据**（实跑确定值）：
  ```
  _tmp_audit_dist_prune_exact.py
  _KEEP_FILES = 120
  created     = 125
  remaining   = 121
  deleted     = 4   ← 用例只断言 >=2
  锁定文件仍在 = True
  ```
- **同族入口**：
  - `tests/test_bugfix_20260924_round7.py:563-586`（同文件 `test_prune_reports_skip_without_crash`）→ **不适用**：该用例的判据是"构造不抛"，本来就没有计数断言。
  - `logger.py:104-195`（`_prune_old`）的唯一调用点 `_DateRotatingFileHandler.__init__`（`:97`）与跨天 `emit`（`:211`）→ **同缺陷已被本用例覆盖**，无遗漏入口。
- **去重**：**新**。`dist-019` 登记的是"第二遍改逐文件 continue"，本轮该夹具已从"日期全早于 cutoff、阶段②一个文件都没见过"的假绿形态修正过来（`:541-557` 的 docstring 已自陈），修正方向正确；本条是**修正后残留的断言过宽**，属"已登记条目的未收口残留"。
- **建议修复**：`self.assertEqual(deleted, created - 1 - logger_mod._KEEP_FILES, ...)`（即断言精确值 4），或至少把下界提到 4。

---

### R8-D-05｜带 BOM 的 `tests/` 文件（`test_item93_cred_dialog.py` 是唯一一个）

- **级别**：P2（约定一致性；当前无功能后果）
- **位置**：`tests/test_item93_cred_dialog.py:1`
- **代码**（首 3 字节）：
  ```
  EF BB BF  ← UTF-8 BOM，紧接其后才是 `# -*- coding: utf-8 -*-`
  ```
- **触发**：任何以字节口径处理该文件的工具。
- **后果**：
  - 仓库约定是"其余全部文件（`*.py` / `*.md` / …）UTF-8 **无 BOM**"（`AGENTS.md`「编码」节）。该文件是 `tests/` 下唯一违反者（实测扫描全部 64 个 `test*.py`：仅此一个）。对 `tests/` 做字节级处理的自研工具会因此多出 3 字节偏移。
  - **已知的良性后果**：ast 解析器不接受首字符是 `\ufeff` 的 `utf-8` 解码文本（我的扫描脚本据此把它报成 `SyntaxError`），但 CPython 的 **tokenizer** 认 BOM 并跳过，所以 `unittest discover` 照常导入、该文件 26 个用例本轮全跑全绿——**不构成功能缺陷**。这也是为什么它没被任何门禁发现：ruff 不扫 `tests/`（R8-D-03），ast 级脚本会把它当语法错跳过。
- **证据**：`[System.IO.File]::ReadAllBytes(...)[0..2]` = `EF BB BF`；全 `tests/` 扫描唯一命中；`_tmp_audit_dist_scan_vacuous.py` 对它的输出是 `SYNTAX_ERROR L0 invalid non-printable character U+FEFF`，而同一文件在 `unittest` 下正常收集。
- **同族入口**：全仓 `*.py` 扫描 → **仅此一处**（`tests/` 之外无）。`*.bat` 刻意保持 cp936/ANSI（`chcp 936`），**不适用**。
- **去重**：**新**。
- **建议修复**：去掉 BOM（`utf-8` 无 BOM 重存）。

---

## 二、发布链路与进程级基建：**未发现真阳性**（列明已核实项，避免"全绿"被误读为覆盖完整）

本轮改动**没有触及** `build_exe.bat` / `build_installer.bat` / `run_tests.bat` / `setup.bat` / `sign_exe.ps1` / 两个 `.spec` / `requirements.txt`（`git status` 与文件 mtime 双重确认）。对这些文件我只做了**回归核实**，结论如下（全部按 cp936 读取 `.bat`）：

1. **版本号无漂移**：`common.APP_VERSION = "1.5.0"`（`common.py:203`）、`version_info.txt` 的 `filevers/prodvers/FileVersion/ProductVersion = 1.5.0`、`installer.iss` 的 `AppVersion=1.5.0` + `OutputBaseFilename=TeamVision-Setup-1.5.0`、`README.md` / `docs/install.md` 引用与文件头、`docs/changelog.md` 章节号一致。**实际产物实测**：`dist\fps-host\fps-host.exe` 与 `dist\fps-viewer\fps-viewer.exe` 的 `FileVersion`/`ProductVersion` 均为 `1.5.0`，`installed` 侧 `dist\TeamVision-Setup-1.5.0.exe` 的 `ProductVersion` 也是 `1.5.0`（116,480,072 B ≈ 111 MiB，落在 `build_installer.bat` 的目标带内）。两个 exe 与安装包 `Get-AuthenticodeSignature` 均 `Valid`（`CN=TeamVision`，自签——脚本已明示自签只在验签本机有效）。
2. **`installer.iss` 的 `OutputBaseFilename` 解析实测可用**：`build_installer.bat` 用 `for /f "usebackq tokens=1,* delims=="` 现读该行（`dist-012` 的反解做法）。该文件带 UTF-8 BOM，首行 token 会带 BOM 而**不匹配** `if /i "%%A"=="OutputBaseFilename"`，但匹配发生在真正的第 9 行，实测输出 `SETUP_BASE=[TeamVision-Setup-1.5.0]`——**BOM 不构成缺陷**（我把它列为"看起来会坏、实测不坏"的排查项，不写进报告条目）。
3. **`build_installer.bat` 的产物守卫在本机真实环境可用**：`if exist "dist\%%D\_internal\python*.dll"` 与 `if exist "av.libs"`。我在临时目录做了隔离对照：cmd 的 `if exist`（路径含目录、模式含 `*`）**确实做通配匹配**（`python*.dll` → `MATCH`，故意改成不存在的 `python*.dllNOPE` → `NO MATCH`）；`dist\fps-host\_internal\` 实测同时有 `python3.dll` + `python310.dll` 与 `av.libs` 目录。故该守卫不是"恒真"也不是"恒假"。
4. **spec 未被重生成、`collect_all('av')` 与 `pathex=['vendor']` 未丢**：`build_exe.bat` 用 `pyinstaller --noconfirm --clean fps-host.spec`（不走 CLI 传参），两份 spec 均保留 `collect_all('av')`、`_splash_datas`、`hiddenimports=['dxcam']`、`pathex=['vendor']`、`console=False`、`version='version_info.txt'`；`fps-host.spec` 还带 `dxcam/comtypes` 的 `a.pure` 硬校验。实测 `dist\fps-host\_internal` 与 `dist\fps-viewer\_internal` 都有 `python310.dll` 与 `av.libs`。
5. **CI 单测口径与 discover 一致**：`.github/workflows/test.yml:42-46` 用 `cmd /c run_tests.bat` + `TV_CI_NO_CONDA=1`；`run_tests.bat` 的 `cd /d "%~dp0"` → `python -m unittest discover -s tests -p "test*.py"` → `set "RC=%ERRORLEVEL%"` → `exit /b %RC%`，退出码链路完整，与 `AGENTS.md` 口径一致。本轮新增的 3 个测试文件均在 `test*.py` 匹配内（见基线节的 64/64）。
6. **`logger.py` 本轮 34 行改动**（`_bootstrap_note` + 两处 F821 修正 + `setup_logger` 失败留痕）逻辑自洽：`_prune_old` 的两处跳过分支改用 `_bootstrap_note`（不再引用未定义的 `log`），`__init__` 期无 handler 可用 → 退 stderr，`sys.stderr is None` 时静默，异常在 `_bootstrap_note` 内部被吞（不会反噬主流程）。`_setup_logger_locked` 仍在 `except Exception` 里兜底，因此"文件 handler 构造失败"不再静默——本轮 `test_prune_reports_skip_without_crash` 就是它的钉子。
7. **`diag.py` / `update_check.py` / `gen_assets.py` 本轮**：只有 `update_check.py:108` 的 `isinstance(checked_at, (int, float))` → `isinstance(checked_at, int | float)` 一行（ruff 口径改写，py310 合法，语义等价；`isinstance` 不改写 `bool` 语义，`isinstance(True, int|float)` 仍为 `True`，与原实现一致）。`diag.py`、`gen_assets.py` 本轮未改。

---

## 三、本轮未发现真阳性的范围（查过但没结论的区域）

- **discover 覆盖面**：64/64 个 `test*.py` 全部被收集，874 例无跳过、无 `expectedFailure`、无静默截断。4 个不匹配文件是文档声明的重负载脚本（**它们不由任何门禁执行**，改协议后是否仍可用**无人验证**，属既有设计，不单列条目但记此一句）。
- **断言恒真形态**：AST 全仓扫描（`_tmp_audit_dist_scan_vacuous.py`）找 `assertTrue(a or b)` 型析取断言与"无 else 的守卫式断言"，命中 6 处，除 R8-D-01/R8-D-02 外，其余 4 处逐条核实为**正确**：`test_bugfix_20260920_t06_selfcheck.py:414`（先 `self.fail` 再断言，等价于无条件失败）、`test_ui_common.py:137-141`（`ast.walk` + `self.fail` 双守卫）、其余为夹具装配分支。**全仓无 `assertTrue(x or y)` 恒真断言**（该项只出现在 D-01 之外的 T-15 已修位置）。
- **零触碰生产代码的用例**：写了 AST 扫描（`_tmp_audit_dist_zero_touch.py`），但"模块级 import + 函数体内 import"让该启发式误报率过高（把 `import viewer` 当成了触碰），扫描结果不可用，**本项未能给出可信结论**；已知 T-16 型（自造数据不过生产路径）在 `tests/` 中仍未被系统性排查。这是本报告最大的覆盖缺口，见"跨域移交"。
- **发布链**：见第二节 7 条，均**已核实**而非"未查"。
- **`tests/` 的既有假绿存量**：`ruff check tests` 的 89 处里，我只逐条判定了两条与缺陷类相关的（`F821 fy2`、`F823 delay`）；`E702`/`E741`/`UP0xx` 等风格类**未逐条判**，其中可能仍藏有"分号压两行导致某断言永不执行"之类的形态（`E702` 16 处最可疑，未查）。
- **`docs/audit/test.md` 的 T-17 账目**：该条称"约 17 个文件、39 处 `inspect.getsource`，残留为与行为用例配对的 AST/结构断言"。我确认 `tests/test_item99_gen_assets.py:144-157`（`TestRealSourceFixed`，纯 `inspect.getsource` 字面串断言）属于该形态且**未被 T-17 点名**，但**没有**做全仓 `inspect.getsource` 清点，故不判定"T-17 账目失真"，仅记录该文件为该形态的一个未点名实例。该文件本轮未被修改。

---

## 四、跨域移交

1. **→ capture 域**：`tests/test_capture011_region_geom_missing.py:133-140`（R8-D-01）是 capture-011 的测试位之一。capture-011 的**功能**回归仍被 `test_returns_out_of_range_flag` 与 `TestGrabEndToEnd` 钉住，**修复本身不失守**；但若轮次报告要为 capture-011 写"关联测试路径"，`test_never_leaks_absolute_coordinates` 不应被算作判定力来源。
2. **→ capture 域**：`tests/test_capture012_geometry_drift.py` 里 `TestOutOfRangeMonitorConfig` / `TestRegionFollowsDesktop` 用 `cap._geometry` 手工设基准来区分"拔屏 vs 配置错误"（`:271-283`、`:516-525`）。这些用例钉的是 `_grab_mss` 内部判据，属 capture 域；我只确认"它们让改回缺陷形态会红"，未审判据本身的正确性（capture-013/014 的核心）。
3. **→ net 域**：`tests/test_multiview_integration.py` 本轮两处解包从 4 元改成 5 元（`_seq`），是 net-045 载荷布局变更的机械跟改——**这些位置只断言 `nal`/`flags`，对新增的 `seq` 零断言**。若 net-045 的验收标准要求"序号被正确传出"，缺口在 net 域（`tests/test_net045_frame_seq.py` 需自行负责该面）。
4. **→ doc 域**：`AGENTS.md`「环境与命令」仍写 `Lint: ruff check common.py codec.py screen.py`，与 `.github/workflows/test.yml:37` 的 `ruff check *.py` 不一致（本轮扩范围未同步文档）。另：`docs/changelog.md` 的 1.5.0 段落出现**两个 `**网络**` 小标题**（"采集/网络/网络"），第二段才是 net-045——分组标签重复，属文档面。
5. **→ viewer / host_ui 域（进程级基建相关）**：`logger._bootstrap_note`（`logger.py:58-77`）是本轮新增的**唯一**降级诊断通道，且它在 `sys.stderr is None` 时**静默**。打包形态是 `console=False`（`fps-host.spec` / `fps-viewer.spec` 的 `EXE(..., console=False)`），此时 `sys.stdout` 与 `sys.stderr` 同为 `None`（`logger.py:345` 的注释明确把这条当作既有事实）。因此**在打包发行版里，"文件日志 handler 构造失败"与"`_prune_old` 跳过文件"这两条提示都不可见**——恰好是 dist-020 要解决的那两种场景。请 viewer/host_ui 域确认：冻结 exe（尤其 `pythonw` 语义下）`sys.stderr` 是否确为 `None`；若是，这两处提示应改走 `diag` 里已有的持久通道（如写 `logs/` 下的一个 sidecar 或在 `report.txt` 落一行），而不是 stderr。
6. **→ 全体/Lead（操作性，非缺陷条目）**：本轮 `ruff check *.py` 这一形态**当前是红的**（`Found 8 errors, exit 1`），8 处全部来自审查队友的临时脚本 `_tmp_audit_*.py`（其中 5 处 `[*] fixable`）。这不代表生产代码退化——显式列 14 个根模块时 `All checks passed!`。但两件事需要注意：(a) 谁用 `ruff check *.py --fix` 收尾，会**就地改写审查队友正在引用的证据脚本**；(b) 最终门禁截图必须用显式清单，否则会留下一个看起来像回归的红。相关：`.gitignore:51` 的 `_tmp_*.py` 只覆盖 `.py`，本轮各域产出的 `_tmp_audit_*.txt` / `.diff` 会出现在 `git status`（我那两份 `.diff` 已自行删除）。

---

## 附录：本报告全部证据的可复现命令

```powershell
cd E:\AI\teamview
$PY = "F:\conda\envs\fps-screen\python.exe"

# 测试有效性
& $PY -m unittest discover -s tests -p "test*.py" -v        # → Ran 874 tests, OK，0 skipped
& $PY -m unittest tests.test_panel_float_no_overlap.TestPanelAvoidsFloat.test_no_overlap_invariant -v   # → ok（fy2 被短路，未求值）
& $PY _tmp_audit_dist_panel_term4.py                        # → 第 4 项求值 0/7，globals 无 fy2
& $PY _tmp_audit_dist_prune_exact.py                        # → deleted = 4（用例只断言 >=2）
& $PY _tmp_audit_dist_scan_vacuous.py                       # → 6 处守卫式断言，含 capture011:138

# 门禁覆盖
& $PY -m ruff check tests --statistics                      # → Found 89 errors（含 F821 fy2）
& $PY -m ruff check tests --select F821 --output-format concise   # → test_panel_float_no_overlap.py:193:81
& $PY -m ruff check tests --select F823,B007,F841,E731,UP041,B008 --output-format concise
& $PY -m ruff check common.py codec.py screen.py host.py viewer.py pipeline.py share.py `
        logger.py update_check.py diag.py gen_assets.py ui_common.py splash.py host_ui.py   # → All checks passed!
& $PY -m ruff check *.py                                    # → Found 8 errors（全来自 _tmp_audit_*.py）
& $PY _tmp_audit_dist_mypy_coverage.py                      # → 141 个 def，0 个带返回注解
F:\conda\python.exe -m mypy                                  # → Success: no issues found in 4 source files

# 发布链（*.bat 必须按 cp936 读）
[System.IO.File]::ReadAllText("E:\AI\teamview\build_installer.bat",[System.Text.Encoding]::GetEncoding(936))
git check-ignore -v _tmp_audit_dist_scan_vacuous.py         # → .gitignore:51:_tmp_*.py
(Get-Item dist\fps-host\fps-host.exe).VersionInfo.FileVersion   # → 1.5.0
Get-AuthenticodeSignature dist\fps-host\fps-host.exe            # → Valid (CN=TeamVision)
```
