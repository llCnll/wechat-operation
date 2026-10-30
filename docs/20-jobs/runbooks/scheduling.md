# 调度与任务生产操作

## 自动采集调度

自动调度仅针对系统配置中已选择的“当前操作游戏”，由 Supervisor 托管；不应另行执行常驻 Scheduler 命令。运营人员在系统配置台“自动任务生产者”中独立暂停/恢复生产者或规则，并在规则管理弹窗中新增、调整或删除运行时点：定时规则只填写 `HH:MM`，间隔规则只填写分钟数，系统按模板反推并保存受限表达式。调度统一使用中国标准时间，保存后最多 30 秒生效。删除规则只停止未来自动入队，仍保留已入队 Job 与逻辑槽审计；暂停也不取消已有或人工 Job。完整时间表见[系统总览](../../01-platform/design/system-overview.md)。SQLite 部署仍只能运行一个 Scheduler Worker；`--once` 仅用于排障或验证当前时间槽，若 Scheduler 正在运行会因专用锁拒绝执行。

### 漏采处理

定时器停机超过 30 分钟时，在系统配置首页“自动任务生产者 → 帖子采集 → 立即执行”选择“历史补采（当前操作游戏）”，输入最近小时数（1 至 744）。任务由人工补采 Worker 异步处理，不会阻塞实时采集，也不会自动启动 Worker。

迁移说明：旧的 `/admin/runtime/session-keepalive/`、`/admin/runtime/permanent-mute-renewal/` 与 `/admin/runtime/ingestion/enqueue/` POST 入口已移除。自动化调用应改用受认证、CSRF 保护的生产者路由；历史补采使用 `/admin/scheduled-producers/post_ingestion/run/` 并提交 `mode=history` 与 `hours`，不得提交 `appid`。运营人员统一从对应自动任务生产者卡的“立即执行”操作进入。

仅在后台不可用或排障脚本需要复现入队行为时，才使用人工窗口命令；例如补拉最近 6 小时：

```bash
uv run python manage.py enqueue_ingestion --appid "<已授权并同步的游戏 AppID>" --hours 6
```

确认任务已入队后保持 Pipeline 中的帖子采集 Worker 运行；该节点会同时启动实时采集和人工补采两个进程。会话失效、契约错误或任务耗尽重试次数时，先修复 Chrome 登录或上游适配器，再创建新的补采窗口；不要将 Cookie、令牌或完整认证 URL 写入命令、日志或数据库。
