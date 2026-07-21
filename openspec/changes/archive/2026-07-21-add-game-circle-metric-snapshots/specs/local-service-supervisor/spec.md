## MODIFIED Requirements

### Requirement: Supervisor 启动与服务所有权

Supervisor SHALL 作为 `web`、`scheduler`、`ingestion`、`backfill_ingestion`、`metric_sync` 与 `review` 固定服务的唯一进程所有者。`metric_sync` SHALL 运行固定的指标同步管理命令；日常部署 MUST 通过 Supervisor 启动和控制该服务。

#### Scenario: 首次或日常启动 Supervisor

- **WHEN** Supervisor 预检成功并启动固定服务
- **THEN** 它 SHALL 按期望状态启动 `metric_sync`，且不得要求运营人员另行执行指标 Worker 命令

### Requirement: 固定服务的受限生命周期控制

`metric_sync` SHALL 与其他 Worker 一样只接受 `start`、`stop`、`restart`、`status` 和日志读取。Supervisor MUST NOT 接受浏览器、数据库或配置文件传入的任意可执行命令、参数、路径或 PID。

#### Scenario: 指标 Worker 优雅停止

- **WHEN** Supervisor 停止或重启 `metric_sync`
- **THEN** 它 SHALL 使用既有优雅终止和 checkpoint 恢复机制；中断的指标任务 SHALL 保留业务日期和检查点
