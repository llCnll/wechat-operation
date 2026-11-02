# 指标与报告决策

原文来源 commit：c9299f168b7566f9d188017acc16e08f8ad6b43b。当前指标与报告行为以 reporting 模块主规格为准。

| L1 决策 | 理由 | 原始来源 |
| --- | --- | --- |
| 指标按来源和业务时间保存，不合并成无来源总分 | 不同平台来源具有不同口径和可用时间 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-21-add-game-circle-metric-snapshots/design.md |
| 报告锁定输入范围与来源快照，每次重跑生成独立 Run | 支持重现结论并保留历史结果 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-27-add-async-public-opinion-reports/design.md |
| 页面实时分析保持只读，刷新由异步任务执行 | 避免页面请求建立会话或依赖上游响应时长 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-19-internal-scheduler-and-live-reporting/design.md |
| 模型结论是可独立关闭的派生能力，不阻断确定性报告 | 模型服务故障不能阻断报告数据交付 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-28-enhance-public-opinion-analysis-and-official-authors/design.md |
| 发布后采用分阶段稳定性复核，不把待复核缺口展示为真实零值 | 数据完整性与平台业务零值语义不同 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-21-add-game-circle-metric-snapshots/design.md |
| 日指标按来源写入报告 Run，不合并为跨来源总分 | 每个上游来源拥有不同定义、完整度和更新时点 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-28-enhance-public-opinion-analysis-and-official-authors/design.md |
| 模型结论消费白名单聚合证据，并与确定性报告独立降级 | 模型不需要访问原始帖子集合或认证上下文 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-28-enhance-public-opinion-analysis-and-official-authors/design.md |
| 报告算法版本是运行契约，不是展示标签 | 不同版本不得静默重解释已锁定输入 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-28-enhance-public-opinion-analysis-and-official-authors/design.md |
| 舆情结论是确定性报告之后可独立关闭的阶段 | 模型服务故障不应阻断可复现报告 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-27-add-async-public-opinion-reports/design.md |
| 每个 Run 锁定来源与算法版本，互动时点按 Run 记录 | 使重跑可解释而不伪装为完全相同输入 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-27-add-async-public-opinion-reports/design.md |
