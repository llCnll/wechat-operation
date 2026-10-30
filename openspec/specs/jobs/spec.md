# Purpose

定义 jobs/ 的持久化任务、调度生产者、Worker 消费职责和发布协议边界。

## Requirements

### Requirement: 项目内部的定时任务入队
系统 SHALL 提供独立于 Web Server 的常驻 Scheduler Worker，并按已启用的受限生产者规则及固定中国标准时间创建帖子任务和指标任务。Scheduler Worker SHALL 不依赖外部 `cron`、`launchd`、消息队列、事件总线或其他系统定时器，且 SHALL 仅在短数据库事务内写入逻辑槽、后台任务及脱敏运行投影，不得建立 CDP 会话、调用 OCR/AI、认领任务或调用上游写操作。

默认帖子生产者 SHALL 每 15 分钟创建既有帖子任务。默认日指标规则 SHALL 在每日 `07:30` 后为五个已启用日指标来源分别创建锁定昨日的稳定任务；默认周指标规则 SHALL 在周一 `17:30`、周二 `10:00` 和周二 `16:00` 为运营效果创建锁定已完成周的稳定任务。日任务范围 SHALL 覆盖 `D-2` 至 `D0`，周任务范围 SHALL 覆盖最近三个已完成周。默认配置 SHALL 复现这些既有时刻与业务范围，但运营人员可在生产者定义允许的范围内暂停、恢复或调整规则。

#### Scenario: 时间桶内首次调度
- **WHEN** Scheduler Worker 运行并进入已启用的帖子生产者尚未处理的北京时间 15 分钟逻辑槽
- **THEN** 系统 SHALL 先幂等创建一个上限为 20 的 `STATUS_SYNC` 任务，再幂等创建一个回看最近 30 分钟、页大小为 100 的 `SCHEDULED_INGEST_WINDOW` 任务

#### Scenario: 重复检查或重复启动
- **WHEN** 一个或多个 Scheduler Worker 在同一已启用生产者逻辑槽内重复检查或重启
- **THEN** 系统 SHALL 通过生产者逻辑槽和既有稳定任务幂等键复用已有任务，且不得为同一任务类型创建重复任务

#### Scenario: Scheduler 职责隔离
- **WHEN** Scheduler Worker 创建或取得任务
- **THEN** 它 SHALL 仅写入后台任务表，且不得建立 CDP 会话、调用 OCR/AI、认领采集任务或调用任何上游写操作

#### Scenario: 指标调度桶重复检查
- **WHEN** Scheduler 在同一已启用指标规则的日或周逻辑槽内重复检查或重启
- **THEN** 系统 SHALL 复用每个来源对应的稳定任务，且不得创建重复任务

#### Scenario: 终态日任务的后续修订
- **WHEN** 某日稳定指标任务已终态失败或成功
- **THEN** 当日 Scheduler 重复检查不得新建同一调度任务
- **THEN** 次日的既定复核窗口和显式人工复核批次仍可读取并更新该业务日

#### Scenario: 生产者暂停
- **WHEN** 运营人员暂停帖子、指标、会话保活或禁言续期中的一个自动生产者或规则
- **THEN** Scheduler SHALL 不再为该配置未来逻辑槽调用自动入队
- **THEN** Scheduler SHALL 继续按其他已启用生产者规则创建其既有任务

### Requirement: 运营效果发布后复核桶
Scheduler SHALL 为同一已完成业务周使用默认周一 `17:30`、周二 `10:00` 和周二 `16:00` 三个独立的稳定发布规则；每个规则 SHALL 读取该周及最近两个已完成周。规则逻辑槽幂等键 MUST 包含目标业务周和发布阶段。规则使用允许的 `CATCH_UP_LIMITED` 策略时，晚启动 SHALL 仅在定义限制内补入已过而未入队的发布规则；同槽重复检查或重启 MUST NOT 创建重复任务。

#### Scenario: 周二晚启动补齐复核
- **WHEN** Scheduler 在周二 `16:00` 后首次运行且默认周指标规则允许补跑
- **THEN** 系统 SHALL 为该已完成周在定义上限内补入初始、上午复核和下午复核三个任务，并分别复用其稳定幂等键

