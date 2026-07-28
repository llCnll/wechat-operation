## MODIFIED Requirements

### Requirement: Scheduler Worker 的运行边界
系统 SHALL 将 Scheduler、采集和审核作为三个任务类型隔离的常驻进程运行；SQLite 部署 SHALL 只运行一个 Scheduler Worker。一次性调度入口仅用于诊断和测试，不得作为生产环境的外部定时器依赖。

#### Scenario: Scheduler 中断恢复
- **WHEN** Scheduler Worker 停止后重新启动
- **THEN** 它 SHALL 在启动后检查当前生产者逻辑槽并按每条规则的错过触发策略继续调度，且同槽重复入队 SHALL 保持幂等

#### Scenario: 超过回看窗口的空洞
- **WHEN** 帖子生产者停止超过 30 分钟且其规则使用默认 `SKIP` 策略
- **THEN** 系统 SHALL 不自动扩大采集窗口，运营人员 MUST 在系统配置首页帖子采集生产者的立即执行弹窗创建受控历史补采窗口；直接管理命令仅用于诊断
