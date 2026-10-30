# 运行与恢复手册

## 部署边界

- 日常只启动一个 `uv run python -m service_supervisor`。Supervisor 是受控 Chrome、Web、Scheduler、游戏圈会话续接 Worker、实时采集 Worker、人工补采 Worker、指标快照 Worker、AI Worker 与禁言维护 Worker 的唯一进程所有者；它使用独占文件锁防止第二个实例接管同一运行态。
- 停止整套服务时，在启动 Supervisor 的终端按 `Ctrl+C` 并等待命令退出；不要只终止外层 `uv` 或直接关闭受控 Chrome。正常关闭会先立即撤销当前实例租约，再按业务 Worker、会话、受控 Chrome、Web 的顺序回收该实例登记且四重身份校验通过的全部 Web/Worker，`desired` 运行意图保持不变。若 Supervisor 被强制终止，Web 和 Worker 会在本机实例租约过期后自行退出；下一次 Supervisor 只回收已登记且 PID、创建时间、完整固定 argv 与项目工作目录均吻合的旧进程，绝不扫描或终止其他系统进程。遗留 Chrome 可以保留，下一次 Supervisor 会先清理专用 `127.0.0.1:9222` 再创建全新会话 owner。该端口只能用于本项目受控 Chrome。受控 Chrome 连续异常恢复三次失败后会停止自动重试，需查看脱敏日志后在页面手动重启。
- 所有可变运行态均位于 `runtime/`：`runtime/.env`、`runtime/operations.sqlite3`、收集后的 `runtime/static/`、`runtime/worker-locks/`、`runtime/supervisor/` 与日志。`runtime/supervisor/` 包含控制令牌、服务状态、实例租约、心跳和日志，代码包和版本控制均应排除整个目录。
- `instance-lease.json` 是 Web 和 Worker 用于确认 Supervisor 实例仍有效的短期租约。Windows 上短暂文件共享冲突会在原子替换时自动退避重试；出现 `Supervisor 租约续租失败` 或 `Supervisor 租约续租已恢复` 时，查看启动 Supervisor 的终端中脱敏的错误类型、系统错误码和尝试次数，不要复制或记录运行态文件内容。连续故障会立即记录首条，并最多每 60 秒输出一次累计失败汇总；恢复日志会带该次数。单次失败会跳过当轮服务调和，下一周期自动重试；若持续失败，既有租约不会被延长，Web 和 Worker 会在租约 TTL 到期后自行退出。此时检查磁盘空间、目录 ACL 与安全扫描/备份软件的文件占用，修复后让仍在运行的 Supervisor 自动恢复；若它已退出，按正常流程重启整套 Supervisor。不得手工删除、修改或替换活跃的 `runtime/supervisor/` 文件（包括 `instance-lease.json`、锁、心跳和 `tmp*`），以免使仍在运行的服务误判租约或破坏排障证据。
- 数据库位于 `runtime/operations.sqlite3`。离线文件备份前必须停止整个 Supervisor，再复制数据库文件及可能存在的 `-wal`、`-shm` 文件；只停止部分 Worker 不能保证没有 Web 或其他受管服务写入。
- 首次部署在启动 Supervisor 前，执行 Agent 必须手工运行 `uv run python manage.py migrate` 和 `uv run python manage.py createsuperuser`；不得记录管理员密码。后续迁移、静态资源收集和默认策略检查由 Supervisor 幂等执行。

### 打包

常规部署从与 `v<version>` 对应的 GitHub Release 下载 `wechat-content-operations-<version>-src.zip` 及其 `.sha256` 校验文件，不手工从工作区打包。源码 ZIP 由发布 tag 的跟踪源码和构建时生成的发布 provenance 组成，并在上传前拒绝包含整个 `runtime/`；因此不会携带部署密钥、数据库、锁或生成的静态文件。

下载后的校验、解压、`runtime/` 初始化与锁定依赖安装见[发布与多端升级](release-and-upgrade.md#下载与验证-release-源码包)。ZIP 端点必须完成 SHA-256、`uv sync --locked --all-groups` 与 `validate_release_package`；只有 Git 工作区才执行 `validate_releases --require-current-tag`。

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


## 任务恢复

- Job 类型、生产者、消费者与调度时间表见[系统总览](../design/system-overview.md)。所有 Worker 均由 Supervisor 启停；不要手工运行会话服务绕过 capability 注入。Supervisor 重启会先停止运行态中可确认身份的既有 Web/Worker，再按持久化 `desired` 恢复；不要在此期间手工拉起同职责进程。`run_ingestion_worker` 保留为实时命令的兼容别名。
- 视频 Worker 默认关闭。工具安装、受控样本预检、ASR 降级和停止/回滚步骤见[视频审核手册](../../40-reviews/runbooks/video-review.md)。失败时保持关闭，视频任务记录脱敏状态；仅空文字输入且媒体证据不足时进入重点人工核验。
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


## PostgreSQL 迁移阈值

当需要为任一 Worker 水平扩容、SQLite 写锁持续出现、数据库备份窗口不可接受或需要高可用时，应迁移 PostgreSQL。Django 迁移只能创建目标表结构，不能自动搬运 SQLite 业务数据：先停止所有进程并备份 SQLite，再制定显式的数据导出、导入与校验方案，切换连接配置后执行迁移，最后按 README 启动并验证 Web、Scheduler 和两个专用 Worker。

## 相关文档

- [文档中心](../../README.md)
- [系统全局地图](../design/system-overview.md)
- [发布升级](release-and-upgrade.md)
- [config 会话恢复](../../10-config/runbooks/session-recovery.md)
- [jobs 调度](../../20-jobs/runbooks/scheduling.md)
- [posts 作者与内容运营](../../30-posts/runbooks/author-and-content-operations.md)
- [reporting 指标与报告](../../50-reporting/runbooks/metrics-and-reports.md)
- [service_supervisor 生命周期](../../60-service-supervisor/design/process-lifecycle.md)
