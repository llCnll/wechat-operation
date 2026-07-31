# Test Suite Execution Isolation Specification

## Purpose

定义 pytest 在本地开发与持续集成中隔离运行态、阻止意外出站请求、保持模型结论测试确定性，并以可验证门槛控制固定小并发试验的规则。

## Requirements


### Requirement: pytest 专用运行态启动边界
系统 SHALL 在 Django settings 和业务模块加载之前，由 pytest-django 直接加载的测试专用 settings 模块建立测试专属运行态。测试运行态覆盖 MUST 使用 pytest/test 专用启动标识和环境变量，且测试目录必须解析到 `BASE_DIR/.pytest-runtime/` 下；默认生产配置不得因该机制获得通用运行态目录覆盖能力。

未设置精确测试启动标识时，系统 SHALL 保持当前 `runtime/`、`runtime/.env`、数据库、静态文件和配置解析行为。设置测试标识但缺少测试运行态目录、目录越出 `.pytest-runtime/` 或路径不可安全解析时，系统 MUST 在加载 settings 时失败，而不得回退读取开发 `runtime/.env`。

#### Scenario: 默认 pytest 不继承开发模型配置
- **WHEN** 仓库 `runtime/.env` 含有 AI base URL、API key、model 和启用开关
- **THEN** 默认 pytest 会话 SHALL 不读取这些值，并显式关闭可选外部集成

#### Scenario: 未使用测试启动标识的生产配置
- **WHEN** 非 pytest 进程以现有环境启动且没有测试启动标识
- **THEN** 系统 SHALL 继续使用仓库 `runtime/` 和既有 `.env` 配置语义

#### Scenario: 非法测试运行态路径
- **WHEN** pytest 启动标识存在但测试运行态路径不在 `BASE_DIR/.pytest-runtime/` 下
- **THEN** settings 初始化 MUST 失败，且不得创建、修改或删除该路径

### Requirement: 每 worker 测试状态隔离
系统 SHALL 将每个 pytest 执行 worker 的测试数据库源路径、Worker/Scheduler 锁、Supervisor lease/heartbeat/token/logs、静态文件和其他 RuntimeStore 派生资源置于 `.pytest-runtime/<worker-id>/`。串行运行的 worker ID SHALL 为 `master`；xdist 运行时 SHALL 使用稳定且唯一的 xdist worker ID。

`runtime.database_path` MUST 在 Django settings 加载前派生到对应 worker 子树，使从其父目录派生的 `worker-locks` 同样按 worker 隔离。pytest-django 的数据库名称后缀不能替代该路径派生。

#### Scenario: Scheduler 与开发服务并存
- **WHEN** 开发 Scheduler 或 Worker 已持有仓库 `runtime/worker-locks` 中的锁并执行 `run_scheduler` 测试
- **THEN** 测试 SHALL 使用 `.pytest-runtime/master/worker-locks` 中的独立锁并通过，且仓库 `runtime/` 中的锁、lease 与其他文件均不被测试创建、修改或删除

#### Scenario: 两个 xdist worker 执行锁相关测试
- **WHEN** pytest 以 `-n 2` 运行两个会调用 `worker_lock` 的测试 worker
- **THEN** 每个 worker SHALL 使用自身 `<worker-id>/worker-locks` 下的锁，且不会因另一 worker 持锁而报告同类 Scheduler 或 Worker 已运行

#### Scenario: 测试运行态安全清理
- **WHEN** pytest 会话开始清理可再生测试状态
- **THEN** 清理逻辑 SHALL 只删除当前 worker 子树中名称受白名单约束的易失资源，并拒绝删除 `.pytest-runtime/` 根、仓库 `runtime/`、工作区根目录或任何越界路径

### Requirement: 数据库复用与易失状态生命周期
系统 SHALL 保留 `.pytest-runtime/<worker-id>/` 以支持本地测试数据库复用，但 MUST 在每次会话开始清理 lease、heartbeat、token、日志、锁和其他易失运行态。测试数据库仅在使用 `--reuse-db` 时保留；默认 pytest 与 `--create-db` MUST 重建测试数据库。

系统 SHALL 保留 `uv run pytest` 作为干净数据库正确性基线，并提供 `uv run pytest --reuse-db` 与 `uv run pytest --create-db` 的使用规则。CI MUST 使用干净数据库验证，不得以 `--reuse-db` 替代。

#### Scenario: 本地第二次复用数据库
- **WHEN** 开发者在无迁移变化的情况下连续两次执行 `uv run pytest --reuse-db`
- **THEN** 第二次执行 SHALL 复用测试数据库并得到与干净基线一致的测试结果，且不继承上一会话的锁、lease、token 或心跳

#### Scenario: 强制重建测试数据库
- **WHEN** 迁移已变化、数据库损坏或开发者执行 `uv run pytest --create-db`
- **THEN** pytest SHALL 重建测试数据库，并在不读取或修改仓库 `runtime/` 的前提下完成测试