#### Scenario: 周指标长期停机后恢复
- **WHEN** Scheduler 在多个周指标发布窗口之后恢复运行
- **THEN** 系统 SHALL 仅为当前目标业务周补入每个阶段最近一个允许槽
- **THEN** 系统不得为更早完成周自动批量创建历史周指标任务

#### Scenario: 重复检查已过复核桶
- **WHEN** Scheduler 在同一周二 `16:00` 后再次检查
- **THEN** 系统 SHALL 复用三个发布桶既有任务，且不得创建重复任务

### Requirement: Scheduler Worker 的运行边界
系统 SHALL 将 Scheduler、采集和审核作为三个任务类型隔离的常驻进程运行；SQLite 部署 SHALL 只运行一个 Scheduler Worker。一次性调度入口仅用于诊断和测试，不得作为生产环境的外部定时器依赖。

#### Scenario: Scheduler 中断恢复
- **WHEN** Scheduler Worker 停止后重新启动
- **THEN** 它 SHALL 在启动后检查当前生产者逻辑槽并按每条规则的错过触发策略继续调度，且同槽重复入队 SHALL 保持幂等

#### Scenario: 超过回看窗口的空洞
- **WHEN** 帖子生产者停止超过 30 分钟且其规则使用默认 `SKIP` 策略
- **THEN** 系统 SHALL 不自动扩大采集窗口，运营人员 MUST 在系统配置首页帖子采集生产者的立即执行弹窗创建受控历史补采窗口；直接管理命令仅用于诊断

### Requirement: 一次性作者状态回填任务的消费边界
系统 SHALL 将 `AUTHOR_MUTE_STATUS_BACKFILL_V2` 明确归类为一次性 Job，并仅允许既有人工补采 Worker 消费。入队入口 SHALL 只创建或复用 V2 固定幂等键的后台任务，不得建立 CDP 会话、认领任务或调用上游。V2 任务不得加入 Scheduler 或实时采集 Worker 的消费范围，且其失败重试上限 SHALL 为零。`AUTHOR_MUTE_STATUS_BACKFILL` 为不兼容的 V1 类型：系统不迁移、恢复或认领该类型。

#### Scenario: 重复入队
- **WHEN** 运营人员重复执行 V2 一次性回填入队命令
- **THEN** 系统 SHALL 返回同一个 V2 固定幂等键的后台任务，而不得创建并行的全量详情读取

#### Scenario: 显式恢复失败任务
- **WHEN** V2 一次性回填任务因非自动重试错误而处于失败或取消状态，且运营人员在修复环境后再次执行入队命令
- **THEN** 系统 SHALL 将同一 V2 任务重新置为待执行、保留其检查点并重置重试计数，而不得创建新任务或自动重试

#### Scenario: Worker 中断
- **WHEN** 人工补采 Worker 在 V2 回填任务执行期间中断
- **THEN** 系统 SHALL 复用通用 Worker 的中断恢复机制重新排队该 V2 任务，并从其最近保存的帖子游标继续

#### Scenario: 遗留 V1 任务
- **WHEN** 部署前检查发现非终态 `AUTHOR_MUTE_STATUS_BACKFILL` V1 任务
- **THEN** 部署 SHALL 停止，直至该任务达到终态
- **THEN** 新版人工补采 Worker 不得认领、恢复或以 V2 逻辑解释该 V1 任务

### Requirement: 评论导出任务的人工补采消费边界

系统 SHALL 将 `COMMENT_EXPORT_V1` 归类为手动一次性评论导出任务，并仅允许既有人工补采 Worker 消费。入队入口 SHALL 只在短数据库事务内创建或复用 Run 与 BackgroundJob，不得建立 CDP 会话、读取评论、写入 Result 或调用平台写接口。实时采集、指标、审核、会话和 Web 进程不得认领或执行该任务。

