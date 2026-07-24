## Context

游戏圈来源需要在受控 Chrome 的外层公众平台会话和游戏圈 iframe 来源身份同时有效时才可调用。会话服务已经具备受限 IPC、lease 和最小只读验证基础；本设计为这些能力定义单一 owner、状态机和自动化边界。

## Architecture

```text
Supervisor
  ├─ controlled_chrome
  └─ game_circle_session
       ├─ SessionBootstrap
       ├─ SessionRepair (optional)
       ├─ SessionKeepalive (optional Job consumer)
       ├─ lease registry
       └─ loopback IPC

Scheduler
  └─ creates only enabled SessionKeepalive Jobs

Web / business Workers
  └─ fixed IPC intents and source requests only
```

`game_circle_session` is the only process that may use CDP, hold outer/source credentials, create service-owned targets, bind identities, or publish leases. Credentials exist only in memory inside ACTIVE or RETIRING leases. IPC accepts fixed business intents and approved source parameters; it never accepts executable CDP inputs.

## Design Choices

### Why a single Bootstrap coordinator

会话建立涉及 HOME、外层登录、游戏选择、插件 Frame、来源身份和验证，它们共享同一份浏览器状态。将其拆成 Probe、Recover 和 Keepalive 的独立入口，会使每个入口都有创建候选 target、覆盖状态或误判登录态的机会。Bootstrap 把这些步骤固定为一个有界协调过程，Repair 与 Keepalive 只是在 control lane 中请求它，而不是各自实现第二套建会话流程。

### Why the control lane is global

浏览器中当前外层游戏是共享状态；不同 AppID 的 Frame 虽可同时保有 lease，但建立候选、切换外层游戏、reload、bind 和关闭 target 不能并行。全局 lane 串行化生命周期操作，AppID single-flight 只负责合并同 scope 的重复 Bootstrap。来源 IPC 不进入 lane：它先固定 lease，再在访问计数保护下完成来源请求，从而避免保活关闭正在使用的 Frame。

### Why login waiting is internal and non-blocking

扫码等待是服务状态，不是业务 Job。将其设计为内部短间隔观察能保证 Worker 心跳和 IPC 继续工作，也不会产生一个长期占用消费者的任务或一批轮询 Job。等待只观察已登记 target；它不会探测 tab 列表、创建候选页或读取认证材料。

### Why keepalive uses a candidate session

保活需要改变外层游戏以获得新的来源身份。它不能直接破坏已发布 Frame：新 HOME/Frame 作为候选完成 `A -> B -> A` 或直接对齐、身份绑定和最小读取后，才原子替换 lease。任何中转游戏不可用、后台建页失败或验证失败都保留旧 lease，结果是可解释的安全失败而不是“会话有效”的误报。

### Browser and owner lifecycle

Chrome and the session Worker have fate-shared ownership. Supervisor starts Chrome before the session Worker. Before replacing a session Worker for a restart, crash recovery or release, Supervisor stops Chrome, confirms that process exited, starts Chrome again, obtains a new browser generation, then starts the new Worker. The session Worker must not become a new owner of a still-running Chrome generation.

All target registration is owner-memory only. A registration contains the internal target reference, role, AppID, owner epoch and browser generation. Runtime projection contains only role, count, non-sensitive time and fixed category. A new owner clears all predecessor lease and login-wait metadata and never enumerates, identifies, reuses or closes predecessor targets.

Chrome 联动重启的最小操作顺序是：停止 session Worker 接受新 access 和控制请求，等待有界的已固定 access 释放；停止 Chrome；确认旧进程退出；启动 Chrome；确认新的 generation；启动新的 session Worker。超时只能记录固定脱敏状态，不能为了清理而关闭未知 target。该顺序既阻止跨 owner 接管，也避免新 Worker 连接到仍在关闭中的 CDP 端点。

### Unexpected controlled Chrome exit

受控 Chrome 意外退出不是可由仍存活的会话 Worker 独立修补的普通 target 故障。它使该 Worker 持有的 browser generation 与已登记 lease 不再可信；若只重新拉起 Chrome，默认关闭的 Repair 不会建立新 Frame，运行态会停留在 Frame 未就绪。Supervisor 发现 `controlled_chrome` 缺失且 `game_circle_session` 仍为 desired 时，必须走与会话服务替换相同的 owner 交接：终止当前 session Worker、启动并确认新的 Chrome generation，再启动新的 session Worker。这样启动 Bootstrap 才会在新的 owner 内存中建立 HOME 与 Frame。

会话服务替换可能包含有界的 Worker 停止与 Chrome generation 等待，不能在固定的短 HTTP 客户端超时内同步完成。Supervisor 控制面对此类重启应立即返回已提交状态，并在后台执行一次受锁保护的替换；重复请求只会遵循现有串行控制语义。返回已提交不代表 Frame 已 READY，运营人员仍以运行态为准。

