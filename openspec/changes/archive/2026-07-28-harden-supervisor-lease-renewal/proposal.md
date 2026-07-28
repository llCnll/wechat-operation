## Why

Windows 上，`instance-lease.json` 被多个受管服务读取时，Supervisor 对同一文件执行 `os.replace()` 可能因短暂共享锁失败。当前续租异常会逃逸出主循环并进入 `shutdown()`，导致一次瞬时文件访问冲突停止整套受管服务，同时遗留 `tmp*` 临时文件。

该问题已经在运行环境中重复出现。系统需要在不放宽租约失效安全边界的前提下，将可恢复的短暂运行时文件竞争与 Supervisor 真正失联区分开。

## What Changes

- 为运行时 JSON 的原子替换增加针对 Windows `WinError 5` 与 `WinError 32` 文件共享冲突的有界、带退避的重试；成功前继续使用同一完整临时文件，最终失败时清理该临时文件。
- 将 Supervisor 的租约续租失败改为可观测的循环内故障：一次或有限次短暂失败不得退出主循环、调用全局 `shutdown()` 或主动终止受管服务。
- 对同一段连续续租失败的日志限频：立即记录首条故障，之后最多每 60 秒输出一次脱敏汇总；恢复时记录该段的累计失败次数。
- 保留现有 30 秒租约 TTL、实例标识校验和 Worker/Web 在租约失效后的安全退出语义；持续无法续租时，受管服务仍必须因租约到期或实例不匹配而停止。
- 增加覆盖重试、清理、瞬时续租失败恢复和持续失效边界的测试。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `local-service-supervisor`: 调整 Supervisor 运行时租约发布与短暂文件系统故障时的生命周期语义。

## Impact

- 受影响代码：`service_supervisor/runtime.py`、`service_supervisor/manager.py`、`tests/test_service_supervisor.py`，以及按需补充运行手册中的故障诊断说明。
- 不新增外部服务、网络接口、配置项或依赖；不修改控制令牌、租约内容格式、TTL 与 Worker 命令。
- 本 change 不实现 Windows Job Object、软链接/版本化租约或外部进程管理器集成；这些属于后续进程托管架构改造。
