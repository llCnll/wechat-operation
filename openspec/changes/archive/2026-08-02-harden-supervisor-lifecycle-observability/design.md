## Context

Supervisor 当前将每个服务的最后一个 PID、创建时间、累计 `restart_count` 与实例标识写入独立状态文件。正常关闭只终止状态文件中与当前实例匹配的单个 PID；租约守卫在 Worker 的循环边界检查实例标识和 TTL。该模式有两个可观测且安全性不足的空洞：

- 某服务重复启动而旧 PID 已被后一次状态覆盖时，旧进程不是正常关闭的直接目标，只能等待租约到期；正在执行的 Job 还会延后退出。
- Chrome 累计启动次数跨 Supervisor 实例累积，而状态和日志没有“谁、为何替换 Chrome”或“只是创建/关闭会话 target”的脱敏事件。Chrome Renderer、Updater 输出会进一步混淆人工判断。

本设计覆盖本机 macOS、Windows、Linux。Supervisor 仍是 Web、固定 Worker 和受控 Chrome 的唯一进程所有者；游戏圈会话服务仍是唯一 CDP owner。Web 不直接访问 CDP、控制令牌、进程 PID 或 target ID。

## Goals / Non-Goals

**Goals:**

- 正常关闭和实例接管在停止子服务前立即使旧实例租约不可用，使所有旧 Web/Worker 都不能开始新的循环单元。
- 用按实例登记的、经 PID 与创建时间验证的进程记录回收同一实例启动的全部 Web/Worker，而不是仅回收每服务最后一个 PID。
- 以固定原因码追溯服务/Chrome 生命周期，并以独立摘要追溯 service-owned HOME/Frame target 动作；两类事件在 UI 和日志中不可混淆。
- 显示本 Supervisor 实例内的启动次数、最近事件和有限历史，而不把跨实例累计次数误呈现为当前循环重启。
- 保持 `desired`、Job checkpoint、会话/IPC 安全边界和现有服务控制 API 的语义不变。

**Non-Goals:**

- 不扫描、终止或接管任意未登记的系统进程、用户 Chrome、其他端口或人工创建的浏览器 target。
- 不把 PID、实例标识、CDP target ID、完整 URL、token、Cookie、capability、命令行或上游响应呈现在 Web、日志、事件或数据库中。
- 不改变会话 Repair、Keepalive、登录或 Frame 建立的业务判断，也不因新增诊断额外创建、激活、关闭 target 或调用上游。
- 不实现跨机器进程协调、外部注册中心、守护进程、消息队列或新的外部依赖。

## Decisions

### 1. 用“立即撤销 + 协作退出”补齐租约，而非缩短 TTL 或依赖强杀

`RuntimeStore` 新增仅供持锁 Supervisor 调用的 `revoke_instance_lease(instance_id)`。它必须先确认运行态中实例标识仍等于调用方，再原子写入既有租约协议字段，并将失效时间设为当前时间；不得使用 `write_instance_lease(..., ttl_seconds=0)`，因为该 API 只允许正 TTL。正常关闭路径的第一个受状态锁保护动作调用此方法，随后才按依赖顺序终止已登记进程、关闭控制通道并释放独占锁。新实例取得锁后发布新实例租约，旧 Web/Worker 也会在下一次守卫检查中因实例不匹配退出。

这保留异常退出时 30 秒 TTL 的容错语义，同时使正常关闭不再等到 TTL 自然过期。若撤销写入失败，Supervisor 必须记录固定 `LEASE_REVOKE_WRITE_FAILED` 事件并继续终止可验证登记进程，但不得伪称租约已撤销；未登记历史进程继续等待最后一次 TTL。Worker 仍只在可取消处理单元边界退出，避免中途破坏数据库事务或来源写回；Supervisor 对已登记进程的现有优雅终止/超时强制终止仍是主要快速收敛手段。

**替代方案：** 将 TTL 缩短到 1--2 秒会放大 Windows 临时文件锁和短暂 I/O 故障的误退出风险，且无法中断长 Job；仅依赖 `SIGTERM` 仍无法覆盖状态丢失的旧进程。因此不采用。

### 2. 引入按实例的最小受管进程登记表

在 `runtime/supervisor/` 新增仅本机可读的进程登记状态。每条登记仅保存服务名、PID、进程创建时间、Supervisor 实例标识和登记代次；PID 必须始终与创建时间、规范化后的**有序完整 argv**及规范化项目工作目录共同校验，才可被终止。Python 可执行文件与 `manage.py` 路径必须在比较前解析为固定绝对路径，参数顺序和固定 `--loop` 参数必须完全匹配；不得使用命令行子串、前缀匹配或仅比较管理命令名。登记表按实例分组，并与每服务状态中的“当前进程”并存：前者用于完整回收，后者用于日常状态和控制。

`_launch` 在 `Popen` 成功后、发布服务当前状态前原子登记进程；若登记或状态发布失败，Supervisor 必须立即终止刚启动且可验证的进程，并记录固定失败事件，避免产生无登记孤儿。终止、退出确认或新实例回收后删除对应登记。关闭和启动前接管都枚举登记表中可验证的所有旧记录，并与每服务状态记录去重；不能验证的 PID 一律不处理，交由租约兜底。

**替代方案：** 以进程名或父 PID 扫描系统进程可覆盖未知孤儿，但存在误杀其他项目、PID 复用和跨平台差异，违反受控范围；为每个服务设置 OS 进程组会增加 Windows/macOS/Linux 语义差异且仍需 PID 验证。因此采用显式最小登记表。

### 3. 生命周期事件与服务状态分离，并使用固定原因码