### Repairing a broken lease

`SessionRepair` 发现当前 scope 的 HOME 或 PLUGIN target 丢失时，该 lease 不得继续阻止建立流程。Repair 必须显式请求候选替换，而不是复用“已有 ACTIVE lease 则直接返回”的普通 Bootstrap 分支：候选 HOME、Frame、身份绑定和最小只读验证完成后，才原子发布新 lease，并按既有退役规则回收旧 lease。

候选失败仍保留旧 lease 的审计与可访问计数语义，但不得把新建 HOME 留作无限累积的 `HOME_IDLE`。target 已被浏览器移除时，回收将其视为已关闭并清除当前 owner 登记；这不是接管未知 tab，也不改变活跃访问的关闭边界。

Repair 已确认旧 lease target 丢失时，该 lease 不再是可用性投影的依据。若候选替换失败，运行态必须为 Frame 未就绪并提供人工建立动作；它不能沿用“保活候选失败但旧 lease 完整”的 READY 投影。旧 lease 仍可保留到其访问与退役边界完成，但不会再对外声称来源可用。

### Chrome replacement failure containment

Chrome generation 等待属于单项依赖的暂时失败，不得终止 Supervisor 主循环或控制 HTTP 服务。会话 owner 替换在 Chrome 未能于有界时间内就绪时，必须保留服务期望状态、写入固定脱敏的启动失败状态，并让下一次 reconcile 重试；异步控制请求的“已提交”也不得把后台异常静默丢失。只有 Chrome generation 确认后才启动新的 session Worker。

Supervisor 将 owner 替换分为“已启动 replacement Chrome、等待 generation”与“启动新 session Worker”两个阶段。generation 等待超时后，后续 reconcile 只等待同一 replacement Chrome 或在其已退出时重新启动它；不得重复终止刚启动的 replacement Chrome。这样临时 CDP 就绪慢只表现为可观察、可重试的依赖失败，不会演变成浏览器反复重启。

## State Model

The runtime state describes the current `ActiveGameScope`, not a Job result.

| Outer state | Frame state | Meaning |
| --- | --- | --- |
| `BROWSER_UNAVAILABLE` | unknown | Chrome generation is unavailable |
| `BOOTSTRAPPING` | building | SessionBootstrap owns a bounded establishment attempt |
| `LOGIN_PENDING` | unready | A single service-owned HOME awaits manual scan |
| `OUTER_READY` | unready | Outer session is valid but no current game is selected |
| `OUTER_READY` | building | Current game Frame is being established |
| `OUTER_READY` | ready | ACTIVE lease for current scope passed minimum read verification |
| `OUTER_READY` | unready | Frame cannot currently be verified |

Only `ready` means an ACTIVE lease for the current scope exists. Missing lease, fresh service start, target failure and browser failure do not imply scan is required. Scan is required only when the registered HOME explicitly requires it or when the registered login window ends.

状态投影始终由当前 `ActiveGameScope` 选择。服务可以持有非当前 AppID 的已验证 lease，但它只能用于该 AppID 的来源请求，不能改变当前游戏的外层或 Frame 文案。当前游戏切换发生时，页面立即显示新 scope 的建立中或未就绪状态；已经冻结的旧 scope Keepalive 继续完成，但其成功不会覆盖新 scope 状态。

## SessionBootstrap

`SessionBootstrap` is the sole establishment coordinator. It is invoked by service startup, explicit login check, explicit Frame establishment, or explicit current-game selection. It is not a Scheduler Job and source requests never invoke it implicitly.

1. Read the current Chrome generation and acquire AppID single-flight ownership in the control lane.
2. Reuse only a current-owner HOME from the same generation; otherwise request a background HOME target.
3. Wait for the public-platform contract and inspect the outer session.
4. If manual scan is required, register or update `LOGIN_PENDING` and stop. If no current game exists, publish `OUTER_READY` / Frame unready and stop.
5. Read the current platform game. When it differs from the requested scope, switch only when the managed-game catalogue proves the target is selectable, then reload and read back the selected AppID and outer token.
6. Create or reuse a current-owner plugin target, wait for a stable iframe, bind the source identity and make the fixed one-page minimum read.
7. Atomically publish the HOME, Frame and short-lived source identity as the ACTIVE lease for the AppID.
8. Return a credential-free `BootstrapResult` that distinguishes published, scan-required, no-scope and failed outcomes. On failure, close every unpublished candidate HOME and Frame created or reused for this attempt, retain any old ACTIVE lease, and publish a fixed sanitised failure category.

An explicit login check ends after step 3. Establish Frame and startup perform all applicable steps. Manual keepalive adds its switch policy before step 5. These intents remain separate: login check never silently switches games or performs keepalive.

