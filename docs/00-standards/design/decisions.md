# 跨模块开发决策

| L1 决策 | 理由 | 原始来源 |
| --- | --- | --- |
| pytest 的数据库、锁和易失运行态按 worker 隔离，并在 HTTP 发送边界默认拒绝未模拟外连 | 保证并行测试不共享副作用，且测试失败不依赖真实外部服务 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-31-speed-up-pytest-suite/design.md |
| AI 结论测试以确定性证据和 mock 契约分层验证 | 避免真实模型波动使正确性基线不稳定 | c9299f168b7566f9d188017acc16e08f8ad6b43b:openspec/changes/archive/2026-07-31-speed-up-pytest-suite/design.md |
