# ui — 界面（Tk viewer/host_ui / 悬浮窗与面板刷新 / 热键 / 对话框 / 绘制与异常保护 / splash / ui_common）

新条目格式：`ui-序号｜一行现象｜关联测试文件路径`（序号从 001 起，无对应测试写 `—`）。
以下"示例"仅演示新格式；历史条目以旧编号原样保留在末节，不重编号。

示例（演示新格式）：
- `ui-001｜UI 基建（excepthook/色板/ttk 风格）收拢 ui_common.py｜tests/test_ui_common.py`

ui-002｜断线/重连状态跃迁作废帧缓存并递增序号，poll 判据含状态维度——「已断开，正在重连…」可达｜tests/test_viewer_poll_backoff.py（仅桩侧）+ tests/test_bugfix_20260920_t05_viewer.py（真实 Channel 三处跃迁 + poll 判据，补 T-05）
ui-003｜登录等待超时/中断标记请求作废（done 置位）并与「用户取消」区分，不再幽灵框复活/谎报取消/30s 静默冷却｜—
ui-004｜post_to_ui 投递失败只留痕，不再在工作线程就地执行控件操作｜—
ui-005｜账户管理窗单例守卫（连点不再叠 Toplevel）｜—
ui-006｜配置子节读取收口 _section()，标量节不再让 GUI 构造崩溃/设置不保存｜—
ui-007｜listen_host 非法值兜底改为 127.0.0.1（与默认/迁移/文案一致）｜—
ui-008｜诊断包导出移入工作线程 + data_dir 为 None 明确提示 + 可重入守卫｜—
ui-009｜UNSHARE 异步发送、切源控制消息超时 5s→1.2s（主线程不再冻结 5–10 秒）｜—
ui-010｜切源/重连清空帧率与延迟样本（P50/P95/fps 不再混两个源）｜—
ui-011｜关键帧与登录冷却改用单调钟（墙钟后跳不再永久静默）｜—
ui-012｜画面绘制失败日志节流（首帧 traceback + 每 10 秒汇总），不再每帧写盘｜—
ui-013｜旧共享会话清理移出 _share_lock（join 不再挡接收线程）｜—
ui-014｜悬浮窗定位宽度含状态叠加行（长队名/P95 不再把窗口推出屏幕）｜—
ui-015｜热键兜底注销改为留痕（UnregisterHotKey(None) 是空操作，注释与实现不再漂移）｜—
ui-016｜账户面板 hint 随列表清空（不再只写不擦）｜—
ui-017｜日志 handler 单份挂载 + 队列有界 + 退出摘除｜—
ui-018｜viewer 面板不再继承 host 基础 Treeview/Combobox 样式（§8 外观口径）｜—
ui-019｜B4 更新提示改走 _post_ui 收口（不再绕开跨线程 UI 调度）｜—
ui-020｜DPI 感知声明提到建 splash 之前（第 36 条回退修复，返回值记日志）｜tests/test_startup_order.py
ui-021｜观看端导出诊断包改走 diag-export 工作线程 + _diag_exporting 重入守卫 + data_dir 缺失明确提示（ui-008 此前只做在共享端）｜—
ui-022｜配置子节判型收口到 cfg_section()/load_config 出口校验：get_snapshot 等六处不再因标量节抛 AttributeError 让控制台永久陈旧｜—
ui-023｜_store_frame 对 fps/延迟列表的裁剪收进 self.lock，与 _reset_stats_locked 的 clear 不再竞争（IndexError 不再被误判断线重连）｜—
ui-024｜凭据队列淘汰改独立 _cred_evicted 标志：不再谎报「用户取消登录」、不再给无辜频道设 30 秒静默｜—
ui-025｜switch_source 把 WATCH 与 REQ_KEYFRAME 的失败处理分开：关键帧请求超时不再回滚已生效的订阅（标签与画面不再长期错位）｜—
ui-026｜认证失败不再无条件把 auth_msg 覆写成「需要登录后观看」，_authenticate 的具体真因在面板/poll/日志可见｜—
ui-027｜_reset_stats_locked 一并清 round_trip_ms（第 12 条补齐 RTT 半边，断开态不再显示上一条连接的 RTT）｜tests/test_bugfix_20260920_t05_viewer.py
ui-028｜热键切鼠标穿透后回写面板 _click_var，勾选框与实态一致、再点不再是空操作｜—
ui-029｜poll 纯文字分支也落账 serial/idx/status，断线态下 15→33→50ms 空闲退避恢复生效｜tests/test_bugfix_20260920_t05_viewer.py
ui-030｜字符串 capture.monitor 经 safe_int：图形控制台不再构造失败并静默回落到无窗口（console=False）｜—
ui-031｜叠加文本变长后重新定位悬浮窗（ui-014 只改宽度来源、未改触发条件）｜—

