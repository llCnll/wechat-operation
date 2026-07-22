## ADDED Requirements

### Requirement: 帖子和受控操作的会话服务调用

帖子采集、帖子状态与互动读取、作者处置前置读取以及已确认的人工上游操作 SHALL 通过 `GameCircleSessionService` 的固定本地 IPC 契约执行。调用方 MUST 仅传递 execution scope、固定来源方法及其业务参数；不得自行扫描 CDP tab、传递 target ID/URL/token，或调用 `binduseridentity`。

会话服务 SHALL 选择对应 AppID 的 ACTIVE SessionLease，并在服务内完成身份使用、target 生命周期和单次来源请求。经认证、CSRF 二次确认的单条人工操作，以及当前游戏内最多 20 条且每条独立审计的沉底/屏蔽批量操作，可同步提交固定 `operation_id`，但不得自行管理 target、游戏切换、插件重建或身份绑定；人工上游写操作在会话续接完成后 MUST NOT 自动重放，运营人员必须重新确认。若写入已发送但服务在返回前中断，调用方 SHALL 标记待回读确认，不得再次发送该写请求。

明确插件失效时，Worker SHALL 保留 checkpoint，并在会话服务返回活动刷新状态时延后，不增加业务 retry；会话服务负责创建或复用刷新 Job。刷新直接失败时，Worker SHALL 以同一脱敏原因终止；刷新耗尽普通 retry 后，Worker SHALL 走自身有限 retry。网络、限流、5xx、权限、契约和普通业务错误 MUST NOT 触发刷新。

会话服务对来源契约失败 SHALL 只返回固定、脱敏的原因分类，例如“CDP 返回结构无效”“来源响应不是 JSON”或“来源载荷结构无效”。分类 MAY 包含 HTTP 状态等非认证元数据，但 MUST NOT 包含响应正文、URL、Cookie、token、账号或上游原始错误文本。人工操作审计 SHALL 保存该固定原因，以便区分写前读取失败与上游写入失败；该失败不得发送或重放写请求。

固定 IPC 的 `POSTS_GET_METRICS` 可选 `timeout_seconds`；调用方省略该字段时，会话服务 SHALL 使用 30 秒的默认上限，而不得将缺省值视为无效契约。显式值仍须为大于 0 且不超过 30 秒的有效数值。

#### Scenario: 人工写操作在插件失效后恢复
- **WHEN** 已确认写操作遇到明确插件失效且刷新 Job 成功
- **THEN** 系统 SHALL 恢复会话但不得重放该写操作
- **THEN** 运营人员必须重新提交确认

#### Scenario: 人工操作的详情回读返回非 JSON
- **WHEN** 写前详情读取收到无法解析为 JSON 的来源响应
- **THEN** 系统 SHALL 以固定的“来源响应不是 JSON”原因结束审计
- **THEN** 审计前后状态保持为空，且不得发送上游写入

#### Scenario: AI 建议的目标状态已由上游满足
- **WHEN** 人工确认批量执行沉底或屏蔽建议，且写前详情回读已是该建议的目标状态
- **THEN** 系统 SHALL 记录 `ALREADY_APPLIED` 审计、同步该回读状态并完成本地审核
- **THEN** 系统不得发送重复上游写入
- **THEN** 若回读为其他非正常状态，系统 SHALL 同步该状态、保持待审核并记录 `SKIPPED_UPSTREAM_NOT_NORMAL`

### Requirement: 状态巡检的成功语义

`STATUS_SYNC` SHALL 只在选中批次的每条帖子均完成来源详情回读、指标保存，或被来源明确确认删除时标记成功。任务结束 MUST 写入仅含 `attempted`、`refreshed`、`failed`、`limit` 和执行范围的检查点；没有选中帖子时以 `attempted=0` 正常成功。若任一来源读取、契约解析或指标保存失败，任务 MAY 继续处理其余帖子，但最终 MUST 进入既有有限 retry，且不得标记为 `SUCCEEDED`。

#### Scenario: 来源 Frame 不可用
- **WHEN** 状态巡检选中至少一条帖子，但任意来源读取未完成
- **THEN** 检查点 SHALL 记录该失败
- **THEN** Background Job SHALL 进入既有 retry，而不是显示成功
