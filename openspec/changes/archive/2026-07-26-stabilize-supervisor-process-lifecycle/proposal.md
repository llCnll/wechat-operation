## Why

Supervisor 目前以保存的 `Popen.pid` 判断受控 Chrome 是否存活。在 macOS 的启动器 PID 交接场景中，浏览器实际可用时仍可能被误判为退出，进而每次调和都重建 Chrome 和会话 Worker，持续打开空白页。现有 `shutdown()` 也没有按依赖顺序终止受管服务；当 Supervisor 异常退出时，Web 和 Worker 可能继续运行且不再受控。

需要将 Web 和 Worker 的现有心跳升级为 Supervisor 租约守卫，使业务服务在 Supervisor 消失后自行退出；Chrome 不参与异常退出后的自清理，而以固定 `9222` CDP 端口和 browser generation 消除 PID 误判。该部署明确约定：`127.0.0.1:9222` 仅供本项目受控 Chrome 使用。

## What Changes

- 为每次 Supervisor 实例创建短期本地租约；复用并扩展 Web/Worker 现有心跳，使其在上报前确认租约仍属于当前实例，租约过期或实例替换后走既有优雅退出路径。
- 保留 `desired` 作为持久化的运营运行意图。人工停止之外，Supervisor 正常退出、异常退出和服务自退出均不得改写它；新 Supervisor 依据它重新拉起服务。
- Chrome 健康改为固定回环 `9222` CDP 可连接性与已确认 `browser_generation`；PID 仅作诊断或正常关闭的辅助信息，不能作为存活判断。
- 将 Chrome 与游戏圈会话替换收敛为单一 generation 状态机。候选 Chrome 未就绪时只等待和核验同一候选，不得在每次调和中反复打开浏览器；连续失败达到阈值后熔断并要求人工重启。
- Supervisor 正常关闭时按依赖顺序停止 Web、Worker、会话与 Chrome；异常被杀时 Web/Worker 会因租约失效自行退出，遗留 Chrome 可以保持运行。
- 新 Supervisor 启动时，在拉起新 Chrome 前无条件清理 `127.0.0.1:9222`：优先通过该端口的 CDP `Browser.close` 优雅关闭，端口未释放时才终止监听 PID；再创建新的 Chrome generation 和 session Worker。此行为依赖该端口专用约束，不对未知端口或浏览器做扫描、接管或处理。
- 补充跨 macOS、Windows、Linux 的测试和运行手册，覆盖租约自退出、PID 交接、一次性端口清理、熔断、正常关闭与固定端口使用边界。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `local-service-supervisor`: 变更 Web/Worker 的 Supervisor 租约、服务恢复、正常关闭和固定端口清理语义。
- `game-circle-login-continuity`: 变更受控 Chrome 的健康判定、启动恢复和配对替换边界，避免 PID 误判引发重复空白页。

## Impact

- 受影响代码：`service_supervisor/manager.py`、`service_supervisor/runtime.py`、Web/Worker 的公共运行循环、`tests/test_service_supervisor.py` 及会话连续性测试。
- 受影响运行态：`runtime/supervisor/` 新增实例租约并为心跳写入实例标识；不写入认证材料、Cookie、CDP target、完整来源 URL、二维码目标或密码。
- 受影响平台：macOS、Linux、Windows。服务自退出只依赖 Python 运行态协议；`9222` 监听进程的终止使用跨平台本机进程 API。
- 受影响文档：运行与恢复手册、系统总览及本机 Supervisor 规格。
