## Why

采集与状态巡检目前依赖外部 `cron` 或 `launchd` 调用一次性命令，部署时需要额外维护触发器。单日“运营日报”虽已改为实时查询，但无法按时间范围观察运营表现，也不能对所选范围内帖子批量回读最新互动指标。另一方面，符合条件的反馈和建议帖已在本地生成回复草案，但运营人员必须进入 Admin 才能查看和维护，无法在审核帖子时连续完成内容判断与回复准备。

## What Changes

- 新增项目内部常驻 Scheduler Worker，在每个北京时间 15 分钟时间桶内为城主别慌张幂等创建 `INGEST_WINDOW` 和 `STATUS_SYNC` 任务，替代外部定时器。
- **BREAKING** 删除对外部 `cron`、`launchd` 触发自动采集的运行要求；运行组件改为 Web Server、Scheduler Worker、采集 Worker、审核 Worker。
- 将日报能力替换为工作台内的实时查询 Tab，支持按日期和单选游戏查看原有日报指标。
- 将“运营日报”重命名为“运营分析”，改为按北京时间日期范围和单选游戏实时聚合；默认展示最近 7 天（含当天），单次范围最大 31 天。
- 新增运营人员显式触发的范围互动刷新：为所选范围内原创帖异步回读浏览、点赞、评论和分享，并以新的互动快照保存结果。
- 实时日报对每个帖子仅取最新互动快照，避免历史快照累加导致互动量重复计算。
- **BREAKING** 删除预生成日报、日报持久化表、`generate_daily_report` 命令、`export_daily_report` 命令和 CSV 导出。
- 在内容管理的单帖审核区域按需展示本地“回复建议”：仅有草案时出现，支持查看诉求、草案、待补充信息、知识来源和风险提示，支持本地编辑、复制与标记采纳。
- 明确回复建议始终只是本地辅助内容：不得自动发布，也不得提供任何原平台回复或发布接口。
- 更新快速启动、运行手册、系统总览和测试，使其符合新的运行与报表模型。

## Capabilities

### New Capabilities

- `internal-job-scheduling`: 项目内常驻调度器的时间桶入队、单实例边界与恢复行为。
- `live-operations-reporting`: 工作台实时运营分析查询、指标聚合、范围互动刷新与隐私边界。

### Modified Capabilities

- `service-foundation`: SQLite 单体的常驻进程组成由两个专用 Worker 扩展为 Scheduler、采集、审核三个职责隔离的 Worker；采集 Worker 增加范围互动刷新任务。
- `daily-operations-reporting`: 日报从预生成、持久化和 CSV 导出改为只读实时范围分析。
- `ai-review-workflow`: 回复草案从仅本地持久化且不在工作台展示，调整为可在人工审核时按需查看和本地维护。
- `moderation-review-workspace`: 审核详情增加不干扰处置操作的回复建议交互。

## Impact

- 受影响代码：`jobs` 管理命令与 Worker 循环、`posts/ingestion_jobs.py`、`posts/tasks.py`、工作台导航与视图、审核详情模板与样式、`reporting` 聚合服务/模型/命令、测试和运行文档。
- 需要一个删除 `DailyReport`、`ReportMetric` 数据表的 Django 迁移；历史预生成日报不保留。
- 不新增第三方依赖，不改变 CDP、OCR、AI 或任何上游写操作边界；范围互动刷新只读原平台并只写本地互动快照，回复建议也只读写本地草案记录。
