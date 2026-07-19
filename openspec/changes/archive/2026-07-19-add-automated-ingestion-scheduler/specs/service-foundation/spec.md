## ADDED Requirements

### Requirement: 一次性采集调度器边界
系统 SHALL 提供一次性 `run_scheduler --once` 调度入口，供单一外部系统定时器每 15 分钟调用。该命令 SHALL 仅计算周期窗口并创建或取得持久化后台任务，随后退出；它 SHALL NOT 连接 CDP、读取浏览器会话、执行帖子采集、等待任务完成、调用 AI 服务或调用任何上游写适配器。

#### Scenario: 调度结果可观测
- **WHEN** 一次性调度完成
- **THEN** 命令 SHALL 输出脱敏的目标 AppID、对齐窗口、任务编号及任务是新建还是已存在的结果

#### Scenario: 采集 Worker 独立执行
- **WHEN** 调度器已创建 `INGEST_WINDOW` 任务
- **THEN** 仅帖子采集 Worker SHALL 认领并执行该任务，调度器退出不得影响任务的分页检查点、重试或恢复行为

#### Scenario: 外部定时器重复配置
- **WHEN** 多个外部定时器在同一时间桶触发一次性调度器
- **THEN** 系统 SHALL 通过任务幂等键保证只存在一个对应的采集任务，并在运行文档中要求生产环境仅启用一个定时器来源
