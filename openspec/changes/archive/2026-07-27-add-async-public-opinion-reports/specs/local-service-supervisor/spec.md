## MODIFIED Requirements

### Requirement: Supervisor 启动与服务所有权
系统 SHALL 提供独立于 Django Web 的本机 Supervisor 入口，作为 `controlled_chrome`、`web`、`scheduler`、`game_circle_session`、`ingestion`、`backfill_ingestion`、`metric_sync`、`review`、`video_extraction`、`ai_moderation`、`mute_maintenance` 与 `public_opinion_reporting` 固定服务的唯一进程所有者。`ingestion` SHALL 运行实时采集 Worker，`backfill_ingestion` SHALL 运行人工补采 Worker，`metric_sync` SHALL 运行固定的指标同步管理命令，`public_opinion_reporting` SHALL 只运行舆情报告 Worker。Supervisor SHALL 在启动时创建所需运行态目录，执行幂等迁移、静态资源收集和默认策略初始化预检，然后启动 Web 并等待健康检查成功；日常部署 MUST 通过 Supervisor 启动和控制受管服务。

#### Scenario: 首次或日常启动 Supervisor
- **WHEN** 运营人员启动 Supervisor 且预检成功
- **THEN** Supervisor SHALL 启动 Web 服务并在健康检查成功后报告可访问状态，且不得要求运营人员另行执行 Web 启动命令
- **THEN** 它 SHALL 按期望状态启动 `metric_sync` 与 `public_opinion_reporting`，且不得要求运营人员另行执行对应 Worker 命令

#### Scenario: 启动预检失败
- **WHEN** 迁移、静态资源收集或默认策略初始化失败
- **THEN** Supervisor SHALL 记录脱敏阶段错误并以非成功状态退出，且 MUST NOT 启动 Web 或任一 Worker

### Requirement: 固定服务的受限生命周期控制
Supervisor SHALL 使用跨平台独占运行态文件锁保证同一部署仅有一个实例。`web` 仅接受 `status`、日志读取和延迟 `restart`；`controlled_chrome`、`scheduler`、`game_circle_session`、`ingestion`、`backfill_ingestion`、`metric_sync`、`review`、`video_extraction`、`ai_moderation`、`mute_maintenance` 与 `public_opinion_reporting` 接受 `start`、`stop`、`restart`、`status` 和日志读取操作。Supervisor SHALL 使用固定的受管 Python 与管理命令启动服务，且 MUST NOT 接受浏览器、数据库或配置文件提供的任意可执行命令、参数、路径或 PID。

#### Scenario: 启动已配置的 Worker
- **WHEN** Supervisor 收到对已停止的任一可控 Worker 或 `controlled_chrome` 的启动请求
- **THEN** Supervisor SHALL 只启动对应固定 Worker 命令，并记录该服务为期望运行状态

#### Scenario: 已有 Supervisor 正在运行
- **WHEN** 第二个 Supervisor 尝试在同一运行态目录启动
- **THEN** 它 MUST NOT 执行预检、修改服务状态或管理子进程，并 SHALL 返回已有实例正在运行的可处理信息

#### Scenario: 拒绝未知服务或操作
- **WHEN** 请求包含未注册服务名或未注册操作
- **THEN** Supervisor SHALL 拒绝请求、记录不含敏感数据的错误，并不得创建或终止任何进程

#### Scenario: 指标 Worker 优雅停止
- **WHEN** Supervisor 停止或重启 `metric_sync`
- **THEN** 它 SHALL 使用既有优雅终止和 checkpoint 恢复机制；中断的指标任务 SHALL 保留业务日期和检查点

## ADDED Requirements

### Requirement: 舆情报告 Worker 的受管边界
Supervisor SHALL 将 `public_opinion_reporting` 作为默认受管的固定 Worker。该 Worker SHALL 只认领 `PUBLIC_OPINION_REPORT`，只读取本地数据库并写入报告快照；不得建立 CDP 会话、获得会话 IPC capability、调用上游、执行审核或发起写操作。系统配置 SHALL 展示该 Worker 的状态、日志以及启动、停止和重启控制。

#### Scenario: Worker 停止时创建报告
- **WHEN** 运营人员创建报告且 `public_opinion_reporting` 未运行
- **THEN** 系统 SHALL 创建或复用报告 Job，并明确提示该任务等待 Worker 启动
- **THEN** Web 请求不得自行计算报告或启动 Worker

#### Scenario: Worker 只认领报告任务
- **WHEN** `public_opinion_reporting` 与其他固定 Worker 同时运行
- **THEN** 它 SHALL 只认领 `PUBLIC_OPINION_REPORT`
- **THEN** 其他 Worker 不得认领该任务或将其标记为未注册
