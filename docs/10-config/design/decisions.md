# 运行配置与会话决策

原文来源 commit：c9299f168b7566f9d188017acc16e08f8ad6b43b。详细 L2 取舍见[会话服务设计](session-service.md)。

| L1 决策 | 理由 | 原始来源 |
| --- | --- | --- |
| 游戏圈会话状态只由单一 session owner 管理 | 凭证不能持久化或由业务 Worker 分别重建 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-22-avoid-daily-game-circle-relogin/design.md |
| 用户扫码只通过固定受控登录页，不接受任意 URL 或 target | 防止 Web 跳转和人工浏览器页变成认证绕过 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-26-align-observable-game-circle-session/design.md |
| 外层会话检查与建立 Frame 是不同控制动作 | 避免只读检查意外创建页面、切换游戏或改变会话 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-25-reorganize-runtime-console/design.md |
| 配置台从单一运行快照派生流程与服务两种视图 | 避免多个页面各自拼接不一致的服务状态 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-25-reorganize-runtime-console/design.md |
| 启动密钥和端口与管理台动态开关分层 | 启动安全配置和运营策略具有不同生命周期 | 5addf7760605202b5303dbd5af6a5446dc3d2fda:openspec/changes/configure-runtime-settings/design.md (未提交工作区快照) |
| 外层会话检查由成功的无缓存平台验证证明，不由 URL 或历史状态推断 | token 存在和上次成功均不能证明当前认证有效 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-25-detect-expired-game-circle-session/design.md |
| 改变受控 target 生命周期的动作进入同一控制串行通道 | 浏览器外层游戏是共享状态，分散并发操作会破坏 lease 所有权 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-23-improve-local-login-session-recovery/design.md |
| 扫码等待是会话服务内的互斥观察状态，不占用业务 Worker | 登录等待期间仍须响应控制请求并维护 Worker 心跳 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-23-improve-local-login-session-recovery/design.md |
| 外层会话探测复用受控 HOME，不以 token 或页面地址推断登录成功 | 明确区分外层登录证明和来源 Frame 可用性 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-25-detect-expired-game-circle-session/design.md |
