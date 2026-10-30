# 游戏圈会话服务设计

原文决策来源：commit c9299f168b7566f9d188017acc16e08f8ad6b43b，`openspec/changes/archive/2026-07-22-avoid-daily-game-circle-relogin/design.md`、`2026-07-23-improve-local-login-session-recovery/design.md`、`2026-07-26-align-observable-game-circle-session/design.md`。可用 `git show <commit>:<path>` 追溯。当前验收行为以 [config 模块规格](../../../openspec/specs/config/spec.md)为准。

## 关键取舍

### 来源身份只由单一会话服务持有

- 背景：上游来源身份与受控浏览器会话绑定，token 不持久化。
- 决策：由单一会话服务持有 CDP、来源身份和 service-owned target；其他进程只调用固定本机 IPC。
- 否决方案：让 Web、每类 Worker 各自管理浏览器页或共享认证 token。
- 约束：会话服务重启后须重新验证外层会话和建立 lease；不可识别所有权的人工页面不得接管或关闭。
- 重新评估：上游提供正式、可独立轮换且受支持的服务端 API 身份后，再评估是否需要浏览器会话。

### 候选 lease 验证成功后再原子替换

- 背景：立即销毁旧 lease 会中断已开始的请求；先发布未验证的新 lease 会让来源读取使用不完整会话。
- 决策：完整验证候选后发布新 lease，旧 lease 进入退役并等待访问结束后回收。
- 否决方案：原地覆盖 token 或刷新时立即关闭旧页面。
- 约束：新请求只取得 ACTIVE lease；旧请求只可完成已有访问，不能重新绑定。
- 重新评估：若上游将请求会话改为无状态并提供显式版本化凭证，可重新审视退役模型。

## 其他 L1 决策

| 决策 | 理由 | 来源 |
| --- | --- | --- |
| 不把登录页地址或 token 当作登录成功证据 | 必须有受控、可复验的外层会话响应 | 2026-07-25-detect-expired-game-circle-session |
| Web 不直接执行身份绑定 | 让认证上下文和 target 所有权留在唯一 owner | 2026-07-19-add-initialization-supervisor-pipeline |
| 使用唯一可观察会话页 | 人工核验应观察服务登记页，不能接管任意 tab | 2026-07-26-align-observable-game-circle-session |
| 登录等待是互斥控制状态，不阻塞服务主循环 | 扫码期间保留恢复能力且不并发建立候选 | 2026-07-27-fix-login-pending-entry-state |


### 控制通道串行化 target 生命周期操作

- 背景：外层浏览器页面是共享状态；并发切换游戏、reload、bind 和关闭页面会破坏其他会话操作。
- 决策：所有 target 生命周期动作进入单一控制串行通道；同一 AppID 的重复建立以 single-flight 合并；已固定 lease 的来源读取在独立访问计数下完成。
- 否决方案：让 Recover、Keepalive、登录探测和修复各自并发控制浏览器。
- 约束：控制动作需有界并支持取消；来源读取不得因等待控制通道而重新选择范围。
- 重新评估：只有上游允许隔离浏览器会话并行、且 owner/target 证明独立时，才评估按会话分片。
- 原文来源：c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-23-improve-local-login-session-recovery/design.md

### Keepalive 使用候选会话而非原地破坏现有 lease

- 背景：刷新身份会改变外层当前游戏并建立新来源身份；直接覆盖当前 lease 会使在途只读请求失效。
- 决策：使用候选 HOME/Frame 完成对齐、绑定和最小读取后原子发布；失败时保留旧 lease。
- 否决方案：原地改写 ACTIVE token、先关闭旧 target 再验证新 target，或无中转确认结果时降级为成功。
- 约束：旧 lease 仅在退役等待结束且访问归零后关闭；写操作失败不得自动重放。
- 重新评估：上游提供无状态可轮换令牌且确认原子替换语义后，重新审视双 lease 成本。
- 原文来源：c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-23-improve-local-login-session-recovery/design.md

### Chrome 与 session Worker 成对替换

- 背景：session Worker 内存持有 lease 和 target 所有权；替换 Chrome 后旧 Worker 的引用不可继续信任。
- 决策：owner 更换时先结束旧 session Worker 与其 Chrome generation，再以新 generation 启动新 owner。
- 否决方案：保留旧 Worker 连接新 Chrome，或让新 Worker 扫描并接管前任 target。
- 约束：generation 未确认时不得发布新 lease；替换失败保持 desired 并熔断/重试，不循环创建候选 Chrome。
- 重新评估：改为容器化浏览器或外部会话服务时，需提供等价的 owner epoch 和 lease 撤销证明。
- 原文来源：c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-23-improve-local-login-session-recovery/design.md
