## ADDED Requirements

### Requirement: 实例租约立即撤销与完整受管进程回收

Supervisor SHALL 通过仅供持锁当前实例调用的 `revoke_instance_lease(instance_id)`，在正常关闭、当前实例的全量重启和新实例接管时、停止任何受管 Web/Worker 前原子发布当前实例已失效的租约。该操作 MUST 先确认已发布租约仍属于调用实例，再以当前时间作为失效时间发布既有协议字段；不得以零 TTL 调用普通续约 API。租约撤销不得将任一服务的 `desired` 改为 `false`，不得删除运行态目录、控制令牌、会话材料或 Job checkpoint。撤销写入失败时，Supervisor MUST 记录固定 `LEASE_REVOKE_WRITE_FAILED` 结果并继续终止可验证登记进程，但不得伪称租约已撤销。

Supervisor SHALL 按实例维护最小受管进程登记表。每条登记只可包含固定服务名、PID、进程创建时间、实例标识与服务代次；终止前 MUST 同时验证 PID、创建时间、规范化后的有序完整 argv 及规范化项目工作目录。Python 可执行文件和 `manage.py` 路径必须解析为固定绝对路径，参数顺序及固定 `--loop` 参数必须完全匹配；不得使用命令行子串、前缀、仅管理命令名、进程名、父 PID、端口、任意命令或系统扫描终止未登记进程。每次服务启动后 MUST 在发布当前服务状态前登记进程；登记或状态发布失败时 MUST 终止该次可验证启动的进程，且不得把它留给后续租约超时处理。正常关闭和新实例接管 MUST 回收同一实例或既有实例登记的全部可验证 Web/Worker 记录，并与服务当前 PID 去重。

进程登记缺失、PID 已退出、创建时间不符或命令不符时，Supervisor MUST 跳过该 PID 并记录固定脱敏结果；未登记的历史 Web/Worker 继续依赖租约守卫退出。受控 Chrome 仍只由既有专用端口与浏览器 generation 规则管理，不得因本登记表接管人工浏览器或任意其他端口。

#### Scenario: 正常关闭立即阻止遗留 Worker 开始新单元
- **WHEN** 当前 Supervisor 收到正常终止信号且存在由该实例登记的 Worker
- **THEN** 它 SHALL 先撤销当前实例租约，再按既有依赖顺序终止全部可验证登记进程
- **THEN** Worker 不得在租约撤销后开始新的 Job 或新的空队列循环，且持久化 `desired` 保持不变

#### Scenario: 重复启动后旧 PID 仍被完整回收
- **WHEN** 同一服务存在多个属于同一 Supervisor 实例且通过 PID、创建时间和固定命令验证的登记记录
- **THEN** 正常关闭或新实例接管 SHALL 终止全部这些记录，而非仅终止服务状态中的最后一个 PID
- **THEN** 不属于登记表或无法验证身份的进程不得被终止

#### Scenario: 启动状态发布失败不遗留新子进程
- **WHEN** Supervisor 已创建固定服务子进程，但登记表或服务状态原子发布失败
- **THEN** Supervisor SHALL 在受限时间内终止该可验证子进程并记录固定失败原因
- **THEN** 它不得将该服务视为已启动、继续调和或启动同职责替代进程

#### Scenario: 进程参数相似但身份不完整
- **WHEN** 登记 PID 存在但其创建时间、规范化有序 argv 或项目工作目录任一项与固定服务定义不一致
- **THEN** Supervisor SHALL 跳过该 PID 并记录固定脱敏结果
- **THEN** 它不得以子串、前缀或进程名相似为由终止该进程

### Requirement: 脱敏的 Supervisor 生命周期诊断

Supervisor SHALL 在受限 runtime 环形缓冲区发布固定服务的脱敏生命周期事件，并通过既有受认证本机控制通道向 Django 提供只读摘要。事件 SHALL 仅包含时间、服务名、事件类型、固定原因码、当前实例内服务代次、终止结果类别和有限的非敏感摘要；事件、HTTP 响应、日志和页面 MUST NOT 包含 PID、实例标识、命令行、路径、控制令牌、capability、Cookie、token、CDP target、完整 URL、二维码或来源响应。

事件类型 MUST 区分服务启动、优雅停止、超时强制停止、启动状态发布失败、实例接管回收和受控 Chrome 替换。原因码 MUST 来自固定 allow-list，至少包含 `BOOTSTRAP`、`DESIRED_START`、`OPERATOR_RESTART`、`TAKEOVER_RECLAIM`、`GRACEFUL_SHUTDOWN`、`LEASE_REVOKED`、`LEASE_REVOKE_WRITE_FAILED`、`TERMINATION_TIMEOUT`、`LAUNCH_STATE_PUBLISH_FAILED`、`CHROME_CDP_UNAVAILABLE`、`CHROME_GENERATION_CHANGED` 与 `SESSION_OWNER_REPLACEMENT`。Chrome 替换事件 MUST 另含固定检测结果类别（CDP 不可用、generation 不匹配或会话 owner 联动）及固定触发路径（启动恢复、调和健康检查、人工重启或实例接管）。事件缓冲达到上限时 SHALL 丢弃最旧事件；诊断写入失败不得改变服务生命周期决策或创建额外进程。

每个服务状态 SHALL 分别保存并展示当前 Supervisor 实例内启动次数和本机历史累计启动次数。实例标识变化时当前实例计数 MUST 从本次首次启动重新计算；历史 `restart_count` 存在时 MUST 作为累计诊断值兼容迁移，不得在页面中标注为当前实例的循环重启次数。

#### Scenario: Chrome 替换有明确原因而非仅累计次数
- **WHEN** Supervisor 因 CDP 不可用、generation 不匹配、会话 owner 联动或人工重启替换受控 Chrome
- **THEN** 只读诊断 SHALL 记录一次 `controlled_chrome` 生命周期事件、对应固定原因码、检测结果类别、触发路径和当前实例内代次
- **THEN** 页面 SHALL 将其展示为 Chrome 进程替换，不得将历史累计次数呈现为本次重启次数

#### Scenario: 控制台只能读取脱敏事件摘要
- **WHEN** 已认证运营人员在系统配置查看服务运行追溯
- **THEN** Django SHALL 经既有受认证本机控制通道取得固定诊断摘要，并展示最近事件与当前实例内启动次数
- **THEN** 浏览器不得获得运行态文件路径、PID、实例标识、控制令牌、任意命令或浏览器/会话认证材料

#### Scenario: 诊断事件超过容量
- **WHEN** 生命周期事件达到受限缓冲容量后产生新事件
- **THEN** 系统 SHALL 保留最新有限事件并淘汰最旧事件
- **THEN** 事件淘汰不得重启服务、修改 `desired`、影响 Job 或扩大日志输出
