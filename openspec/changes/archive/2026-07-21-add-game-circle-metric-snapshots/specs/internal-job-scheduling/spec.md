## MODIFIED Requirements

### Requirement: 项目内部的定时任务入队

系统 SHALL 提供独立于 Web Server 的常驻 Scheduler Worker，并以北京时间时间桶为城主别慌张创建帖子任务和指标任务。Scheduler Worker SHALL 不依赖外部 `cron`、`launchd` 或其他系统定时器，且 SHALL 仅写入后台任务表，不得建立 CDP 会话、调用 OCR/AI、认领任务或调用上游写操作。

除既有 15 分钟帖子任务外，Scheduler SHALL 在每日 `07:30` 后为五个已启用日指标来源分别创建锁定昨日的稳定任务，并在每周一 `17:30` 后为运营效果创建锁定已完成周的稳定任务。日任务范围 SHALL 覆盖 `D-2` 至 `D0`，周任务范围 SHALL 覆盖最近三个已完成周。

#### Scenario: 指标调度桶重复检查

- **WHEN** Scheduler 在同一指标日或周调度桶内重复检查或重启
- **THEN** 系统 SHALL 复用每个来源对应的稳定任务，且不得创建重复任务

#### Scenario: 终态日任务的后续修订

- **WHEN** 某日稳定指标任务已终态失败或成功
- **THEN** 当日 Scheduler 重复检查不得新建同一调度任务
- **THEN** 次日的既定复核窗口和显式人工复核批次仍可读取并更新该业务日

### Requirement: 运营效果发布后复核桶

Scheduler SHALL 为同一已完成业务周使用周一 `17:30`、周二 `10:00` 和周二 `16:00` 三个独立的稳定发布桶；每个桶 SHALL 读取该周及最近两个已完成周。桶幂等键 MUST 包含目标业务周和发布阶段。Scheduler 晚启动时 SHALL 为当前目标周所有已过而未入队的发布桶补入任务；同桶重复检查或重启 MUST NOT 创建重复任务。

#### Scenario: 周二晚启动补齐复核

- **WHEN** Scheduler 在周二 `16:00` 后首次启动
- **THEN** 系统 SHALL 为该已完成周补入初始、上午复核和下午复核三个任务，并分别复用其稳定幂等键

#### Scenario: 重复检查已过复核桶

- **WHEN** Scheduler 在同一周二 `16:00` 后再次检查
- **THEN** 系统 SHALL 复用三个发布桶既有任务，且不得创建重复任务
