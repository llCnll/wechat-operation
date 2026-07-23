## ADDED Requirements

### Requirement: 会话所有权与浏览器生命周期

系统 SHALL 将受控 Chrome、CDP、认证材料和 service-owned target 的所有权限定在 `game_circle_session`。Supervisor SHALL 在 session Worker 启动前启动 Chrome；替换 session Worker 前 SHALL 先终止 Chrome，并在新 browser generation 就绪后启动新 Worker。target 所有权 SHALL 仅存在于当前 owner 内存中。

#### Scenario: session Worker 重启

- **WHEN** session Worker 因发布、人工重启或异常恢复而被替换
- **THEN** Supervisor SHALL 先终止受控 Chrome 并确认旧 generation 已退出
- **THEN** 新 Worker SHALL 仅在新 generation 中创建自己的 HOME、Frame 和登录等待
- **THEN** 新 Worker MUST NOT 枚举、识别、复用或关闭前任 owner 的 target

#### Scenario: 受控 Chrome 意外退出

- **WHEN** Supervisor 发现期望运行的受控 Chrome 已退出且游戏圈会话服务仍为 desired
- **THEN** Supervisor SHALL 将其作为会话 owner 更换，启动新 Chrome generation 后替换游戏圈会话服务
- **THEN** 新会话服务 SHALL 为当前操作游戏重新执行 Bootstrap；不得继续投影旧 generation 的 lease 为可用

#### Scenario: 会话服务重启请求

- **WHEN** 运营人员请求重启游戏圈会话服务
- **THEN** Supervisor SHALL 立即返回已提交状态，并在后台完成 Chrome generation 与 session owner 的替换
- **THEN** 控制客户端 MUST NOT 因该正常替换耗时而把已接受的请求报告为控制通道失败

#### Scenario: Chrome replacement 未在限定时间就绪

- **WHEN** Supervisor 在替换 session owner 时未能确认新的 Chrome generation
- **THEN** Supervisor SHALL 保持控制面和其他服务运行，并记录固定脱敏失败状态
- **THEN** 后续 reconcile SHALL 重试同一 replacement Chrome 的依赖恢复；除非该 Chrome 已退出，不得重复终止并重启它
- **THEN** 系统不得启动新的 session Worker 或退出 Supervisor 主循环

### Requirement: SessionBootstrap 建立主线

系统 SHALL 由 SessionBootstrap 在服务启动、人工检查登录态、人工建立当前游戏 Frame 和当前游戏切换时建立会话。来源调用 SHALL 只使用最小只读验证成功后发布的 ACTIVE lease，不得隐式创建恢复 Job、target 或游戏切换。

#### Scenario: 外层会话和当前游戏可用

- **WHEN** Chrome 可用、外层会话有效且存在当前操作游戏
- **THEN** Bootstrap SHALL 建立或复用当前 owner HOME 与 Frame，完成身份绑定和最小只读验证
- **THEN** 系统 SHALL 原子发布当前 scope ACTIVE lease，并分别投影外层会话和 Frame 为有效

#### Scenario: 切换游戏后的 HOME 回读

- **WHEN** Bootstrap 在同一 HOME 中切换当前游戏并安排页面 reload
- **THEN** 系统 SHALL 丢弃 reload 前 document 的执行上下文，并在同一 HOME 建立新上下文后回读 AppID 与 outer token
- **THEN** 系统不得因复用失效上下文而关闭候选 HOME、重试创建额外 HOME 或跳过目标 Frame 建立

#### Scenario: 没有当前操作游戏

- **WHEN** 外层会话有效但不存在 `ActiveGameScope`
- **THEN** 系统 SHALL 投影外层有效和 Frame 未就绪
- **THEN** 系统 MUST NOT 发布半成品 lease 或自行选择游戏

### Requirement: 人工登录页与脱敏边界

系统 SHALL 最多维护一个未到期的 `LOGIN_PENDING` 页面。仅运营人员明确请求时 MAY 显示或激活该页；重复请求 SHALL 复用页面且不得创建或聚焦第二页。系统不得持久化或输出 target ID、认证材料、完整 URL、二维码、账号身份或原始来源响应。

#### Scenario: 扫码完成

- **WHEN** 已登记登录页的外层会话变为有效
- **THEN** 系统 SHALL 复用该 HOME 建立当前游戏 Frame
- **THEN** 只有最小只读验证成功后才可发布 ACTIVE lease

#### Scenario: 登录等待结束

