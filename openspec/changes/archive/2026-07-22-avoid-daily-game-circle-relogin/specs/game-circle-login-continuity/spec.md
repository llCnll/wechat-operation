## ADDED Requirements

### Requirement: 当前操作游戏与不可变执行范围

系统 SHALL 持久化唯一 `ActiveGameScope(active_appid, scope_version, updated_at, updated_by)`。`active_appid` MUST 是本地 `ManagedGame`；系统 MUST NOT 创建第二套游戏业务实体或持久化外部切换展示字段。目录首次同步后，配置为空时系统 MAY 以部署配置中的默认游戏初始化；业务 AppID MUST NOT 写死在迁移、Scheduler、Worker 或领域服务中。

任何访问游戏圈的 Job 在入队时 MUST NOT 固化当前游戏。首次准备访问 CDP 前，系统 SHALL 在事务中读取当前 scope 并写入 checkpoint `execution_scope={target_appid,scope_version}`；已有合法 scope 时 MUST 直接复用。后续 retry、分页、刷新触发和来源调用 MUST 只使用该快照。

人工补采、状态、互动、作者处置和已确认受控操作 SHALL 仅对当前操作游戏创建 Job。兼容入口收到 AppID 时 MUST 拒绝与当前 scope 不一致的值。

#### Scenario: 未开始 Job 读取新配置
- **WHEN** Job 尚未写入 `execution_scope`，运营人员切换当前操作游戏
- **THEN** Job 首次 CDP 执行 SHALL 写入新的 `target_appid` 和 `scope_version`

#### Scenario: 已开始 Job 保持范围
- **WHEN** Job 已写入 `execution_scope` 后运营人员切换当前操作游戏
- **THEN** Job SHALL 继续使用原 scope
- **THEN** 系统 SHALL 为新 scope 创建或复用合格刷新 Job

### Requirement: 唯一会话服务与按 AppID 的 SessionLease

`game_circle_session` SHALL 是唯一的 `GameCircleSessionService` 宿主。只有该服务可以创建、扫描、接管或关闭系统 CDP target，读取 outer token，调用 `binduseridentity`，持有 `source_token`，以及在 `gamemp` iframe 内执行来源请求。Web、Scheduler 和其他 Worker MUST NOT 创建 CDP client、frame、协调器实例或来源身份 token；它们仅可通过固定、类型化、仅本机可达的 IPC 契约提交非认证来源请求或已确认操作。

服务 SHALL 在自身内存维护 `appid -> [SessionLease]`。每个 `SessionLease` SHALL 将 `appid`、browser generation、HOME/PLUGIN target ID、PLUGIN generation、`source_token`、签发时间、`ACTIVE|RETIRING` 状态、最早退役时间和 SessionAccess 集合作为同一生命周期单元。每个 AppID SHALL 恰有一个 ACTIVE lease，并 MAY 有零到多个 RETIRING lease。token MUST NOT 被持久化、序列化到 Job、写入日志、运行态或返回给调用方。

候选会话 SHALL 先创建、验证、绑定和最小只读验证；只有全部成功后，服务才可原子发布新的 ACTIVE lease，并将旧 ACTIVE lease 标记为 RETIRING。未知 tab 即使同域也 MUST NOT 被选择、接管或关闭。candidate 失败时 SHALL 只清理 candidate；受控 Chrome 已由 Supervisor 管理时，系统 target 可以回收至零 tab。

服务 SHALL 为每个自行创建的 target 记录非认证性的 owned 标识：target ID、service owner epoch、角色和关联 lease/probe 标识。它 MUST NOT 通过域名、标题、URL、页面文字或列表顺序推断 target 所有权。仅 owned target 可被服务关闭：candidate 验证失败时关闭本次 candidate；`LEASE_HOME`/`LEASE_PLUGIN` 仅在关联 RETIRING lease 满足回收条件时关闭；`PROBE_CANDIDATE` 在探测结束后关闭；`LOGIN_PAGE` 在扫码期间 MUST NOT 关闭，只可在后续已验证会话明确替代后回收。手动打开的未知 tab 永远不得关闭或作为来源 target。

#### Scenario: 多个同域 tab
- **WHEN** Chrome 同时存在多个公众平台或插件页
- **THEN** 协调器 SHALL 只使用登记的 target ID
- **THEN** 不得按 `/json/list` 顺序、域名、标题或页面文字选择目标

