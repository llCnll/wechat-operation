# 平台基础

## 职责

提供跨模块产品目标、系统全局地图、数据所有权和部署发布入口。

## 权威规格

跨模块行为归主要代码所有者维护：[config](../../openspec/specs/config/spec.md)、[jobs](../../openspec/specs/jobs/spec.md)、[posts](../../openspec/specs/posts/spec.md)、[reviews](../../openspec/specs/reviews/spec.md)、[reporting](../../openspec/specs/reporting/spec.md)、[service_supervisor](../../openspec/specs/service_supervisor/spec.md)。

## 代码入口

没有独立业务应用。全局配置位于 config/；任务、领域数据和服务实现由各模块维护。

## 文档

- design/：[系统全局地图](design/system-overview.md)、[数据归属](design/data-ownership.md)、[跨模块 L1 决策](design/decisions.md)。
- runbooks/：[部署](runbooks/deployment.md)、[日常运维](runbooks/operations.md)、[发布升级](runbooks/release-and-upgrade.md)、[环境核验](runbooks/release-verification.md)。
- [产品目标、范围和待评审方向](requirements.md)。

## 缺口

当前无已确认的跨模块设计缺口。
