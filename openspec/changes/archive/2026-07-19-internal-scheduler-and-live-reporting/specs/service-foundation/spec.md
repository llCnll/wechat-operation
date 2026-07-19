## MODIFIED Requirements

### Requirement: SQLite 部署边界与专用 Worker
系统 SHALL 在首期使用启用 WAL 模式、外键约束和 busy timeout 的 SQLite，并运行一个 Web 实例、一个 Scheduler Worker、一个帖子采集 Worker 与一个 AI 审核 Worker。三个 Worker SHALL 按职责隔离；SQLite 写入阶段会串行化，同一职责 SHALL NOT 启动多个实例。

#### Scenario: 初始化数据库
- **WHEN** 服务初始化新的 SQLite 数据库
- **THEN** 它 SHALL 在正常处理任务前应用迁移，并配置 WAL 模式、外键约束和已配置的 busy timeout

#### Scenario: 专用 Worker 不互相认领任务
- **WHEN** Scheduler Worker、帖子采集 Worker 与 AI 审核 Worker 同时运行
- **THEN** Scheduler Worker SHALL 仅创建调度任务而不认领业务任务，采集 Worker SHALL 仅认领采集、状态巡检和范围互动刷新任务，AI 审核 Worker SHALL 仅认领 OCR 与 AI 评估任务，且不得将另一类任务标记为未注册或失败

#### Scenario: 需要水平扩容
- **WHEN** 任一任务类型需要多个消费者，或 SQLite 写锁持续影响任务成功率
- **THEN** 部署 SHALL 迁移到 PostgreSQL 后再扩展 Worker 实例，而不得在 SQLite 上横向扩容同一职责
