## Context

项目使用 pytest、pytest-django 与 SQLite。pytest 当前通过 `config.settings` 启动 Django；该模块固定从仓库 `runtime/.env` 构造 `runtime`，并将数据库、静态文件和受 Supervisor/Worker 使用的文件放在仓库 `runtime/`。`jobs.worker.worker_lock()` 再以 `runtime.database_path` 的父目录生成锁目录。

因此，测试会继承开发机的 AI 配置和文件锁空间。已观测的全量基线为 `631 passed, 3 failed in 197.43s`：三个 `run_scheduler` 测试与正在运行的 Scheduler 争抢 `worker-locks`；四个舆情报告测试实际访问模型端点并等待超时，合计约 80 秒。模型结论是可选增强，模型失败不会让报告失败，因此常规测试不应依赖真实模型或网络。

本变更同时触及配置加载、Django 测试数据库、Supervisor/Worker 文件锁、HTTP 客户端测试和开发者命令。它必须满足以下约束：

- 生产进程在未设置测试启动标识时继续使用现有 `runtime/`、`.env` 与配置解析语义。
- 默认 pytest 不得向外部服务发请求、使用真实凭据或发送帖子正文等敏感数据。
- SQLite 数据库和文件锁在 xdist worker 间必须一起派生；只给 Django 数据库加后缀不足以解决 `runtime.database_path` 派生锁的共享问题。
- 本地循环可以复用测试数据库，CI 与发布前验证仍从干净数据库运行。

## Goals / Non-Goals

**Goals:**

- 令默认 pytest 与开发/生产 `runtime`、`.env`、服务锁和网络状态完全脱钩。
- 令未被测试明确替身化的 HTTP 调用快速、可诊断地失败，而不是消耗连接或读取 timeout。
- 用确定性的 fake/mock 覆盖模型客户端和报告的成功、失败与展示边界。
- 提供数据库复用和耗时观测的稳定开发体验，并以可重复测量决定是否推荐有限并行。

**Non-Goals:**

- 不改变 AI 结论提示词、模型提供商、生产 timeout、失败语义或报告内容。
- 不将测试 runtime 覆盖设计为生产服务的通用运行时配置能力。
- 不引入真实模型冒烟测试、真实模型凭据、响应录制或 live 测试标记；需要该能力时另行设计成本、凭据和数据脱敏边界。
- 不承诺默认启用 xdist，也不通过降低真实网络 timeout 伪造性能收益。
- 不为所有生产客户端引入新的依赖注入框架；现有局部 fake/mock 足以表达外部边界时不做架构性重写。

## Decisions

### 1. 使用 pytest 专用启动标识和受限路径，而非通用 runtime 覆盖

新增 `config.test_settings` 作为 pytest-django 的 `DJANGO_SETTINGS_MODULE`。该模块不导入 Django 业务模块，在导入 `config.settings` 前设置测试启动标识、测试运行态目录和 xdist worker 标识。变量命名必须带 pytest/test 前缀，且不得作为生产配置文档的一部分。

`config.settings` 仅在测试启动标识精确匹配时读取测试运行态目录；目录必须解析为 `BASE_DIR/.pytest-runtime/` 下的子目录，否则立即失败。未设置标识时，`RUNTIME_DIR`、`.env`、数据库、静态文件和现有环境变量解析保持原样。测试模式下不加载 `runtime/.env`，并以固定、无凭据的测试模型占位配置替换继承的模型 URL/key/model；真实发送仍由 HTTP 护栏阻止。

该边界把“测试为启动 pytest 而改变路径”与“生产支持任意 runtime 覆盖”区分开。即使误将测试变量带入非 pytest 进程，路径与启动标识校验也会拒绝越出 `.pytest-runtime/` 的目标；正常生产进程则完全不进入该分支。

**替代方案：**

- 使用通用 `OPERATION_RUNTIME_DIR`：会无意新增生产配置入口，可能移动数据库、锁和静态文件，违反不改变生产语义的目标。
- 在 conftest fixture 中 monkeypatch `runtime.database_path`：此时 Django settings 和命令模块可能已加载，不能可靠覆盖导入时配置与所有锁。
- 继续读取 `runtime/.env`，只 patch 少数慢用例：无法阻止未来外部调用或文件状态重新泄漏。