实现以显式 `BootstrapIntent` 区分建立 Frame、检查外层会话、修复损坏 Frame 与保活候选，而不是由多个布尔开关组合行为。对已有 ACTIVE lease，建立 Frame、外层检查和 Repair 先验证其已登记的 HOME/Frame：健康时复用而不创建额外 HOME，Repair 也不会主动轮换；损坏时才建立候选，并且候选失败不得继续投影 Frame ready。保活是唯一即使旧 lease 健康也强制创建候选的意图；其候选失败仅在旧 lease 仍健康时保留 ready 投影。

步骤 5 的切换前提是目录能够确认目标 AppID 对当前外层账号可管理且平台允许切换。切换后必须 reload 并重新回读 AppID 与外层 token；不得仅根据点击成功或旧页面缓存认定切换完成。步骤 6 的 iframe 初次出现可能短暂重载，稳定检查必须在该重载窗口后进行；上下文销毁、非 JSON 响应和最小读取失败都属于候选验证失败，不得发布 lease。

外层 HOME 在账号切换 reload 后仍是同一受管 target，但其旧 document 的隔离执行上下文已失效。适配器在安排 reload 后立即释放旧上下文；回读目标游戏和读取新 outer token 时必须在同一 HOME 上重新建立上下文。这样保活的 `A → B → A` 两次回读不会把中转页面的失效上下文误判成 target 故障，也不会因重试产生额外候选 HOME。

### Login wait

`LOGIN_PENDING` is an in-memory record containing only the service-owned target reference, browser generation, owner epoch, start/end time, attempt status and a fixed result category. The default is 900 seconds and configuration has a positive bounded whitelist range. A short service-internal loop observes only this registered target and must not block IPC, heartbeats or Job consumption.

Only an explicit “open login page” action may display or activate the page. A repeated action returns the already registered waiting page without creating or focusing another target. On successful scan, the same HOME becomes the Frame candidate. On timeout or failure, only that unpublished service-owned target may be closed. A Worker restart destroys the entire Chrome generation, clears `LOGIN_PENDING`, and begins a fresh bootstrap; it never restores a waiting target.

登录等待窗口到期、target 丢失或 generation 变化时，状态从 `LOGIN_PENDING` 退回 `FRAME_UNREADY` 并保留“需扫码”的可操作原因。服务不会自动创建下一张登录页；运营人员的下一次明确打开操作才会创建新的唯一页。这避免后台循环在桌面上不断制造需要人工处理的页面。

## Lease and Concurrency

All target lifecycle work flows through one session-service control lane: bootstrap, login-wait inspection, repair, keepalive and explicit control actions. AppID single-flight deduplicates only concurrent bootstrap requests with the same scope and establishment intent; a Keepalive candidate never joins an ordinary Frame-establishment attempt. The lane is global because outer-game selection and target lifetime share one browser session.

Source IPC first fixes an ACTIVE lease into a short-lived `SessionAccess`; it may then perform the authorised source read or already-confirmed write without target creation, reload, bind or close. Access accounting is released in `finally` for success, exception, timeout and cancellation.

来源身份失效、Frame 不存在或服务正在建立时，来源调用返回固定未就绪/恢复中类别。它可以观察 control lane 中既有 Bootstrap 的有界结果，但不得以自身失败为理由排入新的 Recover、切换游戏或建立 target。人工写操作在这种情况下保持待确认；即使随后 Bootstrap 成功，也必须由运营人员重新确认。

New lease publication is atomic. Before publication, the old lease continues to accept access. After publication, the old lease becomes RETIRING; its HOME and Frame close only after the configured retirement delay and a zero access count. The service maintenance loop reclaims eligible retiring leases independently of source-access completion. Service shutdown rejects new access and control work, waits a bounded time for existing access, then only closes current-owner targets.

## Optional SessionRepair

SessionRepair is a default-off service-internal loop. It reads only current-owner ACTIVE lease HOME and Frame state. Healthy targets update a sanitised check timestamp. Missing or invalid targets enqueue one AppID-deduplicated Bootstrap in the control lane. Repair never creates a BackgroundJob, never creates a probe-only page, and never opens a login page. If outer login requires scan, it exposes the scan-required state for an operator.

When Repair is disabled, a lost Frame is shown as unready and requires explicit operator recovery. During a Repair attempt, source calls either use a still-valid old ACTIVE lease or receive a bounded recovering/unready result.

## Optional SessionKeepalive

SessionKeepalive is default-off and is the only periodic session Job. When enabled, Scheduler creates or reuses one `GAME_CIRCLE_SESSION_KEEPALIVE` Job per explicit-timezone four-hour bucket. Its initial data contains the bucket, `trigger=schedule` and `automation_key=session_keepalive`; it contains no execution scope or credentials. The idempotency key does not include AppID or scope version.