任务活动去重 SHALL 使用既有 `enqueue_active_coalesced_job`，并以创建者、冻结范围、帖子、作者和规则的规范 JSON SHA-256 摘要构造受长度控制的 `active_coalescing_key`；随机 `idempotency_key` 仅标识新 Job，不得承担去重。创建 Job、关联 Run 和写入最小参数摘要必须处于同一外层事务。恢复中断任务 SHALL 保留通用 Job 生命周期，但 handler 每次尝试必须删除全部旧结果并重置进度。`BackgroundJob.status=SUCCEEDED` 是结果唯一发布标记：handler 返回与通用 Worker 成功标记之间中断时，恢复后的下一次尝试仍须清空并重建，页面不得展示该 Job 的结果。该任务不属于 Scheduler、自动生产者或任何定时规则。

#### Scenario: 人工补采 Worker 独占认领
- **WHEN** 存在待执行或恢复中的 `COMMENT_EXPORT_V1`
- **THEN** 只有人工补采 Worker SHALL 认领和执行该 Job
- **THEN** 其它 Worker 不得将该 Job 标记为未注册、成功、失败或取消

#### Scenario: 页面只创建任务
- **WHEN** 运营人员从流程式评论导出页面提交创建
- **THEN** 系统 SHALL 创建或复用活动 Job 并返回任务成功反馈
- **THEN** 页面不得认领 Job、调用评论分页或生成 CSV

### Requirement: 固定自动生产者的受限配置

系统 SHALL 以代码注册表声明可用自动生产者、规则模板、固定 Job 构建器、范围冻结契约、允许的调度与策略组合及安全默认值。系统 SHALL 为每个已注册生产者及其规则实例持久化独立配置和脱敏运行投影；运营人员只能从生产者允许的规则模板新增、调整、启用、暂停或删除规则实例，不得创建自定义生产者、Job 类型、命令、参数、AppID、来源操作或时区。

关闭生产者或其规则 SHALL 只阻止未来自动入队，不得取消、修改、终态化、重放或重新解释任何既有自动 Job；人工创建的 Job SHALL 不受生产者开关影响。删除规则 SHALL 归档该规则，使其退出页面与调和循环，同时保留既有 Job 和逻辑槽审计。

#### Scenario: 新部署初始化默认生产者
- **WHEN** 部署首次加载已注册生产者
- **THEN** 系统 SHALL 为每个定义创建默认配置，且默认时刻、开关和策略 SHALL 与发布前既有自动调度语义一致
- **THEN** 后续启动或发布仅可创建缺失配置，不得覆盖运营人员已有配置或重建已归档规则

#### Scenario: 暂停一个自动生产者
- **WHEN** 运营人员暂停禁言续期生产者
- **THEN** Scheduler SHALL 停止为其未来逻辑槽创建自动续期 Job
- **THEN** 已处于待执行、运行、延后或重试状态的续期 Job SHALL 保持既有队列语义

#### Scenario: 拒绝超出白名单的配置
- **WHEN** 请求包含未知生产者、未知规则、Job 类型、来源范围、任意命令或该定义不允许的策略
- **THEN** 系统 SHALL 拒绝保存并返回固定的可处理错误
- **THEN** 系统不得创建 Job、访问来源或改变既有配置

#### Scenario: 迁移旧调度开关
- **WHEN** 首次应用定时生产者迁移
- **THEN** 系统 SHALL 用现有帖子采集间隔初始化帖子规则，并用现有会话保活开关初始化会话保活生产者
- **THEN** 后续 Scheduler SHALL 只读取持久化生产者配置，不得继续用旧采集间隔或会话保活环境开关决定自动入队

### Requirement: 受限调度表达式与逻辑槽

每条生产者规则实例 SHALL 使用其模板允许的正分钟固定间隔或五字段 Cron 表达式，并统一按中国标准时间（`Asia/Shanghai`）解释。运营页面 SHALL 仅接受分钟数或 `HH:MM` 时点，并由服务端生成、保存和反推展示受限表达式；系统 MUST 拒绝秒字段、Cron 别名、其他时区、子分钟间隔、超出定义频率边界和不被模板允许的表达式。Cron 的日与周字段同时受限时 SHALL 使用标准 Cron 的“满足其一”语义。每次调度 SHALL 以生产者、规则实例和实际 UTC 触发实例构造逻辑槽；配置版本只作为审计快照，显示时间和本地时间均不得作为去重键。系统 SHALL 以已覆盖实例和当前实例计算错过范围，不得为未受限的历史实例逐个建槽。

