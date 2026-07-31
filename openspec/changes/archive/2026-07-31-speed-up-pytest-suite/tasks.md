## 1. 基线与测试边界盘点

- [x] 1.1 在执行任何 `uv run` 命令前运行 `uv sync --locked --all-groups`，记录当前串行全量 pytest 的耗时、失败数与 `--durations=30 --durations-min=0.5` 输出。
- [x] 1.2 盘点测试期间的 runtime 派生路径、httpx 真实发送入口、现有 HTTP mock 和可选外部集成开关，并将实际发现与设计假设对照。
- [x] 1.3 确认 pytest-django 直接加载测试 settings 模块，且该模块在导入 `config.settings` 前建立测试运行态；为该时序增加最小回归测试。

## 2. pytest 专用配置与每 worker 运行态隔离

- [x] 2.1 在 `config.settings` 中实现 pytest 专用启动标识和测试运行态目录解析：仅接受 `BASE_DIR/.pytest-runtime/` 下的路径，未启用测试标识时保持生产 runtime 与 `.env` 语义不变，非法测试路径快速失败。
- [x] 2.2 实现不导入 Django 业务模块的 `config.test_settings`，作为唯一测试运行态环境变量写入者；串行派生 `master`，xdist 派生稳定且唯一的 worker ID。
- [x] 2.3 在 settings 加载前，将每个 worker 的 `runtime.database_path`、静态文件、RuntimeStore 与 `worker-locks` 派生到 `.pytest-runtime/<worker-id>/`，并验证 pytest-django 数据库后缀不会被误认为锁隔离。
- [x] 2.4 实现会话启动时的易失状态清理，仅允许删除当前 worker 子树内白名单资源；保留数据库以支持 `--reuse-db`，并为根目录、仓库 `runtime/`、工作区根和越界路径建立拒绝删除回归测试。
- [x] 2.5 补充 scheduler/worker 回归用例：开发 runtime 锁存在时串行测试仍通过，且仓库 `runtime/` 的文件内容与时间戳不发生变化。

## 3. 默认 HTTP 护栏与模型测试确定性

- [x] 3.1 在 pytest 专用启动配置中显式关闭可选 AI/外部集成，并以固定无凭据的测试模型占位配置替换继承值；新增回归测试证明不读取开发 `runtime/.env`。
- [x] 3.2 实现覆盖项目 httpx 真实发送入口的全局 HTTP 护栏，使未 mock 的请求在连接前以非敏感诊断失败，同时保留现有 `httpx.Client`、`httpx.post` 和客户端方法 mock 路径。
- [x] 3.3 为 HTTP 护栏添加最小回归测试：遗漏模型 mock 时不发生网络等待，错误不泄漏凭据/正文/查询参数；固定响应和 `httpx` 异常替身仍可覆盖客户端分支。
- [x] 3.4 整理舆情模型结论测试为纯函数、客户端契约和报告集成三类；使用固定脱敏响应/`httpx` 异常覆盖成功、timeout、HTTP 状态、非 JSON、结构和本地约束失败。
- [x] 3.5 验证模型不可用不阻断报告，且报告与测试输出只暴露固定错误码/展示文案，不暴露上游正文、凭据或未脱敏输入。

## 4. 数据库复用与开发者指引

- [x] 4.1 配置并验证干净数据库、`--reuse-db` 和 `--create-db` 在每 worker 测试运行态下的正确行为，确保复用不保留 Worker/Scheduler lease、token、心跳或锁所有权。
- [x] 4.2 将 `.pytest-runtime/` 加入适当的忽略规则，并验证 Windows 文件占用下的清理重试或安全失败不会触及仓库 `runtime/`。
- [x] 4.3 更新测试开发文档，说明标准串行命令、数据库复用/重建时机、durations 参数、CI 的干净数据库要求、默认 HTTP 禁止规则及真实模型测试不在本 change 范围内。

## 5. 受门控的并行评估

- [x] 5.1 在完成运行态和 HTTP 隔离后，连续两次执行串行全量 pytest 并记录 durations 基线；任一次失败时先修复隔离问题，不开始并行试验。
- [x] 5.2 仅在串行基线通过后评估并添加 `pytest-xdist` 开发依赖；以固定 `-n 2` 验证每个 worker 的 Django 数据库、`runtime.database_path`、`worker-locks` 和 RuntimeStore 目录均不共享。
- [x] 5.3 连续两次执行 `-n 2` 全量测试并与串行基线比较墙钟时间；随后连续两次执行 `-n 3` 并确认收益更高。只有至少提升 20% 且无新增失败、重试或顺序依赖时，才在文档中列出可选并行命令。
- [x] 5.4 若并行未通过隔离或收益门槛，移除试验依赖/配置并记录结论；保持裸 `uv run pytest` 串行且不使用 `-n auto`。

## 6. 最终验证与变更核对

- [x] 6.1 在开发 Supervisor/Scheduler 同时运行的条件下执行 scheduler 最小验证，确认测试通过且仓库 `runtime/` 未变化。
- [x] 6.2 在存在开发模型配置的条件下执行报告与 HTTP 护栏最小验证，确认默认 pytest 不读取开发 `.env` 且无真实 HTTP 调用。
- [x] 6.3 执行两轮干净串行全量 pytest，并记录与基线相比的耗时、最慢测试和失败数；再按门槛完成两轮 `-n 2` 与两轮 `-n 3` 验证，并记录固定并发选择。
- [x] 6.4 按 OpenSpec 规格逐项审查实现和验证证据，确认没有改变生产运行态、AI 调用语义或裸 pytest 的串行语义。
