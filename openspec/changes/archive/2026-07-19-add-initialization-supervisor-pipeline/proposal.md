## Why

当前首次启用和日常恢复依赖 README、多个终端和人工判断进程状态。Web、Scheduler、采集 Worker 与 AI Worker 的生命周期彼此独立，导致交接成本高，也无法在系统内直观确认配置、登录态与任务链是否已就绪。

## What Changes

- 新增本机运行控制 Supervisor，作为 Web、Scheduler、实时采集 Worker、人工补采 Worker 与 AI Worker 的唯一进程管理者，统一处理固定服务的启动、停止、重启、状态、心跳、日志和退出恢复。
- 将日常入口收敛为启动 Supervisor；Supervisor 自动完成必要预检并拉起 Web，四个 Worker 由系统配置页按依赖显式启用。
- 在系统配置首页 Hero 下方新增全宽“初始化与运行状态”Pipeline，按默认策略、AI/OCR 配置、人工处置开关、CDP 登录和三个业务节点（自动调度、双进程帖子采集、AI 审核）展示下一步、依赖、脱敏错误与受限操作；帖子采集节点还可创建受控的人工历史补采任务。
- 将自动采集与人工长任务拆为独立任务类型和消费者：实时采集 Worker 消费自动采集与状态巡检，人工补采 Worker 消费历史补采、范围互动刷新及旧采集任务，避免人工长任务阻塞每 15 分钟调度任务。
- 提供仅可编辑白名单运行配置的表单；密钥只可写入、不回显，配置保存后明确提示并可重启受影响服务。
- 将 Web 自行管理 Worker 的方式替换为通过本地受限 Supervisor API 管理；Web 不提供自身的停止或启动操作。
- 保留首次管理员账号为 README 中由执行 Agent 手工完成的首装前置条件，不在页面或 Supervisor 中自动创建账号或处理密码。

## Capabilities

### New Capabilities
- `local-service-supervisor`: 本机 Supervisor 对固定服务进行跨平台生命周期、日志、心跳和受限控制。

### Modified Capabilities
- `administration-configuration-console`: 系统配置首页提供初始化 Pipeline、受控运行配置和 Supervisor 服务控制入口。
- `service-foundation`: 服务运行模型由手工启动四个独立进程调整为由 Supervisor 托管 Web 与专用 Worker。

## Impact

- 新增独立 Supervisor 进程、运行态元数据、仅本机可用的控制 API、双采集 Worker 启动命令及相应的任务类型。
- 调整 `runtime/` 目录，新增服务 PID、心跳、日志与 Supervisor 凭据等可变运行态。
- 调整系统配置模板、样式、运行时配置读取方式和 README/运行手册。
- 可能新增轻量跨平台进程查询依赖，用于校验 PID、创建时间和退出状态。