#### Scenario: 新增 Cron 规则
- **WHEN** 运营人员为允许使用 Cron 的生产者选择一个规则模板并填写一个 `HH:MM` 时点
- **THEN** 系统 SHALL 用模板固定的日期语义生成受限五字段表达式，并展示反推的中文时点
- **THEN** 配置 SHALL 在下一轮 Scheduler 评估时生效而不要求重启 Scheduler

#### Scenario: 保存配置不会重复已处理逻辑槽
- **WHEN** 运营人员在某一已处理逻辑槽期间保存生产者或规则配置
- **THEN** 系统 SHALL 记录新的配置版本供后续追溯
- **THEN** 系统不得因配置版本变化为同一生产者、规则和 UTC 实例再次自动入队

### Requirement: 错过触发与重叠处理

每条规则 SHALL 使用生产者定义明确允许的错过触发策略和重叠策略。错过触发 SHALL 仅为 `SKIP`、`COALESCE` 或定义限制的 `CATCH_UP_LIMITED`；重叠 SHALL 仅为 `ALLOW`、`SKIP_WHILE_ACTIVE` 或 `COALESCE_WHILE_ACTIVE`。系统 MUST 持久化逻辑槽的处理结果、关联 Job 和固定脱敏错误类别，以便重启后继续正确评估。

#### Scenario: 合并错过的日指标触发
- **WHEN** 日指标生产者在停机后发现多个未处理的历史槽且规则使用 `COALESCE`
- **THEN** 系统 SHALL 仅为最新可执行业务工作创建一次自动入队尝试
- **THEN** 系统 SHALL 保留领域 Job 的稳定幂等键，不得为每个错过槽创建重复业务任务

#### Scenario: 活动工作期间合并重叠触发
- **WHEN** 规则使用 `COALESCE_WHILE_ACTIVE` 且其先前关联工作仍处于活动状态
- **THEN** 系统 SHALL 仅保留最新未处理逻辑槽，并不得创建并行自动 Job
- **WHEN** 该活动工作进入终态
- **THEN** 系统 SHALL 仅对保留的最新槽执行一次固定入队尝试

### Requirement: 调度事件的事务边界与单实例执行

系统 SHALL 将实际需要处理的逻辑槽持久化为调度事件账本，并记录其状态、配置版本快照、关联 Job、有限尝试次数和固定脱敏错误类别。Scheduler SHALL 在同一个数据库事务内重新读取规则配置、创建或领取唯一逻辑槽、调用固定入队适配器、关联全部预期 Job 并写入状态投影。事务失败时，本次新建 Job、槽状态与投影 SHALL 一并回滚；事务提交后，Worker 才可消费完整的 Job 集合。

配置保存 SHALL 使用独立短事务；暂停的生效边界是保存事务提交。系统 MUST 使用跨平台专用运行时文件锁确保同一 runtime 同时最多一个 `run_scheduler --loop` 或 `--once` 进程；未取得锁的进程不得评估规则或写入数据库。

#### Scenario: 多 Job 生产尝试中途失败
- **WHEN** 一个逻辑槽需要创建多个固定 Job，且任一入队适配器调用抛出异常
- **THEN** 系统 SHALL 回滚该尝试中新建的所有 Job、槽关联与成功状态
- **THEN** 系统 SHALL 记录脱敏失败结果，且不得将该槽显示为已自动入队

#### Scenario: 暂停与正在评估的规则并发
- **WHEN** 运营人员提交暂停操作，同时 Scheduler 正在评估同一规则
- **THEN** 已在暂停保存事务提交前完成提交的一个自动入队 MAY 保留
- **THEN** 暂停提交后开始的 Scheduler 事务 SHALL 读取已暂停状态，且不得为未来槽创建自动 Job

### Requirement: 生产者调和的职责隔离

