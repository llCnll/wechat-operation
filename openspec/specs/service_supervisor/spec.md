# Purpose

定义 service_supervisor/ 的进程所有权、服务生命周期、运行状态、控制通道和恢复边界。

## Requirements

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

### Requirement: Web 与 Worker 的 Supervisor 租约守卫
Supervisor SHALL 在取得同一运行态目录的独占锁后创建随机且不可预测的实例标识，并在 `runtime/supervisor/` 原子发布短期实例租约。租约仅可包含协议版本、实例标识、续约时间和失效时间；不得包含认证材料、Cookie、CDP target、完整来源 URL、二维码目标、密码或手动进程 PID。Supervisor SHALL 在自身运行期间续约。

除人工 `stop` 外，Supervisor 正常退出、异常退出、租约失效和服务自退出均 MUST NOT 将持久化 `desired` 改为 `false`。

由 Supervisor 启动的 Web 和固定 Worker SHALL 使用同一租约守卫：每次心跳前确认租约实例标识匹配且未过期，再上报带实例标识的心跳；持续读取失败、实例标识变化或租约过期时，Worker SHALL 在当前受限处理单元完成后沿既有优雅退出路径停止。固定 Worker 的循环睡眠与单个可取消处理单元 MUST NOT 超过租约宽限期。手工运行且未注入 Supervisor 实例环境的 Web/Worker MUST NOT 启用该守卫。守卫不得接受外部提供的服务名、实例标识、命令、路径或 PID。

Web 的守卫 SHALL 仅在 Supervisor 注入实例环境时运行；租约失效时，它在 POSIX SHALL 请求标准终止信号，在 Windows SHALL 执行受控进程终止。Web 不得持有 Job、来源会话或上游写入，因此 Windows 终止不需要等待业务 checkpoint。

Supervisor 主循环中一次租约续租写入失败 SHALL 被视为可恢复运行态故障：它 MUST NOT 退出 Supervisor、调用全局关闭路径、主动终止受管服务或在该失败轮调和服务。Supervisor SHALL 记录脱敏故障，并在下一续租周期重新尝试。对于同一段连续失败，它 MUST 立即记录首条故障，之后最多每 60 秒记录一次包含累计失败次数的脱敏汇总；恢复事件 SHALL 包含该段累计失败次数。首次实例租约发布失败仍 MUST 使启动失败，且不得启动任一受管服务。

若续租持续失败，系统 MUST NOT 延长既有租约 TTL 或将过期租约视为有效；匹配该实例的 Web 和 Worker SHALL 继续按既有租约过期路径退出。续租恢复后，Supervisor SHALL 使用同一当前实例标识发布新租约，并在成功发布后的调和周期恢复既有服务监督。

#### Scenario: Supervisor 正常关闭
- **WHEN** Supervisor 收到正常终止信号
- **THEN** 它 SHALL 按“业务 Worker、游戏圈会话、受控 Chrome、Web”的依赖顺序停止受管服务；Worker SHALL 优雅完成当前受限单元后退出，Web 在平台不支持可捕获终止信号时 SHALL 受控终止，并在受限超时后执行现有本机强制终止兜底
- **THEN** 它 SHALL 在全部停止尝试后才关闭控制通道和释放独占锁

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

### Requirement: 实例租约立即撤销与完整受管进程回收

Supervisor SHALL 通过仅供持锁当前实例调用的 `revoke_instance_lease(instance_id)`，在正常关闭、当前实例全量重启和新实例接管时、停止任一受管 Web/Worker 前原子发布当前实例已失效的租约。该操作 MUST 先确认已发布租约仍属于调用实例，再以当前时间作为失效时间发布既有协议字段；不得以零 TTL 调用普通续约 API。租约撤销不得将任一服务的 `desired` 改为 `false`，不得删除运行态目录、控制令牌、会话材料或 Job checkpoint。撤销写入失败时，Supervisor MUST 记录固定 `LEASE_REVOKE_WRITE_FAILED` 结果并继续终止可验证登记进程，但不得伪称租约已撤销。

