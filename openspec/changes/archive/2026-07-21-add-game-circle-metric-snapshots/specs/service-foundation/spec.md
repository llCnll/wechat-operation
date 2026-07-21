## MODIFIED Requirements

### Requirement: SQLite 部署边界与专用 Worker

系统 SHALL 在 SQLite 部署中由本机 Supervisor 托管 Web、Scheduler、实时采集、人工补采、指标同步和 AI 审核 Worker。Worker SHALL 按职责隔离；同一职责 SHALL NOT 启动多个实例。

指标同步 Worker SHALL 仅认领 `METRIC_DAILY_SYNC`、`METRIC_WEEKLY_SYNC` 和 `METRIC_BACKFILL`，不得认领帖子采集、互动刷新、OCR 或 AI 任务。Scheduler 不得认领任何指标任务。

#### Scenario: 专用指标 Worker 与其他 Worker 同时运行

- **WHEN** 指标同步 Worker、实时采集 Worker、人工补采 Worker 和 AI 审核 Worker 同时运行
- **THEN** 指标同步 Worker SHALL 仅消费指标任务，其他 Worker SHALL 不得将指标任务标为未注册或失败

### Requirement: 持久化后台任务执行

系统 SHALL 在任务表持久化指标任务的类型、幂等键、状态、检查点、重试次数、下次运行时间和脱敏错误摘要。范围指标 checkpoint SHALL 至少包含来源、日期块、下一页、保存数和最后业务日；Worker SHALL 在页成功落库后推进页码。

#### Scenario: 指标范围任务中断恢复

- **WHEN** 指标任务在已完成页后中断
- **THEN** 后续指标 Worker SHALL 从持久化的下一页和日期块恢复，不得假设单页或重新补齐缺失日期

### Requirement: 外部服务适配器边界

系统 SHALL 将游戏圈私有只读接口隔离在指标 CDP 适配器中；领域服务 SHALL 只接收 source 特定 DTO 和白名单字段，不得持久化原始响应或认证上下文。

适配器和任务 SHALL 将登录/身份问题、权限或契约问题、传输/限流/5xx 和未知业务码区分处理。仅明确权限和契约错误可直接终态；临时错误 SHALL 按指标规格有限退避。

#### Scenario: 指标接口字段或临时状态变化

- **WHEN** 上游缺少必需字段或返回不兼容结构
- **THEN** 适配器 SHALL 返回契约错误，调用方不得覆盖既有快照
- **WHEN** 上游限流、5xx、传输失败或返回未知业务码
- **THEN** 调用方 SHALL 按指标规格退避重试并保留 checkpoint 与脱敏错误类别