Scheduler SHALL 逐条独立评估已启用规则，并且只调用其注册的固定入队适配器写入后台任务表。Scheduler MUST NOT 建立 CDP 会话、调用来源、AI/OCR、Job handler 或上游写操作。一个生产者的配置错误、没有当前游戏、策略拒绝或入队失败不得阻止同轮其他生产者的评估。

自动生产者 SHALL 保持其既有范围冻结时机：帖子采集、状态巡检、指标与禁言续期在自动入队时读取当前操作游戏；会话保活在 Worker 首次实际浏览器操作前冻结范围。生产者配置不得接受或覆盖范围。

#### Scenario: 一个生产者配置无效
- **WHEN** Scheduler 发现某一生产者规则的配置无效
- **THEN** 系统 SHALL 仅为该规则记录固定脱敏错误并跳过其本轮入队
- **THEN** Scheduler SHALL 继续评估其余已启用规则

#### Scenario: 自动入队缺少当前游戏
- **WHEN** 需要在入队时冻结当前游戏的生产者运行且当前游戏未选择
- **THEN** 系统 SHALL 为该生产者记录固定未就绪结果且不得创建 Job
- **THEN** 系统不得猜测 AppID、连接 CDP 或影响其他生产者

### Requirement: 固定生产者的人工一次性入队
系统 SHALL 为每个代码注册的固定自动生产者提供受认证、CSRF 保护的人工“立即执行”映射。服务端 MUST 按生产者白名单直接创建对应的一次性**业务 Job**；不得接受客户端提供的 AppID、来源、Cron、任意 Job 类型、执行命令、Worker 名称或上游参数。页面提交不得直接调用 CDP、上游、AI、OCR 或 Job handler。

每个直接创建的业务 Job SHALL 在入队事务中读取当前激活游戏与范围版本，并写入服务端构造的 `execution_scope`；页面不得接受 AppID 或 scope 参数。排队期间发生游戏切换时，系统 MUST 继续使用入队时冻结范围，不得改用执行时游戏，也不得以点击时游戏与当前游戏不一致终态失败。

帖子采集入口 SHALL 在同一“立即执行”弹窗内提供当前窗口与历史补采两种受限模式，并仅创建由人工补采 Worker 消费的 Job。当前窗口模式 MUST 创建 `MANUAL_CURRENT_INGEST_WINDOW`：服务端以提交时上海时区当前分钟（秒和微秒清零）为结束时间、其前 30 分钟为开始时间，并固定页大小为 100。历史补采模式 MUST 只接受 1 至 744 小时，并以提交时当前操作游戏、上海当前分钟和固定页大小 100 创建 `MANUAL_INGEST_WINDOW`；页面不得接受 AppID。相同 AppID、范围版本、开始时间、结束时间和页大小的 `PENDING/RUNNING` Job SHALL 被复用，终态后 SHALL 允许再次创建。两种模式均不读取自动规则的启用状态、间隔或数量；暂停、修改或删除自动规则不得影响人工运行。`STATUS_SYNC` 继续由实时采集 Worker 消费并仅用于定时待审核状态巡检，不得由人工入口创建。

暂停生产者或规则 SHALL 只阻止未来自动逻辑槽入队；已认证运营人员的手动一次性业务 Job SHALL 继续可用。手动入队 MUST NOT 启用/暂停生产者、修改规则、推进 `covered_through_at`、创建 `ScheduledProducerSlot`、改变最近自动入队时间或影响下次自动触发。页面 SHALL 将自动运行投影和手动任务结果分开呈现。

固定映射 SHALL 为：帖子采集按所选受限模式创建当前时间窗口的 `MANUAL_CURRENT_INGEST_WINDOW` 或历史范围的 `MANUAL_INGEST_WINDOW`；日指标创建全部启用日来源的范围工作；周指标创建运营效果范围工作；会话保活创建当前游戏 Frame 保活工作；永久禁言续期创建当前游戏续期工作。会话保活和永久禁言续期只能通过各自生产者卡的“立即执行”入口创建；会话自动修复不属于此映射。

日指标的立即执行弹窗 SHALL 保留手动开始/结束日期，并提供“昨天”“近 7 天”“近 30 天”快捷选择。快捷选择 MUST 使用服务器上海日期、以昨天为结束日期，并只填充既有日期输入；不得自动提交表单、创建 Job 或绕过既有日期范围校验。

