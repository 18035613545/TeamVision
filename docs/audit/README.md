# 审计条目索引（docs/audit）

本目录是 TeamVision 的**审计条目索引**。

- **自 2026-09-18 起，新条目不再续接"第 N 条"，改用 `模块-序号` 格式。**
  模块文件：`capture.md`（采集）/ `codec.md`（编解码）/ `net.md`（网络与协议）/
  `auth.md`（认证与安全）/ `ui.md`（界面）/ `dist.md`（打包与发布）/`cfg.md`（进程级基建：config/探针/日志初始化）。
  自第 6 轮（2026-09-20）起另有两个归属：`doc.md`（文档与代码口径不一致，承载报告的 **D 系列**）、
  `test.md`（测试有效性，承载报告的 **T 系列**）。
- **每条的书写格式** = `模块-序号｜一行现象｜关联测试文件路径`
  （关联测试文件写仓库相对路径；若暂无对应测试，测试位写 `—`）。
  例：`net-014｜中继整帧物化由 2 次降到 1 次｜tests/test_relay_zero_copy.py`。
  测试位为 `—` 就是"无测试"，**不得**为凑格式写一个看起来相关但没有钉住该修复的文件名——
  第 6 轮的 cfg-009 正是这类失真的后果（三行与 `tests/` 现状相反，索引本身不可信）。
  若登记的用例存在但**不覆盖修复点**（假绿），须在测试位后括注说明，例：`tests/xxx.py（假绿：…）`。
- **例外**：`doc.md` 与 `test.md` 沿用报告自身的 `D-序号` / `T-序号` 编号，不另起 `doc-001`/`test-001`
  序列（与"历史条目以旧编号原样保留、不重编号"同一处理方式），以免报告、修复记录、索引三处编号互查断链。
  这两个文件的"关联测试"含义也略有不同，各自文件头已注明。
- 序号在各模块内**从 001 起独立编号**，不再跨模块连续，避免历史上的"第 N 条"
  被多处互不相关的修复复用、编号被稀释的问题。
- **同族入口硬性步骤（第 7 轮审查 §三 横向建议，2026-09-24 起）**：本项目的最高频
  缺陷形态是"修一处漏一处"（第 7 轮 22/57 条属此类：codec-011 只修 share 侧、
  net-019 的守卫少罩一步、net-029 只给 WATCH 补回话、cfg-004 的返回值无人跟进）。
  因此**每条修复落地时，必须 grep 出同族的其它入口/调用点，并在修复记录里逐一标注
  "已改"或"不适用及原因"**；索引条目里若存在未收口的同族入口，必须在现象位写明。
  第 7 轮的示范：S3 同时修 host/share 两侧、G5 同时收口 2181/2891/2069/1516 四处、
  G26 同时修 viewer/host 两条 main 路径、dist-016 同时改两个 bat。
  **第 8 轮（2026-10-10）的示范**：`viewer.py` 的 seq 缺口遥测在"换连接/切源/初始化"
  三处复位点同时收口；`host.py` 的旧观看端告警同时接在准入开启与准入关闭两条认证路径上
  （`net-047`）；测试侧的隐式顺序依赖用"逐个模块单跑"把同族的 4 个文件一次扫干净
  （`test.md` 的 T-22）。
  **第 9 轮（2026-10-10 之后的增量）的示范**：更新提示的页面地址在 `update_check` 一处生成，
  两端按钮共用 `extract_url` / `hint_headline` / `open_url`，不再各写一套分流；布局上限下限
  从四处字面量收成 `viewer.WIDTH_MIN/MAX`、`ALPHA_MIN/MAX`，构造期夹紧、滑块、setter、
  预设全部引用同一常量；分辨率档位切换沿用 `set_fps` 已有的"锁外构造 + 失败退避"契约，
  而不是在编码路径里另起一套。

## 关于第 118–130 条缺号

**第 118–130 条缺号，且不可修复——历史文档从未落盘该区间。**
第 117 条（C8/C10 热键与穿透）之后直接跳到第 131 条（反作弊误报收敛 + 默认安全加固）。
（唯一命中的"第 130 条"是 `docs/反作弊兼容性评估.md` 里对 `fps-host.spec` 关闭 UPX 的一句
行内注释，属该文档自身的建议编号，**不属于**本索引的连续缺陷序列，故不作为条目登记。）
下方冻结表以一行区间记录此缺号，请勿再据此续号。

## 冻结对照表（**冻结，不再更新**）

