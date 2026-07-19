# Purpose

定义内容运营单体服务的运行时、任务、适配器和隐私边界。

## Requirements

### Requirement: Django 单体服务运行时
系统 SHALL 提供基于 Python 3.12 和 Django 5.2 LTS 的单体服务，包含领域应用、数据库迁移、健康检查端点以及供内部运营使用的 Django Admin 入口。

#### Scenario: 运行时配置有效时启动服务
- **WHEN** 必需的运行时配置齐全且已应用数据库迁移
- **THEN** 服务 SHALL 成功启动，提供健康检查端点，并向已认证的内部用户开放已配置的 Admin 入口

#### Scenario: 运行时配置无效
- **WHEN** 启动时缺少必需配置或配置校验失败
- **THEN** 服务 SHALL 快速失败并给出已脱敏、可处理的错误信息，且 SHALL NOT 记录缺失的密钥值

### Requirement: 运行态目录隔离
系统 SHALL 将部署配置、SQLite 数据库、收集后的静态文件、Worker 锁、Supervisor 独占锁、Supervisor 状态、Supervisor 凭据、服务 PID、心跳和可选日志统一置于项目根目录的 `runtime/`。代码包与版本控制 SHALL 排除整个目录；`.env.example` 仅作为可打包的配置模板。

#### Scenario: 初始化或打包服务
- **WHEN** 运营人员部署或打包服务代码
- **THEN** 系统 SHALL 能在缺少 `runtime/` 的代码包中初始化该目录，并从 `runtime/.env` 读取运行时配置，而不得将数据库、密钥、Supervisor 凭据、锁文件或日志混入代码目录

### Requirement: SQLite 部署边界与专用 Worker
系统 SHALL 在首期使用启用 WAL 模式、外键约束和 busy timeout 的 SQLite，并由本机 Supervisor 托管一个 Web 实例、一个 Scheduler Worker、一个实时采集 Worker、一个人工补采 Worker 与一个 AI 审核 Worker。四个 Worker SHALL 按职责隔离；SQLite 写入阶段会串行化，同一职责 SHALL NOT 启动多个实例。日常部署 MUST 通过 Supervisor 启动和控制这些服务；直接管理命令仅用于开发或故障诊断。

#### Scenario: 初始化数据库
- **WHEN** 服务初始化新的 SQLite 数据库
- **THEN** 它 SHALL 在正常处理任务前应用迁移，并配置 WAL 模式、外键约束和已配置的 busy timeout

#### Scenario: 专用 Worker 不互相认领任务
- **WHEN** Scheduler Worker、实时采集 Worker、人工补采 Worker 与 AI 审核 Worker 同时运行
- **THEN** Scheduler Worker SHALL 仅创建调度任务而不认领业务任务；实时采集 Worker SHALL 仅认领 `SCHEDULED_INGEST_WINDOW` 与 `STATUS_SYNC`；人工补采 Worker SHALL 仅认领 `MANUAL_INGEST_WINDOW`、`METRICS_REFRESH_WINDOW` 与升级前遗留的 `INGEST_WINDOW`；AI 审核 Worker SHALL 仅认领 OCR 与 AI 评估任务，且不得将另一类任务标记为未注册或失败

#### Scenario: 人工长任务不阻塞自动采集
- **WHEN** 人工补采 Worker 正在执行历史补采或范围互动刷新，且自动调度器创建新的 15 分钟任务
- **THEN** 新的 `SCHEDULED_INGEST_WINDOW` 或 `STATUS_SYNC` SHALL 由实时采集 Worker 独立认领，不得等待人工补采 Worker 完成

#### Scenario: 需要水平扩容
- **WHEN** 任一任务类型需要多个消费者，或 SQLite 写锁持续影响任务成功率
- **THEN** 部署 SHALL 迁移到 PostgreSQL 后再扩展 Worker 实例，而不得在 SQLite 上横向扩容同一类 Worker

### Requirement: 持久化后台任务执行
系统 SHALL 在数据库任务表中持久化定时和异步工作，记录任务类型、幂等键、状态、检查点、重试次数、下次执行时间和已脱敏的错误摘要。

#### Scenario: 中断的采集任务恢复
- **WHEN** 采集任务在保存页检查点后停止
- **THEN** 后续 Worker 运行 SHALL 从已持久化的检查点恢复，而不是重新处理已完成页面

#### Scenario: 可重试的任务失败
- **WHEN** 任务因可重试的外部服务错误失败且仍有重试次数
- **THEN** Worker SHALL 持久化一个有上限的指数退避下次执行时间，且 SHALL NOT 为相同幂等键创建重复任务

### Requirement: 外部服务适配器边界
系统 SHALL 将原平台、OCR 和 AI 集成隔离在内部适配器接口之后，领域服务 SHALL 使用标准化内部数据传输对象而不是供应商原始响应。

#### Scenario: 原平台响应字段变化
- **WHEN** 原平台响应缺少预期字段或具有不兼容结构
- **THEN** 适配器 SHALL 返回有类型的契约错误，领域服务 SHALL 记录非破坏性的失败或待处理任务状态

### Requirement: 公众平台只读页面适配器
系统 SHALL 将公众平台外层页面的只读调用隔离在专用 CDP 适配器中。适配器 SHALL 在当前受控 `mp.weixin.qq.com` 页面上下文内临时使用会话并返回标准化 DTO；它 SHALL NOT 供 scheduler、Worker 或任何上游写操作调用。

#### Scenario: 外层页面不可用
- **WHEN** 未找到已登录的受控公众平台页面或其响应结构无效
- **THEN** 适配器 SHALL 返回脱敏的可处理错误，调用方不得持久化认证上下文

### Requirement: 敏感配置与可观测性
系统 SHALL 仅从运行时配置或密钥存储获取会话凭据和第三方密钥，并 SHALL 输出带关联标识的结构化日志，同时脱敏令牌、Cookie、完整请求 URL 和非必要的个人身份标识。

#### Scenario: 记录外部请求失败
- **WHEN** 适配器请求失败
- **THEN** 系统 SHALL 记录任务或关联标识、适配器名称、安全错误类别和结果码，且不得记录凭据或完整的已认证请求 URL
