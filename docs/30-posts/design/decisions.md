# 帖子与作者运营决策

原文来源 commit：c9299f168b7566f9d188017acc16e08f8ad6b43b。当前规则以 posts 模块主规格为准。

| L1 决策 | 理由 | 原始来源 |
| --- | --- | --- |
| 删除帖子状态使用独立位，不与正常/可见状态混成同一枚举值 | 可组合历史删除与处置状态，并保持来源判定清楚 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-18-unify-post-status/design.md |
| 候选帖核验内容卡片后才进入原创运营池 | 列表摘要不足以证明原创身份 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-18-bootstrap-content-operations-service/design.md |
| 作者禁言状态按作者和游戏独立于帖子 | 同一作者可有多篇帖子，删除帖子不应抹除作者状态 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-20-add-author-mute-management/design.md |
| 官方作者按游戏隔离为本地配置关系 | 身份认定需运营核验，不能用昵称实时猜测或改写帖子 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-28-enhance-public-opinion-analysis-and-official-authors/design.md |
| 评论导出结果由异步任务一次构建并在成功后发布 | 避免页面请求直接读取上游或暴露部分结果 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-08-01-add-comment-export-tasks/design.md |
| 游戏目录无同步记录时不成为默认操作范围 | 当前范围必须来自明确授权与选择 | 5addf7760605202b5303dbd5af6a5446dc3d2fda:openspec/changes/genericize-faq-and-game-catalog/design.md (未提交工作区快照) |
| 作者状态通过固定用户详情读取，不依赖帖子详情或帖子可见性 | 作者身份与帖子生命周期分离 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-26-decouple-author-status-and-show-names/design.md |
| 昵称是帖子本地投影，页面读取不触发上游查询 | 保持列表和详情渲染无副作用 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-26-decouple-author-status-and-show-names/design.md |
| 永久禁言是本地意图，平台截止时间仍是上游观察 | 不把本地续期策略伪装为平台状态 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-23-add-permanent-mute-management/design.md |
| 作者显示名称是已核验的本地投影，不从昵称实时认定官方身份 | 展示与身份判断需使用不同证据 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-26-decouple-author-status-and-show-names/design.md |
| 话题只从正文末尾独立话题行标准化提取 | 避免正文中间的井号文本被误作话题元数据 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-28-enhance-public-opinion-analysis-and-official-authors/design.md |