- **WHEN** 登录窗口到期、已登记 target 丢失或 Chrome generation 改变
- **THEN** 系统 SHALL 清除等待状态并仅关闭该 owner 的未发布 target
- **THEN** 系统 MUST NOT 自动创建下一张登录页或接管未知 tab

### Requirement: 来源调用与 lease 连续性

来源 IPC SHALL 固定 ACTIVE lease 后执行受权的来源读取或已确认人工写入。来源 IPC MUST NOT 创建、reload、绑定或关闭 target。新 lease SHALL 在完整验证后原子替换旧 lease；旧 lease 仅在退役延迟到达且访问计数为零后关闭。

#### Scenario: 来源会话未就绪

- **WHEN** 当前 scope 不存在 ACTIVE lease
- **THEN** 来源读取 SHALL 返回固定脱敏未就绪结果
- **THEN** 已确认人工写入 SHALL 保持待确认，恢复后 MUST NOT 自动重放

### Requirement: 可选被动修复

系统 SHALL 提供默认关闭的 SessionRepair。启用时，它 SHALL 在服务内部只检查当前 owner HOME 和 Frame，并将同一 AppID 的失效合并为一次有界 Bootstrap。它不得使用 Scheduler 或 BackgroundJob，不得自动打开登录页。

#### Scenario: Repair 被关闭

- **WHEN** service-owned Frame 失效且 Repair 未启用
- **THEN** 系统 SHALL 投影 Frame 未就绪
- **THEN** 系统 MUST NOT 创建 target 或入队会话维护 Job

#### Scenario: Repair 发现当前 Frame 丢失

- **WHEN** Repair 已启用且当前 scope ACTIVE lease 的 HOME 或 Frame target 不再存在
- **THEN** 系统 SHALL 建立并验证候选 lease，成功后才原子替换损坏 lease
- **THEN** 系统 MUST NOT 因损坏 lease 仍为 ACTIVE 而跳过 Frame 建立，亦不得在重复检查中积累未发布 HOME target
- **THEN** 浏览器已移除的当前 owner target SHALL 在退役回收时从登记中清除

#### Scenario: Repair 候选失败且旧 lease 已损坏

- **WHEN** Repair 已确认旧 lease 的 HOME 或 Frame target 丢失，且候选建立或验证失败
- **THEN** 系统 SHALL 投影当前 Frame 未就绪并提供人工建立动作
- **THEN** 系统 MUST NOT 因该旧 lease 仍在内存中而投影 Frame 有效

### Requirement: 可选主动保活

系统 SHALL 提供默认关闭的 SessionKeepalive。启用时 Scheduler SHALL 为每个四小时桶创建或复用带 `trigger=schedule` 和 `automation_key=session_keepalive` 的 Job。入队 Job MUST NOT 保存 execution scope 或认证材料。

#### Scenario: 首次执行冻结 scope

- **WHEN** 消费者首次认领自动保活 Job
- **THEN** 消费者 SHALL 在任何浏览器操作前原子读取当前 `ActiveGameScope` 并写入 checkpoint
- **THEN** 重试和中断恢复 SHALL 使用该 checkpoint scope，idempotency key 不得包含入队时 AppID 或 scope version

#### Scenario: 没有 current scope

- **WHEN** 首次执行时不存在 `ActiveGameScope`
- **THEN** Job SHALL 以 `NO_ACTIVE_GAME_SCOPE` 终态结束
- **THEN** 系统 MUST NOT 调用 CDP、创建 target、安排恢复或重试该 Job

#### Scenario: scope 冻结后切换游戏

- **WHEN** 保活 Job 已冻结 scope，运营人员随后切换当前操作游戏
- **THEN** Job SHALL 完整执行并发布冻结 scope 的 lease，不得被改写或取消
- **THEN** 系统 SHALL 为新当前 scope 安排独立 Bootstrap，且页面不得把旧 scope 结果投影为当前 Frame 有效

#### Scenario: 禁用自动保活

- **WHEN** 运营人员关闭 SessionKeepalive
- **THEN** Scheduler SHALL 停止接受新的周期 key
- **THEN** 已入队或运行中的 Job SHALL 按既有队列语义完成，手动保活 SHALL 保持可用

#### Scenario: 保活候选失败

- **WHEN** 保活候选在中转、回切目标、Frame 稳定检查、身份绑定或最小读取任一步失败
- **THEN** 系统 SHALL 关闭未发布的候选 HOME 和 Frame，且不得保留中转游戏页面作为可复用 target
- **THEN** Job MUST NOT 标记成功；瞬态失败 SHALL 使用有限重试，需人工扫码或目标不可选 SHALL 以固定终态结束
- **THEN** 若旧 ACTIVE lease 仍适用于当前 scope，系统 SHALL 保留其可用性和 Frame ready 投影