> 本表用于把历史"第 N 条"一次性钉死为对照索引；此后新增/变更一律走各模块文件的
> `模块-序号` 格式，**本表不再改动**。
> 每行 = `编号 N｜一行现象｜归属模块`。现象取自提交标题（`git log --grep 条`）、
> `docs/BUG审查报告-2026-09-11.md`、`docs/修复记录-2026-09-11*.md`、
> `docs/优化方案-2026-09-08.md` / `-2026-09-17.md` 与相关代码注释；
> 归属模块按现象判断（capture / codec / net / auth / ui / dist / 其他）。
> 个别编号在不同文档中被复用为不同含义时，102–136 以 `git log` 的落地提交为准。
> 取不到逐条描述的编号，现象位写"（历史文档未留一行描述）"，不臆造。

| 编号 | 一行现象 | 归属模块 |
|---|---|---|
| 1 | 切源后解码器不重建（HEVC→H.264）致永久黑屏 | codec |
| 2 | 关键帧门控与 pong 空闲计时叠加，画面定格死锁 | net |
| 3 | 共享侧 key_wanted 提前清空，关键帧请求被吃掉 | codec |
| 4 | _encode_send 无 try，上传线程静默死亡且永不重启 | codec |
| 5 | 采集器句柄置 None 后空转，会话活着什么都不传 | capture |
| 6 | encode 异常被就地吞、available 恒真致永久冻结零日志 | codec |
| 7 | 只解最新帧打断 H.264 参考链，周期性花屏拖影 | codec |
| 8 | 切源未清 frame_serial，悬浮窗仍显示上一个人画面 | ui |
| 9 | switch_source 先本地提交无回包，标签显示队友实为本地 | net |
| 10 | 硬断线只能靠 60s keepalive，观看端冻结约 60 秒 | net |
| 11 | 缺 PyAV 时解码恒空仍置 video_mode，永远"连接中…" | codec |
| 12 | fps/RTT 断流后锁存旧值，死频道仍显示 30fps·12ms | ui |
| 13 | 非管理员装 Program Files，import 期建 logs 即 PermissionError | dist |
| 14 | 两进程抢同一日志文件，rollover 改名失败后永久停写 | 其他 |
| 15 | host 为 windowed 且全进程无 excepthook，崩溃无弹窗无日志 | ui |
| 16 | config.json 损坏无容错，起不来且不回退默认 | 其他 |
| 17 | host 与 viewer 整文件回写，互相陈旧覆盖配置 | 其他 |
| 18 | run_server 裸 daemon 线程静默死亡，控制台假健康 | net |
| 19 | listen_host 失效恒绑 ::，日志谎报实际监听地址 | net |
| 20 | Windows SO_REUSEADDR 等于允许端口被劫持 | net |
| 21 | 准入关闭可自报任意用户名，任何人能伪造队友 | auth |
| 22 | register 对任意端开放，PBKDF2 在全局锁内可被打满 | auth |
| 23 | auth_timeout 是每 recv 超时，未认证 slowloris 可占线程囤 64MB | auth |
| 24 | frp token 明文进命令行与配置，卸载留凭据残渣 | auth |
| 25 | 成员可订阅自己，无环路保护致镜像隧道双倍流量 | net |
| 26 | 中继 MSG_FRAME 零校验，is_key 完全由成员自报 | net |
| 27 | accounts.json 损坏即全体账号蒸发，全程无日志 | auth |
| 28 | viewer 靠中文字符串判断，注册重名抹掉有效凭据 | auth |
| 29 | _anon_ok 一旦为真永不复位，只能重启 viewer 才能登录 | auth |
| 30 | 首连开准入的 host 必失败一次、弹两次框 | auth |
| 31 | 登录框等 120 秒而 host 只等 60 秒，输对了也登录失败 | auth |
| 32 | "恢复全屏"是 no-op，旧采集区域被保留并回写 | capture |
| 33 | 采集区域校验形同虚设，非法区域致每帧抛错黑屏 | capture |
| 34 | _set_status 收下 error 参数后完全忽略，成功失败无差别 | ui |
| 35 | 关鼠标穿透清掉 WS_EX_LAYERED，透明度静默失效 | ui |
| 36 | 全进程从未声明 DPI 感知，缩放下悬浮窗与文字皆糊 | ui |
| 37 | host 控制台两条永不合并的刷新链，表格每半秒双重重建 | ui |
| 38 | 一次异常让 _refresh 永久停在旧值，控制台与现实脱节 | ui |
| 39 | 面板 withdraw 后刷新仍全速跑，不可见地白烧 CPU | ui |
| 40 | Treeview 全删全建不调 see，滚动位置每半秒被拽回顶部 | ui |
| 41 | 全局热键过度占用（Alt+数字/旋转冲突），部分注册失败不报 | ui |
| 42 | 热键切 active_idx 而面板跟 _panel_selected，非同一频道 | ui |
| 43 | 单击即置 user_moved，悬浮窗永久失去自动贴右上角 | ui |
| 44 | 向导点"跳过"不落盘，每次启动重弹且默认频道不保留 | ui |
| 45 | 面板隐藏 + 穿透时控制台唯一入口（右键）失效找不回 | ui |
| 46 | 登录框 wait_window 嵌套期间悬浮窗完全停止绘制 | ui |
| 47 | 每次关键帧请求整个重建硬件编码器会话，码率自激振荡 | codec |
| 48 | 码率控制硬编码 CBR，全仓无 vbr 配置，静止画面仍白烧码率 | codec |
| 49 | 回退 JPEG 后 congested 恒真，画质一路压到地板不回升 | codec |
| 50 | libx265 回退路径 4 帧流水线延迟，恒定 +133ms 端到端 | codec |
| 51 | _close_ctx 调用 PyAV 17 不存在的 close()，关闭是死代码 | codec |
| 52 | set_bitrate 会把已关闭编码器复活，关停竞态新开会话可 abort | codec |
| 53 | 编码器打不开无负缓存，每帧新建打满 CPU 并刷爆日志 | codec |
| 54 | 一个卡住的观看端 sendall 阻塞写锁，能挂住整台 host | net |
| 55 | roster 广播释放锁再发，旧快照覆盖新致源列表永久少一人 | net |
| 56 | 成员掉线时不加锁取 clients，误拆的是观看者连接 | net |
| 57 | MAX_PAYLOAD 64MB 囤积 + rx_buf O(n²) 整份重拷 | net |
| 58 | host 日志框只 insert 不删，多小时后会话内存无上限 | ui |
| 59 | tune_socket 把 KEEPINTVL/KEEPCNT 都设成 idle，半开检测≈关闭 | net |
| 60 | 主线程 join(2.0)/wait 硬冻界面，退出按频道最多卡 2N 秒 | ui |
| 61 | threading.Timer 非守护且不 cancel，退出后仍新开上传会话 | capture |
| 62 | _external_running 一旦为真永不复位，frpc 成杀不掉的孤儿 | net |
| 63 | frp 启动只用 tasklist 探测不验活，token 写错秒退仍报已启动 | net |
| 64 | 拖一次滑块触发几十上百次整份写配置 + 悬浮窗全量重绘 | ui |
| 65 | 配置字典被两把不同锁保护，后写用陈旧快照覆盖前写 | ui |
| 66 | shutdown 的 join 预算小于它要等的关停，encoder 常来不及 close | net |
| 67 | 版本号四处漂移（common/version_info/iss/bat/文档），会发陈旧产物 | dist |
| 68 | build_exe.bat 重生成 spec 抹掉 pyav collect_all，静默退 JPEG | dist |
| 69 | vendor pathex 污染打包，两个 exe 打进的 numpy 版本不同 | dist |
| 70 | requirements 全 `>=` 无上限，dxcam 装到的非实测 vendored 版本 | dist |
| 71 | 缺 frpc.exe 仍能编出"成功"安装包，用户装完自启失败 | dist |
| 72 | 三个 .bat 无 `cd /d %~dp0` 且每条退出以 pause 结尾，CI 挂死 | dist |
| 73 | setup.bat 不查 conda activate 返回值就 pip install，装进 base | dist |
| 74 | installer.iss 未装 splash.png，安装版静默降级为纯文字启动画面 | dist |
| 75 | 部署文档默认值与 `--console` 说明与代码大面积不符 | 其他 |
| 76 | host 硬编码兜底与 DEFAULT_CONFIG 两套矛盾，显式 null 崩采集线程 | 其他 |
| 77 | e2e 改写真实 config.json 产 .e2e_bak，可能提交明文 frp token | 其他 |
| 78 | e2e 硬编码机器/端口/坐标，且 FAIL 汇总是永不触发的死代码 | 其他 |
| 79 | 跑单测污染工作树并删日志，viewer import 期换 excepthook 可卡住套件 | 其他 |
| 80 | test_encode_bgr_caps_size 只断言 len>100，从不验分辨率 | 其他 |
| 81 | 无 conftest/pytest.ini/CI，仓库根外任何跑法 ModuleNotFoundError | 其他 |
| 82 | 计划文档勾选双向失真，Task 12/13 勾完却从未执行 | 其他 |
| 83 | perf.still 默认 false 与规格承诺 true 漂移，静止省流卖点不成立 | 其他 |
| 84 | 多处定义但全仓零调用的死代码（unpack_frame/av_available 等） | 其他 |
| 85 | host 从不 record_rtt，RTT 遥测与 GUI RTT 列永久显示 0 | 其他 |
| 86 | splash 带 root 路径下异常时窗口销毁不了，变关不掉的置顶无边框窗 | ui |
| 87 | 静止检测数逐通道元素、quiet_ms 实为秒（当前 enabled=False 不可观测） | capture |
| 88 | calc_output_size target_width=1 时返回宽 0，resize 抛错静默杀线程 | capture |
| 89 | _perf_loop 循环体无异常保护，非数字配置静默杀性能线程 | net |
| 90 | _encode_loop 静止期强制关键帧分支在 try 外，编码器半死即杀线程 | codec |
| 91 | 面板预览/列表刷新函数体无异常保护，一帧异常形状即永久停更 | ui |
| 92 | 热键/诊断线程绕过 _post_ui 直接 root.after，退出瞬间有崩/挂险 | ui |
| 93 | 凭据框 wait_window 可嵌套，超时后结果会串到下一次请求 | auth |
| 94 | 新增频道不校验地址，localhost 与 127.0.0.1 并存致无限弹登录框 | ui |
| 95 | recv_msg 消费 5 字节头后才抛错，残留负载致消息流错位 | net |
| 96 | 共享会话偷偷改共享 socket 的 timeout 不还原，破坏阻塞基线 | net |
| 97 | stop() join 超时 < 发送超时，在上传线程脚下关掉编码器与句柄 | capture |
| 98 | 绘制路径每帧重算并下发窗口位置 + 无条件 reconfigure 状态标签 | ui |
| 99 | gen_assets 字体路径硬编码回退豆腐块，且无提示覆盖两个跟踪资源 | ui |
| 100 | dxcam output_color=BGR 红蓝互换（此号被 13 处杂项修复复用） | capture |
| 101 | Inno Setup 用户级安装路径探测 | dist |
| 102 | 阶段 0 止血：accept 循环遇 OSError 不再永久退出（含 A2 配置回退可写目录） | net |
| 103 | 连接数上限 max_clients + 单条消息总时限 msg_timeout_s 防 slowloris | net |
| 104 | 认证/控制消息 user/pass 字段补 isinstance str 类型校验 | auth |
| 105 | 发送半截消息即断连 + last_frame 只取引用不 copy + 解码代数校验 | net |
| 106 | viewer.channels 坏配置做规范化，只回退不崩 | 其他 |
| 107 | 上行令牌桶限速，防单成员把上行 1:N 放大打满 host | net |
| 108 | viewer UI 操作队列化（_post_ui 只入队）+ 可中断退避 Event + 凭据队列限长 | ui |
| 109 | 拆分巨型函数（run_server 678→51 等）；原表另记「全文件最大单函数 678→82」，该数字只是当时快照，已随功能增长失效，见文末「冻结表复核补记」 | 其他 |
| 110 | 抽出 pipeline.FrameEncoder，host 与 share 共用编码器生命周期与 JPEG 回退 | codec |
| 111 | 采集/静止门控统一到 pipeline.CaptureSource / StillGate | capture |
| 112 | C1 ABR 从开环改闭环，降级目标按实测吞吐一步收敛 | net |
| 113 | 流水线唤醒事件 raw_evt：采集写槽后置位，编码事件驱动消费 | codec |
| 114 | 预打包 Struct `_MSG_HDR` 协议头，替代多段临时 bytes 拼接 | net |
| 115 | C4 观看端两棵树改增量刷新，只更新值变化的行 | ui |
| 116 | C6 解码上下文切源复用 + flush，重建前先 close，不再每次 new VideoDecoder | codec |
| 117 | C8/C10 热键注册结果上屏、频道步进回绕、穿透样式不抛、退出释放热键 | ui |
| 118–130 | （缺号，历史文档从未落盘该区间；见上文说明，不作为条目续号） | 其他 |
| 131 | 反作弊误报收敛 + 默认安全加固（listen_host/auth 迁移）+ onedir 打包 | auth |
| 132 | 中继整帧物化 2→1 + viewer poll 空闲退避 | net |
| 133 | splash 提前到重依赖之前（cv2/av 延迟加载） | ui |
| 134 | 共享端一键诊断包 + 公网自检结论上面板 | net |
| 135 | 协议层单点：Act 常量表 + 共享接收泵 + proto 字段 | net |
| 136 | UI 基建（excepthook / 色板 / ttk 风格）收拢至 ui_common.py | ui |

### 冻结表复核补记

冻结表记录的是**当时的动作**，其中写死的量化结论会随代码演进而失效。失效时不要直接改
历史条目（那会丢掉"当时确实达成过"的记录），改为在此登记复核结论。

**第 109 条的「全文件最大单函数 678→82」（2026-10-09 复核，2026-10-10 订正数字）**：该声明
已不成立。AST 统计当前最大两个函数是 `viewer.py:_receiver_loop`（260 行）与
`host.py:_auth_required_phase`（234 行）——首次复核把前者写成了 252，是按当时快照记的，
本轮改动后已增长。这不必然是回退——很可能是后续功能继续生长盖过了当时的收敛成果——但把一个
会随代码演进而失效的数字写成固定声明，本身就是文档治理上的缺口：后来者按它判断"是否达标"
会得到错误结论。今后涉及体量/行数/占比的量化结论，只写"已拆分"这一动作，不写死具体数字。
