# Purpose

定义项目内部 Scheduler Worker 的定时入队边界与运行约束。

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
