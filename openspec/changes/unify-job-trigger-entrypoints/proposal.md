## Why

当前系统把“创建业务 Job”和“管理 Worker 生命周期”混在同一运行控制台中：历史补采按钮位于帖子采集 Worker 卡片，且会话保活、永久禁言续期还遗留了与自动生产者等价的手动路由。入口归属不清会让运营人员误以为执行一次需要操作 Worker，也使规格、页面和路由难以保持一致。

本变更将一次性业务任务统一归入其业务或自动生产者入口，令 Worker 卡只表达生命周期、日志和等待中的队列状态，降低误操作与重复入队入口的维护风险。

## What Changes

- 将固定自动生产者的“立即执行”明确为会话保活和永久禁言续期的唯一手动创建入口；删除遗留的等价运行时 HTTP 路由及其视图。
- **BREAKING**：移除仅供旧控制台使用的 `POST /admin/runtime/session-keepalive/`、`POST /admin/runtime/permanent-mute-renewal/` 和 `POST /admin/runtime/ingestion/enqueue/`；调用方改用受认证的自动生产者“立即执行”路由。
- 将“创建历史补采”并入自动任务生产者“帖子采集”的“立即执行”弹窗；保留其 1–744 小时范围、创建 `MANUAL_INGEST_WINDOW` 的现有语义，并由当前操作游戏决定范围。
- 明确所有人工入口只创建或复用可审计、幂等且范围冻结的 Job；Worker 未运行时 Job 保持待执行，任何入口均不得隐式启动 Worker 或同步执行 handler。
- 收敛控制台、调度、采集和 Supervisor 规格及运维文档中关于入口归属的重复或冲突说明，并补足最小回归验证。

## Capabilities

### New Capabilities

- 无。

### Modified Capabilities

- `configurable-scheduled-producers`：将固定生产者的人工执行映射定义为会话保活和永久禁言续期的唯一手动 Job 创建入口。
- `runtime-console-orchestration`：区分业务 Job 创建入口与 Worker 生命周期入口，并规定历史补采归“帖子采集”的立即执行入口。
- `original-post-ingestion`：将参数化历史补采入口从 Worker 节点调整为帖子采集生产者的立即执行弹窗，同时保持其范围和异步消费契约。
- `internal-job-scheduling`：更新自动采集空洞的人工补采引导，避免将业务补采描述为 Worker 操作。
- `local-service-supervisor`：移除禁言维护 Worker 卡的“执行一次”职责，仅保留受管生命周期和日志边界。

## Impact

- 受影响代码：`templates/admin/`、`config/urls.py`、`config/supervisor_views.py` 及对应 Django 测试。
- 受影响接口：三个旧 POST 路由被移除；自动生产者的既有 POST 路由保持不变。
- 不引入新的 Worker、队列、依赖或上游调用；既有 Job 类型、幂等键、消费 Worker 与权限/CSRF 边界保持不变。
