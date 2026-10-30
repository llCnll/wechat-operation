# 审核与媒体证据决策

原文来源 commit：c9299f168b7566f9d188017acc16e08f8ad6b43b。当前规则以 reviews 模块主规格为准。

| L1 决策 | 理由 | 原始来源 |
| --- | --- | --- |
| AI 建议与上游写入授权分离 | 模型输出不能直接取得平台写权限 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-20-add-ai-automated-moderation/design.md |
| 自动处置采用独立 Worker、规则授权和事后人工复审 | 便于隔离故障并保留不可变执行依据 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-20-add-ai-automated-moderation/design.md |
| 评估输入变化通过显式重评生成新结果 | 不静默重写既有审核证据 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-19-complete-ai-review-closure/design.md |
| 视频在本机提取受限证据后再交给文本审核 | 不向模型暴露认证上下文或任意视频 URL | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-20-add-video-content-review/design.md |
| FAQ 使用可审阅的结构化资料，由服务端约束规则 ID | 避免模型自行创造来源和事实承诺 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-21-add-faq-driven-reply-drafts/design.md |
| 视频可选证据失败不伪装成完整证据 | OCR、ASR 与抽帧具有独立失败边界 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-25-sample-overlength-videos/design.md |
| FAQ 示例可以复用，但不能替代部署方客服政策 | 开源通用示例不应携带某个组织的服务入口或承诺 | 5addf7760605202b5303dbd5af6a5446dc3d2fda:openspec/changes/genericize-faq-and-game-catalog/design.md (未提交工作区快照) |
| 自动执行沿用评估时已确定的建议规则，不重新选择或降级模型结论 | 执行资格不能反过来改变审核建议 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-21-relax-ai-automation-eligibility/design.md |
| 高额奖励本身不触发重点核验或自动处置否决 | 避免对单一词面过拟合，同时继续要求风险证据 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-21-remove-hardcoded-high-reward-focus/design.md |
| 本地回复草案没有原平台发布能力 | 保持建议生成与平台写操作分离 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-19-internal-scheduler-and-live-reporting/design.md |
| 后台评估任务状态与人工审核状态分离 | 模型调用失败不能伪装为审核完成 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-19-complete-ai-review-closure/design.md |
| 每次评估保留不可变评估记录，审核任务指向当前有效结果 | 支持证据追溯而不覆盖历史模型输出 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-19-complete-ai-review-closure/design.md |
| 自动资格使用评估中已确定的规则阈值和决定风险证据 | 授权门不能重选或改写模型建议 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-21-relax-ai-automation-eligibility/design.md |