ui-032｜面板让位补窄屏兜底：收窄面板到可用宽度（下限 320）/ 纵向躲到小窗下方｜tests/test_panel_float_no_overlap.py
ui-033｜面板纵向适配：y 钳进 [边距, 屏高-面板高-边距]（footer 不越出下缘）｜tests/test_panel_float_no_overlap.py
ui-034｜_panel_user_moved 提供复位入口（_panel_show 清标记与几何缓存后重新让位）｜tests/test_panel_float_no_overlap.py
ui-035｜拖动悬浮窗同步 _float_left 并重新让位（面板不再按失效左缘定位）｜tests/test_panel_float_no_overlap.py
ui-036｜run() 移植分支逐条独立 try（尺寸复位不再被前两条异常跳过）｜tests/test_viewer_splash_root_reset.py
ui-037｜get_snapshot 的 port 过 safe_int（GUI 的 %d 格式化不再每 500ms 抛 TypeError 冻住面板）｜—
ui-038｜splash 之后的异常路径先收掉幽灵置顶窗（viewer/host 两侧）｜tests/test_viewer_splash_root_reset.py
ui-039｜import codec（→av）从 viewer 顶层移入 _load_heavy（splash 收益不再被吃掉一半）｜—
ui-040｜update_check 勾选即触发一次后台检查（不再等下次启动）｜—
ui-041｜_on_panel_configure 变化判定与定位统一用请求尺寸（拖拽面板后不再脱节）｜tests/test_panel_float_no_overlap.py
ui-042｜面板几何加「未变即跳过」缓存（状态文本变化不再做冗余 Tcl 下发）｜tests/test_panel_float_no_overlap.py
ui-043｜悬浮窗按屏高上限同比例缩小宽高（竖长采集区/超宽 display_width 不再越出屏幕）｜—
ui-044｜HostConsole 构造失败时销毁已建的 Tk root（回退命令行不再留无响应空窗）｜—
ui-045｜账户面板 refresh 加异常保护（手改 created 不再让面板永久空白且零提示）｜—
ui-046｜删除死接口：FrameEncoder(width,height)、VideoEncoder(rate_control) 及 cbr 死分支、frame_sent/_rebuilt_at_frame；palette/mask_secret 经复核保留（见修复记录 §7）｜tests/test_optimize_stage2.py + tests/test_ui_common.py

第 8 轮（2026-10-10，`docs/audit/round8/`）：

ui-047｜`_apply_probe_proto` 的 `_chal_capable` 对 `proto[1]` **缺类型保护**：同一函数第 426 行已按"真 int 且非 bool"把次版本收进 `host_minor`，第 431 行却仍拿原始 `proto[1]` 比大小——服务端回包写成 `[2, "4"]` / `[2, null]` 时抛 `TypeError`，被 `_authenticate` 的 except 记成「认证通信失败: '>=' not supported…」，每次重连复现，且这次调用的状态是**半更新**的（`_host_minor`/`_expect_seq` 已覆盖、`_chal_capable` 保持旧值）。改成 `try/except TypeError → False`——**刻意不收窄判据**，否则会把 JSON 里的 `[2, 4.0]`（float）从 True 翻成 False，而那与本条无关｜tests/test_round8_net_ui.py

ui-048｜net-046 新增调用点的注释把**可达路径写反**：注释声称它服务"带凭据的 LOGIN/REGISTER"，实际只有 `Act.PROBE` 分支（`_anon_ok` 重连与首次无凭据探测）能走到；带凭据的登录在 `_chal_capable is None` 时必先经 1086 分支的探针。调用点本身**必要且正确**，只订正注释——否则下一位维护者按注释推理会改错地方｜tests/test_round8_net_ui.py

第 9 轮（2026-10-10 后，"部署与更新 / 观看端体验"增量）：

ui-049｜更新提示只有一句话、没有出口：`check_update` 的返回文案不带地址，「关于」窗口的文档链接是不可点的纯 Label——用户知道有新版，还得自己搜仓库。现文案带 release 页面地址、两端发现新版时出现「前往下载 / 下载新版」按钮（`update_check.open_url` 只放行 http(s)），浏览器拉不起来时自动复制地址；顺带修掉一个同族缺口：共享端取消勾选「检查更新」时，状态栏里刚发现的提示会被"更新检查已关闭"直接覆盖｜tests/test_round9_update_links.py

ui-050｜悬浮窗宽度/透明度只能拖两个滑块；且启动时的配置夹紧口径（宽度 1~4096、alpha 0~1）与滑块口径（160~960、30%~100%）不同，手改过的配置能绕过滑块下界。新增「大宽屏 / 标准 / 小地图」布局预设（按屏幕可用宽夹紧、透明度守 30% 下限），并把四处字面量收成 `WIDTH_MIN/MAX`、`ALPHA_MIN/MAX`｜tests/test_round9_layout_presets.py

ui-051｜观看端「一键诊断」只能复制纯文本，贴进聊天工具后折成一团。新增「复制富文本」（CF_HTML），不支持时退回纯文本并在状态栏说明｜—（纯 UI，Tk 剪贴板依赖；未进 CI）