#### Scenario: 手动 tab 与 owned target 并存
- **WHEN** 用户手动打开、复制或保留与 HOME/PLUGIN 同域的 tab
- **THEN** 会话服务 SHALL 忽略该未知 target，且不得接管、关闭或用它替代 owned target
- **WHEN** 用户手动关闭已登记的 owned target
- **THEN** 服务 SHALL 将其分类为 target 丢失并走 RECOVER，不得寻找相似手动 tab 代替

### Requirement: SessionAccess、退役回收与来源身份

每次来源请求 SHALL 由会话服务原子选择 ACTIVE lease 并创建唯一 `SessionAccess`。Access MUST 记录在 lease 的内存集合中，并在成功、异常、取消或超时时释放；展示计数只能由 Access 集合派生。超时请求不得继续执行 CDP 调用。服务停止时 MUST 拒绝新请求并有界取消或等待旧请求；服务进程重启时内存 token 与 Access 全部失效，旧 lease 不得复用。

RETIRING lease MUST NOT 被新请求取得。已持有该 lease 的请求 MAY 完成；它若收到 `errcode=-2` MUST NOT 在退役 PLUGIN 中重新绑定。只有当 RETIRING lease 已超过最早退役时间且 Access 集合为空时，服务才可关闭它拥有的 HOME/PLUGIN target。连续刷新时 SHALL 保留仍有 Access 的所有退役 lease，不得以固定列表长度提前关闭。

同一 ACTIVE lease 的普通请求 SHALL 复用该 lease 的 `source_token`，不得为每次请求重复绑定。服务 SHALL 对同一 lease 实施细粒度单飞绑定。来源查询返回已验证 `errcode=-2` 时，服务 MAY 仅在关联 HOME 仍可验证 outer token 的 ACTIVE lease 上重绑一次；否则 SHALL 发起 RECOVER。`errcode=1` 与未验证业务码 MUST NOT 被误判为 lease 失效或扫码。

#### Scenario: 重建插件后的身份绑定
- **WHEN** 刷新 Job 以新的外层 token 创建并验证 PLUGIN
- **THEN** 服务 MUST 原子发布新的 ACTIVE lease，并主动绑定 target AppID
- **THEN** 即使旧来源身份 token 可能短时有效，也不得跳过该绑定

### Requirement: 外层会话与当前游戏 Frame 状态投影

顶部状态 SHALL 分为独立的外层会话和 Frame 状态。外层“会话有效”仅表示持久化 `GameCircleLoginRuntimeState` 为 `READY`，不得暗示插件、当前游戏或来源身份可用。当前游戏“Frame 有效”仅表示 `ActiveGameScope` 对应 AppID 在本地脱敏 session projection 中存在 `ACTIVE` lease，且 `game_circle_session` 心跳在一分钟内。Web MUST 只读取本地运行态、心跳和投影，不得调用 CDP、读取 token、调用 Supervisor 控制接口或创建会话。

人工“检查外层会话” SHALL 仅创建 `GAME_CIRCLE_LOGIN_PROBE`，不创建 Frame。当前游戏存在、会话服务在线且 Frame 状态为 `NOT_READY` 时，Frame 状态胶囊 SHALL 为已认证用户提供固定、CSRF 校验的“打开当前游戏 Frame”操作；该操作 MUST 仅创建或复用当前 scope 的 `RECOVER` Job，且页面必须说明其为异步恢复任务。无当前游戏、服务未运行或 Frame 已有效时，该操作 MUST 不可用。系统配置运行节点 2“当前操作游戏” SHALL 集中提供切换游戏、保活当前游戏 Frame、外层会话检查与目录同步；保活操作 MUST 创建或复用当前 scope 的 `KEEPALIVE` Job，且不得改写当前操作游戏。

会话服务启动时 SHALL 立即写入本 owner 的空 lease 投影；停止接收请求时 SHALL 清空 lease 投影。因此旧 owner 或停止服务遗留的投影不得被显示为可用 Frame。

#### Scenario: 外层会话有效但来源 Frame 未就绪
- **WHEN** 外层登录探测为 `READY`，但当前操作游戏没有 ACTIVE lease
- **THEN** 页面 SHALL 显示“会话有效”和“Frame 未就绪”两个独立状态

#### Scenario: 会话服务重启
- **WHEN** 新的会话服务 owner 启动且尚未成功发布 lease
- **THEN** session projection SHALL 为空
- **THEN** 页面不得显示“Frame 有效”

