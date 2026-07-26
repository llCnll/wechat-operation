## Context

当前 Supervisor 每两秒执行一次调和，并以状态文件中的 PID、创建时间和命令行判断服务是否存活。该策略适用于直接运行的 Python Worker，但不适用于 macOS 上可能发生启动器 PID 交接的 Chrome：CDP 端点仍可访问时，保存的启动器 PID 已不再代表最终浏览器进程，调和将错误地替换 Chrome 与 `game_circle_session`。当前 `shutdown()` 只停止调和并释放锁、控制端点和日志句柄，未终止受管服务。

系统部署在 macOS、Windows 和 Linux。`runtime.write_worker_heartbeat()` 当前只向运行态写入服务时间，不能反向判断 Supervisor 是否仍存活；Web 心跳当前由 Supervisor 调和循环写入。Chrome 使用固定的专属 profile、`127.0.0.1:9222` CDP 地址和端口，部署约束明确该端口仅供本项目受控 Chrome 使用。`game_circle_session` 是 CDP、认证材料与 service-owned target 的唯一使用者。

运行态不得保存认证材料、Cookie、CDP target、完整来源 URL、二维码目标、密码或手动浏览器 PID。必须保留 `desired` 作为运营人员的持久化运行意图，不能因异常退出被清空。

## Goals / Non-Goals

**Goals:**

- Web 和所有 Worker 在 Supervisor 正常关闭时按依赖顺序停止，在 Supervisor 崩溃或被强杀后于有限宽限期内自行退出。
- 复用既有 Web/Worker 运行循环和心跳能力，不为每个服务新增一个常驻启动器或监护进程。
- 让 Chrome 健康由 CDP `browser_generation` 而非启动器 PID 决定，消除 macOS 空白页重启循环。
- 新 Supervisor 启动时确定性地清理专用 `9222` 端口并创建全新 Chrome/session owner；Chrome 在异常 Supervisor 退出后可以保留。
- 限制 Chrome 自动恢复次数，避免无法就绪时持续打开空白页。

**Non-Goals:**

- 不保证 Supervisor 被 SIGKILL 或断电时立即关闭 Chrome；这是明确接受的运行行为。
- 不接管、扫描或终止 `9222` 以外的浏览器和端口；也不支持将其他本机程序绑定到本项目的 `9222`。
- 不修改 CDP、登录、来源身份 token、Frame lease、人工扫码或 Job 业务语义。
- 不把 Supervisor 改造为系统级服务管理器，不增加通用 `managed_process_runner`、Windows Job Object 或下一次启动的全盘遗留进程扫描。

## Decisions

### 1. 将现有心跳升级为双向租约守卫

Supervisor 获得独占锁后生成随机 `instance_id`，并原子写入 `runtime/supervisor/instance-lease.json`。文件只包含协议版本、实例标识、最近续约时间与失效时间；Supervisor 每 5 秒续约，租约有效期为 30 秒。实例标识改变即使旧租约的绝对时间仍未到期，也立即使旧服务失效。

Worker 的公共循环在原有心跳位置调用统一的 `check_supervisor_lease_and_heartbeat()`：先读取租约，确认实例标识匹配且未过期，再写入带实例标识的心跳；连续读取失败、实例不匹配或租约过期时，返回“应停止”，由既有管理命令循环完成当前受限单元、释放锁和退出。每个循环的最大睡眠和单个可取消处理单元必须不大于租约宽限期，避免守卫仅在长时间阻塞后才生效。不得在业务 Worker 内新增各自的后台心跳线程。

Web 仅在被 Supervisor 注入实例环境时启动同一租约守卫线程。它以相同周期检查租约，失效后触发当前 Web 进程的受控终止；在 POSIX 使用标准终止信号，在 Windows 明确按进程终止处理，不把 `os.kill(..., SIGTERM)` 误述为可捕获的优雅信号。Web 不持有 Job、会话或上游写入，因此该平台差异不影响业务 checkpoint；手工运行 Web 未注入实例环境时不启用守卫。Web/Worker 都不能接受外部传入的租约路径、服务名或实例标识。

现有的单向 Worker 心跳不能在 Supervisor 死亡后执行停止，因此不足；给每个服务套独立 `managed_process_runner` 虽可统一处理，却额外引入服务层级、进程树和跨平台 Job Object 复杂度，故不采用。

### 2. `desired` 与实例生命周期分离

`desired` 仍只由明确的 start、stop、restart 控制意图更新。Supervisor 正常退出、异常终止、租约失效或 Web/Worker 自退出不得将它设为 `false`。服务状态写入启动实例标识、PID/创建时间、最近退出码和心跳；PID 仅用于当前 Supervisor 正常停止时的辅助终止与诊断，不得用于跨实例认领。

新 Supervisor 获取独占锁后先发布新租约，并对运行态中可由 PID、创建时间确认身份的全部既有 Web/Worker 执行一次受限终止；这一步不依赖其是否已写入实例标识，因此升级前的旧服务也会被统一收敛。随后仍等待旧服务退出、端口与既有 Worker 锁释放，才执行迁移、静态资源收集和其他预检，并依据 `desired` 启动新的服务。`controlled_chrome` 不走该 PID 收敛，而继续使用固定 `127.0.0.1:9222` 的专用清理。运行态无法证明身份的 PID 不得终止，避免误杀无关进程；等待超时不得执行预检或并发启动同职责 Worker。

不把异常退出时的 `desired` 清为 `false`，否则恢复需要人工逐一启动；不沿用旧 PID 作为健康状态，避免 PID 复用和旧进程并发。

### 3. Chrome 使用专用端口和 generation，不使用租约自退出

