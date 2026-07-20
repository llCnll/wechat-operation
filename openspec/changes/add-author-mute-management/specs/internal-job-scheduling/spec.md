## ADDED Requirements

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