Supervisor SHALL 按实例维护最小受管进程登记表。每条登记仅包含固定服务名、PID、进程创建时间、实例标识与服务代次；终止前 MUST 同时验证 PID、创建时间、规范化有序完整 argv 及规范化项目工作目录。Python 可执行文件和 `manage.py` 路径必须解析为固定绝对路径，参数顺序及固定 `--loop` 参数必须完全匹配；不得使用命令行子串、前缀、仅管理命令名、进程名、父 PID、端口、任意命令或系统扫描终止未登记进程。每次服务启动后 MUST 在发布当前服务状态前登记进程；登记或状态发布失败时 MUST 终止该次可验证启动的进程。正常关闭和新实例接管 MUST 回收同一实例或既有实例登记的全部可验证 Web/Worker 记录，并与服务当前 PID 去重。

进程登记缺失、PID 已退出、创建时间不符或命令不符时，Supervisor MUST 跳过该 PID 并记录固定脱敏结果；未登记的历史 Web/Worker 继续依赖租约守卫退出。受控 Chrome 仍只由既有专用端口与浏览器 generation 规则管理，不得因本登记表接管人工浏览器或任意其他端口。

#### Scenario: 正常关闭立即阻止遗留 Worker 开始新单元
- **WHEN** 当前 Supervisor 收到正常终止信号且存在由该实例登记的 Worker
- **THEN** 它 SHALL 先撤销当前实例租约，再按既有依赖顺序终止全部可验证登记进程
- **THEN** Worker 不得在租约撤销后开始新的 Job 或新的空队列循环，且持久化 `desired` 保持不变

#### Scenario: 重复启动后旧 PID 仍被完整回收
- **WHEN** 同一服务存在多个属于同一 Supervisor 实例且通过 PID、创建时间和固定命令验证的登记记录
- **THEN** 正常关闭或新实例接管 SHALL 终止全部这些记录，而非仅终止服务状态中的最后一个 PID
- **THEN** 不属于登记表或无法验证身份的进程不得被终止

#### Scenario: 进程参数相似但身份不完整
- **WHEN** 登记 PID 存在但其创建时间、规范化有序 argv 或项目工作目录任一项与固定服务定义不一致
- **THEN** Supervisor SHALL 跳过该 PID 并记录固定脱敏结果
- **THEN** 它不得以子串、前缀或进程名相似为由终止该进程

### Requirement: 脱敏的 Supervisor 生命周期诊断

Supervisor SHALL 在受限 runtime 环形缓冲区发布固定服务的脱敏生命周期事件，并通过既有受认证本机控制通道向 Django 提供只读摘要。事件仅包含时间、服务名、事件类型、固定原因码、当前实例内服务代次、终止结果类别和有限非敏感摘要；事件、HTTP 响应、日志和页面 MUST NOT 包含 PID、实例标识、命令行、路径、控制令牌、capability、Cookie、token、CDP target、完整 URL、二维码或来源响应。

事件类型 MUST 区分服务启动、优雅停止、超时强制停止、启动状态发布失败、实例接管回收和受控 Chrome 替换。原因码 MUST 来自固定 allow-list，至少包含 `BOOTSTRAP`、`DESIRED_START`、`OPERATOR_RESTART`、`TAKEOVER_RECLAIM`、`GRACEFUL_SHUTDOWN`、`LEASE_REVOKED`、`LEASE_REVOKE_WRITE_FAILED`、`TERMINATION_TIMEOUT`、`LAUNCH_STATE_PUBLISH_FAILED`、`CHROME_CDP_UNAVAILABLE`、`CHROME_GENERATION_CHANGED` 与 `SESSION_OWNER_REPLACEMENT`。Chrome 替换事件还必须包含固定检测结果类别及触发路径。事件缓冲达到上限时 SHALL 丢弃最旧事件；诊断写入失败不得改变服务生命周期决策或创建额外进程。

