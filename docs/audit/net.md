# net — 网络与协议（host 服务端/中继/roster / common 协议 / frp 隧道 / ABR 吞吐）

新条目格式：`net-序号｜一行现象｜关联测试文件路径`（序号从 001 起，无对应测试写 `—`）。
以下"示例"仅演示新格式；历史条目以旧编号原样保留在末节，不重编号。

示例（演示新格式）：
- `net-001｜协议层单点：Act 常量表 + 共享接收泵 + proto 字段（原第 135 条）｜tests/test_protocol_single_point.py`

net-015｜泵把「本轮消费条数」传给 on_buf，note_rx 按完整消息刷新（滴字节不能再续命慢速攻击）｜tests/test_protocol_single_point.py（仅泵侧契约）+ tests/test_bugfix_20260920_t02_recvloop.py（真实 _client_recv_loop 记账，补 T-02）
net-016｜ABR 判据只采信本机源订阅者（含实测吞吐），降级补订阅者门控｜—
net-017｜解码积压检测 + 追新模式（重置解码器、只喂关键帧直到 IDR），延迟不再只增不减｜—
net-018｜parse_addr 支持 IPv6（[::1]:5700、裸 ::1 不再被当成端口 1）｜—
net-019｜入册后任何异常都走统一收尾（不再泄漏客户端/发送线程/删不掉的源）｜—
net-020｜触发全量 roster 广播的 watch/unshare 加限速（10 秒 3 条）｜—
net-021｜peer 源订阅者丢帧不再置本机 force_key（IDR 风暴正反馈）｜—
net-022｜准入关闭时可选 auth 消息加总时限（滴字节不再永久挂连接名额）｜—
net-023｜公网自检周期化（10 分钟）+ sendall 进 try + 面板结论带时效提示｜—
net-024｜tune_socket 接住 TypeError/ValueError；Windows py<3.13 用 WSAIoctl 补 keepalive 空闲/间隔｜—
net-025｜safe_int/safe_float/normalize_region 防 Infinity/NaN/1e999（OverflowError 同接）｜—
net-026｜认证消息非 dict（如 [1,2]）明确按协议错误拒绝，不再误标通信失败｜—
net-027｜parse_addr(":5700") 的空主机补 raise ValueError（第 94 条地址校验的既有口径缺口，非 net-018 引入）｜tests/test_bugfix_20260920_addr.py
net-028｜裸 IPv6+端口 `::1:5700` 不再被整串当主机名（末段是合法端口且前缀是合法 IPv6 时才拆，net-018 回归收口）｜tests/test_bugfix_20260920_addr.py
net-029｜WATCH 被 net-020 配额限流时回 WATCH_REJECT(reason=throttled)，让对端按第 9 条回滚（UNSHARE 侧按设计仍不回话，见修复记录 §8）｜—
net-030｜message_pump 新增 on_tick：单条消息总时限与半开判定也在「每个 recv 轮次」上评估（滴字节不再绕过）｜tests/test_bugfix_20260920_t02_recvloop.py
net-031｜server_handshake 增 deadline 形参：握手阶段补总时限，一次握手最坏时长由 4×10=40 秒收到 10 秒｜tests/test_item100_fixes.py
net-032｜_probe() 的 delay 缺 nonlocal → 自检线程首次迭代即 UnboundLocalError；改自有 next_delay + 线程体 try/except 记日志｜—
net-033｜公网自检循环内复核 frp.is_running()，隧道停掉后不再每 10 分钟对外拨号（与 net-032 同批落地）｜—
net-034｜追新判据去两端墙钟差：改用「时延相对历史最优基线的增量」，固定钟差在差值里抵消，跨机时钟偏差不再周期性黑屏｜—
net-035｜去掉积压判据的 10 秒上界与「单次 recv 装得下 12 帧」的恒不可达档，改批内跨度比；补「追新 5 秒未等到关键帧即重连」兜底｜—
net-036｜观看端 last_rx 改按完整消息刷新（接 on_buf）并接 on_tick 做停滞判定，滴字节不再把频道钉在「接收中」｜—

