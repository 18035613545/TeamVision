# codec — 编解码（codec.py VideoEncoder·VideoDecoder / pipeline.FrameEncoder / 关键帧与 JPEG 回退）

新条目格式：`codec-序号｜一行现象｜关联测试文件路径`（序号从 001 起，无对应测试写 `—`）。
以下"示例"仅演示新格式；历史条目以旧编号原样保留在末节，不重编号。

示例（演示新格式）：
- `codec-001｜解码上下文切源复用 + flush，重建前先 close，不再每次 new VideoDecoder（原第 116 条 C6）｜tests/test_optimize_stage1.py`

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