每个服务状态 SHALL 分别保存并展示当前 Supervisor 实例内启动次数和本机历史累计启动次数。实例标识变化时当前实例计数 MUST 从本次首次启动重新计算；历史 `restart_count` 存在时 MUST 作为累计诊断值兼容迁移，不得在页面中标注为当前实例的循环重启次数。

#### Scenario: Chrome 替换有明确原因而非仅累计次数
- **WHEN** Supervisor 因 CDP 不可用、generation 不匹配、会话 owner 联动或人工重启替换受控 Chrome
- **THEN** 只读诊断 SHALL 记录一次 `controlled_chrome` 生命周期事件、对应固定原因码、检测结果类别、触发路径和当前实例内代次
- **THEN** 页面 SHALL 将其展示为 Chrome 进程替换，不得将历史累计次数呈现为本次重启次数

#### Scenario: 控制台只能读取脱敏事件摘要
- **WHEN** 已认证运营人员在系统配置查看服务运行追溯
- **THEN** Django SHALL 经既有受认证本机控制通道取得固定诊断摘要，并展示最近事件与当前实例内启动次数
- **THEN** 浏览器不得获得运行态文件路径、PID、实例标识、控制令牌、任意命令或浏览器/会话认证材料

### Requirement: 会话服务的受管生命周期

Supervisor SHALL 将受控浏览器与游戏圈会话服务作为固定受管服务。会话服务只有在受控浏览器可用时才可执行来源会话操作；两者均 SHALL 提供既有的运行态、日志、优雅停止和恢复能力。Supervisor 只可管理自身启动并记录的浏览器进程，不得接管手动浏览器。

会话服务重启后不得复用旧会话或身份材料。其本地调用接口只允许固定业务能力，不得暴露任意浏览器命令、认证材料或完整上游响应。

#### Scenario: 会话 Worker 恢复
- **WHEN** `game_circle_session` 被停止、重启或 Supervisor 自身恢复
- **THEN** 它 SHALL 以新 owner epoch 建立受控会话；仅当前已定义的 Bootstrap、登录等待检查、Repair 与 Keepalive 控制路径可恢复会话状态
- **THEN** 它不得认领或恢复已废弃的 `GAME_CIRCLE_LOGIN_PROBE` 与 `GAME_CIRCLE_SESSION_REFRESH` 任务
- **THEN** 采集、指标、审核和媒体 Worker 不得接管 CDP、来源身份或会话生命周期

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

### Requirement: 本机受认证控制通道
Supervisor SHALL 仅监听本机回环地址，并使用存放在受限 `runtime/` 文件中的高熵控制令牌认证 Django 后端请求。浏览器 SHALL 只能通过已认证且受 CSRF 保护的 Django 端点请求固定控制操作，且不得接收或持久化 Supervisor 地址、令牌或任意服务命令。

#### Scenario: 已登录运营人员请求重启 Web
- **WHEN** 已认证运营人员在系统配置页请求重启 Web
- **THEN** Django SHALL 通过受认证本机通道请求 Supervisor 延迟重启 Web，页面 SHALL 在连接短暂中断后轮询健康检查至恢复或显示脱敏失败状态

#### Scenario: Supervisor 控制通道不可用
- **WHEN** Django 无法连接 Supervisor 或令牌校验失败
- **THEN** 系统配置页 SHALL 以只读方式显示 Supervisor 不可用及脱敏错误，且 MUST NOT 直接执行本机进程命令

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

### Requirement: 单一运行快照与双视图投影
系统 SHALL 在一次系统配置首页请求中生成不含认证材料、来源响应、AppID、target ID、URL、Cookie、token、Job 参数或异常堆栈的单一运行快照。该快照 SHALL 包含固定服务状态、Supervisor 控制可达性、当前操作游戏是否存在、外层会话状态、当前游戏 Frame 状态及其本地范围版本匹配结果、审核配置与策略状态、可选能力状态及统一检查时间。

