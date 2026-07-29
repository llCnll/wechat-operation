# 运行与恢复手册

## 部署边界

- 日常只启动一个 `uv run python -m service_supervisor`。Supervisor 是受控 Chrome、Web、Scheduler、游戏圈会话续接 Worker、实时采集 Worker、人工补采 Worker、指标快照 Worker、AI Worker 与禁言维护 Worker 的唯一进程所有者；它使用独占文件锁防止第二个实例接管同一运行态。
- 停止整套服务时，在启动 Supervisor 的终端按 `Ctrl+C` 并等待命令退出；不要只终止外层 `uv` 或直接关闭受控 Chrome。Supervisor 会按业务 Worker、会话、受控 Chrome、Web 的顺序停止当前服务。若 Supervisor 被强制终止，Web 和 Worker 会在本机实例租约过期后自行退出，`desired` 运行意图保持不变；遗留 Chrome 可以保留，下一次 Supervisor 启动会先清理专用 `127.0.0.1:9222` 再创建全新会话 owner。该端口只能用于本项目受控 Chrome。受控 Chrome 连续异常恢复三次失败后会停止自动重试，需查看脱敏日志后在页面手动重启。
- 所有可变运行态均位于 `runtime/`：`runtime/.env`、`runtime/operations.sqlite3`、收集后的 `runtime/static/`、`runtime/worker-locks/`、`runtime/supervisor/` 与日志。`runtime/supervisor/` 包含控制令牌、服务状态、实例租约、心跳和日志，代码包和版本控制均应排除整个目录。
- `instance-lease.json` 是 Web 和 Worker 用于确认 Supervisor 实例仍有效的短期租约。Windows 上短暂文件共享冲突会在原子替换时自动退避重试；出现 `Supervisor 租约续租失败` 或 `Supervisor 租约续租已恢复` 时，查看启动 Supervisor 的终端中脱敏的错误类型、系统错误码和尝试次数，不要复制或记录运行态文件内容。连续故障会立即记录首条，并最多每 60 秒输出一次累计失败汇总；恢复日志会带该次数。单次失败会跳过当轮服务调和，下一周期自动重试；若持续失败，既有租约不会被延长，Web 和 Worker 会在租约 TTL 到期后自行退出。此时检查磁盘空间、目录 ACL 与安全扫描/备份软件的文件占用，修复后让仍在运行的 Supervisor 自动恢复；若它已退出，按正常流程重启整套 Supervisor。不得手工删除、修改或替换活跃的 `runtime/supervisor/` 文件（包括 `instance-lease.json`、锁、心跳和 `tmp*`），以免使仍在运行的服务误判租约或破坏排障证据。
- 数据库位于 `runtime/operations.sqlite3`。离线文件备份前必须停止整个 Supervisor，再复制数据库文件及可能存在的 `-wal`、`-shm` 文件；只停止部分 Worker 不能保证没有 Web 或其他受管服务写入。
- 首次部署在启动 Supervisor 前，执行 Agent 必须手工运行 `uv run python manage.py migrate` 和 `uv run python manage.py createsuperuser`；不得记录管理员密码。后续迁移、静态资源收集和默认策略检查由 Supervisor 幂等执行。

### 打包

常规部署从与 `v<version>` 对应的 GitHub Release 下载 `wechat-content-operations-<version>-src.zip` 及其 `.sha256` 校验文件，不手工从工作区打包。源码 ZIP 由发布 tag 生成，只包含 Git 跟踪文件，并在上传前拒绝包含整个 `runtime/`；因此不会携带部署密钥、数据库、锁或生成的静态文件。