新增有上限的运行态生命周期事件环形缓冲区。事件只包含：时间、服务名、事件类型、固定原因码、当前实例内代次、终止结果类别与可显示的脱敏摘要。Chrome 的替换事件由 Supervisor 记录；服务启动/优雅停止/超时强制停止/启动失败/接管回收也由 Supervisor 记录。

固定原因码至少包括：`BOOTSTRAP`、`DESIRED_START`、`OPERATOR_RESTART`、`TAKEOVER_RECLAIM`、`GRACEFUL_SHUTDOWN`、`LEASE_REVOKED`、`LEASE_REVOKE_WRITE_FAILED`、`TERMINATION_TIMEOUT`、`LAUNCH_STATE_PUBLISH_FAILED`、`CHROME_CDP_UNAVAILABLE`、`CHROME_GENERATION_CHANGED` 与 `SESSION_OWNER_REPLACEMENT`。Chrome 替换事件还必须写入固定检测结果类别（CDP 不可用、generation 不匹配或会话 owner 联动）和固定触发路径（启动恢复、调和健康检查、人工重启或实例接管）。未知异常只能归为固定类别，不得写入异常中的路径、命令、凭据或上游文本。

服务状态拆分为 `instance_launch_count`（实例改变时从本次首次启动重新计数）与 `lifetime_launch_count`（仅本机诊断累计）。旧 `restart_count` 在首次写入时迁移为累计字段；UI 不再将其显示为“当前重启次数”。事件和状态均通过既有原子 JSON 写入器发布。

**替代方案：** 仅扩展文本日志会受轮转影响，无法让控制台稳定显示；将事件存入业务数据库会把机器运行细节纳入备份、迁移和权限面。故采用受限 runtime 的有界、脱敏状态。

### 4. 会话服务只发布 target 动作摘要，不发布 target 身份

游戏圈会话服务在其既有 target 创建、关闭、复用、缺失和登录页激活路径中写入独立的有界摘要。每项只包含时间、动作类别（HOME/PLUGIN 的创建、关闭、复用或缺失）、触发意图（Bootstrap、Repair、Keepalive、人工打开、回收）和结果类别；不包含 target ID、AppID、URL、outer/source token、二维码或页面内容。

该摘要与 Supervisor 生命周期事件分别保存和显示。维护循环每两秒仍仅执行既有检查；写摘要不得调用 CDP 或改变控制锁、退避、target 所有权和页面焦点。可观察控制台将“Chrome 进程替换”和“会话页面动作”分区展示，从而解释“浏览器窗口/页面变化”并避免误称为进程重启。

**替代方案：** 把 target 动作伪装成 Chrome 重启会继续误导排障；保存 target ID 以便关联虽方便，但会扩大 CDP 运行态暴露面。均不采用。

### 5. 只读诊断投影经既有 Supervisor 控制通道提供

Supervisor 的受认证回环控制通道扩展固定的只读诊断摘要；Django 后端继续代为读取，浏览器仅获得已脱敏的服务/Chrome/target 摘要。控制操作端点、IPC capability 机制和服务 allow-list 不变。控制台显示最近事件、当前实例内启动次数、累计诊断次数及“最近 target 动作”，但不提供清空、手工编辑、任意 PID 操作或任意日志路径。

## Risks / Trade-offs

- [进程登记写入失败发生在 `Popen` 后] → 立即验证并终止刚启动的子进程，事件记录为固定失败原因；失败不得进入调和循环。
- [进程已退出或 PID 被复用] → 终止前同时校验 PID、创建时间、规范化有序完整 argv 和项目工作目录；任一不符即跳过且只记录脱敏结果。
- [租约撤销文件写入失败] → `revoke_instance_lease` 拒绝覆盖不匹配实例，写入失败时正常关闭继续对已登记进程执行终止并记录 `LEASE_REVOKE_WRITE_FAILED`；不得伪称租约已撤销或删除运行态文件。
- [高频会话维护写入大量事件] → 采用固定大小环形缓冲与同类连续动作聚合；诊断写入不触发额外 CDP 调用。
- [运行态文件被 Windows 共享锁短暂占用] → 复用既有原子替换和受限退避；读者只能见到完整旧值或新值。
- [历史状态兼容] → 缺失进程登记或新计数字段时按空登记、累计值迁移处理；不得扫描系统进程补偿。
- [跨平台语义差异] → 使用既有 psutil 终止与 PID/创建时间校验；Windows/Linux 的真实生命周期演练在 PRD 留作发布前待办，不以 macOS 结果替代。

## Migration Plan

1. 发布前运行隔离单元测试、Supervisor/会话定向测试、Django 检查和 OpenSpec 严格校验。
2. 部署时沿既有发布清单完整重启 Supervisor；新实例先发布新租约并安全接管可验证旧登记。不存在登记的历史 Worker 继续按旧租约机制退出。
3. 首次新实例写服务状态时，将既有 `restart_count` 保留为 `lifetime_launch_count`，并写入本实例计数；不需要数据库迁移或一次性 Job。
4. 通过控制台确认仅显示脱敏生命周期与 target 摘要，执行一次受控重启，验证旧 Worker 不开始新 Job、Chrome 进程替换与 target 动作被区分记录。
5. 回滚时恢复旧代码并完整重启 Supervisor；新增运行态字段为可忽略的附加字段，不删除 `runtime/supervisor/`，不复制会话材料。

## Open Questions

- 无阻塞实现问题；默认事件缓冲的具体容量将在实现中依据现有日志容量和页面密度固定为一个受限常量，并由测试覆盖其淘汰行为。