### 2. 以每 worker 独立的测试状态树同时隔离数据库和锁

测试状态根固定为仓库忽略的 `.pytest-runtime/`。测试 settings 模块从 xdist worker 标识导出稳定的 worker 名称：串行测试使用 `master`，并行 worker 使用其 xdist ID。每个 worker 的状态根为 `.pytest-runtime/<worker-id>/`，并在 Django settings 加载前用于构造 `runtime.database_path`。因此 `jobs.worker.worker_lock()` 从该数据库路径派生的 `worker-locks` 也天然按 worker 隔离。

每个 worker 的状态树包含测试数据库源路径、`worker-locks`、Supervisor lease/heartbeat/token/logs、静态文件和其他 RuntimeStore 派生资源。pytest-django 的数据库后缀机制继续处理数据库名称；本地插件负责 runtime 路径，不假定数据库后缀会反向更新 `config.settings.runtime.database_path`。

`.pytest-runtime/` 是受控且可复用的测试状态根，而不是会话结束后整体删除的临时目录。会话启动时只清理易失状态（Supervisor/Worker 文件、锁目录、静态输出等）；测试数据库仅在不使用 `--reuse-db` 或显式 `--create-db` 时重建。所有清理操作必须先解析路径、确认目标位于当前 worker 的 `.pytest-runtime/<worker-id>/` 下并且目标名称在白名单内，禁止递归清理 `.pytest-runtime/` 根、仓库 `runtime/`、工作区根目录或用户输入路径。

Windows 上遇到短暂文件占用时，白名单目录清理最多短暂重试三次；仍无法删除时立即保留目标并让测试启动失败，绝不扩大清理范围或触及开发 `runtime/`。

**替代方案：** 单个共享 `.pytest-runtime/` 仅在串行时足够，但 `-n 2` 下仍会共享相同锁文件；随机临时目录虽简单，却无法支持本地 `--reuse-db`。故选择按 worker 分区的受控持久根。

### 3. 默认关闭可选集成，并在 httpx 发送边界快速失败

测试启动配置显式关闭可选 AI 与外部集成，同时清空 URL、API key 和模型名。涉及模型客户端的测试必须在用例内显式开启所需开关，并以固定脱敏响应、fake 或 `httpx` 异常替代请求。

新增全局 autouse HTTP 护栏，覆盖项目实际使用的 httpx 真实发送入口。未经用例明确替身化的请求必须在开始连接前抛出测试错误，错误只包含测试节点与安全的请求方法/主机信息，不包含 Authorization、Cookie、查询参数或请求正文。替换 `httpx.Client`、`httpx.post` 或项目客户端发送方法的既有测试路径必须继续可用。

真实本地控制服务的集成测试可通过 `allow_http_hosts` marker 显式列出主机，但该 marker 只接受 `localhost` 或 loopback IP；不得用它为公网、内网或名称解析目标建立出站例外。

第一阶段只拦截 httpx，因为它是已证实的泄漏边界；不直接禁用 Python socket，以免误伤 CDP mock、Django 本地测试服务或未来本地 IPC。受护栏全量运行若暴露其他真实网络库，应另行列入变更范围并为该库增加明确护栏与回归测试。

### 4. 使模型结论测试完全确定性

模型测试分为三层：

1. 纯函数：证据白名单、hash、版本快照、结论 JSON 校验、禁用词和证据引用；无 Django、无网络。
2. 客户端契约：以 fake/mock 覆盖成功响应、连接/读取/写入/连接池 timeout、HTTP 401/403/404/429/5xx、非 JSON、结构异常和本地约束异常；断言稳定的 `ConclusionError` 错误码。
3. 报告集成：验证模型关闭或不可用时报告仍成功，以及模型可用时只保存已通过本地校验的结论。

断言只针对结构、结论类型、证据 ID、错误码与脱敏边界，不对模型生成的自然语言逐字断言。真实模型端到端连通性不属于默认或本次测试范围。

### 5. 分层提供数据库命令和受门控的并行试验

