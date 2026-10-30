# 文档中心

根目录 README 提供快速启动和升级入口。本目录按代码模块组织；每个模块 README 是本模块的文档入口。

## 目录与业务域

| 编号 | 业务域 | 当前规格 |
| --- | --- | --- |
| 00 | 跨模块规范 | 文档维护与测试规则 |
| 01 | 平台基础 | 横跨多个模块的产品范围、数据归属和运维 |
| 10 | 运行配置与会话 | openspec/specs/config |
| 20 | 后台任务与调度 | openspec/specs/jobs |
| 30 | 帖子与作者运营 | openspec/specs/posts |
| 40 | 审核与媒体证据 | openspec/specs/reviews |
| 50 | 指标与报告 | openspec/specs/reporting |
| 60 | 本机服务监督 | openspec/specs/service_supervisor |

## 按模块查找

- [00 跨模块规范](00-standards/README.md)
- [01 平台基础](01-platform/README.md)
- [10 运行配置与会话](10-config/README.md)
- [20 后台任务与调度](20-jobs/README.md)
- [30 帖子与作者运营](30-posts/README.md)
- [40 审核与媒体证据](40-reviews/README.md)
- [50 指标与报告](50-reporting/README.md)
- [60 本机服务监督](60-service-supervisor/README.md)

## 按文档类型查找

- design/：模块设计取舍、不变量、外部约束；决策等级见 [00-standards/documentation-rules.md](00-standards/documentation-rules.md)。
- integration/：经授权观察的上游接口事实、联调证据和限制。
- runbooks/：可重复执行的部署、恢复、回填和验证步骤。
- api/：本项目自行提供的正式 API 字段契约；当前没有独立 API 手册时不建空目录。

## 真相源

- 当前行为和验收：openspec/specs/{module}/spec.md。
- 当前实现：源码、迁移与测试。
- 设计理由：模块 design/；L1 决策收于 decisions.md，L2 收于主题文档的“关键取舍”。
- 上游私有接口：integration/ 记录观察，不单独定义系统行为。
- 数据字段：由 models 与 migrations 决定；design 只记录跨模块数据所有权。

归档 change 的 delta、proposal、design 和 tasks 不作为长期文档副本。仍有效的行为先同步到模块主规格，设计按 L0/L1/L2 归纳，可重复操作移入 runbooks；信息归位后从当前树清理过程包，并以源 commit 和原始路径从共享 Git 历史追溯。

## 未关闭的文档来源缺口

- 2026-10-30 的运行配置与游戏目录/FAQ 通用化 change 原文来自本地未提交工作区快照，尚未进入共享 Git 历史。当前仍有效决策已归纳到模块规格和 design；原文快照引用仅在本机可追溯，需在相关改动形成共享提交前保留该来源对象。
