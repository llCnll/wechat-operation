## Context

当前部署需要分别启动 Web、Scheduler、采集 Worker 与 AI Worker，并依赖 README 判断迁移、默认策略、AI/OCR、CDP 与上游写开关是否已完成。Django Web 进程不能可靠管理自身生命周期；若由它直接托管 Worker，Web 重启后会失去子进程管理上下文，也无法安全地完成自身停止和端口交接。

系统是运行在单台运营设备上的 Django + SQLite 工具，需同时支持 macOS、Linux 和 Windows。已有运行态统一存放在 `runtime/`，且已有 `/healthz`、`/auth/status`、Worker 锁和任务状态可作为就绪检测基础。

## Goals / Non-Goals

**Goals:**

- 提供独立于 Django Web 的本机 Supervisor，作为 Web、Scheduler、实时采集 Worker、人工补采 Worker 和 AI Worker 的唯一父进程。
- 将日常启动收敛为启动 Supervisor；它安全执行幂等预检后启动 Web，Web 可用后由配置页引导其余步骤。
- 在系统配置页 Hero 下方展示全宽、按依赖排序的初始化与运行状态 Pipeline，给出唯一下一步和受限操作。
- 让配置页能够保存白名单配置、检查 CDP、启停或重启 Worker、查看脱敏日志，并在 Web 重启后保持服务状态可追溯。
- 保证浏览器、数据库和日志均不接触 Supervisor 凭据、AI/OCR 密钥、Cookie、令牌或任意命令执行能力。

**Non-Goals:**

- 不实现分布式编排、容器平台、远程主机控制、多个 Web 实例或 SQLite 上的 Worker 横向扩容。
- 不在系统配置页提供任意 Shell、进程 PID 编辑或自定义 Worker 命令。
- 不提供 Web 的“停止”或“启动”按钮；Web 不存在时页面无法成为恢复入口。页面只允许请求 Supervisor 延迟重启 Web。
- 不替换现有 Django Admin 认证，也不在本 Change 设计多角色权限、首次管理员注册或密码管理；首次管理员仍是 README 约束的手工首装前置条件。

## Decisions

### 1. 独立 Supervisor 作为唯一进程所有者

新增独立 Python Supervisor 入口，而不是 Django 视图调用 `subprocess.Popen`。Supervisor 在前台或由操作系统服务托管，负责创建、终止、重启并等待五个固定服务：`web`、`scheduler`、`ingestion`、`backfill_ingestion`、`review`。`ingestion` 保持既有服务名以复用已持久化的 Supervisor 期望状态，但其命令改为实时采集 Worker；`backfill_ingestion` 是新增的人工补采 Worker。Supervisor 启动时创建 `runtime/`，依次执行迁移、静态资源收集和默认策略初始化等幂等预检，再拉起 Web 并等待 `/healthz` 成功。

选择该方案是因为 Supervisor 在 Web 重启期间仍存活，能保留子进程、日志、退出码和期望状态；同一进程模型可跨三个目标桌面系统实现。替代方案是 Web 直接拉起 Worker，它只适合 Worker 控制，不能解决 Web 自身生命周期和孤儿进程问题。

### 2. 固定服务清单与持久化期望状态

Supervisor 使用独占的跨平台运行态文件锁保证单实例；无法取得锁时只报告已有实例信息，不执行预检、写状态或管理子进程。Supervisor 对 `web` 仅接受 `status`、`logs` 与延迟 `restart`；对四个 Worker 才接受完整的 `start`、`stop`、`restart`、`status`、`logs` 操作。它将服务 PID、创建时间、期望运行状态、最近退出码、启动次数、日志路径和心跳写入 `runtime/supervisor/`；重启 Supervisor 后读取期望状态，自动恢复 Web 以及此前由运营人员启用的 Worker。

进程命令使用当前受管 Python 与固定 `manage.py` 子命令，不传递来自页面的命令行。每个服务的标准输出和错误输出写入独立滚动日志文件。选择文件锁而不是持有 SQLite 数据库锁，是因为后者会长期阻塞业务写入；替代方案是仅扫描系统进程，它不能可靠区分 PID 复用、其他项目或手工启动的命令。

### 3. Django 仅作受认证的控制代理

Supervisor 只监听本机回环地址，并在首次启动时生成高熵控制令牌到权限受限的 `runtime/supervisor/` 文件。Django 后端读取该文件，通过内部 Supervisor 客户端调用固定操作；浏览器只向已认证 Django 视图发起 CSRF 保护请求，永不看到令牌或 Supervisor 地址。

Web 重启请求由 Supervisor 先确认接收，再延迟重启 Web 子进程，让 Django 能返回“正在重启”响应；前端轮询健康检查直至恢复。页面不提供停止 Web，因为停止后无法通过同一 Web UI 恢复。替代方案是浏览器直接访问本地 Supervisor；这会暴露控制令牌并绕过 Django 认证。

### 4. Pipeline 位于系统配置首页的首要位置

