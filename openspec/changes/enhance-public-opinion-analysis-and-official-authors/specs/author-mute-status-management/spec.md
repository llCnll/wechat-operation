## MODIFIED Requirements

### Requirement: 作者目标禁言操作与独立审计
系统 SHALL 将 `MUTE` 解释为当前游戏作者目标的覆盖型期限写入：每次经确认的禁言均 SHALL 以本次提交的受控期限计算新的目标截止时间，并向上游发起一次禁言写入。写前核验显示作者已有当前游戏有效禁言 MUST NOT 使该确认返回“已执行”或跳过写入；不同期限是不同的操作目标。`MUTE` 不得先执行 `UNMUTE`，永久禁言续期同样 SHALL 直接覆盖为 30 天。

作者目标操作仍 SHALL 经认证、CSRF、写能力开关、作者详情身份一致性核验、确认令牌、同一 `(openid, source_appid)` 的执行中互斥和独立审计。写后回读 MUST 只使用固定作者详情接口，并在同一次确认内按有限次数读取；无论回读失败、超时或目标不符，系统不得重发该次禁言写入。`MUTE` 回读 SHALL 仅在当前游戏截止时间与本次目标截止时间处于受控平台传播容差内时成功，不得以“截止时间不早于目标”替代该判断；`UNMUTE` 保持已确认无有效当前游戏禁言即可成功的语义。

#### Scenario: 已禁言作者延长为新期限
- **WHEN** 一名作者当前游戏仍被禁言至 1 天后，运营人员确认将其禁言设为 3 天
- **THEN** 系统 SHALL 以新计算的 3 天目标截止执行一次 `MUTE` 写入，并以该目标验证有限回读
- **THEN** 系统不得将旧禁言表示为本次操作已完成、不得先解除禁言，也不得在回读窗口内重复写入

#### Scenario: 永久禁言直接覆盖续期
- **WHEN** 启用永久意图的作者当前游戏禁言剩余不足续期阈值
- **THEN** 续期 Job SHALL 直接执行一次目标为 30 天的 `MUTE` 写入和有限回读
- **THEN** 系统不得执行 `UNMUTE`；写入或回读失败时 SHALL 保留永久意图并记录可处理失败审计

#### Scenario: 旧期限比目标更晚
- **WHEN** 写前或写后观察到的当前游戏截止时间晚于本次目标截止，但不在该目标截止的受控容差内
- **THEN** 系统 SHALL 将本次 `MUTE` 标记为未确认而非成功
- **THEN** 系统不得在同一确认内再次写入或篡改已有禁言状态

## ADDED Requirements

### Requirement: 平台当前禁言名单只读导入
禁言管理 SHALL 提供“导入平台当前禁言名单”入口。该入口 MUST 仅创建或复用 `BLOCKED_USER_LIST_SYNC` Job；Web 请求不得读取上游、执行禁言或解除禁言。Job MUST 冻结当前游戏 AppID 和范围版本，并由 `mute_maintenance` Worker 经既有游戏圈会话服务调用 `gameopenmpwap/cfcommcgi/spacexcontent_gamecircletopicmanage_getblockuserlist`。

上游请求 SHALL 从 `page=0` 开始，使用已验证的最大 `page_size`。若该大小被上游明确拒绝，或在 `total > 0` 的首页返回空名单或不足预期条数而不能形成可用页，系统 MAY 只按受控降级阶梯尝试更小大小；首个可用大小 MUST 写入该 Job 的无身份 checkpoint，后续页面不得再改变大小。每个冻结 AppID 与范围版本的该任务类型最多允许一个 `PENDING` 或 `RUNNING` Job；重复提交 MUST 返回该活跃 Job。

Worker SHALL 只读取分页响应的 `total` 和 `blocked_user_list`，并仅为当前仍有效的记录同步 `openid`、可用昵称和 `end_date`。`end_date` MUST 按 `Asia/Shanghai` 解析为当前游戏 `local_muted_until`。每个 checkpoint 只可保存页码、稳定页大小、总数、完成标记以及处理/导入/跳过的聚合计数；不得保存 OpenID、昵称、token、头像、审批人、违规原因、原始响应或其他身份材料。

导入 MAY 创建新的 `AuthorMuteStatus` 或更新名单中已有状态，但 MUST NOT 修改 `global_muted_until`、`last_source_post`、`permanent_mute_enabled`、永久意图审计或任何本系统禁言审计。名单中缺席的本地作者 MUST NOT 被清除、解除禁言、移出禁言管理或取消永久意图。任务不得调用帖子详情、作者详情、禁言或解除禁言上游接口。

#### Scenario: 导入当前平台禁言用户
- **WHEN** 运营人员在一个有效当前游戏范围内请求导入，且上游名单返回当前仍有效的作者
- **THEN** 系统 SHALL 通过只读分页 Job 为该作者创建或更新当前游戏禁言状态，并使其出现在禁言管理
- **THEN** 系统 SHALL 保存可用昵称和解析后的当前游戏截止时间，不得设置永久意图或关联来源帖子

#### Scenario: 平台名单未包含已有作者
- **WHEN** 本地存在有效禁言或永久意图作者，而该作者未出现在本次平台名单的任一页面
- **THEN** 系统 SHALL 保持该作者的已有禁言投影和永久意图不变
- **THEN** 系统不得把名单缺席解释为上游解除、到期或权限不足的确定证明

#### Scenario: 上游页大小需要降级
- **WHEN** 初始的已验证页大小被上游明确拒绝，或其首页在总数非零时返回空名单
- **THEN** Job SHALL 仅按受控降级阶梯尝试更小页大小，直到获得首个成功响应或阶梯耗尽
- **THEN** 成功后 SHALL 在本 Job 后续页面复用该大小，并在任务反馈中以脱敏聚合信息说明处理结果

#### Scenario: 导入不触发其他作者读取或写入
- **WHEN** 平台禁言名单导入处理一页记录
- **THEN** 系统不得读取任何帖子详情或 `getuserdetail`，也不得调用禁言或解除禁言接口
- **THEN** 任务的网络记录只包含受控的名单 GET 请求及既有会话建立所需请求
