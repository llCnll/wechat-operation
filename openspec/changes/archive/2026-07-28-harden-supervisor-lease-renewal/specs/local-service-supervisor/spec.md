## ADDED Requirements

### Requirement: 运行时状态原子发布的短暂冲突恢复

Supervisor 运行态 JSON 的发布 SHALL 先写入同目录的完整临时文件，再原子替换目标文件。Windows 上遇到 `PermissionError` 且 `winerror` 为 `5` 或 `32` 的替换失败时，系统 SHALL 在受限总时长内以退避重试发布，且读者只能观察到完整旧值或完整新值。系统不得改为直接覆写目标文件。

重试耗尽或遇到不可重试错误时，系统 MUST 保留原始写入失败；尚未发布的临时文件 SHALL 被尽力清理，且清理错误不得覆盖原始失败。通用运行态写入器 MUST NOT 负责记录日志或包含运行态凭据、租约内容和环境变量的诊断。

#### Scenario: Windows 短暂共享锁在截止时间内释放
- **WHEN** Windows 上运行态目标文件的原子替换首次以 `winerror` 为 `5` 或 `32` 失败，但在受限重试时间内变为可替换
- **THEN** 系统 SHALL 发布原先已完整写入的值，并且目标文件不得出现半写入 JSON
- **THEN** 临时文件路径不得遗留，且调用方不得收到失败

#### Scenario: 共享冲突持续至重试耗尽
- **WHEN** 运行态目标文件在全部受限重试期间始终不可替换
- **THEN** 系统 SHALL 向调用方报告原始写入失败，并尽力删除未发布临时文件
- **THEN** 系统不得将失败伪装为成功；需要租约生命周期诊断的调用方 SHALL 在其自身语境中记录脱敏错误

## MODIFIED Requirements

### Requirement: Web 与 Worker 的 Supervisor 租约守卫
Supervisor SHALL 在取得同一运行态目录的独占锁后创建随机且不可预测的实例标识，并在 `runtime/supervisor/` 原子发布短期实例租约。租约仅可包含协议版本、实例标识、续约时间和失效时间；不得包含认证材料、Cookie、CDP target、完整来源 URL、二维码目标、密码或手动进程 PID。Supervisor SHALL 在自身运行期间续约。

由 Supervisor 启动的 Web 和固定 Worker SHALL 使用同一租约守卫：每次心跳前确认租约实例标识匹配且未过期，再上报带实例标识的心跳；持续读取失败、实例标识变化或租约过期时，Worker SHALL 在当前受限处理单元完成后沿既有优雅退出路径停止。固定 Worker 的循环睡眠与单个可取消处理单元 MUST NOT 超过租约宽限期。手工运行且未注入 Supervisor 实例环境的 Web/Worker MUST NOT 启用该守卫。守卫不得接受外部提供的服务名、实例标识、命令、路径或 PID。

Web 的守卫 SHALL 仅在 Supervisor 注入实例环境时运行；租约失效时，它在 POSIX SHALL 请求标准终止信号，在 Windows SHALL 执行受控进程终止。Web 不得持有 Job、来源会话或上游写入，因此 Windows 终止不需要等待业务 checkpoint。

Supervisor 主循环中一次租约续租写入失败 SHALL 被视为可恢复运行态故障：它 MUST NOT 退出 Supervisor、调用全局关闭路径、主动终止受管服务或在该失败轮调和服务。Supervisor SHALL 记录脱敏故障，并在下一续租周期重新尝试。对于同一段连续失败，它 MUST 立即记录首条故障，之后最多每 60 秒记录一次包含累计失败次数的脱敏汇总；恢复事件 SHALL 包含该段累计失败次数。首次实例租约发布失败仍 MUST 使启动失败，且不得启动任一受管服务。

若续租持续失败，系统 MUST NOT 延长既有租约 TTL 或将过期租约视为有效；匹配该实例的 Web 和 Worker SHALL 继续按既有租约过期路径退出。续租恢复后，Supervisor SHALL 使用同一当前实例标识发布新租约，并在成功发布后的调和周期恢复既有服务监督。

除人工 `stop` 外，Supervisor 正常退出、异常退出、租约失效和服务自退出均 MUST NOT 将持久化 `desired` 改为 `false`。

#### Scenario: Supervisor 正常关闭
- **WHEN** Supervisor 收到正常终止信号
- **THEN** 它 SHALL 按“业务 Worker、游戏圈会话、受控 Chrome、Web”的依赖顺序停止受管服务；Worker SHALL 优雅完成当前受限单元后退出，Web 在平台不支持可捕获终止信号时 SHALL 受控终止，并在受限超时后执行现有本机强制终止兜底
- **THEN** 它 SHALL 在全部停止尝试后才关闭控制通道和释放独占锁，且不得改写任何服务的 `desired`

#### Scenario: Supervisor 被异常终止
- **WHEN** Supervisor 无法执行正常关闭且实例租约不再续约
- **THEN** 匹配该实例的 Web 和 Worker SHALL 在租约宽限期后沿既有退出路径停止
- **THEN** 该退出不得依赖下一次 Supervisor 启动、不得修改 `desired`，且不得终止其他服务或浏览器

#### Scenario: 短暂续租写入失败
- **WHEN** 已运行 Supervisor 的一次租约续租在全部原子发布重试后仍失败，且最后一次成功租约尚未过期
- **THEN** Supervisor SHALL 保持运行、不得执行全局关闭或主动停止任何受管服务，并在下一周期再次尝试续租
- **THEN** 它 SHALL 跳过该失败周期的服务调和并记录脱敏失败诊断

#### Scenario: 续租恢复
- **WHEN** 前序续租失败后，Supervisor 在当前实例的后续周期成功发布租约
- **THEN** 新租约 SHALL 保持同一实例标识并具有既有 TTL
- **THEN** Supervisor SHALL 记录恢复事件并恢复后续服务调和，不得重启健康的受管服务

#### Scenario: 持续续租失败的日志限频
- **WHEN** 同一 Supervisor 实例的续租连续失败超过 60 秒
- **THEN** 它 SHALL 立即记录首条脱敏故障，之后每 60 秒最多记录一次包含累计失败次数的脱敏汇总
- **THEN** 续租恢复事件 SHALL 包含该连续失败段的累计失败次数，且不得暴露租约内容或凭据

#### Scenario: 持续续租失败导致租约过期
- **WHEN** Supervisor 持续无法发布租约，直至最后一次成功租约达到失效时间
- **THEN** Web 和 Worker SHALL 按既有租约失效语义停止，且 Supervisor 不得通过延长 TTL、写入伪造时间或改变实例标识使其继续运行
- **THEN** Supervisor 在存储恢复前不得在失败周期调和或反复启动这些服务

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
