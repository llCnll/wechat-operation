# 运行配置与游戏圈会话

## 职责

维护进程启动配置、共享管理入口、授权游戏圈会话与本机调用边界。

## 权威规格

[config 模块规格](../../openspec/specs/config/spec.md)；服务进程生命周期另见 [service_supervisor 模块规格](../../openspec/specs/service_supervisor/spec.md)。

## 代码入口

config/，尤其 runtime_config.py、game_circle_session.py、game_circle_ipc.py 和 supervisor_views.py。

## 文档

- design/：[L1 决策](design/decisions.md)、[L2 会话服务设计](design/session-service.md)。
- integration/：[登录观察](integration/qr-login.md)、[续接观察](integration/session-refresh.md)、[韧性演练](integration/session-resilience-drill.md)。
- runbooks/：[会话恢复](runbooks/session-recovery.md)。

## 缺口

跨平台真实 Chrome/Supervisor 演练见 01-platform/runbooks/release-verification.md；实际状态以对应发布记录为准。2026-10-30 运行配置 change 的原文尚未进入共享 Git 历史，当前设计摘要引用本地未提交快照。
