# 跨模块平台决策

原文来源 commit：c9299f168b7566f9d188017acc16e08f8ad6b43b。

| L1 决策 | 理由 | 原始来源 |
| --- | --- | --- |
| 首期采用 Django 单体与 SQLite WAL，不引入微服务或消息队列 | 单机部署优先降低运维成本，同时以任务表持久化异步工作 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-18-bootstrap-content-operations-service/design.md |
| 同一职责 Worker 单实例，扩容前迁移到支持并发写入的数据库 | SQLite 写锁和本机运行态不支持同职责横向扩容 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-18-bootstrap-content-operations-service/design.md |
| 系统配置与运行追溯入口不承担内容审核或上游写入 | 将配置诊断与高风险业务决定分开 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-19-redesign-admin-configuration-console/design.md |
| 文档按代码模块编号组织，行为与实现分别以 OpenSpec 和代码为准 | 以模块作为开发和维护边界，避免 capability 文档散落并复制实现 | 当前用户确认的文档重构决策 |
| 发布源码包由已验证 tag 构建，并固定校验值；同一版本附件不静默覆盖 | 避免工作区杂项进入交付物，且保持同版本交付内容不可变 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-21-add-release-source-archive/design.md |
