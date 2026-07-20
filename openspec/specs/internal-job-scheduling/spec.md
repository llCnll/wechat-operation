# Purpose

定义项目内部 Scheduler Worker 的定时入队边界与运行约束。

## Requirements

### Requirement: 项目内部的定时任务入队
系统 SHALL 提供独立于 Web Server 的常驻 Scheduler Worker，并以北京时间每 15 分钟时间桶为城主别慌张创建状态巡检和采集任务。Scheduler Worker SHALL 不依赖外部 `cron`、`launchd` 或其他系统定时器。

#### Scenario: 时间桶内首次调度
- **WHEN** Scheduler Worker 运行并进入一个尚未调度的北京时间 15 分钟时间桶
- **THEN** 系统 SHALL 先幂等创建一个上限为 20 的 `STATUS_SYNC` 任务，再幂等创建一个回看最近 30 分钟、页大小为 20 的 `SCHEDULED_INGEST_WINDOW` 任务

#### Scenario: 重复检查或重复启动
- **WHEN** 一个或多个 Scheduler Worker 在同一时间桶内重复检查或重启
- **THEN** 系统 SHALL 通过稳定时间桶幂等键复用已有任务，且不得为同一任务类型创建重复任务

#### Scenario: Scheduler 职责隔离
- **WHEN** Scheduler Worker 创建或取得任务
- **THEN** 它 SHALL 仅写入后台任务表，且不得建立 CDP 会话、调用 OCR/AI、认领采集任务或调用任何上游写操作

### Requirement: Scheduler Worker 的运行边界
系统 SHALL 将 Scheduler、采集和审核作为三个任务类型隔离的常驻进程运行；SQLite 部署 SHALL 只运行一个 Scheduler Worker。一次性调度入口仅用于诊断和测试，不得作为生产环境的外部定时器依赖。

#### Scenario: Scheduler 中断恢复
- **WHEN** Scheduler Worker 停止后重新启动
- **THEN** 它 SHALL 在启动后检查当前时间桶并继续调度，且同桶重复入队 SHALL 保持幂等

#### Scenario: 超过回看窗口的空洞
- **WHEN** Scheduler Worker 停止超过 30 分钟
- **THEN** 系统 SHALL 不自动扩大采集窗口，运营人员 MUST 在系统配置首页的帖子采集 Worker 节点创建受控历史补采窗口；直接管理命令仅用于诊断

### Requirement: 一次性作者状态回填任务的消费边界
系统 SHALL 将 `AUTHOR_MUTE_STATUS_BACKFILL` 明确归类为一次性 Job，并仅允许既有人工补采 Worker 消费。入队入口 SHALL 只创建或复用固定幂等键的后台任务，不得建立 CDP 会话、认领任务或调用上游。该任务不得加入 Scheduler 或实时采集 Worker 的消费范围，且其失败重试上限 SHALL 为零。

#### Scenario: 重复入队
- **WHEN** 运营人员重复执行一次性回填入队命令
- **THEN** 系统 SHALL 返回同一个固定幂等键的后台任务，而不得创建并行的全量详情读取

#### Scenario: 显式恢复失败任务
- **WHEN** 一次性回填任务因非自动重试错误而处于失败或取消状态，且运营人员在修复环境后再次执行入队命令
- **THEN** 系统 SHALL 将同一任务重新置为待执行、保留其检查点并重置重试计数，而不得创建新任务或自动重试

#### Scenario: Worker 中断
- **WHEN** 人工补采 Worker 在回填任务执行期间中断
- **THEN** 系统 SHALL 复用通用 Worker 的中断恢复机制重新排队该任务，并从其最近保存的帖子游标继续
