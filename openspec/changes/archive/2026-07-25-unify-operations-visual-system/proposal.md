## Why

系统配置已具备较完整的运行台视觉、Lucide 图标与本地深浅主题偏好，但内容管理、AI 操作复审和运营分析仍各自维护页面样式与导航行为。主题开关被限制在系统配置页，后续每增加一个页面都可能复制脚本和 CSS，造成体验及维护口径漂移。

## What Changes

- 建立所有运营工作台共用的页面外壳、主题 token、Lucide 导航操作和本地主题偏好初始化机制。
- 将内容管理（含禁言管理）、AI 操作复审和运营分析接入该视觉系统；保留各页面现有查询、审核、处置、报表和表单行为。
- 将太阳/月亮主题开关提升为认证运营页导航栏的单一入口，移除系统配置页专属的主题脚本和选择器依赖。
- 为三个页面分别采用适配其工作性质的内容层级：队列/详情、复审记录、指标/图表；不把运行控制台的卡片布局机械复制到业务页面。
- 补充模板与样式测试，确保主题偏好不触发请求、不改变筛选、表单提交、CSRF、CDP 状态或任何上游操作。

## Capabilities

### New Capabilities

- `operations-visual-system`: 认证运营页面的共享外壳、本地主题偏好、导航栏主题控制及跨页面视觉 token。

### Modified Capabilities

- `administration-configuration-console`: 系统配置台改为使用共享运营视觉系统，而非持有页面专属主题实现。
- `moderation-review-workspace`: 内容管理、禁言管理与 AI 操作复审接入共享主题和业务工作区层级。
- `live-operations-reporting`: 运营分析接入共享主题和分析面板层级，同时保留现有交互式图表与 GET 回退。

## Impact

- 影响 `posts/operations_base.html`、`templates/admin/base_site.html`、共用 topbar、运营样式与报表样式，以及相关页面结构测试。
- 不新增 API、数据库迁移、Supervisor 服务、CDP 调用、任务或上游写操作；不移除 Bootstrap，亦不引入前端构建链、远程字体或运行时 CDN。