#### Scenario: 人工打开当前游戏 Frame
- **WHEN** 当前游戏已配置、会话服务在线且 Frame 为 `NOT_READY`
- **THEN** 运营人员点击 Frame 未就绪状态
- **THEN** Web SHALL 创建或复用当前 scope 的 `RECOVER` Job，且不得直接调用 CDP

#### Scenario: 人工保活当前游戏 Frame
- **WHEN** 运营人员在系统配置运行节点 2请求保活当前游戏 Frame
- **THEN** Web SHALL 创建或复用当前 scope 的 `KEEPALIVE` Job
- **THEN** 当前操作游戏不得改变

### Requirement: 刷新 Job 和失败边界

系统 SHALL 定义 `GAME_CIRCLE_SESSION_REFRESH`，仅由独立 `game_circle_session` Worker 消费。每个刷新 Job SHALL 以非认证参数保存 `refresh_mode=RECOVER|KEEPALIVE`：Scheduler 与人工保活使用 `KEEPALIVE`，切换当前操作游戏和来源失效使用 `RECOVER`；来源失效须复制来源 Job 已解析的 execution scope。完成或失败 Job MUST NOT 复用。

同 scope 的 `RECOVER` 请求 SHALL 仅复用活动 `RECOVER` Job；同 scope 的 `KEEPALIVE` 请求 SHALL 仅复用活动 `KEEPALIVE` Job。另一模式已活动时，系统 SHALL 保留新 Job 为待执行；会话服务在同 AppID 上顺序构建候选，避免两个模式相互改变预期的切换语义。

每个 AppID 同时至多一个刷新候选可被构建。刷新成功时，服务 SHALL 为目标 AppID 发布一个经外层会话、目标游戏、身份绑定和最小只读来源验证的 ACTIVE lease；该 lease SHALL 恰有一对新建的 HOME 与 PLUGIN target。候选未完整验证时 MUST NOT 改变现有 ACTIVE lease。

新 HOME 显示扫码或无法读取 token SHALL 以脱敏“需要人工扫码”直接失败。`RECOVER` 当前游戏与 execution scope 不一致时，以及 `KEEPALIVE` 当前不在目标时，服务 SHALL 仅在目录唯一含 target 且平台许可时直接切换到目标一次。`KEEPALIVE` 当前已在目标时，服务 MUST 执行 `A → B → A`：B 从非目标、平台许可的目录项中均匀随机选择；候选集合必须先去重和稳定排序，不得依赖上游列表顺序。中转候选为空、目录/许可不满足或任一步切换后回读不一致时必须安全失败，且 MUST NOT 降级为 `RECOVER`。Chrome browser generation 变化后，旧登记 target 和 lease MUST NOT 再被使用；新 HOME 仍具备可用外层会话时，服务 SHALL 尝试恢复而不得直接要求扫码。其他所有错误（包括当前项契约未确认）SHALL 使用既有有限 retry，不得猜测或新增持久等待状态。

一个刷新 Job SHALL 仅使用既有有限 retry 机制。该 Job 用尽 retry 后的失败 MUST NOT 禁止、暂停或改变后续 Scheduler 时间槽创建的独立刷新 Job。服务 SHALL 合并同一 AppID 的并发刷新请求，避免刷新风暴和 lease 无界增长。

刷新 Job SHALL 保留 `RECOVER` 与 `KEEPALIVE` 的不同会话构建语义：两者均只创建新的 HOME 和最终 PLUGIN；`RECOVER` 在当前等于目标时不切换，否则只直接切到目标，`KEEPALIVE` 在当前等于目标时必须经受控中转执行 `A → B → A`，当前不等于目标时只直接切到目标。不得创建临时回读插件，不得按目录顺序选择中转，也不得在中转失败后降级。

#### Scenario: 来源检测插件失效
- **WHEN** iframe/登记 target 丢失、出现登录超时提示，或绑定在重新绑定后仍不能建立可用身份
- **THEN** 会话服务 SHALL 创建或复用刷新 Job；来源 Worker 保留 checkpoint 并在刷新 Job 活动时延后
- **THEN** 网络、限流、5xx、权限、契约和普通业务错误不得触发刷新

#### Scenario: RECOVER 当前游戏与任务范围一致
- **WHEN** refresh mode 为 `RECOVER`，且外层当前项等于 Job target
- **THEN** 服务 SHALL 不执行游戏切换，直接以当前 HOME token 创建最终 PLUGIN 并重新绑定

