# codec — 编解码（codec.py VideoEncoder·VideoDecoder / pipeline.FrameEncoder / 关键帧与 JPEG 回退）

新条目格式：`codec-序号｜一行现象｜关联测试文件路径`（序号从 001 起，无对应测试写 `—`）。
以下"示例"仅演示新格式；历史条目以旧编号原样保留在末节，不重编号。

示例（演示新格式）：
- `codec-001｜解码上下文切源复用 + flush，重建前先 close，不再每次 new VideoDecoder（原第 116 条 C6）｜tests/test_optimize_stage1.py`

codec-002｜set_fps 改「锁外构造、锁内切换」，失败保留旧编码器（第 112 条口径补齐）｜—
codec-003｜一次 encode 多包合并输出（不再只取 pkts[-1]），空包跳过｜—
codec-004｜静止期强制 IDR 校验 is_key（未生效则不发且保留请求）；「校验缓存帧尺寸」那半边两侧均未落地，尺寸不一致由 codec.py 的自动重建吸收（第 6 轮 codec-013 实测更正）｜—
codec-005｜解码器释放与编码侧同口径（PyAV 17 无 close 时 flush + 解引用）｜—
codec-006｜FrameEncoder.encode(bitrate=) 对已存在编码器生效（带 ±15% 迟滞）｜—
codec-007｜候选编码器不可用原因按名字记一次（INFO 可见）｜—
codec-008｜_send_key_response 返回是否真送出，失败不再消费 key_evt（第 3 条口径恢复）｜—
codec-009｜stop() join 超时保留 _thread 引用，幂等 start() 不再可能双线程共用编码器｜tests/test_item97_stop_race.py（cfg-009 更正：原记「—」系反向漏记，:61-142 为行为 + AST + 旧逻辑负向对照三段式）
codec-010｜rate_control 是从不传入的死参数：三处 CBR/VBR 口径统一为 VBR（峰值 1.5×码率），按 VBR 估上行占用｜—
codec-011｜静止期 force_idr 返回 (None,None) 且缓存帧在手时改走 JPEG 回退出帧（新观众不再永等 IDR）｜—
codec-012｜set_fps 失败回滚 self._fps 并返回是否生效，pipeline 按返回值落账（注释与实现不再相反）｜—
codec-013｜host 侧静止期强制 IDR 补 is_key 门禁、回了 P 帧时保留 force_key 不消费（codec-004 此前只改了 share 侧）｜—
codec-014｜客户端 REQ_KEYFRAME 走带 1 秒冷却的去重（本地 _request_local_keyframe / peer 复用 _request_peer_keyframe），帧帧 IDR 的廉价放大堵死｜—

codec-015｜host 侧静止期关键帧请求补 JPEG 出口（编码器不可用时用缓存帧出图，新观众不再永久黑屏）｜tests/test_bugfix_20260924_round7.py
codec-016｜preset 按编码器族校验（x264 风格 preset 不再原样喂给 NVENC → 不再静默退软件编码）｜tests/test_bugfix_20260924_round7.py
codec-017｜set_bitrate 加「重建中」护栏：一次调整只构造一套上下文（保留第 112 条锁外构造）｜tests/test_bugfix_20260924_round7.py
codec-018｜share 侧关键帧兜底只走 JPEG（不再可能送 P 帧并谎报成功、也不消费请求）｜tests/test_bugfix_20260924_round7.py
codec-019｜PyAV 缺失时记一条一次性告警（不再零日志永久跑 JPEG）｜tests/test_bugfix_20260924_round7.py

## 历史（旧编号，冻结）

> 逐条来自 `docs/audit/README.md` 冻结对照表，归属为 codec 的条目。一行一条 `第 N 条｜简述`。

- 第 1 条｜切源后解码器不重建（HEVC→H.264）致永久黑屏
- 第 3 条｜共享侧 key_wanted 提前清空，关键帧请求被吃掉
- 第 4 条｜_encode_send 无 try，上传线程静默死亡且永不重启
- 第 6 条｜encode 异常被就地吞、available 恒真致永久冻结零日志
- 第 7 条｜只解最新帧打断 H.264 参考链，周期性花屏拖影
- 第 11 条｜缺 PyAV 时解码恒空仍置 video_mode，永远"连接中…"
- 第 47 条｜每次关键帧请求整个重建硬件编码器会话，码率自激振荡
- 第 48 条｜码率控制硬编码 CBR，全仓无 vbr 配置，静止画面仍白烧码率
- 第 49 条｜回退 JPEG 后 congested 恒真，画质一路压到地板不回升
- 第 50 条｜libx265 回退路径 4 帧流水线延迟，恒定 +133ms 端到端
- 第 51 条｜_close_ctx 调用 PyAV 17 不存在的 close()，关闭是死代码
- 第 52 条｜set_bitrate 会把已关闭编码器复活，关停竞态新开会话可 abort
- 第 53 条｜编码器打不开无负缓存，每帧新建打满 CPU 并刷爆日志
- 第 90 条｜_encode_loop 静止期强制关键帧分支在 try 外，编码器半死即杀线程
- 第 110 条｜抽出 pipeline.FrameEncoder，host 与 share 共用编码器生命周期与 JPEG 回退
- 第 113 条｜流水线唤醒事件 raw_evt：采集写槽后置位，编码事件驱动消费
- 第 116 条｜C6 解码上下文切源复用 + flush，重建前先 close，不再每次 new VideoDecoder