系统 SHALL 仅从该快照派生“按运行流程”和“按服务管理”两种视图以及控制台可用、来源可用、核心审核可用三个等级；模板与浏览器端不得重新推断服务依赖或就绪等级。

#### Scenario: 两个视图读取同一状态
- **WHEN** 已登录运营人员访问系统配置首页并在两个视图间切换
- **THEN** 两个视图 SHALL 显示同一检查周期内的同一服务、会话、范围和能力状态
- **THEN** 系统不得因视图切换创建 Job、调用 CDP、请求 Supervisor 控制或改变运行配置

#### Scenario: 状态暂不可用
- **WHEN** Supervisor 控制通道或某项运行状态在页面请求中不可用
- **THEN** 系统 SHALL 显示固定脱敏的“暂不可用”状态和可处理的恢复方向
- **THEN** 系统不得将未知、过期或外层会话有效状态推断为当前游戏 Frame 有效

#### Scenario: 陈旧会话投影
- **WHEN** 游戏圈运行态的 `checked_at` 超过十分钟，或受控 Chrome、游戏圈会话服务任一未运行
- **THEN** 系统 SHALL 将外层会话与 Frame 投影为未知或未就绪，并将受控游戏圈连接作为流程阻塞节点
- **THEN** 系统不得以持久化的 `OUTER_READY` 或 `READY` 继续显示来源可用或核心审核可用

### Requirement: 运行流程与服务管理
系统 SHALL 将运行流程固定展示为控制台与 Supervisor、受控游戏圈连接、当前操作游戏、当前游戏 Frame、审核能力配置、核心后台服务六个有序节点；流程 SHALL 突出第一个未满足的必需节点及其唯一推荐动作。可选能力未配置、可选 Worker 停止、指标快照 Worker 停止或禁言维护 Worker 停止不得阻塞来源可用或核心审核可用。

服务管理视图 SHALL 按基础托管、游戏圈访问、调度与业务 Worker、可选能力分组展示固定服务，并分别显示中文名称、用途、生命周期状态、业务前置条件、缺失行为和既有控制/日志/配置入口。业务条件缺失不得改变固定生命周期语义。

#### Scenario: 当前游戏没有有效 Frame
- **WHEN** 已选择当前操作游戏且外层会话有效，但当前 scope 没有 ACTIVE lease
- **THEN** 流程 SHALL 将当前游戏 Frame 显示为第一个阻塞节点，并提供建立当前游戏 Frame 的唯一恢复入口
- **THEN** 系统不得通过外层会话或旧 scope lease 把当前 Frame 显示为有效

#### Scenario: 切换范围后旧 lease 完成
- **WHEN** 运营人员切换当前操作游戏后，已冻结旧 scope 的保活或恢复在后台完成
- **THEN** 系统 SHALL 仅当其 `frame_scope_version` 匹配新当前 scope 时才显示当前游戏 Frame 有效
- **THEN** 旧 scope 结果不得使新范围的来源或核心审核能力变为有效

### Requirement: 运行控制入口唯一归属
系统 SHALL 为每个可变运行操作保留唯一主入口：登录页与外层会话检查归受控游戏圈连接；目录同步和当前操作游戏切换归工作范围；建立 Frame 和自动修复配置归游戏圈会话服务；服务生命周期控制和日志归服务管理视图；调度规则与固定生产者的一次性业务 Job 归自动任务生产者；其他需要运营人员选择业务参数的 Job 归对应业务页面。历史补采 MUST 位于帖子采集生产者的立即执行弹窗，不得位于帖子采集 Worker 生命周期卡或独立业务卡。流程视图可以链接至相应入口，但 MUST NOT 重复渲染同一 POST 表单、配置开关、业务 Job 创建按钮或服务控制按钮。