#### Scenario: KEEPALIVE 当前游戏与任务范围一致
- **WHEN** refresh mode 为 `KEEPALIVE`，且外层当前项等于 Job target A
- **THEN** 服务 SHALL 解析受控的非目标中转 B，严格执行 `A → B → A`
- **THEN** 两次切换后均须重载同一 HOME 并重新读取 token 与当前 AppID，最终为 A 才可创建 PLUGIN

#### Scenario: KEEPALIVE 没有可用中转
- **WHEN** `KEEPALIVE` 当前为目标，且目录中没有非目标、可切换的中转游戏
- **THEN** Job SHALL 以安全错误失败，且不得创建最终 PLUGIN 或发布新 lease
- **THEN** 服务 MUST NOT 退化为不切换的 `RECOVER`

#### Scenario: 当前游戏与任务范围不一致
- **WHEN** 外层当前 AppID 不等于 Job target
- **AND** 外层目录唯一包含 Job target 且平台允许切换
- **THEN** 刷新 SHALL 发送一次直接切换请求，并在同一 HOME 重载后重新读取 token 与当前 AppID
- **THEN** 只有最终 AppID 等于 Job target 时才可创建最终 PLUGIN 并发布 lease

#### Scenario: 当前游戏与任务范围不一致且目标不可切换
- **WHEN** 外层目录缺少 Job target、target 不唯一或平台不允许切换
- **THEN** Job SHALL 以安全错误失败，且不得发布新 lease

#### Scenario: RECOVER 遇到活动 KEEPALIVE
- **WHEN** 同一 scope 的 `KEEPALIVE` Job 已为 `PENDING` 或 `RUNNING`，来源请求触发 `RECOVER`
- **THEN** 系统 SHALL 创建或保留独立待执行的 `RECOVER` Job
- **THEN** 会话服务 SHALL 在同 AppID 的 `KEEPALIVE` 完成候选构建后，按 `RECOVER` 语义执行该 Job

#### Scenario: KEEPALIVE 遇到活动 RECOVER
- **WHEN** 同一 scope 的 `RECOVER` Job 已为 `PENDING` 或 `RUNNING`，Scheduler 触发 `KEEPALIVE`
- **THEN** 系统 SHALL 创建或保留独立待执行的 `KEEPALIVE` Job
- **THEN** 它 SHALL 在 `RECOVER` 完成同 AppID 的刷新流程后执行主动保活

#### Scenario: Chrome 重启后外层会话仍有效
- **WHEN** 登记的 browser generation 与当前 CDP browser generation 不同
- **THEN** 协调器 SHALL 清除旧 target 和 lease，并从新 HOME 重新开始续接
- **THEN** 新 HOME 有 token 且当前 AppID 回读通过时，系统 SHALL 重建 PLUGIN 并重新绑定，而不得要求扫码

### Requirement: 时间槽幂等、调用身份与执行截止时间

Scheduler SHALL 将五分钟登录探测和六个北京时间 `KEEPALIVE` 槽以稳定的 `task_type + execution scope（如有）+ Asia/Shanghai 时间槽` 原子入队。同一个时间槽的 Job 即使已经完成或失败，也 MUST NOT 因 Scheduler 在该分钟内再次轮询而重复创建。`RECOVER` 和人工请求 SHALL 保持各自现有的活动 Job 复用语义，不得被时间槽去重吞掉。

固定 loopback IPC SHALL 要求 Supervisor 注入的独立 capability。服务端 MUST 在解析业务参数、构造 execution scope 或调用会话服务前以恒定时间校验 capability；失败请求 MUST 只返回固定未授权结果，且不得触达 CDP、来源读取或上游写入。capability MUST NOT 持久化、记录、展示或复用为 Supervisor 控制 token。

每个 `SessionAccess` SHALL 拥有单调截止时间、取消标记和受其管理的 frame 集合。每次来源或身份重绑 CDP 调用 MUST 使用剩余执行时限；超时或服务停止后，相关 frame MUST 关闭，后续 CDP 通信 MUST 终止，Access MUST 释放。过期 deadline 不得只作为运行态展示字段。

#### Scenario: 同一分钟的 Scheduler 重复轮询
- **WHEN** Scheduler 在同一个登录探测或 `KEEPALIVE` 时间槽内运行多次，且第一个 Job 已结束
- **THEN** 后续调用 SHALL 返回同一时间槽的既有 Job
- **THEN** 不得创建第二个探测或主动保活 Job

