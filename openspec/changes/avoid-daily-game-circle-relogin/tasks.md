## 1. 范围与基础设施

- [x] 1.1 增加 `ActiveGameScope` 与不可变 `execution_scope`，并在所有游戏圈 Job 首次执行时固化范围。
- [x] 1.2 删除 Worker/Web 直接使用 CDP、扫描 tab 或自行绑定身份的生产路径。
- [x] 1.3 将 `game_circle_session` 建为唯一 CDP 和认证材料持有者，并提供固定 loopback IPC。
- [x] 1.4 由 Supervisor 管理独立 profile 的受控 Chrome 与会话服务，注入独立 IPC capability。

## 2. 会话与 target 生命周期

- [x] 2.1 实现 owned target 登记和明确 target ID 的 HOME、PLUGIN、iframe 适配器；未知手动 tab 永不接管或关闭。
- [x] 2.2 实现内存 `appid -> [SessionLease]`、ACTIVE/RETIRING 状态和安全运行态投影；不持久化认证材料。
- [x] 2.3 实现 `SessionAccess` 的原子取得、截止时间、取消、finally 释放和退役 target 延迟回收。
- [x] 2.4 实现 ACTIVE lease 的单飞重绑，以及仅 `errcode=-2` 可触发的一次重绑/恢复分类。

## 3. 刷新路径

- [x] 3.1 受控验证外层 HOME 的绝对 gamemp `getappinfo` 契约，并固化为 adapter contract test。
- [x] 3.2 删除临时 PLUGIN probe；保留目录唯一性/许可校验后的受控游戏切换。
- [x] 3.3 实现刷新基础路径：新 HOME、扫码/token 预检、当前 AppID 回读、受控切换后的 token 回读、新最终 PLUGIN、稳定 iframe、绑定、最小只读验证、原子发布 lease。
- [x] 3.4 实现 `RECOVER` / `KEEPALIVE` 的有方向 Job 准入。
- [x] 3.5 分类处理扫码、目录/切换不满足、Chrome 不可用、target/协议失败；人工写操作不自动重放。

## 4. 调用方与可观测性

- [x] 4.1 将帖子、状态/互动、指标、作者回填和受确认人工操作迁移到会话服务 IPC。
- [x] 4.2 将登录态探测、固定登录页和 `/auth/status` 收敛到脱敏运行态；Web 只读取或提交固定 IPC 请求。
- [x] 4.3 完成 Scheduler 时隙幂等、会话服务 Worker 独占消费、中文 Job 显示名和受控 Chrome 生命周期。
- [x] 4.4 分离外层会话与当前游戏来源 Frame 的只读状态投影；页面不再将外层 READY 误示为 Frame 可用。
- [x] 4.5 修复 `STATUS_SYNC` 成功语义：来源读取未完成时保留脱敏统计检查点并走有限重试。
- [x] 4.6 将人工外层会话检查与 Frame 恢复分离：Probe 不建 Frame，顶部 Frame 未就绪可安全入队 `RECOVER`。
- [x] 4.7 增加当前游戏 Frame 保活入口，并在服务停止/新 owner 启动时按登记 target 与 browser generation 安全清理遗留会话 tab。

## 5. 文档与验证

- [x] 5.1 重写调研文档、proposal、design 和规格，使其描述最终生产路径与受控切换边界。
- [x] 5.2 覆盖当前游戏绝对回读、目录/许可限制的直接切换、扫码/token 预检、身份拒绝分类、lease 生命周期和刷新 Job 准入。
- [x] 5.3 执行受影响测试、全量测试、Django 检查、严格 OpenSpec 校验和 diff 空白检查。
- [x] 5.4 记录高风险最小验证：新 candidate 的绑定和单页只读查询；切换仅限获授权目标与中转，且不进行上游写入或扫码自动化。
- [x] 5.5 区分 `RECOVER` 与 `KEEPALIVE` 的刷新切换语义；为 `A → B → A` 中转增加目录/许可校验、无序随机选择与安全失败边界。
- [x] 5.6 覆盖两种刷新模式、受控中转候选及失败不发布 lease 的最小单元测试，并更新运维与调研文档。
