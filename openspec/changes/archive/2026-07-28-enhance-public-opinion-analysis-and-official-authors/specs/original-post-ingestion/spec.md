## MODIFIED Requirements

### Requirement: 当前操作游戏的周期性采集入队

系统 SHALL 为当前操作游戏创建周期性原创帖子采集任务。每个调度周期 SHALL 创建或取得一个 `SCHEDULED_INGEST_WINDOW` 任务；时间窗口与页面大小属于可调整调度参数，不得以业务 AppID 或游戏名称写死。自动采集的受限页大小 SHALL 为 100，并在入队事务中写入任务参数摘要、检查点和稳定幂等键；已入队任务 MUST 继续使用其已冻结页大小。

#### Scenario: 在非边界时刻调度
- **WHEN** 调度器在北京时间 `10:16` 运行
- **THEN** 系统 SHALL 创建或取得开始于 `09:45`、结束于 `10:15`、页大小为 100 的采集窗口任务

#### Scenario: 连续周期覆盖边界帖子
- **WHEN** 调度器分别为相邻的 15 分钟边界创建采集任务
- **THEN** 两个窗口 SHALL 重叠 15 分钟，且重复读取的同一 `topic_id` SHALL 幂等更新而不得重复创建帖子

#### Scenario: 同一时间桶重复触发
- **WHEN** 调度器在同一个 15 分钟对齐时间桶内重复运行
- **THEN** 系统 SHALL 使用 AppID、对齐后的开始和结束时间及页大小为 100 创建稳定幂等键，并返回已有任务而不得创建重复 `SCHEDULED_INGEST_WINDOW` 任务

### Requirement: 受控的人工历史补采入队

系统 SHALL 在系统配置首页的帖子采集 Worker 节点提供“创建历史补采”入口。入口 SHALL 仅允许从本地可管理游戏目录选择单个游戏，并接受受配置约束的补采时长。系统 SHALL 创建页大小为 100 的 `MANUAL_INGEST_WINDOW` 任务而不得在 HTTP 请求内建立 CDP 会话；任务由人工补采 Worker 异步消费。直接 `enqueue_ingestion` 管理命令仅用于故障诊断。

#### Scenario: 创建可管理游戏的历史补采
- **WHEN** 已认证运营人员选择一个本地可管理游戏并提交有效的小时数
- **THEN** 系统 SHALL 使用该游戏、按北京时间对齐的开始和结束时间及固定页大小 100 创建或复用 `MANUAL_INGEST_WINDOW`，并返回任务已创建或已存在的结果

#### Scenario: 拒绝无效补采请求
- **WHEN** 请求选择不在本地可管理游戏目录中的 AppID，或小时数不在 1 至 744 之间
- **THEN** 系统 SHALL 拒绝入队并显示可处理错误，不得创建任务或建立 CDP 会话