#### Scenario: 未授权本机连接
- **WHEN** loopback 连接缺少或提供错误 IPC capability
- **THEN** 服务 SHALL 在业务请求解析前拒绝该连接
- **THEN** 不得执行来源读取、刷新、登录页打开或上游写入

#### Scenario: Access 到期或服务停止
- **WHEN** 来源请求超过其 SessionAccess 剩余时限，或会话服务开始停止
- **THEN** 服务 SHALL 关闭该请求关联的 frame 并释放 Access
- **THEN** RETIRING lease 只可在 Access 归零后按既有规则回收

### Requirement: Target 丢失恢复与原子 RECOVER admission

已登记 HOME/PLUGIN target 或其游戏圈 iframe 丢失（包括 browser generation 变化后的旧 target 不存在）MUST 作为明确会话失效传递到会话服务。帖子与指标来源 adapter MUST NOT 将其降级为普通来源传输失败或未分类 IPC 失败；会话服务 SHALL 创建或复用对应 scope 的 `RECOVER`，人工写仍只返回待重新确认而不得重放。

只有在确认 `Target.closeTarget` 已成功或 target 已确认消失后，服务才可删除 owned target 登记。临时 CDP 清理失败时，登记和关联 RETIRING lease MUST 保留，以便后续安全重试；不得将保留的系统 target 伪装为未知 tab。浏览器连接不可建立才可标记 Chrome 不可用；已连接浏览器中的 target、iframe 或页面协议失败 SHALL 作为普通有限重试，且不得覆盖外层登录态。

同一 execution scope 的 `RECOVER` admission SHALL 通过稳定活动 key 原子创建。并发请求 MUST 得到同一个活动恢复 Job；已终态 Job MUST 让出活动 key 后创建新的 Job，且不得重新打开、重放或改写终态 Job。

CDP frame SHALL 要求调用方提供已登记 target ID，且 MUST NOT 保留按页面列表选择默认 target 的兼容分支。目录同步和登录态探测只能经会话服务的 owned candidate 执行；普通来源传输错误不得被帖子或指标 Worker 误报为会话恢复或扫码等待。只有固定 IPC `SESSION_RECOVERING` 结果可以无消耗地延后来源 Job，其余来源传输错误 SHALL 使用该 Worker 的既有有限 retry。

外层当前游戏回读和来源详情读取的契约失败 SHALL 投影为固定、脱敏的原因分类。允许的投影仅包括 CDP 返回结构无效、HTTP 状态异常、非 JSON、业务拒绝与载荷无效；不得保存或返回响应正文、最终 URL、Cookie、token、账号或上游原始错误文本。该分类用于 Job/审计排障，不得改变刷新、写入重放或扫码判定。

新建 HOME 在 URL 到达 `mp.weixin.qq.com` 后 SHALL 在有界时间内轮询已验证的 `getappinfo` 当前游戏契约；只有该契约成功返回非空 AppID 后，刷新才可继续创建 PLUGIN 或判断是否需要切换。候选 HOME 在超时或任一后续失败时仍 SHALL 回收；不得以 URL 到达即视为业务就绪。

#### Scenario: 来源请求发现旧 target 已丢失
- **WHEN** 帖子或指标请求使用的已登记 target 在 CDP 中不存在
- **THEN** 会话服务 SHALL 创建或复用 `RECOVER`
- **THEN** 调用方 SHALL 收到固定的会话恢复结果，而不是普通来源失败

#### Scenario: 受控 target 回收至零 tab
- **WHEN** Supervisor 管理的 Chrome 仅剩一个 owned candidate 或 RETIRING lease target
- **THEN** 服务 SHALL 在确认关闭后移除其 owned 登记，允许 Chrome 暂时零 tab
- **THEN** 未知手动 tab 仍不得被接管或关闭

#### Scenario: 会话服务重启清理旧 owner target
- **WHEN** 新会话服务 owner 启动，且上一份脱敏 projection 的 browser generation 与当前 Chrome 一致
- **THEN** 服务 SHALL 在创建新 HOME 前仅关闭其中登记的旧 HOME、PLUGIN 与 candidate target
- **THEN** `LOGIN_PAGE` 和任何未登记手动 tab 均不得关闭
- **WHEN** target 关闭暂时失败但仍存在
- **THEN** 服务 SHALL 保留其 orphan 登记，以便后续安全重试