Chrome 不执行项目代码，无法读取租约，因此不参与异常退出后的自清理。Supervisor 正常关闭时，先停止 session Worker，再通过当前 `9222` 监听进程或记录 PID 停止 Chrome；PID 不可用时不得据此判定 Chrome 已死。

Supervisor 运行时，`controlled_chrome` 健康定义为：CDP 在 `127.0.0.1:9222` 可连接，且读取的 `browser_generation` 与当前状态保存的 generation 相同。启动器 PID 变化但 generation 未变时，Chrome 健康；CDP 不可连接或 generation 改变时才触发 Chrome/session 配对替换。generation 仅是浏览器代次标识，不记录 target、Cookie 或 token。

每次 Supervisor bootstrap 在启动 Chrome 前，先在 `127.0.0.1:9222` 的 CDP browser endpoint 发送 `Browser.close`，随后等待端口释放；若端口仍被监听，再使用跨平台本机进程 API 终止该监听 PID 并再次等待。该处理不检查或推断进程归属，安全性来自部署约束“该端口只供受控 Chrome 使用”。CDP 不可用且无法取得监听 PID、监听 PID 无法终止或端口未在限定时间内释放时，Supervisor 以固定脱敏错误退出并不启动新的 Chrome 或 session Worker。`Browser.close` 的连接在浏览器退出前断开属于预期现象，结果以端口是否释放为准。

不复用异常退出后遗留 Chrome：前任 session owner 已消失，复用会使新 Worker面对未知 target 和旧上下文；不引入 Chrome 专用监护器：遗留 Chrome 不执行业务任务，且下一次启动的一次性端口清理已足够。

### 4. 配对替换使用单一候选 generation 状态机和熔断

`game_circle_session` 仍与 Chrome 成对更换。状态机分为 `IDLE`、`LAUNCHING_CHROME`、`WAITING_FOR_GENERATION`、`LAUNCHING_SESSION` 和 `CIRCUIT_OPEN`。一次 replacement 只创建一个候选 Chrome；调和循环在 `WAITING_FOR_GENERATION` 中只验证同一候选，绝不再次终止、重开 Chrome 或创建 session Worker。

候选 Chrome 已明确退出时才可开始下一次尝试。每次 generation 就绪失败或候选异常退出累计一次；连续三次失败后进入 `CIRCUIT_OPEN`，停止自动启动，记录固定脱敏原因 `CHROME_RECOVERY_CIRCUIT_OPEN`。运营人员手动重启 Chrome 或会话服务会开始一个新 replacement 周期并重置计数；任何一次成功 generation 确认也会清零。

### 5. 正常关闭顺序与跨平台边界

正常关闭顺序固定为：`ai_moderation`、`video_extraction`、`review`、`metric_sync`、`backfill_ingestion`、`ingestion`、`mute_maintenance`、`scheduler`、`game_circle_session`、`controlled_chrome`、`web`。Supervisor 对自己直接启动且仍记录的服务先发优雅终止，受限超时后使用现有跨平台本机进程 API 强制终止；全部停止尝试完成后才关闭控制端点和释放锁。

异常退出不依赖 `shutdown()`：Web/Worker 的租约守卫自行退出；Chrome 可以继续运行，直至正常关闭、机器退出或下一次 Supervisor bootstrap 的固定端口清理。该取舍将跨平台复杂度限制在 Python 服务的协作退出和一个专用端口的本机终止，不将不必要的进程树监护扩展到所有服务。

## Risks / Trade-offs

- [Supervisor 短暂停顿导致 Worker/Web 误停] → 使用 5 秒续约、30 秒租约和连续读取或心跳写入失败门槛；一次读写失败不触发退出。
- [新旧 Supervisor 短暂重叠] → 独占锁先于预检和租约发布；新实例等待旧服务察觉实例变化并释放端口/Worker 锁，超时不并发拉起。
- [其他程序占用 `9222`] → 该程序会被启动清理终止；这是专用端口约束的明确代价。无法接受该约束的部署不得使用本模式。
- [Chrome 监听进程 PID 无法获取或无法终止] → 启动失败并报告固定脱敏错误，不改端口、不启动第二个 Chrome、不继续启动 session Worker。
- [租约文件被删除或损坏] → Web/Worker 在宽限期内将其视为不可续租并退出；新 Supervisor 取得锁后原子重建，不从损坏内容恢复 PID。
- [Worker 在守卫检查后长时间阻塞] → 管理命令必须保持现有受限单元和超时；本 change 不承诺中断不可取消的上游调用。

## Migration Plan

1. 实现实例租约与公共守卫，保持既有心跳文件的兼容读取；旧心跳无实例标识时仅用于历史显示，不作为当前服务健康证明。
2. 将所有固定 Worker 循环和 Supervisor 启动的 Web 接入守卫，明确 Windows Web 的受控终止语义，并在迁移、静态资源收集和其他预检前完成新实例替换等待。
3. 将 Chrome 健康改为 CDP generation，并在 bootstrap 前加入一次性 `9222` 清理；保留 PID 仅用于诊断和正常关闭。
4. 在不含真实认证材料的受控环境演练 macOS PID 交接、三个操作系统的租约退出、固定端口清理和熔断。
5. 更新运行手册后发布。回滚前正常停止新 Supervisor；若遗留 Chrome 占用 `9222`，按旧版本启动前的受控方式手工关闭，避免两个 session owner 同时运行。

## Open Questions

- Web 进程的租约守卫应放在 Django AppConfig 生命周期还是专用 `run_web` 管理命令中；实施时以不影响管理命令、测试进程和手工开发服务器为准。
- Windows 上获取固定 loopback 监听 PID 的权限/实现差异需要在受控环境演练；若当前用户无法解析监听 PID，启动必须失败而不是绕过专用端口清理。
