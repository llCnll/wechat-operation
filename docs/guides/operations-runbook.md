# 运行与恢复手册

## 部署边界

- 日常只启动一个 `uv run python -m service_supervisor`。Supervisor 是 Web、Scheduler、实时采集 Worker、人工补采 Worker、指标快照 Worker 与 AI Worker 的唯一进程所有者；它使用独占文件锁防止第二个实例接管同一运行态。
- 所有可变运行态均位于 `runtime/`：`runtime/.env`、`runtime/operations.sqlite3`、收集后的 `runtime/static/`、`runtime/worker-locks/`、`runtime/supervisor/` 与日志。`runtime/supervisor/` 包含控制令牌、服务状态、心跳和日志，代码包和版本控制均应排除整个目录。
- 数据库位于 `runtime/operations.sqlite3`。离线文件备份前必须停止 Web、Scheduler、实时采集 Worker、人工补采 Worker、AI Worker 和 AI 自动处置 Worker，再复制数据库文件及可能存在的 `-wal`、`-shm` 文件；只停止 Worker 不能保证没有 Web 写入。
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
  --exclude='./.ruff_cache' \
  --exclude='./.DS_Store' \
  -czf ../wechat-operation.tar.gz .
```

Windows PowerShell：

```powershell
$excluded = @('runtime', '.git', '.venv', '.pytest_cache', '.ruff_cache', '.DS_Store')
$items = Get-ChildItem -Force | Where-Object { $_.Name -notin $excluded }
Compress-Archive -Path $items.FullName -DestinationPath ../wechat-operation.zip -Force
```

解包后创建 `runtime/`，复制 `.env.example` 为 `runtime/.env`，再执行迁移与静态资源收集。

## 会话与密钥

- Chrome CDP 仅使用本地已登录浏览器；不保存 Cookie、令牌或运营者身份标识。管理详情中的发帖作者用户 ID仅可按产品定义保存到原创帖和作者禁言状态，用于禁言管理与历史关联，不得写入日志或导出。
- 首次部署执行 `mkdir -p runtime && cp .env.example runtime/.env`。日常 AI/OCR 和人工处置授权均从系统配置首页的 Pipeline 保存；`runtime/.env` 仅用于后台不可用时的运维恢复。
- AI/OCR 凭据只通过 `AI_*`、`OCR_*` 运行时配置注入。系统配置首页保存时已有密钥不会回显；轮换后由 Pipeline 重启 AI Worker。
- `ENABLE_UPSTREAM_WRITES` 与对应的 `ENABLE_SINK_WRITES`、`ENABLE_BLOCK_WRITES`、`ENABLE_MUTE_WRITES`、`ENABLE_UNMUTE_WRITES` 或 `ENABLE_RESTORE_WRITES` 必须同时为 `true` 才会发送对应的已验证请求；默认均关闭。禁言和解除禁言均在写前读取详情核验；写后固定立即回读及 2、5、10 秒补读（总等待不超过 20 秒），不会自动重复写入。解除禁言只在当前游戏处于禁言状态且经二次确认后发送请求。

### 禁言管理最小验证

1. 使用已授权、明确可查看的样本帖子点击“同步状态与指标”，确认单帖显示“状态未知”“已确认未禁言”“当前游戏禁言”或“全局禁言”之一；禁言管理仅显示仍有效的后两类状态。
2. 对同一作者的另一篇已同步帖子，确认禁言管理的“历史帖子”只返回当前游戏上下文的关联原创帖；切换游戏不得看到其他游戏的本地禁言。
3. 真实禁言属于高风险写操作：仅可对运营负责人明确授权的测试账号执行一次，记录目标帖子与操作者的内部工单关联。确认审计中仅有一次写入，随后详情回读最多四次、总等待不超过 20 秒；不得以重试方式补发写请求。写接口失败时即使状态已变化也必须保留 `ACTION_FAILED`。

### 一次性历史作者状态回填

需要补齐既有原创帖的作者用户 ID和禁言状态时，只入队一次性 Job；它由已运行的人工补采 Worker 串行消费，不会发起上游写入，也不会改写帖子正文、媒体、互动、审核或帖子状态：

```bash
uv run python manage.py enqueue_author_mute_status_backfill
```

任务使用固定幂等键，重复执行该命令会复用同一个任务；若任务失败或被取消，修复环境后再次执行命令会将同一任务重新置为待执行并保留游标。成功任务不会被重复全量执行；如确有重新全量读取的需要，应先与开发人员确认新的、有意版本化的任务，而不要手改任务记录。每帖最多读取一次管理详情；历史空 AppID 按默认游戏处理。上游已删除帖子会跳过，单帖读取失败会记录有限的脱敏样本并继续，Worker 中断后从最近帖子游标继续。

最小验证：在不包含真实作者身份信息的测试数据库中入队后运行一次人工补采 Worker，确认后台任务最终为 `SUCCEEDED`、checkpoint 的 `complete=true`，并抽查一篇帖子仅更新 `author_openid` 和作者禁言状态。生产执行前确认受控 Chrome 已登录，并检查该任务的 `error_summary` 与 checkpoint；不得将作者用户 ID写入命令行、日志或工单。

## 任务恢复

- `run_scheduler` 只创建任务，不认领业务任务；`run_realtime_ingestion_worker` 认领 `SCHEDULED_INGEST_WINDOW` 与 `STATUS_SYNC`，`run_backfill_ingestion_worker` 认领 `MANUAL_INGEST_WINDOW`、`METRICS_REFRESH_WINDOW`、`AUTHOR_MUTE_STATUS_BACKFILL` 和历史 `INGEST_WINDOW`，`run_metric_worker` 认领 `METRIC_DAILY_SYNC`、`METRIC_WEEKLY_SYNC` 与 `METRIC_BACKFILL`，`run_review_worker` 只认领 OCR/AI 任务，`run_video_extraction_worker` 只认领 `VIDEO_EXTRACT`；采集任务可从 checkpoint 继续。它们由 Supervisor 统一启停，页面不执行任意系统命令。`run_ingestion_worker` 保留为实时命令的兼容别名。
- 视频 Worker 默认关闭。工具安装、受控样本预检、ASR 降级和停止/回滚步骤见[视频审核手册](video-review-runbook.md)。失败时保持关闭，视频任务记录脱敏状态；仅空文字输入且媒体证据不足时进入重点人工核验。
- SQLite 中每个 Worker 职责都只能运行一个实例。实时采集和人工补采分别串行处理所属任务：长时间历史补采或范围互动刷新不会使后续 15 分钟采集、状态巡检积压。AI 审核 Worker 独立消费 OCR/AI 队列，不会被任一采集队列阻塞。
- 任务生产者为：自动调度器创建 `SCHEDULED_INGEST_WINDOW` 与 `STATUS_SYNC`；系统配置首页“帖子采集 Worker”的“创建历史补采”创建 `MANUAL_INGEST_WINDOW`；运营分析页创建 `METRICS_REFRESH_WINDOW`；`enqueue_author_mute_status_backfill` 仅创建一次性 `AUTHOR_MUTE_STATUS_BACKFILL`；原创帖采集、单帖刷新和人工重新评估创建 `OCR_EXTRACT` 与 `AI_EVALUATE`。`enqueue_ingestion` 仅保留为故障诊断入口。完整生产/消费图见[系统总览](system-overview.md#运行架构与任务流)。
- 契约变更或会话失效会保留已脱敏错误摘要。修复后重新排队，不应修改数据库中的会话字段。
- `/admin/` 仅用于系统配置与运行追溯：策略版本与策略规则可编辑，游戏目录只能通过受控 Chrome 同步；帖子、审核、AI/OCR、审计和后台任务不可手工修改。所有上游处置均从内容管理工作台执行，不能以 Admin 或直接修改数据库的方式绕过。

### 排障顺序

| 现象 | 首先检查 | 恢复方式 |
| --- | --- | --- |
| 没有新增帖子 | Pipeline 中 CDP 登录态、Scheduler 与实时采集 Worker 是否运行 | 恢复受控 Chrome 登录后在 Pipeline 重启帖子采集 Worker；停机超过 30 分钟时在“帖子采集 Worker”创建历史补采。 |
| 顶部 `CDP` 状态胶囊为红色 | Chrome 是否以 `9222` 调试端口启动 | 已登录本系统的运营人员可点击红色胶囊，只在受控 Chrome 打开固定插件登录页后手动扫码；若提示 CDP 不可用，先启动受控 Chrome，绝不在工作台浏览器直接登录。 |
| 任务长期 `PENDING` | 任务的 `task_type` 与 Pipeline 对应 Worker | 在 Pipeline 启动帖子采集 Worker（会同时启动两个采集服务）或 AI Worker；不要并行启动第二个相同职责实例。 |
| 任务 `FAILED` | 系统配置与追溯台“后台任务”的脱敏 `error_summary` 与 checkpoint | 修复会话、配置或上游契约后重新入队；不要修改会话字段。 |
| AI/OCR 任务失败 | Pipeline 中的 AI/OCR 配置和 AI Worker 日志 | 在配置弹窗修正对应配置并重启 AI Worker；已采集帖子数据不受影响。 |
| 人工上游处置未执行 | 总开关、动作开关、会话与人工确认 | 确认批准配置后重启 Web，再由运营人员重新确认；不得通过 Worker 重试写操作。 |
| AI 自动处置未执行 | AI/人工双重开关、决定规则的自动许可与阈值、`EVIDENCE_MISSING`、`ai_moderation` Worker、24 小时配额 | 未授权或证据不足的结论保留人工审核；空文字输入还须确认决定依据能回链到当前图片 OCR 或视频时间证据。历史任务若仍含旧重点原因也必须保留人工审核；已授权任务在 Worker 恢复后重新复核，不得手工重试已发出的写请求。 |

AI 自动沉底或屏蔽成功后，运营人员必须在内容管理的“AI 操作复审”中填写说明并确认正确，或标记异常。标记异常只会升级人工跟进，不会自动回滚；确需纠正时，进入原有工作台执行“恢复正常”的人工二次确认。只有该恢复成功且完成状态回读后，复审记录才会标记为“已人工恢复”。

## 自动采集调度

自动调度只覆盖城主别慌张（`wx6d2454a8afcd9706`）。在系统配置 Pipeline 启动 Scheduler；它由 Supervisor 托管，不应另行执行常驻 Scheduler 命令。

它按北京时间每 15 分钟时间桶先入队一项最多回读 20 条待审核帖的状态巡检，再入队最近 30 分钟的 `SCHEDULED_INGEST_WINDOW`；每日 07:30 后还会创建前一日为主、复核近三日的 `METRIC_DAILY_SYNC`，周一 17:30 后创建已完成周为主、复核近三周的 `METRIC_WEEKLY_SYNC`。Scheduler 不连接 CDP、不读取登录态、不执行 AI 或上游写操作，必须保持实时采集 Worker、指标快照 Worker 和 AI Worker 运行以消费任务。`--once` 仅用于排障或验证当前时间桶；不再配置 cron 或 `launchd`。

同一时间桶的重复检查会复用已有任务，但 SQLite 部署仍只能运行一个 Scheduler Worker。可通过服务日志检查 AppID、窗口和任务编号。

### 漏采处理

定时器停机超过 30 分钟时，在系统配置首页的“帖子采集 Worker”点击“创建历史补采”，选择可管理游戏并输入最近小时数（1 至 744）。任务由人工补采 Worker 异步处理，不会阻塞实时采集。

仅在后台不可用或排障脚本需要复现入队行为时，才使用人工窗口命令；例如补拉最近 6 小时：

```bash
uv run python manage.py enqueue_ingestion --appid wx6d2454a8afcd9706 --hours 6
```

确认任务已入队后保持 Pipeline 中的帖子采集 Worker 运行；该节点会同时启动实时采集和人工补采两个进程。会话失效、契约错误或任务耗尽重试次数时，先修复 Chrome 登录或上游适配器，再创建新的补采窗口；不要将 Cookie、令牌或完整认证 URL 写入命令、日志或数据库。

## 运营分析互动刷新

运营人员可在 `/reports/` 的“运营分析”中选择最多 31 个北京时间自然日，并点击“刷新本范围数据”。该操作只入队 `METRICS_REFRESH_WINDOW`，不会在 Web 请求内连接 Chrome。人工补采 Worker 按 `topic_id` 保存 checkpoint，成功帖子新增互动快照；原平台已删除的帖子会计入跳过数，传输或契约异常会记录失败帖子样本并继续处理其余帖子。再次手动触发同一范围会创建新任务并重新尝试失败帖子。刷新不修改帖子正文、媒体、投票、审核状态或任何上游状态。

## PostgreSQL 迁移阈值

当需要为任一 Worker 水平扩容、SQLite 写锁持续出现、数据库备份窗口不可接受或需要高可用时，应迁移 PostgreSQL。Django 迁移只能创建目标表结构，不能自动搬运 SQLite 业务数据：先停止所有进程并备份 SQLite，再制定显式的数据导出、导入与校验方案，切换连接配置后执行迁移，最后按 README 启动并验证 Web、Scheduler 和两个专用 Worker。

## 相关文档

- [文档中心](../README.md)
- [系统总览](system-overview.md)
- [视频审核手册](video-review-runbook.md)
- [数据库数据字典](../technical/database-schema.md)