在 `/admin/` Hero 下方新增全宽“初始化与运行状态”区域，位于核心配置、运行健康和追溯数据之前。它按依赖显示：Web 状态、默认策略、AI/OCR、可选上游写入授权、CDP 登录、Scheduler、帖子采集 Worker、AI Worker 与整体就绪结果。帖子采集 Worker 是一个业务节点，内部展示实时采集和人工补采两个受管进程的状态、日志入口与异常原因；节点的启动、停止和重启操作会对两个进程执行对应的固定 Supervisor 操作，并通过受控弹窗创建人工历史补采任务。

页面默认突出唯一未满足的核心步骤；全部核心步骤就绪后收缩为紧凑摘要，运营人员可展开诊断。上游写入开关是高风险可选分支，不影响“核心审核能力就绪”。替代方案是放在内容管理工作台；该页面面向帖子处理，混入部署和进程控制会破坏其审核工作流。

### 5. 白名单配置写入与依赖感知重启

配置页仅能编辑 AI/OCR 的端点、模型、超时和密钥，以及显式列出的上游写开关。已有密钥仅显示“已配置”，提交空值代表保留；所有密钥输入只写入 `runtime/.env` 且不回显。保存后 Pipeline 标识受影响服务，并通过 Supervisor 提供“保存并重启”操作：AI/OCR 改动影响 `review`，上游写开关改动影响 `web`。

默认策略仍通过既有可编辑策略模型维护；Pipeline 只检查唯一启用策略和规则数量，不覆盖运营人员的策略。CDP 继续复用固定受控登录页打开能力，不读取二维码、Cookie、令牌或 `openid`。

### 6. 以任务类型而非参数来源隔离双采集消费者

自动调度器创建 `SCHEDULED_INGEST_WINDOW` 与 `STATUS_SYNC`，实时采集 Worker 专属消费这两类任务；系统配置首页帖子采集节点的“创建历史补采”创建 `MANUAL_INGEST_WINDOW`，运营分析页创建 `METRICS_REFRESH_WINDOW`，人工补采 Worker 专属消费后二者。历史补采仅可选择本地可管理游戏和最近 1 至 744 小时；`enqueue_ingestion` 保留为故障诊断入口。两类采集窗口复用同一个断点采集处理函数，只在 handler 映射上绑定不同任务类型，避免复制 CDP、分页和 checkpoint 逻辑。

既有存量 `INGEST_WINDOW` 不通过 AppID、时间窗口或参数猜测来源；它由人工补采 Worker 兼容消费，直到无存量后再评估移除兼容映射。两个 Worker 的 handler 集合不同，因此既有文件锁会生成不同锁名；启动、恢复中断任务和任务认领均按各自任务类型限定。该设计无需增加优先级字段、JSON 来源字段或全局限流器。

## Risks / Trade-offs

- [Supervisor 本身退出会使全部服务失去自动恢复] → 保留前台运行模式并提供可选的系统服务安装脚本；运行页明确显示 Supervisor 连通性和恢复命令。
- [多个 Supervisor 争抢服务状态或子进程] → 使用跨平台独占文件锁；未取得锁的实例不执行任何预检或服务操作。
- [自动预检可能在升级时延长启动时间] → 仅执行幂等迁移、静态资源收集和默认策略检查，记录阶段日志并在失败时不拉起 Web。
- [运行配置页面处理密钥] → 仅绑定回环地址、要求 Django 登录和 CSRF；使用密码输入，永不回显或写入应用日志。
- [停止/重启导致正在处理的任务中断] → Worker 接收优雅终止信号，Supervisor 设定超时后强制终止；任务既有 checkpoint 和锁机制负责恢复。
- [双采集 Worker 误领或重复消费任务] → 以互斥任务类型划分 handler 集合，分别恢复和认领；不以 `parameter_summary` 中的来源字段过滤。
- [升级时存在旧 `INGEST_WINDOW` 任务] → 人工补采 Worker 保留只读兼容映射，不进行不可验证的来源迁移。
- [Web 与 Supervisor 的控制协议失配] → 协议版本化，Django 连通失败时降级为只读状态并显示脱敏错误，绝不回退到直接执行系统命令。

## Migration Plan

1. 新增 Supervisor、运行态文件契约和本机控制客户端，并保留现有 Worker 任务契约。
2. 实现 Supervisor 启动命令、固定服务定义、日志与状态检测；保留现有直接 `manage.py` 命令用于开发和故障诊断。
3. 将新创建的自动采集和人工补采任务切换为独立任务类型；人工补采 Worker 兼容消费既有 `INGEST_WINDOW`，不修改其参数或检查点。
4. 在系统配置页新增 Pipeline、白名单配置表单与受认证控制端点；现有 Admin 配置和追溯入口保持不变。
5. 更新 README 和运行手册，将 Supervisor 设为日常唯一启动入口，并说明双采集 Worker 的职责、直接命令的诊断定位和旧任务兼容边界。
6. 回滚时停止 Supervisor 并按原有命令分别启动服务；`runtime/` 中的 Supervisor 状态与日志可删除，不影响业务数据库和任务数据。

## Open Questions

- 首版以 Supervisor 前台命令作为唯一启动方式；是否需要在同一 Change 中提供 macOS `launchd`、Linux `systemd` 和 Windows 任务计划程序的可选安装脚本，将在任务拆分时按实现成本决定。
