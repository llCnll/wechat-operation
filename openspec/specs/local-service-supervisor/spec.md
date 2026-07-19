# Purpose

定义本机 Supervisor 对 Web 与固定 Worker 的生命周期、状态恢复、日志和受认证控制边界。

## Requirements

### Requirement: Supervisor 启动与服务所有权
系统 SHALL 提供独立于 Django Web 的本机 Supervisor 入口，作为 `web`、`scheduler`、`ingestion`、`backfill_ingestion` 与 `review` 五个固定服务的唯一进程所有者。`ingestion` SHALL 运行实时采集 Worker，`backfill_ingestion` SHALL 运行人工补采 Worker。Supervisor SHALL 在启动时创建所需运行态目录，执行幂等迁移、静态资源收集和默认策略初始化预检，然后启动 Web 并等待健康检查成功。

#### Scenario: 首次或日常启动 Supervisor
- **WHEN** 运营人员启动 Supervisor 且预检成功
- **THEN** Supervisor SHALL 启动 Web 服务并在健康检查成功后报告可访问状态，且不得要求运营人员另行执行 Web 启动命令

#### Scenario: 启动预检失败
- **WHEN** 迁移、静态资源收集或默认策略初始化失败
- **THEN** Supervisor SHALL 记录脱敏阶段错误并以非成功状态退出，且 MUST NOT 启动 Web 或任一 Worker

### Requirement: 固定服务的受限生命周期控制
Supervisor SHALL 使用跨平台独占运行态文件锁保证同一部署仅有一个实例。`web` 仅接受 `status`、日志读取和延迟 `restart`；`scheduler`、`ingestion`、`backfill_ingestion` 与 `review` 接受 `start`、`stop`、`restart`、`status` 和日志读取操作。Supervisor SHALL 使用固定的受管 Python 与管理命令启动服务，且 MUST NOT 接受浏览器、数据库或配置文件提供的任意可执行命令、参数、路径或 PID。

#### Scenario: 启动已配置的 Worker
- **WHEN** Supervisor 收到对已停止的 `scheduler`、`ingestion`、`backfill_ingestion` 或 `review` 的启动请求
- **THEN** Supervisor SHALL 只启动对应固定 Worker 命令，并记录该服务为期望运行状态

#### Scenario: 已有 Supervisor 正在运行
- **WHEN** 第二个 Supervisor 尝试在同一运行态目录启动
- **THEN** 它 MUST NOT 执行预检、修改服务状态或管理子进程，并 SHALL 返回已有实例正在运行的可处理信息

#### Scenario: 拒绝未知服务或操作
- **WHEN** 请求包含未注册服务名或未注册操作
- **THEN** Supervisor SHALL 拒绝请求、记录不含敏感数据的错误，并不得创建或终止任何进程

### Requirement: 服务状态、日志与恢复
Supervisor SHALL 在 `runtime/supervisor/` 持久化每个托管服务的期望状态、PID、创建时间、最近退出码、启动次数、心跳和日志位置。Supervisor 重启后 SHALL 恢复 Web 以及此前期望运行的 Worker；日志 SHALL 与 Web 进程生命周期解耦。

#### Scenario: Supervisor 重启后恢复服务
- **WHEN** Supervisor 在已有服务期望状态的情况下重新启动
- **THEN** 它 SHALL 校验已有进程身份并恢复缺失的期望服务，且页面 SHALL 能展示恢复后的状态与最近退出信息

#### Scenario: Worker 优雅停止
- **WHEN** Supervisor 停止或重启一个 Worker
- **THEN** 它 SHALL 先发送优雅终止信号并等待受限超时，再在必要时强制终止；中断任务 SHALL 继续依赖既有锁和 checkpoint 恢复机制

### Requirement: 本机受认证控制通道
Supervisor SHALL 仅监听本机回环地址，并使用存放在受限 `runtime/` 文件中的高熵控制令牌认证 Django 后端请求。浏览器 SHALL 只能通过已认证且受 CSRF 保护的 Django 端点请求固定控制操作，且不得接收或持久化 Supervisor 地址、令牌或任意服务命令。

#### Scenario: 已登录运营人员请求重启 Web
- **WHEN** 已认证运营人员在系统配置页请求重启 Web
- **THEN** Django SHALL 通过受认证本机通道请求 Supervisor 延迟重启 Web，页面 SHALL 在连接短暂中断后轮询健康检查至恢复或显示脱敏失败状态

#### Scenario: Supervisor 控制通道不可用
- **WHEN** Django 无法连接 Supervisor 或令牌校验失败
- **THEN** 系统配置页 SHALL 以只读方式显示 Supervisor 不可用及脱敏错误，且 MUST NOT 直接执行本机进程命令