net-037｜broadcast_roster 纳入「入册之后每一步」的统一收尾守卫（异常不再泄漏 client/发送线程/fd）｜tests/test_bugfix_20260924_round7.py
net-038｜pack_msg 对游离代理位兜底（非严格 UTF-8 编码）+ 用户名统一净化（去代理位/控制字符 + 长度截断）｜tests/test_bugfix_20260924_round7.py
net-039｜隧道自检线程改幂等补建（GUI「启动隧道」成功回调也建，frpc 重启后可补）｜tests/test_bugfix_20260924_round7.py
net-040｜被配额限流的 UNSHARE 挂起并在窗口内补做（不再静默丢弃留幽灵源）｜tests/test_bugfix_20260924_round7.py
net-041｜RTT 改单调钟配对（墙钟回拨不再把读数永久锁在旧值）｜tests/test_bugfix_20260924_round7.py
net-042｜_resync/_resync_at 随重连复位（新连接第一个巡检窗口不再误判）｜—
net-043｜外部托管改实探 SakuraFrpService（2 秒缓存），服务退出即复位 latch｜tests/test_item62_frp.py
net-044｜main 尾部单一 try/finally：frp.start() 之后的失败也走 runtime.shutdown()（不留孤儿 frpc）｜—

## 历史（旧编号，冻结）

> 逐条来自 `docs/audit/README.md` 冻结对照表，归属为 net 的条目。一行一条 `第 N 条｜简述`。

- 第 2 条｜关键帧门控与 pong 空闲计时叠加，画面定格死锁
- 第 9 条｜switch_source 先本地提交无回包，标签显示队友实为本地
- 第 10 条｜硬断线只能靠 60s keepalive，观看端冻结约 60 秒
- 第 18 条｜run_server 裸 daemon 线程静默死亡，控制台假健康
- 第 19 条｜listen_host 失效恒绑 ::，日志谎报实际监听地址
- 第 20 条｜Windows SO_REUSEADDR 等于允许端口被劫持
- 第 25 条｜成员可订阅自己，无环路保护致镜像隧道双倍流量
- 第 26 条｜中继 MSG_FRAME 零校验，is_key 完全由成员自报
- 第 54 条｜一个卡住的观看端 sendall 阻塞写锁，能挂住整台 host
- 第 55 条｜roster 广播释放锁再发，旧快照覆盖新致源列表永久少一人
- 第 56 条｜成员掉线时不加锁取 clients，误拆的是观看者连接
- 第 57 条｜MAX_PAYLOAD 64MB 囤积 + rx_buf O(n²) 整份重拷
- 第 59 条｜tune_socket 把 KEEPINTVL/KEEPCNT 都设成 idle，半开检测≈关闭
- 第 62 条｜_external_running 一旦为真永不复位，frpc 成杀不掉的孤儿
- 第 63 条｜frp 启动只用 tasklist 探测不验活，token 写错秒退仍报已启动
- 第 66 条｜shutdown 的 join 预算小于它要等的关停，encoder 常来不及 close
- 第 89 条｜_perf_loop 循环体无异常保护，非数字配置静默杀性能线程
- 第 95 条｜recv_msg 消费 5 字节头后才抛错，残留负载致消息流错位
- 第 96 条｜共享会话偷偷改共享 socket 的 timeout 不还原，破坏阻塞基线
- 第 102 条｜阶段 0 止血：accept 循环遇 OSError 不再永久退出（含 A2 配置回退可写目录）
- 第 103 条｜连接数上限 max_clients + 单条消息总时限 msg_timeout_s 防 slowloris
- 第 105 条｜发送半截消息即断连 + last_frame 只取引用不 copy + 解码代数校验
- 第 107 条｜上行令牌桶限速，防单成员把上行 1:N 放大打满 host
- 第 112 条｜C1 ABR 从开环改闭环，降级目标按实测吞吐一步收敛
- 第 114 条｜预打包 Struct _MSG_HDR 协议头，替代多段临时 bytes 拼接
- 第 132 条｜中继整帧物化 2→1 + viewer poll 空闲退避
- 第 134 条｜共享端一键诊断包 + 公网自检结论上面板
- 第 135 条｜协议层单点：Act 常量表 + 共享接收泵 + proto 字段
