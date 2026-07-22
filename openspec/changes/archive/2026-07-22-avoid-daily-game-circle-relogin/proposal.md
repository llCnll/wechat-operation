## Why

游戏圈来源调用依赖外层公众平台会话、外层 HOME 的运行时 token 和最终插件 iframe 的来源身份 token。旧实现把页面发现、身份绑定和来源调用分散在多个进程，且会在候选 tab 并存时误用上下文，导致会话失效后需要人工干预。

受控调研已确认：当前游戏可以在明确 HOME 的外层页面通过绝对 `gamemp` `getappinfo` 回读。`RECOVER` 当前不等于目标时仅可在目录唯一包含目标且平台许可时直接切换一次；`KEEPALIVE` 当前已为目标时必须经受控中转执行 `A → B → A`。每次切换后均必须在同一 HOME 重载并重新读取 token 和当前 AppID。刷新不创建临时插件探测；一次刷新只建立一组新的 HOME + 最终 PLUGIN，并在绑定与最小只读验证成功后原子发布。

## What Changes

- 引入当前操作游戏和不可变 `execution_scope`，确保每个 Job 只在明确 AppID 范围内运行。
- 将 CDP、outer token、来源身份 token 和 target 生命周期收敛到唯一的 `game_circle_session` 服务；其他进程仅通过固定本地 IPC 调用。
- 使用 `appid -> [SessionLease]` 管理内存会话版本；新 lease 发布后让正在使用的旧 lease 自然退役，再回收其 owned target。
- 区分两种受控刷新语义：`RECOVER` 仅将外层游戏直接对齐执行范围；`KEEPALIVE` 在已处于目标游戏时，从平台许可的非目标目录游戏中随机选择中转并执行 `A → B → A`，再重建最终插件会话。两者均只在最终 HOME/PLUGIN 完成绑定与最小只读验证后发布 lease。
- 明确刷新失败边界：扫码/无 token、目录或切换不符合契约、Chrome 不可用、插件/协议异常互不混淆；人工写入绝不自动重放。
- 由 Supervisor 管理受控 Chrome 和会话服务；Web、Scheduler 与普通 Worker 不直接触碰 CDP。

## Non-Goals

- 自动扫码、保存或导出 Cookie、外层 token 或来源身份 token。
- 读取切换面板、以 DOM/展示字段/列表顺序推断当前游戏，或执行未受配置与目录许可约束的多步切换。
- 改变人工处置授权；未知写入结果只能待回读确认，不能自动重试。

## Capabilities

| 类型 | 能力 | 说明 |
| --- | --- | --- |
| 新增 | `game-circle-login-continuity` | 执行范围、唯一会话服务、SessionLease、SessionAccess、刷新 Job 和 target 生命周期。 |
| 修改 | `service-foundation` | 明确 target 的 CDP 适配器、外层当前游戏回读契约和认证信息边界。 |
| 修改 | `managed-game-catalog` | 目录与调用期当前游戏校验的边界。 |
| 修改 | `original-post-ingestion` | 来源读取和已确认操作经会话服务执行。 |
| 修改 | `game-circle-metric-snapshots` | 指标读取复用同一会话与身份 lease。 |
| 修改 | `local-service-supervisor` | 受控 Chrome 与会话服务的固定生命周期。 |

## Impact

- 影响 CDP 基础设施、配置台、Job、Scheduler、来源客户端、Worker、Supervisor 和本地 IPC。
- 所有认证材料及账号标识均不得进入数据库、日志、Job 参数、checkpoint、运行态或页面。
- 高风险最小验证仅允许在新 candidate 上完成绑定和单页只读查询；不得执行上游写入。若需验证切换，仅能在目标与中转游戏均获授权、目录与许可均通过时，按刷新模式执行受控切换并逐步回读。