下载后的校验、解压、`runtime/` 初始化与锁定依赖安装见[发布与多端升级](release-and-upgrade.md#下载与验证-release-源码包)。首次使用某个自动 Release 时，必须完成文档列出的 SHA-256、`uv sync --locked --all-groups` 与 `validate_releases --require-current-tag` 最小验证。

只有在 GitHub Release 和工作流均不可用的受控故障恢复中，才允许本地打包；该包不应被标记为正式 Release 附件。打包代码时排除整个 `runtime/`：

macOS/Linux：

```bash
tar \
  --exclude='./runtime' \
  --exclude='./.git' \
  --exclude='./.venv' \
  --exclude='./.pytest_cache' \
  --exclude='./.pytest-runtime' \
  --exclude='./.ruff_cache' \
  --exclude='./.DS_Store' \
  -czf ../wechat-operation.tar.gz .
```

Windows PowerShell：

```powershell
$excluded = @('runtime', '.git', '.venv', '.pytest_cache', '.pytest-runtime', '.ruff_cache', '.DS_Store')
$items = Get-ChildItem -Force | Where-Object { $_.Name -notin $excluded }
Compress-Archive -Path $items.FullName -DestinationPath ../wechat-operation.zip -Force
```

解包后创建 `runtime/`，复制 `.env.example` 为 `runtime/.env`，再执行迁移与静态资源收集。

## 会话与密钥

- Chrome CDP 仅由 Supervisor 启动的受控 Chrome 使用；它采用独立 profile 且只监听固定的 `127.0.0.1:9222`，不保存 Cookie、令牌或运营者身份标识。Supervisor 以 CDP browser generation 判断 Chrome 健康，不能以启动器 PID 变化判定浏览器死亡；启动时优先请求 CDP 关闭旧浏览器，端口仍占用才终止监听进程。不得将其他浏览器或本机程序绑定到该端口。管理详情中的发帖作者用户 ID仅可按产品定义保存到原创帖和作者禁言状态，用于禁言管理与历史关联，不得写入日志或导出。
- `game_circle_session` 是唯一持有 CDP、outer token、来源身份 token 与系统自建 target 的常驻服务。Web、采集、指标、AI、回填与一次性任务只通过固定、仅监听 `127.0.0.1`、且由 Supervisor 注入独立 capability 的 IPC 提交 scope 和已批准的业务参数；IPC 不接受 CDP 命令、JavaScript、URL、target ID、路径或认证材料。无 capability 的连接在业务解析前拒绝；不得复制或记录 capability。它阻止没有运行时进程权限的本机调用方，但不能替代操作系统账户隔离。

受控 Chrome 中的“受控会话页”由该服务唯一登记。运营人员需要扫码、核对页面状态或在 Network 中观察 `getappinfo` 时，应从控制台点击“查看受控会话页”；检查外层会话和建立 Frame 都使用这同一页面。系统不会接管任意人工新开标签，也不会显示 target ID、完整 URL、Cookie 或 token。若该页显示扫码、token 在验证中消失，或 Frame 前复读 token 失败，控制台会显示会话未就绪，不能以历史检查成功继续处理来源操作。扫码等待期间提交检查或建立 Frame 时，控制台会明确提示该动作未执行，而不会显示为已完成。
- 会话服务按 AppID 维护一个 ACTIVE 及零到多个 RETIRING lease。新 lease 完整验证后才发布；旧 lease 仅在退役等待结束且请求访问归零后关闭它自己的 HOME/PLUGIN tab。因为 Chrome 已由 Supervisor 管理，受控 target 可以回收至零 tab；手动打开的未知 tab 永不接管、使用或关闭。
- 首次部署执行 `mkdir -p runtime && cp .env.example runtime/.env`。日常 AI/OCR 和人工处置授权均从系统配置首页的“按服务管理”保存；`runtime/.env` 仅用于后台不可用时的运维恢复。
- AI/OCR 凭据只通过 `AI_*`、`OCR_*` 运行时配置注入。系统配置首页保存时已有密钥不会回显；轮换后由服务卡重启 AI Worker。
- 运行配置保存后的重启提示使用与 Pipeline 一致的中文服务名；按提示定位对应节点并点击“重启”，无需识别内部 service key。
- `SESSION_REPAIR_ENABLED` 只需重启“游戏圈会话服务”和“自动调度器”；会话保活的启停和节奏在“自动任务生产者”保存后由下一轮 Scheduler 自动加载，无需重启。
- 启用自动修复后，当前 scope 已登记的 HOME 或 Frame 丢失会触发候选 lease 替换；外层会话已验证但当前 scope 没有 ACTIVE lease 时也会补建 Frame。候选验证成功才发布新 Frame；登录等待、需要重新登录或外层未验证时不会自动创建候选。零 lease 补建失败固定至少等待 15 秒再试，避免持续上游故障时反复创建候选。
- 会话服务启动、建立 Frame、检查外层会话、自动修复和保活使用明确的建立意图：健康 lease 会复用已登记 HOME/Frame，不会因重复点击创建空闲 HOME；已确认损坏时，候选失败显示“Frame 未就绪”，不得沿用旧的“Frame 有效”文案。
- session Worker 替换期间若 Chrome generation 在限定时间未就绪，Supervisor 保留 replacement Chrome 并记录可重试的启动失败；后续调和只等待该实例，除非它已退出，不会反复关闭并重开浏览器。
- `ENABLE_UPSTREAM_WRITES` 与对应的 `ENABLE_SINK_WRITES`、`ENABLE_BLOCK_WRITES`、`ENABLE_MUTE_WRITES`、`ENABLE_UNMUTE_WRITES` 或 `ENABLE_RESTORE_WRITES` 必须同时为 `true` 才会发送对应的已验证请求；默认均关闭。禁言和解除禁言均在写前读取详情核验；写后在第 2、5、8、10 秒回读（最长确认点为第 10 秒，总操作预算不超过 20 秒），不会自动重复写入。解除禁言只在当前游戏处于禁言状态且经二次确认后发送请求。
- 经认证、CSRF 二次确认的单条人工刷新或处置，以及当前游戏内最多 20 条的沉底/屏蔽批量操作，均可同步使用协调器已登记的插件会话以立即反馈结果；批量中的每条独立回读、审计与幂等确认。Web 不创建、扫描或关闭受协调器管理的 target，不切换游戏、不重建插件、不直接绑定身份。会话不可用时协调器仅返回固定未就绪结果；运营人员明确建立 Frame 后重新确认，绝不自动重放写入。人工扫码可显式打开固定、无认证登录页，但不读取认证信息或接管会话。

### 游戏圈会话操作路径

| 触发条件 | 系统操作者 | 运营人员动作 | 预期结果 |
| --- | --- | --- | --- |
| 受控 Chrome 被关闭或 session Worker 被替换 | Supervisor 将其视为 session owner 更换：重新启动受控 Chrome，再启动游戏圈会话服务；服务自动建立 HOME 与当前游戏 Frame。 | 无；等待状态从“建立中”收敛。 | 外层会话与 Frame 均有效时，来源任务可继续使用新 lease。 |
| 外层会话失效、显示“需扫码登录” | 会话服务登记唯一登录等待，不自动创建或聚焦登录页；扫码后复用同一 HOME 建立 Frame。 | 仅点击一次“打开登录页”并完成扫码，等待状态收敛；恢复中不重复点击。 | 成功前不会发布 lease；晚到的重复打开请求不会创建额外 HOME。 |
| 外层有效但 Frame 未就绪 | 启用自动修复时，会话服务再次验证外层后补建当前游戏 Frame；未启用时仅投影未就绪。 | 未启用自动修复时，点击顶部 Frame 状态或“建立当前游戏 Frame”。 | 建立并最小只读验证成功后，Frame 显示有效。 |
| 需要确认外层会话 | 会话服务只检查 HOME，不切换游戏、不建立候选 Frame。 | 在“按服务管理 → 游戏圈访问”点击“检查外层会话”。 | 保留既有 Frame 状态；Frame 已有效时仍提供保活入口。 |
| 需要主动刷新来源身份 | 游戏圈会话服务消费手动保活 Job，以候选会话执行刷新。 | 在“自动任务生产者 → 会话保活”点击“立即执行”。 | 只有新 lease 发布才成功；失败按有限重试记录 Job，旧 lease 继续可用。 |
| 需要更换业务游戏 | 当前操作游戏节点更新全局 scope，并请求对应 Frame 建立。 | 在“当前操作游戏”选择游戏并点击“切换并建立 Frame”。 | 后续任务使用新 scope；已经开始的 Job 保持其冻结 scope。 |

### 禁言管理最小验证

1. 使用已授权、明确可查看的样本帖子点击“同步状态与指标”，确认单帖显示“状态未知”“已确认未禁言”“当前游戏禁言”“全局禁言”或“当前游戏及全局禁言”之一；禁言管理会额外显示已明确设置自动续期意图、但正在等待 Worker 重新核验的作者。
2. 对同一作者的另一篇已同步帖子，确认禁言管理的“历史帖子”只返回当前游戏上下文的关联原创帖；切换游戏不得看到其他游戏的本地禁言。
3. 真实禁言属于高风险写操作：仅可对运营负责人明确授权的测试账号执行一次，记录作者与操作者的内部工单关联。确认审计中仅有一次写入，随后作者详情在写后第 2、5、8、10 秒最多回读四次；不得以重试方式补发写请求。删除或仅自己可见的来源帖不应导致作者操作读取帖子详情或被拒绝。写接口失败时即使状态已变化也必须保留 `ACTION_FAILED`。
4. “标记为自动续期”仅是本服务按“作者 + 当前游戏”维护的永久禁言意图：该操作只同步写入本地标记，不立即调用上游平台。禁言维护 Worker 由自动任务生产者的受限定时规则入队（默认每日 00:00、12:00），仅处理距离到期不足 5 天或当前游戏禁言时间为 0 的已标记作者；先以用户详情回读，若有效期仍至少 5 天则跳过，否则直接覆盖写入新的 30 天目标期限并回读，绝不先解除禁言。取消自动续期只停止续期，不会解除当前上游禁言。禁言管理的“刷新禁言列表用户状态”只入队一个只读任务，逐位刷新自动续期或仍有效禁言的列表作者；“扫描全量帖子作者，补充被禁言用户”则按作者去重扫描全量帖子，并只将仍禁言作者加入列表，不设置自动续期；“导入平台当前禁言名单”从 `page=0`、已验证的 `page_size=500` 开始读取平台当前名单，只投影昵称和当前游戏有效期，名单缺席不会解除本地状态。三者均不执行禁言或解除。需要人工续期时，在“自动任务生产者 → 永久禁言续期”执行“立即执行”；该操作只入队，Worker 未运行时任务保持等待。

候选帖子采集、当前窗口人工采集和历史补采统一使用 `page_size=100`；作者身份/禁言状态读取使用 `page=0,page_size=3`，只消费 `data.user`，不得保存或依赖返回的主题列表。平台禁言名单仍使用 500→10 自适应页大小，日/周指标仍使用 `per_page=366`。

### 一次性历史作者状态回填

需要补齐既有原创帖的作者用户 ID和禁言状态时，只入队一次性 Job；它由已运行的人工补采 Worker 串行消费，不会发起上游写入，也不会改写帖子正文、媒体、互动、审核或帖子状态：

```bash
uv run python manage.py enqueue_author_mute_status_backfill
```

任务使用固定幂等键，重复执行该命令会复用同一个任务；若任务失败或被取消，修复环境后再次执行命令会将同一任务重新置为待执行并保留游标。成功任务不会被重复全量执行；如确有重新全量读取的需要，应先与开发人员确认新的、有意版本化的任务，而不要手改任务记录。每帖最多读取一次管理详情；历史空 AppID 按默认游戏处理。上游已删除帖子会跳过，单帖读取失败会记录有限的脱敏样本并继续，Worker 中断后从最近帖子游标继续。

最小验证：在不包含真实作者身份信息的测试数据库中入队后运行一次人工补采 Worker，确认后台任务最终为 `SUCCEEDED`、checkpoint 的 `complete=true`，并抽查一篇帖子仅更新 `author_openid` 和作者禁言状态。生产执行前确认受控 Chrome 已登录，并检查该任务的 `error_summary` 与 checkpoint；不得将作者用户 ID写入命令行、日志或工单。

## 任务恢复

- Job 类型、生产者、消费者与调度时间表见[系统总览](system-overview.md)。所有 Worker 均由 Supervisor 启停；不要手工运行会话服务绕过 capability 注入。Supervisor 重启会先停止运行态中可确认身份的既有 Web/Worker，再按持久化 `desired` 恢复；不要在此期间手工拉起同职责进程。`run_ingestion_worker` 保留为实时命令的兼容别名。
- 视频 Worker 默认关闭。工具安装、受控样本预检、ASR 降级和停止/回滚步骤见[视频审核手册](video-review-runbook.md)。失败时保持关闭，视频任务记录脱敏状态；仅空文字输入且媒体证据不足时进入重点人工核验。
- SQLite 中每个 Worker 职责都只能运行一个实例。实时采集和人工补采分别串行处理所属任务：长时间历史补采或范围互动刷新不会使后续 15 分钟采集、状态巡检积压。AI 审核 Worker 独立消费 OCR/AI 队列，不会被任一采集队列阻塞。
- 契约变更或会话失效会保留已脱敏错误摘要。修复后重新排队，不应修改数据库中的会话字段。
- `/admin/` 仅用于系统配置与运行追溯：策略版本与策略规则可编辑，游戏目录只能通过受控 Chrome 同步；帖子、审核、AI/OCR、审计和后台任务不可手工修改。所有上游处置均从内容管理工作台执行，不能以 Admin 或直接修改数据库的方式绕过。

### 排障顺序

| 现象 | 首先检查 | 恢复方式 |
| --- | --- | --- |
| 没有新增帖子 | Pipeline 中游戏圈会话服务、受控 Chrome 会话、Scheduler 与实时采集 Worker 是否运行 | 先恢复游戏圈会话服务与受控 Chrome 登录，再在 Pipeline 重启帖子采集 Worker；停机超过 30 分钟时在“自动任务生产者 → 帖子采集 → 立即执行”创建历史补采。 |
| 顶部“外层会话”或“Frame”状态异常 | 先看文案：`正在恢复会话`、`需扫码登录`、`Chrome 未启动`、`Frame 未就绪` 或 `状态待检查` | “正在恢复会话”表示服务保留了自身新建的登录路由 target，先等待平台自动回到 Home；可点击入口将该 target 前台显示，只有稳定停留在扫码页时才需人工扫码。`Frame 未就绪` 使用“建立当前游戏 Frame”，其余状态不打开登录页。若 Chrome 未启动，在 Supervisor 中重启游戏圈会话服务；不要手工启动浏览器或在工作台浏览器直接登录。 |
| 来源任务提示等待会话刷新 | `game_circle_session` 服务、当前操作游戏和刷新 Job 的脱敏摘要 | 保持该服务运行；它会先从新 HOME 自动重建。仅摘要为“需要人工扫码”时，才在受控 Chrome 中扫码后重新创建/等待刷新任务；不得重放人工写操作。 |
| 任务长期 `PENDING` | 任务的 `task_type` 与 Pipeline 对应 Worker | 在 Pipeline 启动帖子采集 Worker（会同时启动两个采集服务）或 AI Worker；不要并行启动第二个相同职责实例。 |
| 任务 `FAILED` | 系统配置与追溯台“后台任务”的脱敏 `error_summary` 与 checkpoint | 修复会话、配置或上游契约后重新入队；不要修改会话字段。 |
| AI/OCR 任务失败 | Pipeline 中的 AI/OCR 配置和 AI Worker 日志 | 在配置弹窗修正对应配置并重启 AI Worker；已采集帖子数据不受影响。 |
| 人工上游处置未执行 | 总开关、动作开关、会话与人工确认 | 确认批准配置后重启 Web，再由运营人员重新确认；不得通过 Worker 重试写操作。 |
| AI 自动处置未执行 | AI/人工双重开关、决定规则的自动许可与阈值、`EVIDENCE_MISSING`、`ai_moderation` Worker、24 小时配额 | 未授权或证据不足的结论保留人工审核；空文字输入还须确认决定依据能回链到当前图片 OCR 或视频时间证据。历史任务若仍含旧重点原因也必须保留人工审核；已授权任务在 Worker 恢复后重新复核，不得手工重试已发出的写请求。 |

AI 自动沉底或屏蔽成功后，运营人员必须在“AI 操作复审”中直接选择最终帖子状态（保持、恢复、沉底、屏蔽）与作者决定（不变、禁言）。沉底、屏蔽直接覆盖目标状态，只有恢复才会恢复帖子；帖子步骤成功后才会执行作者禁言。任一步失败都保留待复审与独立审计，不自动回滚、补偿或重放；历史 `ESCALATED` 记录仍可从内容管理继续处理。

## 自动采集调度

自动调度仅针对系统配置中已选择的“当前操作游戏”，由 Supervisor 托管；不应另行执行常驻 Scheduler 命令。运营人员在系统配置台“自动任务生产者”中独立暂停/恢复生产者或规则，并在规则管理弹窗中新增、调整或删除运行时点：定时规则只填写 `HH:MM`，间隔规则只填写分钟数，系统按模板反推并保存受限表达式。调度统一使用中国标准时间，保存后最多 30 秒生效。删除规则只停止未来自动入队，仍保留已入队 Job 与逻辑槽审计；暂停也不取消已有或人工 Job。完整时间表见[系统总览](system-overview.md)。SQLite 部署仍只能运行一个 Scheduler Worker；`--once` 仅用于排障或验证当前时间槽，若 Scheduler 正在运行会因专用锁拒绝执行。

### 漏采处理

定时器停机超过 30 分钟时，在系统配置首页“自动任务生产者 → 帖子采集 → 立即执行”选择“历史补采（当前操作游戏）”，输入最近小时数（1 至 744）。任务由人工补采 Worker 异步处理，不会阻塞实时采集，也不会自动启动 Worker。

迁移说明：旧的 `/admin/runtime/session-keepalive/`、`/admin/runtime/permanent-mute-renewal/` 与 `/admin/runtime/ingestion/enqueue/` POST 入口已移除。自动化调用应改用受认证、CSRF 保护的生产者路由；历史补采使用 `/admin/scheduled-producers/post_ingestion/run/` 并提交 `mode=history` 与 `hours`，不得提交 `appid`。运营人员统一从对应自动任务生产者卡的“立即执行”操作进入。

仅在后台不可用或排障脚本需要复现入队行为时，才使用人工窗口命令；例如补拉最近 6 小时：

```bash
uv run python manage.py enqueue_ingestion --appid wx6d2454a8afcd9706 --hours 6
```

确认任务已入队后保持 Pipeline 中的帖子采集 Worker 运行；该节点会同时启动实时采集和人工补采两个进程。会话失效、契约错误或任务耗尽重试次数时，先修复 Chrome 登录或上游适配器，再创建新的补采窗口；不要将 Cookie、令牌或完整认证 URL 写入命令、日志或数据库。

## 运营分析互动刷新

运营人员可在 `/reports/` 的“运营分析”中选择最多 31 个北京时间自然日，并点击“刷新本范围数据”。该操作只入队 `METRICS_REFRESH_WINDOW`，不会在 Web 请求内连接 Chrome。人工补采 Worker 按 `topic_id` 保存 checkpoint，成功帖子新增互动快照；原平台已删除的帖子会计入跳过数，传输或契约异常会记录失败帖子样本并继续处理其余帖子。再次手动触发同一范围会创建新任务并重新尝试失败帖子。刷新不修改帖子正文、媒体、投票、审核状态或任何上游状态。

## 一次性作者状态回填

`uv run python manage.py enqueue_author_mute_status_backfill` 只创建或恢复 `AUTHOR_MUTE_STATUS_BACKFILL_V2`，由人工补采 Worker 消费。任务对已有作者身份按当前游戏去重读取固定作者详情，并补齐昵称和禁言状态；只有本地缺少作者身份时才读取帖子详情。已删除且缺少作者身份的帖子会以脱敏计数跳过，不会推断“未禁言”。

V2 检查点只保存范围、帖子游标、处理/跳过/失败计数和有限失败类别，不保存作者用户 ID、昵称、原始来源响应或认证信息。部署 V2 前必须确认没有待执行或运行中的旧 `AUTHOR_MUTE_STATUS_BACKFILL`；旧类型不被新版 Worker 认领，也没有 checkpoint 迁移路径。

## 一次性尾部话题回填

发布并完成迁移后，如需为历史原创帖生成独立尾部话题投影，执行：

```bash
uv run python manage.py enqueue_post_topic_backfill
```

该任务由人工补采 Worker 消费，只读取本地 `OriginalPost`，以主键游标和无身份聚合计数保存 checkpoint；不会建立浏览器会话、访问上游或修改正文/话题以外的字段。重复命令复用同一版本任务；失败或取消后再次执行会从原游标恢复。最小验证：在测试库创建一条正文末尾含 `#话题` 的原创帖，执行任务后确认正文去除尾行、`topics` 有序保存，且 checkpoint 为 `complete=true`。

内容管理的“话题”筛选默认不过滤；展开后可勾选“仅看带话题的帖子”，或输入一个完整标签（可带 `#`）进行单一精确匹配。话题不参与正文关键词搜索。筛选后的“同步筛选帖子”会复用同一筛选范围。

“帖子 ID”按当前游戏范围内的 `topic_id` 精确筛选，可与关键词、话题及其他筛选组合；它与工作台 URL 中用于选择当前详情的 `topic` 参数分离，避免影响上下条导航和复审返回链接。

## 官方作者与舆情报告 V2

在系统配置的“当前操作游戏”节点打开“管理官方作者”。选择本地可管理游戏后，只有点击“查询作者”才会通过会话服务读取一次作者详情；核对预览的 OpenID、昵称与可见帖子摘要后确认加入。确认和停用都只写本地配置，不执行任何上游写操作，也不会批量改写帖子或禁言状态。

新建 V2 舆情报告会锁定当时的正文、话题、AI 快照和官方身份，并在每个 Run 固定互动及已验证日指标。模型结论默认关闭；由系统配置中“舆情报告 Worker”的“配置舆情报告”弹窗管理。弹窗只切换模型结论开关，并只读显示共享 AI 连接是否完整；服务地址、模型名称和密钥仍在“AI 审核 Worker”的“配置 AI 审核”中统一维护，密钥不会回显。

保存开关后只需重启舆情报告 Worker。已完成的 Run 不会补写结论；应新建报告或“重跑固定来源”创建新的不可变 Run。最小验证应使用测试游戏和本地测试帖：先保持开关关闭，确认报告仍成功且配置页显示“未启用”；启用后确认仅提示重启舆情报告 Worker；再核对话题未进入热词、代表帖带锁定身份、日指标未跨来源汇总。模型请求不应由保存配置触发。

模型结论失败仍不影响确定性报告交付。报告详情、Word 与配置弹窗会显示固定脱敏分类，例如“模型响应超时”“模型鉴权失败”或“模型服务限流”；不得根据页面猜测或记录上游原始响应。已完成 Run 保留当时的失败代码，调整超时或连接配置后需重启 Worker 并创建新 Run 验证。

报告中的高互动代表帖会保留锁定时的官方身份。玩家内容可以作为玩家关注的核验线索；官方内容仅显示为“官方内容互动”，不应解读为玩家兴趣或玩家反馈，也不会送入模型结论。模型结论的“引用证据”以中文标签和固定摘要展示，方便核验；它们只来自当前 Run 的不可变结果，不查询当前帖子、作者或上游数据。

## PostgreSQL 迁移阈值

当需要为任一 Worker 水平扩容、SQLite 写锁持续出现、数据库备份窗口不可接受或需要高可用时，应迁移 PostgreSQL。Django 迁移只能创建目标表结构，不能自动搬运 SQLite 业务数据：先停止所有进程并备份 SQLite，再制定显式的数据导出、导入与校验方案，切换连接配置后执行迁移，最后按 README 启动并验证 Web、Scheduler 和两个专用 Worker。

## 相关文档

- [文档中心](../README.md)
- [系统总览](system-overview.md)
- [游戏圈会话韧性演练记录（2026-07-23）](session-resilience-drill-2026-07-23.md)
- [视频审核手册](video-review-runbook.md)
- [数据库数据字典](../technical/database-schema.md)
