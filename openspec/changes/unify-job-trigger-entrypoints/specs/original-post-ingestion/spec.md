## MODIFIED Requirements

### Requirement: 受控的人工历史补采入队
系统 SHALL 在系统配置首页的帖子采集生产者“立即执行”弹窗提供“创建历史补采”模式，而不得将该入口归入帖子采集 Worker 节点或独立业务卡。入口 SHALL 使用提交时当前操作游戏，并接受受配置约束的补采时长；页面不得接受 AppID。系统 SHALL 创建 `MANUAL_INGEST_WINDOW` 任务而不得在 HTTP 请求内建立 CDP 会话、同步执行 Job handler 或启动 Worker；任务由人工补采 Worker 异步消费。直接 `enqueue_ingestion` 管理命令仅用于故障诊断。

帖子采集弹窗 SHALL 将“采集最近 30 分钟”和“历史补采”渲染为独立操作块和独立 POST 表单。最近 30 分钟操作只提交固定当前窗口模式且不显示小时输入；历史补采操作只提交历史模式和 1 至 744 小时输入。两个操作块均须说明使用提交时当前操作游戏、固定页大小和 Worker 未运行时仅保持待执行。

#### Scenario: 创建可管理游戏的历史补采
- **WHEN** 已认证运营人员在帖子采集的立即执行弹窗提交有效的小时数
- **THEN** 系统 SHALL 使用提交时当前操作游戏、按北京时间对齐的开始和结束时间及固定页大小创建或复用 `MANUAL_INGEST_WINDOW`，并返回任务已创建或已存在的结果

#### Scenario: 拒绝无效补采请求
- **WHEN** 请求携带 AppID，或小时数不在 1 至 744 之间
- **THEN** 系统 SHALL 拒绝入队并显示可处理错误，不得创建任务或建立 CDP 会话

#### Scenario: 人工补采 Worker 未运行
- **WHEN** 运营人员成功创建历史补采但人工补采 Worker 未运行
- **THEN** `MANUAL_INGEST_WINDOW` SHALL 保持待执行并在 Worker 启动后按既有 checkpoint 语义消费
- **THEN** 系统不得因入队自动启动 Worker 或把任务显示为已完成
