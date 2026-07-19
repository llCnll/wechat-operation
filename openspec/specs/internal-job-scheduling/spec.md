# Purpose

定义项目内部 Scheduler Worker 的定时入队边界与运行约束。

## Requirements

### Requirement: 项目内部的定时任务入队
系统 SHALL 提供独立于 Web Server 的常驻 Scheduler Worker，并以北京时间每 15 分钟时间桶为城主别慌张创建状态巡检和采集任务。Scheduler Worker SHALL 不依赖外部 `cron`、`launchd` 或其他系统定时器。

#### Scenario: 时间桶内首次调度
- **WHEN** Scheduler Worker 运行并进入一个尚未调度的北京时间 15 分钟时间桶
- **THEN** 系统 SHALL 先幂等创建一个上限为 20 的 `STATUS_SYNC` 任务，再幂等创建一个回看最近 30 分钟、页大小为 20 的 `INGEST_WINDOW` 任务

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
- **THEN** 系统 SHALL 不自动扩大采集窗口，运营人员 MUST 使用受控的手动采集命令补采历史窗口
