## ADDED Requirements

### Requirement: 城主别慌张的周期性采集入队
系统 SHALL 为城主别慌张 AppID `wx6d2454a8afcd9706` 创建周期性原创帖子采集任务。每个调度周期 SHALL 创建或取得一个页面大小为 `20` 的 `INGEST_WINDOW` 任务，其结束时间 SHALL 为调度执行时刻向下对齐到最近的北京时间 15 分钟边界，开始时间 SHALL 比结束时间早 30 分钟。

#### Scenario: 在非边界时刻调度
- **WHEN** 调度器在北京时间 `10:16` 运行
- **THEN** 系统 SHALL 创建或取得开始于 `09:45`、结束于 `10:15` 的采集窗口任务

#### Scenario: 连续周期覆盖边界帖子
- **WHEN** 调度器分别为相邻的 15 分钟边界创建采集任务
- **THEN** 两个窗口 SHALL 重叠 15 分钟，且重复读取的同一 `topic_id` SHALL 幂等更新而不得重复创建帖子

#### Scenario: 同一时间桶重复触发
- **WHEN** 调度器在同一个 15 分钟对齐时间桶内重复运行
- **THEN** 系统 SHALL 使用 AppID、对齐后的开始和结束时间及页面大小创建稳定幂等键，并返回已有任务而不得创建重复 `INGEST_WINDOW` 任务
