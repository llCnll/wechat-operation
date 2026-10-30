# 本机服务监督

## 职责

维护 Web、Scheduler、Worker、会话服务和受控 Chrome 的进程所有权、期望状态、租约和恢复边界。

## 权威规格

[service_supervisor 模块规格](../../openspec/specs/service_supervisor/spec.md)；游戏圈会话边界另见 [config 模块规格](../../openspec/specs/config/spec.md)。

## 代码入口

service_supervisor/manager.py、runtime.py，以及 config/supervisor.py 中的本机控制入口。

## 文档

- design/：[L1 决策](design/decisions.md)、[L2 进程生命周期](design/process-lifecycle.md)。
- 运维入口见[平台运行手册](../01-platform/runbooks/operations.md)。
- 跨平台核验见[发布环境核验手册](../01-platform/runbooks/release-verification.md)。

## 缺口

运行状态和权限诊断以发布环境核验结果为准。
