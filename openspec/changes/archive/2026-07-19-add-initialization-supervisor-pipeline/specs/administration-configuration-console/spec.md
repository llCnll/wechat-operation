## MODIFIED Requirements

### Requirement: 系统配置与追溯台导航和视觉
系统 SHALL 将 `/admin/` 呈现为与内容管理工作台一致的“系统配置与追溯台”，使用中文业务术语、明确的配置/运行追溯分区和统一的视觉层级。顶部 SHALL 提供内容管理、运营分析和系统配置的同源导航；当前页面 SHALL 明确标识为系统配置。首页 SHALL 在 Hero 下方、核心配置之前提供全宽“初始化与运行状态”Pipeline。

#### Scenario: 运营人员访问系统配置首页
- **WHEN** 已登录运营人员访问 `/admin/`
- **THEN** 系统 SHALL 先展示初始化与运行状态 Pipeline，再展示配置入口与只读追溯入口的分区说明，并提供内容管理和运营分析的导航链接，而不得仅呈现 Django 默认应用索引

## ADDED Requirements

### Requirement: 初始化与运行状态 Pipeline
系统 SHALL 在系统配置首页按依赖展示 Web、默认策略、AI/OCR、可选上游写入授权、CDP 登录、Scheduler、帖子采集 Worker、AI Worker 与总体就绪状态。每个节点 SHALL 显示当前状态、阻塞原因、最近检测时间和唯一下一步；系统 SHALL 突出第一个未满足的核心节点。帖子采集 Worker 节点 SHALL 汇总实时采集 Worker 与人工补采 Worker 两个受管服务的状态，并提供各自的脱敏日志入口。

核心审核能力就绪 MUST 同时满足：Web 健康、唯一启用且含规则的策略、AI 配置完整、CDP 登录有效、Scheduler 运行、实时采集 Worker 运行、人工补采 Worker 运行与 AI Worker 运行。OCR 配置不完整 SHALL 标记为降级能力；上游写入开关 SHALL 是可选高风险分支，且不得影响核心审核能力就绪。

#### Scenario: 存在未满足的核心依赖
- **WHEN** 已登录运营人员访问首页且存在未满足的核心 Pipeline 节点
- **THEN** 系统 SHALL 显示第一个阻塞节点及其唯一下一步，并不得将整体状态显示为已就绪

#### Scenario: 核心能力全部就绪
- **WHEN** 所有核心 Pipeline 节点满足
- **THEN** 系统 SHALL 将 Pipeline 收缩为紧凑就绪摘要，并允许运营人员展开查看各节点诊断信息

### Requirement: Pipeline 内的受控配置与服务操作
系统 SHALL 允许已认证运营人员在 Pipeline 中编辑白名单 AI/OCR 配置和上游写入开关。已有密钥 SHALL 仅显示是否已配置；空白密钥输入 MUST 保留原值，且系统不得回显、记录或返回密钥。保存配置后，系统 SHALL 明确列出受影响服务并通过 Supervisor 提供重启入口。

系统 SHALL 允许通过 Supervisor 启动、停止、重启 Scheduler、帖子采集 Worker 与 AI Worker，并展示脱敏日志。帖子采集 Worker 节点的启动、停止与重启 SHALL 对实时采集服务和人工补采服务执行相同的固定操作，并在任一子服务失败时明确显示失败服务；单个子服务日志 SHALL 可独立查看。该节点还 SHALL 提供“创建历史补采”弹窗：仅从本地可管理游戏目录选择单个游戏，默认城主别慌张，接受最近 1 至 744 小时，并仅创建 `MANUAL_INGEST_WINDOW` 而不在页面请求中连接 CDP。Web 节点 SHALL 仅提供健康诊断和重启请求，不得提供停止或启动操作。默认策略节点 SHALL 仅检查唯一启用策略及规则数量，并链接至既有策略编辑页面；CDP 节点 SHALL 仅复用受控 Chrome 的固定登录页打开和登录态检测能力。

#### Scenario: 保存 AI 配置并重启审核 Worker
- **WHEN** 运营人员保存完整的 AI 配置并确认重启受影响服务
- **THEN** 系统 SHALL 将白名单字段写入运行时配置、不得回显密钥，并请求 Supervisor 重启 AI Worker

#### Scenario: 未授权上游写入保持关闭
- **WHEN** 运营人员未显式确认高风险上游写入授权
- **THEN** 系统 SHALL 保持总开关和所有单项写开关关闭，并在 Pipeline 中显示为可选未授权状态

#### Scenario: 操作 Worker
- **WHEN** 运营人员从 Pipeline 请求启动、停止或重启一个固定 Worker
- **THEN** Django SHALL 仅代理对应 Supervisor 固定操作，并展示返回的状态、启动时间、最近退出码或脱敏日志摘要

#### Scenario: 从采集节点启动双采集 Worker
- **WHEN** 运营人员在帖子采集 Worker 节点请求启动
- **THEN** Django SHALL 依次请求 Supervisor 启动实时采集服务和人工补采服务，节点 SHALL 仅在两个服务均运行时显示已就绪；任一服务启动失败 SHALL 显示其服务名称和脱敏错误，不得将节点误报为已就绪
