## ADDED Requirements

### Requirement: 流程式帖子确认与规则配置

系统 SHALL 提供仅供已认证运营人员访问的评论导出工作区，并按“确认帖子 ID、配置规则、创建任务”三个顺序步骤呈现。工作区只使用当前活动游戏范围，不得提供 AppID、作者 OpenID、页大小、来源 URL、token、capability 或 CDP 参数输入。

第一步 SHALL 只接受正整数帖子 ID，并通过既有固定帖子详情读取展示标题、正文摘要、阅读/点赞/评论指标和作者昵称。成功确认后，系统 SHALL 签发不含正文、昵称、OpenID、认证材料或来源响应的短时确认凭据；只有有效确认凭据才能进入规则配置和创建步骤。第二步 SHALL 只接受 V1 规则支持的关键词；上一步失败、确认超时或规则非法时，后续步骤不得可用。

#### Scenario: 成功确认帖子并进入规则步骤
- **WHEN** 运营人员输入有效帖子 ID，固定来源返回属于当前活动游戏且带有效作者 OpenID 的帖子
- **THEN** 系统 SHALL 显示经净化的帖子摘要和作者昵称，签发短时确认凭据并激活规则步骤
- **THEN** 系统不得创建 Run、Job、结果行或平台写操作

#### Scenario: 未确认或确认失效时不能创建任务
- **WHEN** 运营人员未完成确认、使用过期/篡改确认凭据，或帖子来源与当前活动游戏不匹配
- **THEN** 系统 SHALL 拒绝进入创建步骤并返回固定可处理原因
- **THEN** 系统不得读取评论分页或写入 Run、Job、结果行

### Requirement: 版本化关键词与数字提取规则

V1 规则 SHALL 使用 `keyword-first-digit-v1`。运营人员 SHALL 输入一个去除首尾空白后长度为 1--100 字符的关键词；评论只有同时包含该关键词和至少一段 ASCII 连续数字时才命中，系统 SHALL 提取正文中第一段连续数字。来源 Unix 秒时间 SHALL 使用 `Asia/Shanghai` 格式化为 `YYYY-MM-DD HH:mm:ss`。

系统 MUST NOT 接受任意正则、任意代码、多个关键词组合、数字拼接、时区输入或未经版本化的提取策略。创建任务时 SHALL 冻结规则版本、归一化关键词和时区，后续表单编辑不得改变既有 Run。

#### Scenario: 命中评论提取第一段数字
- **WHEN** 冻结关键词为“甄嬛传”，评论正文包含该关键词和多段连续数字
- **THEN** 系统 SHALL 将第一段连续数字作为 `game_id` 保存，并以北京时间保存 `create_time`

#### Scenario: 不满足任一固定条件
- **WHEN** 评论缺少冻结关键词或不含 ASCII 连续数字
- **THEN** 系统 SHALL 不创建该评论的结果行，且继续处理同页其它评论

#### Scenario: 非法规则被拒绝
- **WHEN** 运营人员提交空白/超长关键词、任意正则或不支持的提取策略
- **THEN** 系统 SHALL 拒绝创建任务并保持在规则配置步骤

### Requirement: 重新验证并创建或复用任务

第三步提交 SHALL 携带帖子 ID、当前范围版本、规则和短时确认凭据。服务端 MUST 重新调用固定帖子详情读取，验证来源 AppID、作者 OpenID 和帖子内容指纹仍与确认凭据一致；验证通过后 SHALL 原子创建 `CommentExportRun` 与一对一 `COMMENT_EXPORT_V1` Job，冻结来源范围、帖子/作者快照、规则、创建者和页大小 100。

同一创建者、冻结范围、帖子、作者、规则版本和关键词存在 `PENDING` 或 `RUNNING` Job 时，系统 MUST 复用该活动 Run；其它创建者不得复用或查看该 Run。终态 Run 不得阻止同一创建者创建新的任务。创建成功页面 SHALL 明确显示成功反馈、Run/Job 标识和“前往任务列表”入口；不得在 HTTP 请求中读取评论分页或调用平台写操作。

#### Scenario: 创建任务成功
- **WHEN** 运营人员以仍有效且内容未变化的确认凭据提交有效规则
- **THEN** 系统 SHALL 创建或复用活动 Run，并显示“任务创建成功”和任务列表入口
- **THEN** HTTP 请求不得读取评论分页、写入结果行或调用上游写接口

#### Scenario: 确认后帖子内容变化
- **WHEN** 创建时重新读取的帖子指纹、来源 AppID 或作者 OpenID 与确认凭据不一致
- **THEN** 系统 SHALL 拒绝创建并要求运营人员重新确认帖子

### Requirement: 命中评论结果作为任务数据保存

