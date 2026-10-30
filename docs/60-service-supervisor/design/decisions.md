# 本机服务监督决策

原文来源 commit：c9299f168b7566f9d188017acc16e08f8ad6b43b。当前验收行为以 service_supervisor 模块规格为准。

| L1 决策 | 理由 | 原始来源 |
| --- | --- | --- |
| Supervisor 是受管 Web、Worker 和受控 Chrome 的唯一生命周期所有者 | 防止不同入口并发启动同一服务或绕过运行态约束 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-19-add-initialization-supervisor-pipeline/design.md |
| 持久化 desired 与实际进程健康分开 | 异常退出后可依据期望状态恢复，不把瞬时故障误当作停用请求 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-26-stabilize-supervisor-process-lifecycle/design.md |
| Chrome generation 比启动器 PID 更能代表浏览器会话身份 | PID 可交接，固定 CDP browser generation 更直接证明会话连续性 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-26-stabilize-supervisor-process-lifecycle/design.md |
| 受管进程回收要求多项身份同时匹配 | 避免按进程名、端口或命令子串误杀本机其他服务 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-08-02-harden-supervisor-lifecycle-observability/design.md |
| 运行态文件原子替换失败是可恢复故障，不应立即全局关闭 | 短时 Windows 文件占用不应造成服务级联退出 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-28-harden-supervisor-lease-renewal/design.md |
| 正常关闭和服务替换先撤销实例租约，再协作退出 | 缩短旧 Worker 仍能开始新处理单元的窗口 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-08-02-harden-supervisor-lifecycle-observability/design.md |
| 生命周期事件与服务当前状态分开保存 | 可解释状态变化而不把事件日志当成当前真相 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-08-02-harden-supervisor-lifecycle-observability/design.md |
| 会话诊断只发布 target 动作摘要，不发布 target 身份 | 保留排障能力而不暴露浏览器页面标识 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-08-02-harden-supervisor-lifecycle-observability/design.md |
| 租约过期只表示实例失效，不隐式关闭持久化 desired | 保留运营期望，允许新实例按确定策略恢复 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-26-stabilize-supervisor-process-lifecycle/design.md |
| Chrome 使用固定 loopback 端口和 browser generation，不由 PID 单独判活 | PID 交接不必然代表浏览器会话改变 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-26-stabilize-supervisor-process-lifecycle/design.md |
| 进程登记与生命周期事件分离，终止前核验完整身份 | 可解释恢复动作并避免误终止未登记进程 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-08-02-harden-supervisor-lifecycle-observability/design.md |
