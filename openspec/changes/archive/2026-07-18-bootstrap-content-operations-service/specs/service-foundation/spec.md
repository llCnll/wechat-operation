## ADDED Requirements

### Requirement: Django 单体服务运行时
系统 SHALL 提供基于 Python 3.12 和 Django 5.2 LTS 的单体服务，包含领域应用、数据库迁移、健康检查端点以及供内部运营使用的 Django Admin 入口。

#### Scenario: 运行时配置有效时启动服务
- **WHEN** 必需的运行时配置齐全且已应用数据库迁移
- **THEN** 服务 SHALL 成功启动，提供健康检查端点，并向已认证的内部用户开放已配置的 Admin 入口

#### Scenario: 运行时配置无效
- **WHEN** 启动时缺少必需配置或配置校验失败
- **THEN** 服务 SHALL 快速失败并给出已脱敏、可处理的错误信息，且 SHALL NOT 记录缺失的密钥值

### Requirement: SQLite 部署边界
系统 SHALL 在首期使用启用 WAL 模式、外键约束和 busy timeout 的 SQLite，且 SHALL 仅支持一个 Web 实例和一个活跃的数据库写入 Worker。

#### Scenario: 初始化数据库
- **WHEN** 服务初始化新的 SQLite 数据库
- **THEN** 它 SHALL 在正常处理任务前应用迁移，并配置 WAL 模式、外键约束和已配置的 busy timeout

#### Scenario: 不支持的 Worker 并发数
- **WHEN** 运行时配置请求多个活跃的数据库写入 Worker
- **THEN** Worker 启动 SHALL 拒绝运行，并报告单 Worker 的部署约束

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

### Requirement: 敏感配置与可观测性
系统 SHALL 仅从运行时配置或密钥存储获取会话凭据和第三方密钥，并 SHALL 输出带关联标识的结构化日志，同时脱敏令牌、Cookie、完整请求 URL 和非必要的个人身份标识。

#### Scenario: 记录外部请求失败
- **WHEN** 适配器请求失败
- **THEN** 系统 SHALL 记录任务或关联标识、适配器名称、安全错误类别和结果码，且不得记录凭据或完整的已认证请求 URL