保留 `uv run pytest` 作为干净数据库的正确性基线。为本地循环提供 `uv run pytest --reuse-db`；迁移变化、数据库损坏或需要重演创建过程时使用 `uv run pytest --create-db`。文档必须说明 `--reuse-db` 只复用测试数据库，不保留上一会话的 lease、token、心跳或锁所有权；CI 不以数据库复用替代干净环境验证。

性能观测使用 `--durations=30 --durations-min=0.5`，记录全量耗时、失败数和最慢用例。完成运行态隔离后，先在相同机器执行两轮串行全量测试。只有均通过，才安装并试验 `pytest-xdist`，固定从 `-n 2` 开始，并比较相邻固定小并发。每轮并行试验必须验证：

- 每个 worker 的 Django 测试数据库不同；
- 每个 worker 的 `runtime.database_path`、`worker-locks` 和 RuntimeStore 路径均位于自身 `<worker-id>` 子树；
- 无跨 worker 文件写入、SQLite 锁、重试或顺序依赖失败。

仅当候选固定并发在相同机器连续两轮全量通过，且相对隔离后的串行基线至少减少 20% 墙钟时间时，文档才可把它列为可选开发命令。本次验证中 `-n 3` 两轮约为 40 秒，优于 `-n 2` 的两轮结果；`-n 4` 回退，因此文档固定推荐 `-n 3`。裸 `uv run pytest` 始终保持串行，禁止 `-n auto` 作为默认。

## Risks / Trade-offs

- [测试 settings 加载时序晚于 Django settings] → 由 pytest-django 直接加载 `config.test_settings`，该模块先建立测试路径再导入 `config.settings`；增加回归测试证明该时序，禁止依赖 conftest 的晚期 fixture。
- [测试变量被带入非 pytest 进程] → 使用 pytest 专用命名、精确启动标识与 `.pytest-runtime/` 边界验证；未满足条件时拒绝测试覆盖分支。
- [固定测试状态根被误删] → 清理只允许当前 worker 子树中的白名单资源；测试覆盖拒绝根目录、`runtime/`、工作区根及越界路径。
- [`--reuse-db` 残留数据库状态掩盖迁移问题] → CI 和最小全量验证保持干净数据库，文档明确 `--create-db` 使用时机。
- [HTTP 护栏遗漏客户端库或破坏既有 mock] → 先覆盖 httpx 真实发送边界，测试漏 mock 快速失败和现有 mock 兼容；其他库以受护栏基线的实际发现为准。
- [xdist 放大 SQLite 锁或共享状态] → 运行态按 worker 派生，且并行仅为后置、可撤销试验；未通过隔离或收益门槛则不保留依赖或推荐命令。

## Migration Plan

1. 先实现测试 settings 的路径与配置隔离、每 worker 状态派生、易失状态清理及路径安全测试。
2. 添加默认外部集成关闭与 httpx 护栏，修正模型/报告用例为确定性 fake/mock。
3. 在开发 `.env` 存在、开发 Scheduler/Worker 运行的条件下完成最小隔离验证；仓库 `runtime/` 必须无变化。
4. 验证干净数据库、`--reuse-db` 第二次运行和 `--create-db`，然后记录两轮串行 durations 基线。
5. 仅在前述步骤通过后从 `-n 2` 开始试验固定小并发；选择连续两轮通过且满足收益门槛的最优值，未通过时移除试验依赖/配置并记录结果。

本变更只影响测试工具链和开发文档，不需要生产数据迁移、服务重启或发布配置变更。回滚为移除测试插件/fixture/文档与可选 xdist 依赖；生产运行态不受影响。

## Open Questions

- 受 httpx 护栏的全量基线是否会暴露其他未 mock 的网络库；仅以实际发现决定是否扩展范围。
- Windows 文件占用下，哪些 `.pytest-runtime/<worker-id>/` 易失资源需要在会话启动时以可重试方式清理；实现时记录最小可重现验证。
- xdist 若达到门槛，pytest-django 数据库后缀与本地 worker 状态插件的组合是否需要额外 hook；以两轮全量结果确认，不预设并行成为默认。
