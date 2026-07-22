## MODIFIED Requirements

### Requirement: 会话服务的受管生命周期

Supervisor SHALL 将 `controlled_chrome` 与 `game_circle_session` 作为固定受管服务，与既有 Web、Scheduler、采集、指标、审核和媒体 Worker 一同拥有。`controlled_chrome` SHALL 使用独立 profile、固定 loopback 调试地址和固定端口；可执行文件仅可来自部署时的非敏感配置或跨平台安全发现，不得接受 HTTP、数据库或 Job 传入的命令、参数、路径或 PID。`game_circle_session` SHALL 使用固定 Python 与固定管理命令，承载固定本地 IPC 契约、`OPEN_CONTROLLED_LOGIN`、`GAME_CIRCLE_LOGIN_PROBE` 与 `GAME_CIRCLE_SESSION_REFRESH`。

`game_circle_session` 只有在 `controlled_chrome` 受 Supervisor 管理且可用时才可执行 CDP 会话操作；两者均 SHALL 具备既有的运行态、日志、优雅停止和恢复机制。Supervisor 只终止自己记录的 Chrome PID，不得接管手动浏览器。会话服务必须以 owner epoch 区分自身内存会话；Chrome 或会话服务重启后不得复用旧 token/Access，且必须清理或标记旧系统 target 不可用。IPC 仅允许本机进程调用固定方法，且不得暴露任意 CDP 命令、认证材料或完整上游响应。`controlled_chrome`、`game_circle_session`、Scheduler 和其他 Worker 的生命周期控制 SHALL 只允许固定的 start、stop、restart、status 和日志读取动作；Web 仍保持既有更严格控制。

#### Scenario: 会话 Worker 恢复
- **WHEN** `game_circle_session` 被停止、重启或 Supervisor 自身恢复
- **THEN** 它 SHALL 从既有 Job checkpoint/队列状态继续认领探测和刷新任务，并以新 owner epoch 建立会话
- **THEN** 采集、指标、审核和媒体 Worker 不得接管 CDP、来源身份或会话生命周期
