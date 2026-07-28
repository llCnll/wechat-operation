## MODIFIED Requirements

### Requirement: 禁言维护 Worker 的受管生命周期
Supervisor SHALL 将 `mute_maintenance` 作为固定受管 Worker。该 Worker 只消费永久禁言续期 Job、禁言列表状态刷新 Job 和全部帖子作者状态同步 Job，并通过既有会话服务请求来源操作；不得直接管理浏览器、认证材料或任意外部命令。

系统配置运行节点 SHALL 展示该 Worker 的状态，并仅提供启动、停止、重启和日志等受管生命周期操作；不得提供“执行一次”或其他业务 Job 创建操作。永久禁言续期 Job SHALL 由自动任务生产者的“立即执行”创建或复用，不得在 Web 请求中执行续期。

#### Scenario: Worker 未运行时已入队的人工续期
- **WHEN** 运营人员通过永久禁言续期生产者创建或复用 Job，但 `mute_maintenance` 未运行
- **THEN** 系统 SHALL 保留该 Job 待执行并明确提示其等待 Worker 启动
- **THEN** Worker 卡不得因该 Job 自动启动或显示为正在执行

#### Scenario: Worker 只认领禁言维护任务
- **WHEN** `mute_maintenance` 与其他 Worker 同时运行
- **THEN** 它 SHALL 仅认领永久禁言续期、禁言列表状态刷新和全量帖子作者状态同步 Job
- **THEN** 其他 Worker 不得认领这些 Job 或将其标记为未注册
