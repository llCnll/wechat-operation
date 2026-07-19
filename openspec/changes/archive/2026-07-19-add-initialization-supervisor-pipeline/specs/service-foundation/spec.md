## MODIFIED Requirements

### Requirement: 运行态目录隔离
系统 SHALL 将部署配置、SQLite 数据库、收集后的静态文件、Worker 锁、Supervisor 独占锁、Supervisor 状态、Supervisor 凭据、服务 PID、心跳和可选日志统一置于项目根目录的 `runtime/`。代码包与版本控制 SHALL 排除整个目录；`.env.example` 仅作为可打包的配置模板。

#### Scenario: 初始化或打包服务
- **WHEN** 运营人员部署或打包服务代码
- **THEN** 系统 SHALL 能在缺少 `runtime/` 的代码包中初始化该目录，并从 `runtime/.env` 读取运行时配置，而不得将数据库、密钥、Supervisor 凭据、锁文件或日志混入代码目录

### Requirement: SQLite 部署边界与专用 Worker
系统 SHALL 在首期使用启用 WAL 模式、外键约束和 busy timeout 的 SQLite，并在本机 Supervisor 托管下运行一个 Web 实例、一个 Scheduler Worker、一个实时采集 Worker、一个人工补采 Worker 与一个 AI 审核 Worker。四个 Worker SHALL 按职责隔离；SQLite 写入阶段会串行化，同一 Worker 职责 SHALL NOT 启动多个实例。日常部署 MUST 由 Supervisor 启动和控制这些服务；直接管理命令仅用于开发或故障诊断。

#### Scenario: 初始化数据库
- **WHEN** 服务初始化新的 SQLite 数据库
- **THEN** 它 SHALL 在正常处理任务前应用迁移，并配置 WAL 模式、外键约束和已配置的 busy timeout

#### Scenario: 专用 Worker 不互相认领任务
- **WHEN** Supervisor 托管的 Scheduler Worker、实时采集 Worker、人工补采 Worker 与 AI 审核 Worker 同时运行
- **THEN** Scheduler Worker SHALL 仅创建调度任务而不认领业务任务；实时采集 Worker SHALL 仅认领 `SCHEDULED_INGEST_WINDOW` 与 `STATUS_SYNC`；人工补采 Worker SHALL 仅认领 `MANUAL_INGEST_WINDOW`、`METRICS_REFRESH_WINDOW` 与升级前遗留的 `INGEST_WINDOW`；AI 审核 Worker SHALL 仅认领 OCR 与 AI 评估任务，且不得将另一类任务标记为未注册或失败

#### Scenario: 人工长任务不阻塞自动调度任务
- **WHEN** 人工补采 Worker 正在执行历史补采或范围互动刷新，且自动调度器创建新的 15 分钟任务
- **THEN** 新的 `SCHEDULED_INGEST_WINDOW` 或 `STATUS_SYNC` SHALL 由实时采集 Worker 独立认领，不得等待人工补采 Worker 完成

#### Scenario: 需要水平扩容
- **WHEN** 任一任务类型需要多个消费者，或 SQLite 写锁持续影响任务成功率
- **THEN** 部署 SHALL 迁移到 PostgreSQL 后再扩展 Worker 实例，而不得在 SQLite 上横向扩容同一类 Worker