系统 SHALL 使用 `CommentExportRun` 作为任务和权限边界，并使用 `CommentExportResult` 保存**命中**评论。Run SHALL 保存来源 AppID、帖子 ID、标题快照、作者 OpenID、作者昵称、规则版本、关键词、页大小、创建者、进度、结果就绪时间和关联 Job；Result SHALL 保存 Run、`reply_id`、评论者 OpenID、昵称、北京时间、正文和提取数字，并以 `(run, reply_id)` 唯一。

系统 MUST NOT 保存不命中评论、子回复、完整来源响应、认证材料或评论分页原始载荷。通用 Job 参数和错误摘要不得包含关键词、作者 OpenID、评论正文、评论者身份或结果行；Result 只能由其 Run 创建者在当前活动游戏范围内查看或导出。

#### Scenario: 只保存命中结果
- **WHEN** Worker 完成一个包含命中和不命中评论的页面
- **THEN** 系统 SHALL 只为命中评论创建 Result 行
- **THEN** 数据库中不得出现该页不命中评论的正文、昵称或 OpenID

#### Scenario: 创建者隔离结果访问
- **WHEN** 另一已认证用户请求非本人创建的 Run 或结果
- **THEN** 系统 SHALL 拒绝查看和导出，即使请求处于同一活动游戏范围

### Requirement: 人工补采 Worker 构建已发布结果集

只有人工补采 Worker SHALL 认领 `COMMENT_EXPORT_V1`。Worker MUST 使用冻结范围和作者 OpenID 调用 `POSTS_LIST_TOPIC_REPLIES`，只读取顶层评论，使用零基 `page`、固定 `limit=100`，并以 `page + 1` 请求下一页；`next_offset` 不得作为下一页输入。

每次 Worker 执行尝试开始时，系统 SHALL 原子删除该 Run 先前未发布的 Result 行，并将 Run 进度和 Job checkpoint 重置为零。Worker SHALL 按页去重并批量写入命中 Result 行，逐页更新处理数、命中数、页码和可用总数。只有读取末页且所有结果行持久化成功后，系统才可设置结果就绪并完成 Job；失败或中断产生的部分行不得在工作区、结果列表或导出中出现。

#### Scenario: 成功发布数据库结果
- **WHEN** Worker 读取到 `has_next=false` 并成功保存全部命中 Result 行
- **THEN** 系统 SHALL 标记 Run 结果就绪和 Job 成功，并展示最终处理数与命中数

#### Scenario: 中断或可重试失败后重建结果
- **WHEN** Worker 中断或发生可重试来源失败，随后同一 Job 再次执行
- **THEN** 系统 SHALL 先删除先前未发布的 Result 行并清零进度，再从第 0 页重新读取
- **THEN** 最终 Result 行和计数不得包含前一次尝试的重复数据

#### Scenario: 来源或分页契约异常
- **WHEN** 来源认证失效、帖子不可读、Frame 不可用、页码异常、异常空页或来源契约不符
- **THEN** 系统 SHALL 使用固定可处理错误延迟或终止 Job，且不得发布部分结果

### Requirement: 任务列表查看结果并从数据库导出 CSV

评论导出任务列表 SHALL 默认仅展示当前创建者、当前活动游戏范围内的 Run，并展示帖子、关键词、Job 状态、进度、命中数、创建时间、完成时间和固定失败摘要。只有 Job 成功且 Run 结果就绪时，系统 SHALL 提供结果详情与 CSV 导出入口。

结果详情 SHALL 分页读取已保存的 Result 行。CSV 导出 SHALL 只从 Result 行按稳定顺序流式生成 `utf-8-sig`，列严格为 `openid`、`nick_name`、`create_time`、`content`、`game_id`；导出不得调用来源、创建 Job、修改 Result 或保存运行态文件。

#### Scenario: 从完成任务导出 CSV
- **WHEN** Run 创建者在当前活动游戏范围内请求已成功且结果就绪的任务导出
- **THEN** 系统 SHALL 从数据库流式返回 CSV
- **THEN** 系统不得访问受控浏览器、来源接口或运行态文件

#### Scenario: 未完成任务不展示结果
- **WHEN** Run 对应 Job 仍待执行、运行中、失败或结果未就绪
- **THEN** 系统 SHALL 只展示任务状态和进度，不得展示 Result 行或 CSV 导出入口

### Requirement: 评论导出受控会话边界

页面的帖子确认和创建时重新验证 SHALL 仅调用既有固定帖子详情读取；评论分页读取 SHALL 只由 Supervisor 管理的人工补采 Worker 通过环境注入 capability 调用固定 IPC。Web、Run、Result、表单、响应、日志和通用 Job 参数 MUST NOT 读取、保存或显示 capability、token、Cookie、CDP target、JavaScript、任意 URL 或完整上游响应。

#### Scenario: 页面不持有评论读取能力
- **WHEN** 运营人员确认帖子、配置规则、创建任务、查看任务、查看结果或导出 CSV
- **THEN** 页面 SHALL 不构造评论分页客户端，也不得读取 capability 或调用评论分页来源