周指标的立即执行弹窗 SHALL 保留手动开始/结束日期，并提供“上周”“近 4 周”“近 12 周”快捷选择。快捷选择 MUST 使用服务器上海日期，并只提供完整结束的自然周：结束日为本周开始前的周日，开始日为周一；最长范围不得超过既有 90 天校验。快捷选择只填充既有日期输入；不得自动提交表单、创建 Job 或绕过既有日期范围校验。

所有固定生产者的“立即执行”弹窗 SHALL 采用一致的操作块结构：操作标题、语义说明、仅该操作所需的参数、Worker 等待边界提示和明确的提交按钮。无参数操作也 MUST 显示相同的执行边界说明。帖子采集的当前窗口和历史补采 MUST 使用独立表单；当前窗口表单只提交固定 `mode=current`，历史补采表单只提交固定 `mode=history` 和小时参数。页面不得显示会被另一个模式忽略的字段。

#### Scenario: 暂停的日指标生产者手动执行
- **WHEN** 日指标生产者已暂停，运营人员提交有效的人工日期范围
- **THEN** 系统 SHALL 创建或复用对应日指标范围 Job
- **THEN** 系统不得重新启用生产者、创建调度逻辑槽或改变下一次自动触发

#### Scenario: 日指标快捷选择日期范围
- **WHEN** 运营人员在日指标生产者的立即执行弹窗选择“近 7 天”
- **THEN** 系统 SHALL 将既有日期输入填充为截至上海昨天的连续 7 个自然日
- **THEN** 系统不得提交表单、创建 Job 或修改自动生产者配置

#### Scenario: 周指标快捷选择完整业务周
- **WHEN** 运营人员在周指标生产者的立即执行弹窗选择“近 4 周”
- **THEN** 系统 SHALL 将既有日期输入填充为截至最近完整周周日的连续 4 个完整自然周
- **THEN** 系统不得提交表单、创建 Job 或修改自动生产者配置

#### Scenario: 帖子采集操作块没有混合参数
- **WHEN** 运营人员打开帖子采集生产者的立即执行弹窗
- **THEN** 系统 SHALL 分别展示“采集最近 30 分钟”和“历史补采”的独立操作块与提交按钮
- **THEN** 当前窗口操作不得显示历史补采小时数，历史补采操作不得接受当前窗口以外的参数

#### Scenario: 手动保活使用唯一入口
- **WHEN** 运营人员在会话保活生产者卡片点击立即执行
- **THEN** 系统 SHALL 创建或复用既有当前游戏 Frame 保活 Job
- **THEN** 服务卡和旧运行时路由不得提供第二个等价的手动保活提交入口

#### Scenario: 手动永久禁言续期使用唯一入口
- **WHEN** 运营人员在永久禁言续期生产者卡片点击立即执行
- **THEN** 系统 SHALL 创建或复用当前游戏的永久禁言续期 Job
- **THEN** 禁言维护 Worker 卡和旧运行时路由不得提供第二个等价的手动续期提交入口

#### Scenario: 手动运行当前帖子采集窗口
- **WHEN** 运营人员在 10:12:34 提交帖子采集的立即执行
- **THEN** 系统 SHALL 创建或复用结束于 10:12:00、开始于 09:42:00、页大小为 100 的 `MANUAL_CURRENT_INGEST_WINDOW`
- **THEN** 系统 SHALL 冻结提交时游戏范围并仅交由人工补采 Worker 消费，不得创建 `STATUS_SYNC`

#### Scenario: 手动运行历史帖子补采
- **WHEN** 运营人员在帖子采集生产者的立即执行弹窗选择历史补采并提交 12 小时
- **THEN** 系统 SHALL 按提交时当前操作游戏、上海当前分钟与固定页大小 100 创建或复用 `MANUAL_INGEST_WINDOW`
- **THEN** 系统不得接受页面 AppID、创建 `STATUS_SYNC`、启动 Worker 或修改自动规则

