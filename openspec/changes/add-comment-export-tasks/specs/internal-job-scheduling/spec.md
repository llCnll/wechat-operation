## ADDED Requirements

### Requirement: 评论导出任务的人工补采消费边界

系统 SHALL 将 `COMMENT_EXPORT_V1` 归类为手动一次性评论导出任务，并仅允许既有人工补采 Worker 消费。入队入口 SHALL 只在短数据库事务内创建或复用 Run 与 BackgroundJob，不得建立 CDP 会话、读取评论、写入 Result 或调用平台写接口。实时采集、指标、审核、会话和 Web 进程不得认领或执行该任务。

任务活动去重 SHALL 使用包含创建者、冻结范围、帖子、作者和规则的 `active_coalescing_key`；恢复中断任务 SHALL 保留通用 Job 生命周期，但 handler 每次尝试必须重置未发布结果集和进度。该任务不属于 Scheduler、自动生产者或任何定时规则。

#### Scenario: 人工补采 Worker 独占认领
- **WHEN** 存在待执行或恢复中的 `COMMENT_EXPORT_V1`
- **THEN** 只有人工补采 Worker SHALL 认领和执行该 Job
- **THEN** 其它 Worker 不得将该 Job 标记为未注册、成功、失败或取消

#### Scenario: 页面只创建任务
- **WHEN** 运营人员从流程式评论导出页面提交创建
- **THEN** 系统 SHALL 创建或复用活动 Job 并返回任务成功反馈
- **THEN** 页面不得认领 Job、调用评论分页或生成 CSV