#### Scenario: 从服务视图执行固定操作
- **WHEN** 已认证运营人员在服务视图使用启动、停止、重启、日志或配置入口
- **THEN** 系统 SHALL 复用既有受 CSRF 保护的 Django 固定路由和 Supervisor 代理
- **THEN** 服务视图不得创建业务 Job、同步执行 Job handler 或隐式启动消费 Worker

#### Scenario: 从帖子采集立即执行创建历史补采
- **WHEN** 已认证运营人员在帖子采集生产者的立即执行弹窗提交历史补采
- **THEN** 系统 SHALL 仅通过固定、受 CSRF 保护的入队路由创建或复用 Job
- **THEN** 帖子采集 Worker 卡和独立人工补采卡不得同时渲染该业务 Job 创建入口

### Requirement: 受控游戏圈连接的可观察处置入口

运行控制台 SHALL 在受控游戏圈连接节点提供唯一的“查看受控会话页”入口，以及既有的外层检查与建立 Frame 入口。该页面是会话服务登记的唯一受控会话页；控制台不得暗示任意人工浏览器标签可作为会话事实来源，也不得展示 target ID、完整 URL、Cookie 或 token。

“查看受控会话页” SHALL 只提交固定的打开并执行共享外层观察动作；控制台不得在该 HTTP 操作中紧接提交独立 `CHECK_LOGIN`。反馈 SHALL 说明受控会话页已打开，并根据服务返回的脱敏运行态提示运营人员扫码、等待登录恢复或刷新状态；不得把仅完成页面激活表述为“已完成状态检查”。当前存在 ACTIVE lease 时，该入口 SHALL 保持现有会话与 Frame 状态；不存在 ACTIVE lease 且观察为无可用 token 时，入口 SHALL 使控制台显示 `LOGIN_PENDING`。显式外层检查 SHALL 复用会话服务已登记的同一页面，并遵守游戏圈会话连续性对 `LOGIN_PENDING` 的等待规则。

会话服务卡 SHALL 仅渲染运行态 `available_actions` 明确允许的检查外层、建立当前游戏 Frame 与保活操作。`LOGIN_PENDING` 时卡片只可提供查看受控会话页和不改变会话状态的服务管理操作；不得渲染可触发 Bootstrap 的操作按钮。

控制台处理固定检查或建立 Frame 回执时，若结果为 `LOGIN_PENDING`，SHALL 明确说明正在等待扫码且动作未执行；若结果为 `CONTROL_NOT_COMPLETED`，SHALL 明确说明动作未完成并引导运营人员查看运行态。控制台不得将这两种结果显示为成功。

#### Scenario: 会话页显示扫码

- **WHEN** 运行态表明受控会话页需要人工登录或正在 `LOGIN_PENDING`
- **THEN** 控制台 SHALL 推荐查看受控会话页并在该页扫码
- **THEN** 外层检查与 Frame 建立结果 SHALL 以该同一页面为准

#### Scenario: 查看会话页后登记等待

- **WHEN** 运营人员提交查看受控会话页动作
- **THEN** 控制台 SHALL 激活并由会话服务登记该同一页面的登录等待，不得紧接提交独立 `CHECK_LOGIN`
- **THEN** 页面尚未通过外层验证时，控制台 SHALL 显示等待扫码或等待登录恢复的固定脱敏状态，且不得创建第二张会话页

#### Scenario: 会话验证未就绪

- **WHEN** 已登记会话页 token 缺失、验证中导航或 Frame 前 token 复读失败
- **THEN** 控制台 SHALL 显示固定的会话未就绪或登录等待结果，不得继续显示会话有效
- **THEN** 控制台 SHALL 提供刷新状态和查看受控会话页的可处理入口

#### Scenario: 扫码等待时的会话服务卡

- **WHEN** 服务投影为 `LOGIN_PENDING`，且 `available_actions` 仅包含查看受控会话页
- **THEN** 控制台 SHALL 隐藏检查外层、建立 Frame 与保活按钮
