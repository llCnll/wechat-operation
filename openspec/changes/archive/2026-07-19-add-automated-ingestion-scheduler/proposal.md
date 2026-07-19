## Why

当前帖子采集必须由运营人员手动执行入队命令，容易遗漏、窗口不连续，也无法稳定支持日常审核队列。系统已经具备可恢复的采集任务与专用 Worker，应补齐受控的自动入队调度能力。

## What Changes

- 为“城主别慌张”固定小游戏 AppID `wx6d2454a8afcd9706` 增加自动采集调度。
- `run_scheduler --once` 每次仅创建一个最近 30 分钟的原创帖子采集窗口任务；由外部系统定时器每 15 分钟调用，形成 15 分钟重叠。
- 调度器按北京时间对齐时间窗口并使用稳定的时间桶幂等键，重复调用不得创建相同窗口的重复任务。
- 为调度器增加可读的执行结果、配置校验与脱敏错误信息；调度器不调用 CDP、不访问登录态、不等待 Worker 完成。
- 更新运行手册，说明以 cron 或 macOS `launchd` 触发一次性调度命令的推荐部署方式、单实例约束和故障排查方式。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `original-post-ingestion`: 自动按固定频率创建有重叠的采集时间窗口任务。
- `service-foundation`: 为外部定时器提供幂等、受配置约束的一次性调度器入口，并明确其与专用 Worker 的职责边界。

## Impact

- 受影响代码：`jobs/management/commands/run_scheduler.py`、任务入队服务、运行时配置、相关测试与运行文档。
- 不新增第三方依赖、常驻调度进程或数据库表。
- 采集仍由 `run_ingestion_worker` 执行；AI Worker、人工审核和所有上游写适配器均不受调度器调用。
