## 1. Ownership and Bootstrap

- [x] 1.1 将会话运行态调整为外层状态、当前 scope Frame 状态、可用动作、登录等待剩余时间和固定错误类别；移除凭据与 target 标识的持久化/对外表面。
- [x] 1.2 在 Supervisor 实现 Chrome 与 session Worker 的联动生命周期：Worker owner 替换前终止 Chrome，确认新 generation 后再启动 Worker。
- [x] 1.3 实现非阻塞 SessionBootstrap，统一 HOME、当前游戏确认/切换、Frame、身份绑定、最小只读验证和原子 lease 发布。
- [x] 1.4 实现唯一、内存归属的 `LOGIN_PENDING` 和有界等待循环；扫码后复用 HOME，Worker 重启时清除旧等待。
- [x] 1.5 删除周期 LOGIN_PROBE、Worker 启动 Probe、PROBE_CANDIDATE、来源无 lease/身份过期的隐式 Recover，以及固定时段 Refresh 依赖。
- [x] 1.6 调整来源 IPC：仅使用固定 ACTIVE lease；无 lease 返回脱敏未就绪，人工写入不恢复、不重放。

## 2. Concurrency, UI and Compatibility

- [x] 2.1 实现 session service 内单一 control lane、AppID single-flight、SessionAccess `finally` 释放和 lease 退役规则。
- [x] 2.2 调整 IPC、状态接口与工作台，区分外层有效、登录等待、Frame 建立中、Frame 未就绪和 READY；人工登录页是唯一可显示 target。
- [x] 2.3 增加旧 Probe/Refresh 发布终态化工具及发布顺序，保留审计、参数和 checkpoint，并在两个发布版本窗口内保持幂等兼容。

## 3. Optional Repair

- [x] 3.1 增加默认关闭的 SessionRepair 维护策略和服务内部检查循环。
- [x] 3.2 仅检查 current-owner HOME/Frame；失效时按 AppID 合并 Bootstrap，关闭时保持人工恢复语义。

## 4. Optional Keepalive

- [x] 4.1 增加默认关闭的 SessionKeepalive 策略、`automation_key` / `trigger` 审计字段和明确时区的四小时桶。
- [x] 4.2 仅在启用时由 Scheduler 接受未来周期 key；关闭后保留既有 Job 队列语义，手动保活不受影响。
- [x] 4.3 在首次消费者执行、首个浏览器操作前冻结 scope；无 scope 时以 `NO_ACTIVE_GAME_SCOPE` 终态结束且不产生浏览器副作用。
- [x] 4.4 实现候选会话 `A -> B -> A` / 直接对齐策略、冻结 scope 后的独立新 scope Bootstrap，以及当前 scope 状态投影。

## 5. Verification and Documentation

- [x] 5.1 覆盖 Bootstrap 的外层可用、扫码、扫码后同 HOME、无 current scope、不可切换目标和 Frame 验证失败分支。
- [x] 5.2 覆盖 session-only restart 的 Chrome 联动、新 generation、旧 target 不继承和人工/未知 tab 不接管。
- [x] 5.3 覆盖 control lane、来源访问计数、旧 lease 连续性、后台创建无前台 fallback、自动 UI 静默和人工写入不重放。
- [x] 5.4 覆盖 Repair、Keepalive 开关、既有 Job 语义、scope 冻结/重试、无 scope 终态、跨 scope 发布和 UI 投影。
- [x] 5.5 覆盖旧 Job 发布终态化的停 Worker 顺序、审计保留和重复运行幂等性。
- [x] 5.6 更新系统总览、部署、恢复和发布升级文档；运行关联单元测试、Django 检查、OpenSpec 校验，并在 Windows 完成人工焦点与登录验证；经本次验收约定，Windows 结果等同适用于 macOS。

## 6. Retire obsolete surfaces

- [x] 6.1 将旧 Probe / Refresh 标识隔离到发布终态化工具，删除运行时注册、生产者、消费者和遗留测试依赖。
- [x] 6.2 将当前游戏控制台对接到 Bootstrap、Keepalive 与受控目录同步；移除误导性文案和二次操作入口。
- [x] 6.3 覆盖旧任务不可再入队、目录同步成功/失败保留缓存和新版控制入口，并更新运行与发布文档后完成最小验证。

## 7. Keepalive outcome and candidate lifecycle

- [x] 7.1 为 SessionBootstrap 提供无敏感信息的显式结果，保证 Keepalive 仅在新 lease 发布后成功，并按失败类别执行有限重试或终态失败。
- [x] 7.2 在候选失败时关闭未发布 HOME / Frame，保留适用旧 lease 的可用状态；维护循环独立回收已到期且无访问的退役 lease。
- [x] 7.3 覆盖中转失败、Frame 验证失败、保活 Job 状态、旧 lease 投影和无来源请求时的退役 target 回收，并更新系统文档与完成最小验证。

## 8. Console responsibility layout

- [x] 8.1 将当前操作游戏节点收敛为范围选择与目录刷新；将会话检查、手动保活和自动维护配置归位到各自会话节点，移除范围版本与诊断时间等重复主信息。
- [x] 8.2 覆盖控制台入口归属与保留的后端动作，并更新系统总览和完成最小验证。

## 9. Resilience drill follow-up

- [x] 9.1 将意外 Chrome 退出收敛为 Supervisor 的 session owner 替换，并使会话服务重启控制异步确认；避免正常替换被短控制超时误报失败。
- [x] 9.2 覆盖 Chrome 异常退出联动 session 替换、会话重启的立即确认语义，并更新运行手册与本次演练记录；执行关联测试、Django 检查和真实 Windows 恢复演练。

## 10. Repair broken lease lifecycle

- [x] 10.1 使 Repair 对缺失 HOME 或 Frame 显式建立候选替换 lease；回收已消失的当前 owner target，避免 HOME 注册表累积。
- [x] 10.2 覆盖损坏 Frame 的 Repair 替换、候选失败清理与失联 target 回收，并更新运行手册后完成最小验证。

## 11. Repair failure and Supervisor containment

- [x] 11.1 区分已损坏 lease 的 Repair 候选失败与保活候选失败的运行态投影；已损坏 lease 不得继续投影 READY。
- [x] 11.2 将 Chrome generation 启动超时隔离为可重试依赖失败，保持 Supervisor 控制面存活，并覆盖 Repair 失败投影、Chrome 超时与后续恢复测试。
