## ADDED Requirements

### Requirement: Web 与 Worker 的 Supervisor 租约守卫
Supervisor SHALL 在取得同一运行态目录的独占锁后创建随机且不可预测的实例标识，并在 `runtime/supervisor/` 原子发布短期实例租约。租约仅可包含协议版本、实例标识、续约时间和失效时间；不得包含认证材料、Cookie、CDP target、完整来源 URL、二维码目标、密码或手动进程 PID。Supervisor SHALL 在自身运行期间续约。

由 Supervisor 启动的 Web 和固定 Worker SHALL 使用同一租约守卫：每次心跳前确认租约实例标识匹配且未过期，再上报带实例标识的心跳；持续读取失败、实例标识变化或租约过期时，Worker SHALL 在当前受限处理单元完成后沿既有优雅退出路径停止。固定 Worker 的循环睡眠与单个可取消处理单元 MUST NOT 超过租约宽限期。手工运行且未注入 Supervisor 实例环境的 Web/Worker MUST NOT 启用该守卫。守卫不得接受外部提供的服务名、实例标识、命令、路径或 PID。

Web 的守卫 SHALL 仅在 Supervisor 注入实例环境时运行；租约失效时，它在 POSIX SHALL 请求标准终止信号，在 Windows SHALL 执行受控进程终止。Web 不得持有 Job、来源会话或上游写入，因此 Windows 终止不需要等待业务 checkpoint。

除人工 `stop` 外，Supervisor 正常退出、异常退出、租约失效和服务自退出均 MUST NOT 将持久化 `desired` 改为 `false`。

#### Scenario: Supervisor 正常关闭
- **WHEN** Supervisor 收到正常终止信号
- **THEN** 它 SHALL 按“业务 Worker、游戏圈会话、受控 Chrome、Web”的依赖顺序停止受管服务；Worker SHALL 优雅完成当前受限单元后退出，Web 在平台不支持可捕获终止信号时 SHALL 受控终止，并在受限超时后执行现有本机强制终止兜底
- **THEN** 它 SHALL 在全部停止尝试后才关闭控制通道和释放独占锁，且不得改写任何服务的 `desired`

#### Scenario: Supervisor 被异常终止
- **WHEN** Supervisor 无法执行正常关闭且实例租约不再续约
- **THEN** 匹配该实例的 Web 和 Worker SHALL 在租约宽限期后沿既有退出路径停止
- **THEN** 该退出不得依赖下一次 Supervisor 启动、不得修改 `desired`，且不得终止其他服务或浏览器

#### Scenario: Worker 在空队列等待时租约失效
- **WHEN** 固定 Worker 处于空队列轮询且 Supervisor 租约过期或实例标识变化
- **THEN** Worker SHALL 在不晚于一个租约宽限期的时间内结束等待、释放其 Worker 锁并退出
- **THEN** 它不得开始新的 Job、重复认领当前 Job 或修改 `desired`

#### Scenario: Windows Web 租约失效
- **WHEN** 由 Supervisor 启动的 Windows Web 进程发现租约失效
- **THEN** 它 SHALL 以受控进程终止结束，且页面不得将该平台行为展示为优雅业务完成
- **THEN** 该终止不得影响 Worker、Chrome、会话服务或持久化的 `desired`

#### Scenario: 新实例替代过期实例
- **WHEN** 新 Supervisor 获得独占锁并发布新的实例标识
- **THEN** 旧实例的 Web 和 Worker SHALL 在下一个守卫周期发现实例不匹配后退出
- **THEN** 新 Supervisor SHALL 在迁移、静态资源收集和其他预检前，于受限等待内确认端口和既有 Worker 锁已释放后才按 `desired` 启动替代服务；等待超时不得执行预检或并发启动同职责 Worker

## MODIFIED Requirements

### Requirement: 服务状态、日志与恢复
Supervisor SHALL 在 `runtime/supervisor/` 持久化每个托管服务的期望状态、当前实例标识、PID、创建时间、最近退出码、启动次数、带实例标识的心跳和日志位置。PID 只能作为经 PID 与创建时间共同确认的当前或既有受管服务终止句柄和诊断信息，不得单独证明跨实例服务归属或健康。Supervisor 重启后 SHALL 先获得独占锁、发布新实例租约，并在迁移、静态资源收集和其他预检前终止所有可证明身份的既有 Web/Worker（不以是否存在实例标识区分），再等待其释放端口和既有 Worker 锁，最后按 `desired` 恢复 Web 以及此前期望运行的 Worker；日志 SHALL 与 Web 进程生命周期解耦。

#### Scenario: Supervisor 重启后恢复服务
- **WHEN** Supervisor 在已有服务期望状态的情况下重新启动
- **THEN** 它 SHALL 先使旧实例的 Web/Worker 租约失效，并终止所有可证明身份的既有 Web/Worker（包括未写入实例标识的升级前服务），在受限等待后按 `desired` 启动当前实例服务
- **THEN** 页面 SHALL 能展示当前实例恢复后的状态与最近退出信息，且不得将旧实例 PID 显示为当前运行服务

#### Scenario: Worker 优雅停止
- **WHEN** Supervisor 停止或重启一个 Worker
- **THEN** 它 SHALL 先发送优雅终止信号并等待受限超时，再在必要时使用既有跨平台进程 API 强制终止记录的当前服务进程
- **THEN** 中断任务 SHALL 继续依赖既有锁和 checkpoint 恢复机制，且该停止不得影响其他服务