ui-052｜ui-042 的"几何未变即跳过"只比位置串：位置不变而**高度**变了（新增行、状态文本换行）时面板被钉在旧高度、底部留空白。现在同位置下补比实际宽高（`_panel_auto_size`），记过尺寸之前保持 ui-042 的跳过行为｜tests/test_panel_float_no_overlap.py（原用例仍绿，未新增专测——现有夹具的宽高恒定，钉不住"高度变化"这一面）

第 12 轮（2026-10-23，协议 v2.5 质量反馈闭环）：

ui-053｜共享端控制台「已连接观看端」表看不到**观看端那头的实际体验**：表里只有本端的发送速率与累计流量，而"我这发得挺顺、他说卡"这类故障在旧表里完全看不出来。新增"观看体验"列（每个在线观看端的 RTT / 积压 / 丢帧），三种状态分得开：未上报（旧版观看端或反馈关着）/ 已过期（>10s 没上报，该端掉线或卡到发不出）/ 有数据｜tests/test_round12_quality_feedback.py（`_fmt_quality` 的行为经 `check_release` 之外的独立用例间接覆盖；该函数本身是纯格式化，未单列专测）

ui-054｜观看端悬浮窗状态行只显示 fps 与延迟，用户**无法分清"网络差"与"本机解码慢"**：两者都表现为"卡"。现在积压 ≥200ms 或丢帧 ≥1% 时如实标出（"积压 850ms" / "丢帧 3.2%"），与上报给共享端的是同一组数｜tests/test_round12_quality_feedback.py

## 历史（旧编号，冻结）

> 逐条来自 `docs/audit/README.md` 冻结对照表，归属为 ui 的条目。一行一条 `第 N 条｜简述`。

- 第 8 条｜切源未清 frame_serial，悬浮窗仍显示上一个人画面
- 第 12 条｜fps/RTT 断流后锁存旧值，死频道仍显示 30fps·12ms
- 第 15 条｜host 为 windowed 且全进程无 excepthook，崩溃无弹窗无日志
- 第 34 条｜_set_status 收下 error 参数后完全忽略，成功失败无差别
- 第 35 条｜关鼠标穿透清掉 WS_EX_LAYERED，透明度静默失效
- 第 36 条｜全进程从未声明 DPI 感知，缩放下悬浮窗与文字皆糊
- 第 37 条｜host 控制台两条永不合并的刷新链，表格每半秒双重重建
- 第 38 条｜一次异常让 _refresh 永久停在旧值，控制台与现实脱节
- 第 39 条｜面板 withdraw 后刷新仍全速跑，不可见地白烧 CPU
- 第 40 条｜Treeview 全删全建不调 see，滚动位置每半秒被拽回顶部
- 第 41 条｜全局热键过度占用（Alt+数字/旋转冲突），部分注册失败不报
- 第 42 条｜热键切 active_idx 而面板跟 _panel_selected，非同一频道
- 第 43 条｜单击即置 user_moved，悬浮窗永久失去自动贴右上角
- 第 44 条｜向导点"跳过"不落盘，每次启动重弹且默认频道不保留
- 第 45 条｜面板隐藏 + 穿透时控制台唯一入口（右键）失效找不回
- 第 46 条｜登录框 wait_window 嵌套期间悬浮窗完全停止绘制
- 第 58 条｜host 日志框只 insert 不删，多小时后会话内存无上限
- 第 60 条｜主线程 join(2.0)/wait 硬冻界面，退出按频道最多卡 2N 秒
- 第 64 条｜拖一次滑块触发几十上百次整份写配置 + 悬浮窗全量重绘
- 第 65 条｜配置字典被两把不同锁保护，后写用陈旧快照覆盖前写
- 第 86 条｜splash 带 root 路径下异常时窗口销毁不了，变关不掉的置顶无边框窗
- 第 91 条｜面板预览/列表刷新函数体无异常保护，一帧异常形状即永久停更
- 第 92 条｜热键/诊断线程绕过 _post_ui 直接 root.after，退出瞬间有崩/挂险
- 第 94 条｜新增频道不校验地址，localhost 与 127.0.0.1 并存致无限弹登录框
- 第 98 条｜绘制路径每帧重算并下发窗口位置 + 无条件 reconfigure 状态标签
- 第 99 条｜gen_assets 字体路径硬编码回退豆腐块，且无提示覆盖两个跟踪资源
- 第 108 条｜viewer UI 操作队列化（_post_ui 只入队）+ 可中断退避 Event + 凭据队列限长
- 第 115 条｜C4 观看端两棵树改增量刷新，只更新值变化的行
- 第 117 条｜C8/C10 热键注册结果上屏、频道步进回绕、穿透样式不抛、退出释放热键
- 第 133 条｜splash 提前到重依赖之前（cv2/av 延迟加载）
- 第 136 条｜UI 基建（excepthook / 色板 / ttk 风格）收拢至 ui_common.py
