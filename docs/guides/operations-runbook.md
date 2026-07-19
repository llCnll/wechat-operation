# 运行与恢复手册

## 部署边界

- 运行一个 Web 进程、一个 Scheduler Worker、一个帖子采集 Worker 与一个 AI 审核 Worker；SQLite 会串行化它们的写入阶段，因此不得为同一职责启动多个实例。
- 所有可变运行态均位于 `runtime/`：`runtime/.env`、`runtime/operations.sqlite3`、收集后的 `runtime/static/`、`runtime/worker-locks/` 与可选日志。代码包和版本控制均应排除整个目录。
- 数据库位于 `runtime/operations.sqlite3`。离线文件备份前必须停止 Web、Scheduler、采集 Worker 和 AI Worker，再复制数据库文件及可能存在的 `-wal`、`-shm` 文件；只停止 Worker 不能保证没有 Web 写入。
- 启动前运行 `uv run python manage.py migrate` 与 `uv run python manage.py collectstatic --noinput`。

### 打包

打包代码时排除整个 `runtime/`，避免携带部署密钥、数据库、锁或生成的静态文件。

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

- Chrome CDP 仅使用本地已登录浏览器；不保存 Cookie、令牌或 `openid`。
- 首次部署执行 `mkdir -p runtime && cp .env.example runtime/.env`，再按需填写运行时配置。
- AI/OCR 凭据只通过 `AI_*`、`OCR_*` 环境变量注入。轮换时停止 Worker、替换部署密钥并重启 Worker。
- `ENABLE_UPSTREAM_WRITES` 与对应的 `ENABLE_SINK_WRITES`、`ENABLE_BLOCK_WRITES`、`ENABLE_MUTE_WRITES`、`ENABLE_UNMUTE_WRITES` 或 `ENABLE_RESTORE_WRITES` 必须同时为 `true` 才会发送对应的已验证请求；默认均关闭。解除禁言只在当前游戏处于禁言状态且经二次确认后发送请求。

## 任务恢复

- `run_scheduler` 只创建任务，不认领业务任务；`run_ingestion_worker` 认领采集、状态巡检与范围互动刷新任务，`run_review_worker` 只认领 OCR/AI 任务；采集任务可从 checkpoint 继续。
- 契约变更或会话失效会保留已脱敏错误摘要。修复后重新排队，不应修改数据库中的会话字段。
- `/admin/` 仅用于系统配置与运行追溯：策略版本与策略规则可编辑，游戏目录只能通过受控 Chrome 同步；帖子、审核、AI/OCR、审计和后台任务不可手工修改。所有上游处置均从内容管理工作台执行，不能以 Admin 或直接修改数据库的方式绕过。

### 排障顺序

| 现象 | 首先检查 | 恢复方式 |
| --- | --- | --- |
| 没有新增帖子 | `/auth/status`、Scheduler 与采集 Worker 是否持续运行 | 恢复受控 Chrome 登录后重启对应进程；停机超过 30 分钟再补采窗口。 |
| 顶部 `CDP` 状态胶囊为红色 | Chrome 是否以 `9222` 调试端口启动 | 已登录本系统的运营人员可点击红色胶囊，只在受控 Chrome 打开固定插件登录页后手动扫码；若提示 CDP 不可用，先启动受控 Chrome，绝不在工作台浏览器直接登录。 |
| 任务长期 `PENDING` | 任务的 `task_type` 与对应 Worker | 启动正确的唯一 Worker，不要并行启动第二个同类 Worker。 |
| 任务 `FAILED` | 系统配置与追溯台“后台任务”的脱敏 `error_summary` 与 checkpoint | 修复会话、配置或上游契约后重新入队；不要修改会话字段。 |
| AI/OCR 任务失败 | `runtime/.env` 中对应 `AI_*` 或 `OCR_*` 配置 | 修正配置并重启 AI Worker；已采集帖子数据不受影响。 |
| 上游处置未执行 | 总开关、动作开关、会话与人工确认 | 确认批准配置后重启 Web，再由运营人员重新确认；不得通过 Worker 重试写操作。 |

## 自动采集调度

自动调度只覆盖城主别慌张（`wx6d2454a8afcd9706`）。启动一个内部 Scheduler Worker：

```bash
uv run python manage.py run_scheduler --loop
```

它按北京时间每 15 分钟时间桶先入队一项最多回读 20 条待审核帖的状态巡检，再入队最近 30 分钟的采集窗口。Scheduler 不连接 CDP、不读取登录态、不执行 AI 或上游写操作，必须同时保持采集 Worker 和 AI Worker 运行以消费任务。`--once` 仅用于排障或验证当前时间桶；不再配置 cron 或 `launchd`。

同一时间桶的重复检查会复用已有任务，但 SQLite 部署仍只能运行一个 Scheduler Worker。可通过服务日志检查 AppID、窗口和任务编号。

### 漏采处理

定时器停机超过 30 分钟时，使用人工窗口命令补采；例如补拉最近 6 小时：

```bash
uv run python manage.py enqueue_ingestion --appid wx6d2454a8afcd9706 --hours 6
```

确认任务已入队后保持 `run_ingestion_worker --loop` 运行。会话失效、契约错误或任务耗尽重试次数时，先修复 Chrome 登录或上游适配器，再创建新的补采窗口；不要将 Cookie、令牌或完整认证 URL 写入命令、日志或数据库。

## 运营分析互动刷新

运营人员可在 `/reports/` 的“运营分析”中选择最多 31 个北京时间自然日，并点击“刷新本范围数据”。该操作只入队 `METRICS_REFRESH_WINDOW`，不会在 Web 请求内连接 Chrome。采集 Worker 按 `topic_id` 保存 checkpoint，成功帖子新增互动快照；原平台已删除的帖子会计入跳过数，传输或契约异常会记录失败帖子样本并继续处理其余帖子。再次手动触发同一范围会创建新任务并重新尝试失败帖子。刷新不修改帖子正文、媒体、投票、审核状态或任何上游状态。

## PostgreSQL 迁移阈值

当需要为任一 Worker 水平扩容、SQLite 写锁持续出现、数据库备份窗口不可接受或需要高可用时，应迁移 PostgreSQL。Django 迁移只能创建目标表结构，不能自动搬运 SQLite 业务数据：先停止所有进程并备份 SQLite，再制定显式的数据导出、导入与校验方案，切换连接配置后执行迁移，最后按 README 启动并验证 Web、Scheduler 和两个专用 Worker。

## 相关文档

- [文档中心](../README.md)
- [系统总览](system-overview.md)
- [数据库数据字典](../technical/database-schema.md)