### Requirement: 默认 HTTP 出站请求护栏
系统 SHALL 在默认 pytest 会话中阻止项目代码发起未经测试明确替身化的出站 HTTP 请求。护栏 MUST 覆盖项目使用的 httpx 真实发送入口，并在连接开始前快速失败；诊断只能包含测试节点、请求方法和安全的主机信息，不得包含 Authorization、Cookie、查询参数或请求正文。

通过 mock、fake client 或替换发送入口明确模拟请求的测试 MUST 继续可用。第一阶段护栏 SHALL 限定于 httpx，不得为了覆盖外网而全局禁用未证实需要隔离的 socket、本地 IPC 或 CDP 测试边界。

#### Scenario: 漏掉模型 HTTP mock
- **WHEN** 常规测试启用模型结论客户端但没有替换其 httpx 请求
- **THEN** 测试 MUST 在连接或读取 timeout 之前失败，且不得访问配置的模型端点

#### Scenario: 模型客户端使用固定响应
- **WHEN** 测试以固定脱敏响应或 `httpx` 异常替换模型客户端的发送入口
- **THEN** 测试 SHALL 覆盖成功、timeout、HTTP 状态异常、非 JSON 或约束异常分支，而不访问网络

#### Scenario: 已 mock 的其他 httpx 客户端
- **WHEN** 既有测试替换 `httpx.Client`、`httpx.post` 或项目客户端发送方法
- **THEN** HTTP 护栏 SHALL 不阻止该替身行为，且测试仍可断言本地客户端契约

#### Scenario: 显式本地 HTTP 集成
- **WHEN** 测试需要访问本机控制服务并使用 `allow_http_hosts` marker
- **THEN** marker MUST 仅接受 `localhost` 或 loopback IP；任何公网、内网或非本机名称 MUST 在测试建立阶段失败，且不得发起连接

### Requirement: 可选 AI 结论的确定性测试契约
系统 SHALL 将舆情模型结论测试拆分为纯函数、客户端契约和报告集成三类。默认测试 MUST 断言结构性行为，而不得使用真实模型、真实凭据或真实模型生成的自然语言。

纯函数测试 SHALL 覆盖证据白名单、输入 hash、版本快照、结论 JSON 校验、结论类型、证据引用与运营限制。客户端契约测试 SHALL 覆盖模型成功响应及连接、读取、写入、连接池 timeout，HTTP 401/403/404/429/5xx，非 JSON、响应结构和本地约束失败，并断言稳定的 `ConclusionError` 错误码。报告集成测试 SHALL 验证模型关闭或不可用时报告仍成功，模型可用时仅持久化通过本地校验的结论。

#### Scenario: 模型不可用不阻断报告
- **WHEN** 报告运行期间模型客户端返回连接错误、读取超时或无效结论
- **THEN** 报告 SHALL 保持成功完成并记录对应的固定、非敏感结论不可用状态

#### Scenario: 模型结论通过本地约束
- **WHEN** 模拟模型返回包含有效结论类型、非空已知证据 ID 且不含受限内容的 JSON
- **THEN** 客户端 SHALL 返回已规范化的可用结论，并保留其证据引用

#### Scenario: 模型结论违反约束
- **WHEN** 模拟模型返回未知证据、无效类型、数值断言、受限运营用语或非 JSON 内容
- **THEN** 客户端 SHALL 返回对应的稳定失败分类，且不得将原始上游正文、凭据或未脱敏输入暴露给测试输出或报告展示

### Requirement: 受门控的并行试验
系统 SHALL 在运行态隔离完成且串行全量 pytest 连续两次通过之前，保持默认测试命令串行。并行测试仅可作为显式、固定小并发的试验，从 `pytest-xdist -n 2` 开始。

在推荐任何并行命令之前，试验 MUST 验证每个 worker 的 Django 测试数据库、`runtime.database_path`、`worker-locks` 和 RuntimeStore 路径均位于不同的 `<worker-id>` 子树，并在相同机器上连续两次全量通过。只有相对隔离后的串行基线至少减少 20% 墙钟时间、无新增失败、重试或顺序依赖时，文档才可将实测最优的固定小并发列为可选推荐命令；本次为 `-n 3`。裸 `uv run pytest` MUST 保持串行语义，且不得使用 `-n auto` 作为默认。

#### Scenario: 并行试验发现共享状态
- **WHEN** `-n 2` 全量验证出现 SQLite 锁、共享 runtime 文件写入、跨 worker 锁冲突或顺序依赖失败
- **THEN** 并行 SHALL 不被推荐或设为默认，且问题 SHALL 作为独立隔离缺陷处理

#### Scenario: 并行试验满足门槛
- **WHEN** 固定小并发在相同机器上连续两次全量通过，并且墙钟时间比串行基线降低至少 20%
- **THEN** 文档 MAY 将实测最优值提供为可选开发命令；本次为 `-n 3`，同时保留串行默认命令