#### Scenario: 未运行 Worker 时手动入队
- **WHEN** 运营人员成功创建一次性生产者 Job 但对应 Worker 未运行
- **THEN** Job SHALL 保持待执行并在 Worker 启动后按既有语义消费
- **THEN** Web 请求不得同步执行工作或将其显示为已完成

### Requirement: 发布 tag 的源码归档
系统 SHALL 在 `main` 推送的发布校验成功且 `v<version>` tag 已创建或核验后，从该 tag 生成名为 `wechat-content-operations-<version>-src.zip` 的源码归档。归档 SHALL 只包含 tag 中的 Git 跟踪文件，并 SHALL 使用以版本命名的单一顶层目录。

#### Scenario: 新版本成功发布
- **WHEN** `main` 上的版本、发布清单和发布校验均有效，且不存在对应 tag
- **THEN** 系统创建 `v<version>` tag，并从该 tag 生成对应版本的源码 ZIP

#### Scenario: 已有正确 tag 的工作流重跑
- **WHEN** `v<version>` 已存在且指向当前 `main` 提交
- **THEN** 系统仅从该 tag 重建相同版本的源码 ZIP，且不得移动或重建 tag

### Requirement: 归档运行态隔离
系统 SHALL 在发布前验证源码归档不包含 `runtime/`、`.git/`、`.venv/`、数据库、锁、日志或其他端点本地运行态；任一禁止内容存在时，系统 MUST 使发布失败并且不得上传该归档。

#### Scenario: 归档包含禁止路径
- **WHEN** 构建后的归档包含 `runtime/`、`.git/` 或 `.venv/` 路径
- **THEN** 系统使发布 job 失败，并且不创建或覆盖该版本的源码附件

### Requirement: 源码归档完整性校验
系统 SHALL 为每个源码 ZIP 生成 SHA-256 校验文件，并将校验文件名与 ZIP 文件名关联。校验文件 SHALL 与 ZIP 一同作为版本发布附件提供。

#### Scenario: 运维下载源码包
- **WHEN** 运维人员打开已发布版本的 GitHub Release
- **THEN** 其可下载对应版本的源码 ZIP 及 SHA-256 校验文件，并用校验文件验证 ZIP

### Requirement: 幂等且不可静默改写的 Release 发布
系统 SHALL 为每个由受控 `main` 发布流程创建或核验的 `v<version>` tag 创建或复用同名 GitHub Release。Release 不存在时系统 SHALL 创建它并上传源码 ZIP 与校验文件；Release 已存在时系统 SHALL 仅补传缺失附件，并 MUST 在已有附件与从 tag 重建的结果不一致时失败，且不得覆盖已有附件。

#### Scenario: 上传中断后的安全重跑
- **WHEN** Release 已创建但源码 ZIP 或校验文件尚未上传
- **THEN** 同一提交的工作流重跑会补传缺失附件，并保留原有 tag 和 Release

#### Scenario: 检测到同名附件不一致
- **WHEN** 已有 Release 附件的校验结果与当前 tag 重建的校验结果不同
- **THEN** 系统使发布 job 失败并提示人工调查，且不得覆盖已有附件

#### Scenario: 工作流不可用时人工创建 tag
- **WHEN** 维护者因工作流不可用而按紧急流程创建发布 tag
- **THEN** 系统不承诺自动创建 Release 附件，文档要求维护者恢复工作流后从同一提交重跑

### Requirement: 发布源码包的运维指引
系统 SHALL 在发布与运行文档中说明 Release 源码包的下载、SHA-256 校验、解压、运行态初始化和最小验证步骤，并说明 GitHub Packages 不承担本服务源码包分发职责。

#### Scenario: 新端点使用 Release 源码包部署
- **WHEN** 运维人员按照文档下载并校验某版本的源码 ZIP
- **THEN** 其可在解压目录创建 `runtime/`、从示例配置初始化运行态，并完成锁定依赖安装和发布协议校验

#### Scenario: 首个自动 Release 验收
- **WHEN** 新工作流发布首个版本
- **THEN** 维护者下载两个附件，确认 SHA-256 校验通过，并在解压目录完成锁定依赖安装和发布协议校验
