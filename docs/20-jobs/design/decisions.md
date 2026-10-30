# 调度与任务决策

原文来源 commit：c9299f168b7566f9d188017acc16e08f8ad6b43b。详细 L2 取舍见[调度和任务所有权](scheduling-and-task-ownership.md)，当前行为以 jobs 模块规格为准。

| L1 决策 | 理由 | 原始来源 |
| --- | --- | --- |
| Scheduler 仅创建持久化任务，Worker 执行业务 | 调度恢复不需要浏览器、上游访问或业务 handler | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-19-add-automated-ingestion-scheduler/design.md |
| 使用单机数据库任务表而非新增消息队列 | 单机部署已有事务数据库，减少第二套状态和运维边界 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-18-bootstrap-content-operations-service/design.md |
| Job 类型决定消费者所有权，长时人工工作与实时采集隔离 | 避免慢任务阻塞周期采集和状态巡检 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-19-internal-scheduler-and-live-reporting/design.md |
| 生产者定义由代码注册，数据库只保存受限覆盖 | 允许运营调节规则，但不能注入命令、Job 类型或任意范围 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-25-configure-scheduled-producers/design.md |
| 人工立即执行创建业务 Job，不修改自动槽 | 使人工工作与定时调度的审计和幂等语义独立 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-28-unify-job-trigger-entrypoints/design.md |
| 活跃任务以稳定业务键复用，随机幂等键不承担业务去重 | 避免重复提交创建并行业务任务 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-08-01-add-comment-export-tasks/design.md |
| 自动生产者配置仅表达受限规则覆盖，规则定义和 Job 构造器由代码注册 | 配置数据不能变成任意调度执行入口 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-25-configure-scheduled-producers/design.md |
| 调度槽以 UTC 稳定标识，运营时点使用中国标准时间 | 重启、夏令时和展示格式不影响同槽幂等 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-25-configure-scheduled-producers/design.md |
| 生产者停用只停止未来自动入队，不改写已有 Job | 调度配置与已承诺的业务任务具有不同生命周期 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-25-configure-scheduled-producers/design.md |
| Scheduler 使用固定注册规则和逻辑槽账本表达触发，不广播进程内事件 | 规则状态和任务创建需跨重启可追溯、可幂等 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-25-configure-scheduled-producers/design.md |
| 错过触发和重叠处理有界且按规则显式选择 | 避免恢复时无界补跑或重复创建任务 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-25-configure-scheduled-producers/design.md |
