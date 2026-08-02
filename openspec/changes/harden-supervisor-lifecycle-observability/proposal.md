## Why

当前 Supervisor 正常关闭时只终止运行态中记录的单个 PID；若历史重复启动覆盖了该记录，旧 Worker 只能等待最长 30 秒的协作式租约失效后自行退出，正在执行的处理单元还会更晚停止。受控 Chrome 的累计启动次数跨实例累积且没有替换原因审计，运营人员无法判断观察到的是浏览器重启、会话页面 target 修复，还是 Chrome 自身 Renderer/Updater 活动。

需要将“关闭后遗留进程的收敛”和“Chrome / 会话页面生命周期的可解释性”纳入同一可验证边界，避免并发 Worker、错误排障和误判浏览器重启。

## What Changes

- Supervisor 正常关闭、重启接管和启动前回收时，先使旧实例租约立即不可用；保留 `desired`，但禁止旧实例开始新的处理单元。
- 将每个实例实际启动的 Web/Worker 进程登记为可验证的最小进程清单，而非每个服务仅保存最后一个 PID；正常关闭和新实例启动均回收同一实例的全部可证明归属进程。
- 为受管服务新增脱敏生命周期事件：启动、优雅停止、强制停止、健康判定替换、实例接管和回收失败均记录固定且可区分的原因码、检测结果类别、触发路径、服务名、实例内代次、时间与退出结果；不得记录 PID、实例标识、capability、Cookie、token、target ID、完整 URL 或命令行。
- 将启动计数拆分为当前 Supervisor 实例内计数与保留的历史累计计数；状态页优先显示当前实例计数和最近一次生命周期事件，避免将历史累计误读为当前循环重启。
- 为游戏圈会话服务新增脱敏 target 生命周期摘要与固定原因码，区分“创建/关闭 HOME 或 Frame target”与“受控 Chrome 进程替换”；维护循环不得因诊断而增加 target 创建、激活、关闭或上游调用。
- 在系统配置运行追溯中以只读摘要显示最近受控 Chrome 生命周期、会话 target 动作和当前实例内重启次数；日志详情保持既有访问控制和脱敏边界。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `local-service-supervisor`: 收紧正常关闭、遗留进程回收、租约失效、服务状态与生命周期追溯要求。
- `observable-controlled-session-page`: 增加服务自有会话 target 动作的脱敏追溯，并明确其与 Chrome 进程替换的语义区分。

## Impact

- 影响 `service_supervisor/manager.py`、`service_supervisor/runtime.py`、服务状态/控制台投影及其测试。
- 影响 `config/game_circle_service.py`、会话运行态投影、管理台只读展示与相关测试。
- 不新增外部服务、数据库依赖、CDP 权限或浏览器输入；运行态文件继续只位于 `runtime/supervisor/`，敏感能力不得进入事件、状态、日志或 HTTP 响应。
- 需要覆盖 macOS、Windows 和 Linux 的进程回收语义；本机可执行最小验证，Windows/Linux 真实演练列入 PRD 待办。
