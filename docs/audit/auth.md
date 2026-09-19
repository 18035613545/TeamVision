# auth — 认证与安全（accounts.json / 注册登录 / 凭据 / 令牌 / 安全默认迁移）

新条目格式：`auth-序号｜一行现象｜关联测试文件路径`（序号从 001 起，无对应测试写 `—`）。
以下"示例"仅演示新格式；历史条目以旧编号原样保留在末节，不重编号。

示例（演示新格式）：
- `auth-001｜反作弊误报收敛 + 默认安全加固（listen_host / auth 一次性迁移）（原第 131 条）｜tests/test_item131_hardening.py`

auth-002｜KDF 参数强校验（iter 区间/salt 定长/非空 nonce）+ proof 绑定 user 与域分隔串（协议 v2.3）｜tests/test_d1_challenge_auth.py
auth-003｜观看端拒绝明文口令降级路径（proto 被剥/旧服务端即拒绝并提示升级）｜tests/test_d1_challenge_auth.py
auth-004｜注册仍把验证子 dk 过网（pass-the-hash）：已评估需信道层（TLS/PAKE）根治，登记为待设计｜—
auth-005｜认证分档时限（首消息 30s/挑战 60s+宽限/probe 后 150s）+ 未认证连接配额（max_clients/4）｜—
auth-006｜注册凭据上下界（用户名 ≤64、salt 32 位 hex、dk 64 位 hex）｜—
auth-007｜注册策略非法值 fail-closed 归一为 closed（错别字不再静默开放注册）｜tests/test_item131_hardening.py
auth-008｜账号枚举面抹平：不存在/损坏账号同样下发伪盐挑战 + 统一「用户名或密码错误」+ 同代价假比较（服务端限速按项目裁定不做）｜tests/test_d1_challenge_auth.py
auth-009｜accounts.json 跨进程文件锁 + 锁内读-改-写 + 唯一临时名（双开实例不再抹账户/复活）｜tests/test_optimize_stage0.py
auth-010｜bootstrap 窗口持久「已消耗」标记（删唯一账户不再静默重开注册）+ 面板文案与后果对齐｜—
auth-011｜改密同步写入当前 iter（常量上调后不再永久登录失败）｜tests/test_b3_account_manage.py
auth-012｜诊断包脱敏改包含式键名匹配 + 用户名遮蔽 + 只收本进程日志（两端日志不再混包）｜tests/test_diag.py
auth-013｜mask_secret 短串自适应（9~24 位只留尾 2 位，不再近乎明文）｜—

## 历史（旧编号，冻结）

> 逐条来自 `docs/audit/README.md` 冻结对照表，归属为 auth 的条目。一行一条 `第 N 条｜简述`。

- 第 21 条｜准入关闭可自报任意用户名，任何人能伪造队友
- 第 22 条｜register 对任意端开放，PBKDF2 在全局锁内可被打满
- 第 23 条｜auth_timeout 是每 recv 超时，未认证 slowloris 可占线程囤 64MB
- 第 24 条｜frp token 明文进命令行与配置，卸载留凭据残渣
- 第 27 条｜accounts.json 损坏即全体账号蒸发，全程无日志
- 第 28 条｜viewer 靠中文字符串判断，注册重名抹掉有效凭据
- 第 29 条｜_anon_ok 一旦为真永不复位，只能重启 viewer 才能登录
- 第 30 条｜首连开准入的 host 必失败一次、弹两次框
- 第 31 条｜登录框等 120 秒而 host 只等 60 秒，输对了也登录失败
- 第 93 条｜凭据框 wait_window 可嵌套，超时后结果会串到下一次请求
- 第 104 条｜认证/控制消息 user/pass 字段补 isinstance str 类型校验
- 第 131 条｜反作弊误报收敛 + 默认安全加固 + onedir 打包
