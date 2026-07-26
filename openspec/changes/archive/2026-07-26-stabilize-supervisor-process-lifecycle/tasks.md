## 1. 实例租约与服务守卫

- [x] 1.1 在 `service_supervisor.runtime` 定义版本化实例租约、随机实例标识、原子续约与过期判定；确保运行态不写入认证材料、CDP target、完整 URL 或密码。
- [x] 1.2 扩展服务状态和心跳的兼容读取与写入，写入当前实例标识；将 PID 保留为当前实例的诊断和正常停止辅助信息，禁止作为跨实例健康认领依据。
- [x] 1.3 将现有 Worker 心跳入口重构为统一租约守卫，在既有循环边界检查租约、上报心跳并于失效时沿现有优雅退出路径停止；逐个核对固定 Worker 的轮询睡眠与可取消处理单元不超过租约宽限期，不得为每个 Worker 新增守卫线程。
- [x] 1.4 为仅由 Supervisor 启动的 Web 接入同一租约守卫：明确 POSIX 优雅终止与 Windows 受控进程终止差异，同时保证手工 Web、管理命令和测试进程不受影响。

## 2. Supervisor 恢复与正常关闭

- [x] 2.1 在 Supervisor 获得独占锁后发布新实例租约；在迁移、静态资源收集和其他预检前等待旧实例 Web/Worker 发现实例替换并释放端口和既有 Worker 锁，等待超时不得执行预检或并发启动同职责 Worker。
- [x] 2.2 完善 `shutdown()`，按业务 Worker、会话服务、受控 Chrome、Web 的依赖顺序终止当前记录服务，完成停止尝试后才关闭控制端点、日志和文件锁；除人工停止外不得改写 `desired`。
- [x] 2.3 保持固定服务命令、capability 注入、日志轮转和控制 API 边界，不引入通用 `managed_process_runner`、Windows Job Object 或任意命令/PID 执行入口。

## 3. 受控 Chrome 与会话替换

- [x] 3.1 实现 Chrome 运行期健康验证：仅以 `127.0.0.1:9222` CDP 可连接性和已确认 `browser_generation` 判定健康，PID 变化但 generation 未变时不得重启。
- [x] 3.2 为受控 CDP 增加固定的 browser-close 操作，并在每次 Supervisor bootstrap 的 Chrome 启动前先通过 `Browser.close` 请求关闭 `127.0.0.1:9222`；连接断开后以端口释放判定结果，仅在端口仍被监听时才跨平台定位并终止监听 PID；等待端口释放失败时拒绝启动 Chrome/session Worker，不处理其他端口或浏览器。
- [x] 3.3 将游戏圈会话配对替换重构为候选 generation 状态机，确保未就绪时只核验同一候选、不会在调和周期反复打开 Chrome 或启动 session Worker。
- [x] 3.4 实现连续三次 generation 就绪失败或候选退出后的熔断、固定脱敏错误和手动重启复位；成功确认 generation 后清零失败计数。

## 4. 验证与文档

- [x] 4.1 为实例租约增加单元测试：续约、过期、实例替换、运行态损坏宽限、Worker 优雅退出、Windows Web 受控终止及 `desired` 在非人工停止路径保持不变。
- [x] 4.2 为 Supervisor 增加测试：macOS Chrome 启动器 PID 变化但 generation 不变时连续两次调和均不重启；Chrome 真正丢失时只创建一次候选；连续三次失败后不再自动启动；新实例不并发启动旧 Worker。
- [x] 4.3 为固定端口清理和正常关闭增加测试：优先 `Browser.close`、仅在端口未释放时处理 `127.0.0.1:9222` 监听 PID、CDP 断开但端口释放视为成功、清理失败拒绝启动；SIGINT 后当前记录服务停止且 8000/9222 释放；异常退出后 Chrome 可保留而 Web/Worker 在租约宽限期内退出。
- [ ] 4.4 在不含真实认证材料的受控环境完成 macOS、Windows、Linux 最小演练，确认端口专用约束、租约退出和 Chrome 熔断；更新运行与恢复手册、系统总览和 Supervisor 状态说明。
- [x] 4.5 运行聚焦测试、`openspec validate --all --strict`、迁移检查、静态检查与 `git diff --check`，记录无法在当前平台执行的演练边界。