Upon first consumer ownership and before any browser action, the consumer atomically writes the current `ActiveGameScope` to checkpoint. Retries and interrupted recovery use that checkpoint unchanged. If no scope exists, it terminally records `NO_ACTIVE_GAME_SCOPE`; no CDP, target, repair or retry occurs.

For a frozen target scope, keepalive uses a new candidate session. If the outer game already equals the target, it selects a permitted non-target game and performs `A -> B -> A`; otherwise it directly aligns to the target. The candidate then follows Bootstrap validation and atomic publication. No permitted intermediate game, unavailable background target support or failed verification is a safe failure that preserves the old lease.

`keepalive` consumes the `BootstrapResult`: only `published` completes the Job. A scan-required or structurally non-selectable outcome is terminal with its fixed category; transient candidate/target/verification failures use the Job's finite retry policy. A failed candidate is never left at the intermediate game as reusable `HOME_IDLE`. If a retained old ACTIVE lease still matches the current scope, the runtime continues to project Frame ready while the Job audit records the failed refresh; otherwise it projects the fixed unready category.

Current-game selection after a Job has frozen scope schedules a separate Bootstrap for the new scope. The started Job completes and publishes its own scope lease; it is not cancelled, rewritten or prematurely retired. Runtime UI always selects the current scope, so old-scope completion never appears as current Frame readiness.

The automation switch governs future scheduler keys only. Existing scheduled Jobs and running Jobs preserve normal queue semantics; manual keepalive uses `trigger=manual`, has no automation key and is always available.

四小时 bucket 使用明确时区和稳定 key，而不是进程启动时间，避免 Supervisor 重启后在同一时段重复保活。Scheduler 不读取 Chrome、token 或游戏切换能力；它只读取会话维护策略并创建可审计的 Job。消费者在第一次执行才读取 current scope，因而排队期间的游戏切换不会生成同桶重复 Job，也不会把旧 AppID 固化进 Job 参数。

## Compatibility and Observability

The new Worker has no consumer for legacy login-probe and refresh task types. A release utility runs only after the old session Worker has stopped and released its lock. It terminalises active legacy Jobs with a fixed release reason, retains the Job row, parameters, checkpoint and audit history, and is idempotent. Recognition and utility support remain for the release plus one subsequent release.

Compatibility recognition is isolated inside that release utility. Product task-type registries, Scheduler code, Worker dispatch maps, Web endpoints, console labels and ordinary tests do not import or emit legacy task identifiers. This prevents a partially restarted deployment from creating a task that its new Worker cannot consume. The current-game console calls fixed IPC controls directly: selecting a game establishes its Frame, checking a session only checks the outer session, and manual keepalive creates the sole `GAME_CIRCLE_SESSION_KEEPALIVE` Job. The catalogue control is an explicit POST that reads the authorised catalogue through IPC and atomically refreshes the local cache; it is not a link to a second manual Admin action.

Web reads a sanitised projection containing outer state, Frame state, current-scope applicability, available actions, login-wait remaining time and fixed error category. It may poll this projection while visible but may not enqueue work, access CDP or mutate target state. Automatic state changes never use toast, modal, global message, navigation, system notification or focus.

控制台将“当前操作游戏”呈现为范围配置而非服务健康检查：主信息是选定游戏及其对采集、指标和人工操作的影响；范围版本仅保留审计用途。该节点只提供更换游戏（切换后建立对应 Frame）与目录刷新。外层检查归受控 Chrome 会话节点，手动保活和可选自动维护归游戏圈会话服务节点，避免同一会话能力在范围节点出现多个入口。

Logs record only state transitions, target roles, fixed categories, durations and counts. Credentials, target IDs, full URLs and source content are excluded from every persistence, log and response boundary.

## Minimum Verification

- Bootstrap: outer-ready, scan-required, scan completion using the same HOME, missing current scope, unsupported target game and minimum-read failure.
- Ownership: session-only restart stops Chrome, starts a new generation, destroys old service-owned targets and neither duplicates nor adopts tabs.
- Background behavior: every automatic target requests background creation; unsupported creation has no foreground fallback; automatic state changes have no UI interruption.
- Concurrency: simultaneous login wait, repair, keepalive and source IPC serialise lifecycle work; source access counters return to zero on all exits.
- Repair: disabled Repair creates no target; enabled Repair deduplicates recovery and preserves old lease until atomic replacement.
- Keepalive: disabled automation produces no future Jobs; existing Jobs still run; first-consumer scope freeze, no-scope terminal result, retry reuse, cross-scope completion and current-scope UI projection all hold.
- Release: legacy Jobs become auditable terminal records only after old Worker shutdown; repeated cleanup is idempotent.