### Requirement: Bootstrap 结果与退役 lease 回收

`SessionBootstrap` SHALL 返回不含认证材料或 target 标识的结果，明确表示是否已发布 ACTIVE lease。只有发布结果可以使 Keepalive Job 成功。会话服务 SHALL 在其维护循环中回收退役延迟届满且访问数为零的当前 owner lease；该回收不得依赖后续来源请求。

#### Scenario: 成功保活后无来源请求

- **WHEN** 新 lease 已发布、旧 lease 已超过退役延迟且没有活动访问
- **THEN** 会话服务 SHALL 在维护循环中关闭旧 lease 的 HOME 与 Frame
- **THEN** 不得等待新的来源调用才执行回收

### Requirement: 控制操作串行化与静默自动化

会话服务 SHALL 通过单一 control lane 串行化 Bootstrap、登录等待检查、Repair、Keepalive 和人工控制的 target 生命周期操作。自动 target SHALL 请求后台创建能力；后台创建失败 MUST NOT 退化为前台创建。自动状态变化 MUST NOT 聚焦 target、弹出对话框、发送通知、跳转页面或写入全局页面消息。

#### Scenario: 控制操作与来源调用并发

- **WHEN** 控制操作和来源 IPC 并发到达
- **THEN** 同一时刻 SHALL 只有一个控制操作改变或检查 target 生命周期
- **THEN** 来源请求 SHALL 使用已固定 lease 或返回脱敏未就绪结果，访问计数在成功、异常、超时和取消后均须释放

### Requirement: 废弃会话 Job 的发布兼容

移除旧 Probe 与 Refresh consumer 的发布 SHALL 提供幂等发布工具。该工具仅在旧会话 Worker 已退出且任务锁释放后，将活动旧 Job 标记为固定发布终态；它不得物理删除 Job、参数、checkpoint 或审计记录。兼容识别 SHALL 保留本发布及后续一个发布版本。

#### Scenario: 发布时存在旧会话 Job

- **WHEN** 旧 Probe 或 Refresh Job 仍处于活动队列
- **THEN** 发布工具 SHALL 保留其审计数据并使新 Worker 不再认领
- **THEN** 重复执行发布工具 MUST NOT 重复改写已终态的记录

### Requirement: 已废弃任务隔离与当前控制台动作

旧 `GAME_CIRCLE_LOGIN_PROBE` 与 `GAME_CIRCLE_SESSION_REFRESH` SHALL 仅在发布终态化工具的兼容识别中出现。运行时任务类型表、Scheduler、Worker consumer、Web 路由、工作台文案和普通测试 SHALL NOT 生产、消费或展示这两个任务为可执行能力。

系统配置台 SHALL 将当前游戏操作映射到新版固定能力：切换游戏后请求建立该游戏 Frame；检查外层会话不建立 Frame；手动保活只创建 `GAME_CIRCLE_SESSION_KEEPALIVE`。目录同步 SHALL 以经 CSRF 保护的 POST 通过游戏圈会话 IPC 同步本地可管理游戏目录；失败 SHALL 保留既有目录并显示固定失败结果。

当前操作游戏节点 SHALL 只表达并修改全局业务范围：已选择游戏、选择器、切换并建立 Frame，以及用于刷新选择器选项的目录同步。会话检查、手动保活与自动维护配置 SHALL 分别归属受控 Chrome 会话或游戏圈会话服务节点；系统 MUST NOT 删除这些后端能力，仅移除当前操作游戏节点中的重复入口。范围版本与页面诊断检查时间不得作为该节点的日常主信息。

#### Scenario: 部分重启不会创建已废弃任务

- **WHEN** Web、Scheduler 或 session Worker 已加载新版代码
- **THEN** 普通控制和调度路径不得创建 `GAME_CIRCLE_LOGIN_PROBE` 或 `GAME_CIRCLE_SESSION_REFRESH`
- **THEN** 仅发布终态化工具可以匹配这些历史记录

#### Scenario: 运营人员同步授权游戏目录

- **WHEN** 已授权运营人员提交控制台目录同步操作且会话 Frame 可用
- **THEN** Web SHALL 请求固定 `LIST_MANAGED_GAMES` IPC 意图，并在同一请求内更新本地目录
- **THEN** 响应 SHALL 报告同步数量，且不得要求第二次 Admin 操作
