# Purpose

定义原创帖子采集、内容投影、业务删除判定和运营工作台的行为边界。

## Requirements

### Requirement: 帖子和受控操作的会话服务调用

帖子采集、帖子状态与互动读取、作者处置前置读取以及已确认的人工上游操作 SHALL 通过 `GameCircleSessionService` 的固定本地 IPC 契约执行。调用方 MUST 仅传递 execution scope、固定来源方法及其业务参数；不得自行扫描 CDP tab、传递 target ID/URL/token，或调用 `binduseridentity`。

经认证、CSRF 二次确认的单条人工操作，以及当前游戏内最多 20 条且每条独立审计的沉底/屏蔽批量操作，可同步提交固定 `operation_id`。人工上游写操作在会话续接完成后 MUST NOT 自动重放；若写入已发送但服务在返回前中断，调用方 SHALL 标记待回读确认，不得再次发送该写请求。明确插件失效时，Worker SHALL 保留 checkpoint，并在会话服务返回活动刷新状态时延后，不增加业务 retry；网络、限流、5xx、权限、契约和普通业务错误 MUST NOT 触发刷新。

会话服务对来源契约失败 SHALL 只返回固定、脱敏的原因分类；人工操作审计 SHALL 保存该固定原因，以区分写前读取失败与上游写入失败。固定 IPC 的 `POSTS_GET_METRICS` 可选 `timeout_seconds`；调用方省略时，会话服务 SHALL 使用 30 秒默认上限。

#### Scenario: 人工写操作在插件失效后恢复
- **WHEN** 已确认写操作遇到明确插件失效且刷新 Job 成功
- **THEN** 系统 SHALL 恢复会话但不得重放该写操作，运营人员必须重新提交确认

#### Scenario: 人工操作的详情回读返回非 JSON
- **WHEN** 写前详情读取收到无法解析为 JSON 的来源响应
- **THEN** 系统 SHALL 以固定的“来源响应不是 JSON”原因结束审计，审计前后状态保持为空，且不得发送上游写入

#### Scenario: AI 建议的目标状态已由上游满足
- **WHEN** 人工确认批量执行沉底或屏蔽建议，且写前详情回读已是建议目标状态
- **THEN** 系统 SHALL 记录 `ALREADY_APPLIED` 审计、同步该回读状态并完成本地审核，不得发送重复上游写入

### Requirement: 禁言操作的默认确认值
所有会发起禁言的人工处置确认 SHALL 默认选择永久禁言意图、“其他”违规原因和 30 天禁言期限。默认值 MUST 由服务端校验；运营人员 MAY 在二次确认前修改违规原因、期限或取消永久意图。

系统只有在禁言上游回读成功后才可保存永久意图；屏蔽成功但禁言未成功时不得将作者显示为永久禁言。

#### Scenario: 打开禁言确认框
- **WHEN** 运营人员从任一人工处置路径选择禁言
- **THEN** 表单 SHALL 预选永久禁言
- **THEN** 表单 SHALL 预选“其他”和 30 天
- **THEN** 运营人员仍可在提交前修改这些值

#### Scenario: 禁言部分失败
- **WHEN** 屏蔽已成功但禁言回读失败或与预期不符
- **THEN** 系统 SHALL 保留实际屏蔽审计并报告禁言失败
- **THEN** 不得写入永久禁言意图

### Requirement: 状态巡检的成功语义

`STATUS_SYNC` SHALL 只在选中批次的每条帖子均完成来源详情回读、指标保存，或被来源明确确认删除时标记成功。任务结束 MUST 写入仅含 `attempted`、`refreshed`、`failed`、`limit` 和执行范围的检查点；没有选中帖子时以 `attempted=0` 正常成功。若任一来源读取、契约解析或指标保存失败，任务 MAY 继续处理其余帖子，但最终 MUST 进入既有有限 retry，且不得标记为 `SUCCEEDED`。

#### Scenario: 来源 Frame 不可用
- **WHEN** 状态巡检选中至少一条帖子，但任意来源读取未完成
- **THEN** 检查点 SHALL 记录该失败，Background Job SHALL 进入既有 retry，而不是显示成功

### Requirement: 当前操作游戏的周期性采集入队
系统 SHALL 为当前操作游戏创建周期性原创帖子采集任务。每个调度周期 SHALL 创建或取得一个 `SCHEDULED_INGEST_WINDOW` 任务；时间窗口与页面大小属于可调整调度参数，不得以业务 AppID 或游戏名称写死。

#### Scenario: 在非边界时刻调度
- **WHEN** 调度器在北京时间 `10:16` 运行
- **THEN** 系统 SHALL 创建或取得开始于 `09:45`、结束于 `10:15` 的采集窗口任务

#### Scenario: 连续周期覆盖边界帖子
- **WHEN** 调度器分别为相邻的 15 分钟边界创建采集任务
- **THEN** 两个窗口 SHALL 重叠 15 分钟，且重复读取的同一 `topic_id` SHALL 幂等更新而不得重复创建帖子

#### Scenario: 同一时间桶重复触发
- **WHEN** 调度器在同一个 15 分钟对齐时间桶内重复运行
- **THEN** 系统 SHALL 使用 AppID、对齐后的开始和结束时间及页面大小创建稳定幂等键，并返回已有任务而不得创建重复 `SCHEDULED_INGEST_WINDOW` 任务

### Requirement: 受控的人工历史补采入队
系统 SHALL 在系统配置首页的帖子采集 Worker 节点提供“创建历史补采”入口。入口 SHALL 仅允许从本地可管理游戏目录选择单个游戏，并接受受配置约束的补采时长。系统 SHALL 创建 `MANUAL_INGEST_WINDOW` 任务而不得在 HTTP 请求内建立 CDP 会话；任务由人工补采 Worker 异步消费。直接 `enqueue_ingestion` 管理命令仅用于故障诊断。

#### Scenario: 创建可管理游戏的历史补采
- **WHEN** 已认证运营人员选择一个本地可管理游戏并提交有效的小时数
- **THEN** 系统 SHALL 使用该游戏、按北京时间对齐的开始和结束时间及固定页大小创建或复用 `MANUAL_INGEST_WINDOW`，并返回任务已创建或已存在的结果

#### Scenario: 拒绝无效补采请求
- **WHEN** 请求选择不在本地可管理游戏目录中的 AppID，或小时数不在 1 至 744 之间
- **THEN** 系统 SHALL 拒绝入队并显示可处理错误，不得创建任务或建立 CDP 会话

### Requirement: 按时间窗口采集候选帖子
系统 SHALL 通过原平台列表适配器，以北京时间日期窗口采集候选帖子，并持久化包含日期窗口、页位置、处理时间、状态和失败摘要的检查点。

#### Scenario: 每日采集候选帖子
- **WHEN** 定时采集器针对一个北京时间自然日运行
- **THEN** 它 SHALL 请求该窗口的候选帖子分页，并为每个已处理页面创建或更新持久化采集检查点

#### Scenario: 重复出现的候选帖子
- **WHEN** 后续采集任务遇到已有 `topic_id` 的候选帖子
- **THEN** 系统 SHALL 幂等更新已有记录，而不是创建重复帖子

### Requirement: 已验证的原创帖子判定
系统 SHALL 仅在取得候选帖内容卡片详情后进行分类；当 `data.content_card.forward_topic_info` 字段存在且值不为 `null` 时 SHALL 分类为 `FORWARD`，仅当该字段缺失或值为 `null` 时 SHALL 分类为 `ORIGINAL`。

#### Scenario: 排除转发帖子
- **WHEN** 内容卡片详情包含值不为 `null` 的 `forward_topic_info`
- **THEN** 系统 SHALL 将候选帖标记为 `FORWARD`，将其排除在运营池和原创帖报表之外，且 SHALL NOT 持久化转发链载荷

#### Scenario: 原创帖子进入运营池
- **WHEN** 内容卡片详情解析成功且 `forward_topic_info` 字段缺失或值为 `null`
- **THEN** 系统 SHALL 将候选帖标记为 `ORIGINAL`，持久化允许的标准化帖子投影，并将其加入后续评估队列

#### Scenario: 分类数据不完整
- **WHEN** 候选帖没有 `content_id`、详情调用失败或内容卡片结构无效
- **THEN** 系统 SHALL 将候选帖标记为 `CLASSIFICATION_PENDING`，保留已脱敏的失败原因，且 SHALL NOT 将其视为原创帖

### Requirement: 最小化的原创帖子标准化记录
系统 SHALL 以 `topic_id` 为键存储标准化原创帖数据，包含 `content_id`、标题、HTML 和提取文本、投票标题与选项文本、允许的媒体引用、发布时间、状态、推荐状态、互动快照、来源应用标识、分类结果、分类时间和来自管理详情的作者用户 ID。作者昵称、头像、评论与评论作者载荷 SHALL NOT 存入原创帖子记录；作者用户 ID SHALL 排除在普通报表聚合和通用搜索索引之外，并仅用于作者禁言状态与历史帖子关联。

#### Scenario: 标准化原创帖子详情
- **WHEN** 内容卡片详情适配器接收一条原创帖子
- **THEN** 系统 SHALL 从其 HTML 提取可读的 `content_text` 并保留必要换行和话题文本，且 SHALL 仅存储已配置的最小媒体和互动字段

#### Scenario: 管理详情补齐作者用户 ID
- **WHEN** 原创帖的管理详情包含有效作者用户 ID
- **THEN** 系统 SHALL 幂等更新该帖的作者用户 ID，而不得将昵称、头像、评论或评论作者载荷写入原创帖子记录

#### Scenario: 作者数据不用于普通报表
- **WHEN** 原创帖子包含作者身份字段
- **THEN** 系统 SHALL 将昵称、头像和评论作者字段排除在持久化、普通报表聚合和通用搜索索引之外，并仅允许作者用户 ID 用于作者禁言状态和历史帖子关联

#### Scenario: 图片与视频媒体投影
- **WHEN** 内容卡片的 `image_list` 或 `video_info` 包含允许的媒体 URL
- **THEN** 系统 SHALL 仅保存媒体类型、URL 和可用尺寸；视频 SHALL 标记为 `VIDEO`，且 SHALL NOT 持久化作者或评论载荷

#### Scenario: 投票标题和选项作为审核内容
- **WHEN** 原创内容卡片包含 `ballot_info_list`
- **THEN** 系统 SHALL 按原始顺序保存每个投票的 `title` 和全部 `option_item_list[].text`，在工作台展示并与帖子正文共同用于搜索和 AI 审核；系统 SHALL NOT 持久化投票或选项 `content_id`、`is_chosen`、`is_voted`、`is_owner` 等会话或身份状态

### Requirement: 不采集评论正文的互动指标
系统 SHALL 将浏览、点赞、回复和分享数量保存为带时间戳的互动快照，且在本阶段 SHALL NOT 持久化 `reply_list` 或 `child_reply_list` 的评论正文。

#### Scenario: 浏览数需要详情适配器
- **WHEN** 内容卡片响应不包含浏览数
- **THEN** 系统 SHALL 仅通过指定的帖子详情适配器获取浏览数，或将快照值保留为空并记录其数据来源

#### Scenario: 详情响应包含评论
- **WHEN** 收集帖子指标时互动详情响应包含评论列表
- **THEN** 系统 SHALL 丢弃评论列表，并仅持久化允许的帖子级互动数量

### Requirement: 管理队列删除优先于内容卡片缓存
系统 SHALL 以帖子管理详情适配器的已验证业务删除响应作为帖子是否仍可运营的权威判定；内容卡片可读性 SHALL NOT 推翻该判定。原创帖 `status` SHALL 使用正常基值 `2`、屏蔽位 `4`、沉底位 `8` 与本地删除位 `128` 表示状态；删除、屏蔽和沉底 SHALL 以按位与判定，正常 SHALL 仅以 `status == 2` 判定且 SHALL NOT 与其他状态组合。

#### Scenario: 管理详情确认帖子已删除
- **WHEN** 刷新原创帖时 `topicsearchgetdetail` 返回已验证的业务删除错误码 `-10075007`
- **THEN** 系统 SHALL 在保留已有屏蔽位 `4` 和沉底位 `8` 的前提下为 `status` 添加删除位 `128`，停止本次依赖管理详情的指标更新；系统 MAY 只读更新内容卡片中的历史正文、媒体和投票内容，但 SHALL NOT 以此清除删除位。在默认运营队列、搜索结果和原创统计中保留该记录并明确展示其状态，且 SHALL NOT 建立上游处置会话或写入审计

#### Scenario: 管理详情恢复可用
- **WHEN** 带删除位的原创帖后续管理详情读取成功
- **THEN** 系统 SHALL 使用管理详情回读的原平台状态覆盖本地 `status` 并清除删除位，随后按正常刷新流程更新允许的帖子投影和互动快照

#### Scenario: 删除与原平台处置状态组合
- **WHEN** 帖子在确认删除前已同步为屏蔽或沉底
- **THEN** 系统 SHALL 分别保存 `132` 或 `136`，同时存在屏蔽与沉底时 SHALL 保存 `140`；系统 SHALL NOT 生成 `130` 或以 `status & 2` 判断正常

### Requirement: 原创帖子运营工作台与媒体预览
系统 SHALL 提供不依赖帖子标题的原创帖子管理工作台：左侧为可筛选的帖子摘要队列，右侧为当前帖的正文、投票文本、媒体与帖子级互动指标。该工作台 SHALL NOT 提供直接编辑帖子或自动上游写入的能力。

#### Scenario: 队列中的帖子切换
- **WHEN** 运营人员点击队列项或使用 `J`、`K`、左右方向键
- **THEN** 系统 SHALL 在当前筛选结果中切换上一条或下一条帖子，并保持筛选条件

#### Scenario: 页内预览媒体
- **WHEN** 运营人员点击图片或视频附件缩略图
- **THEN** 系统 SHALL 在页内模态框预览该媒体；左右方向键 SHALL 在当前帖附件间切换，且 SHALL NOT 打开新浏览器标签

#### Scenario: 审核动作作为受控入口
- **WHEN** 工作台显示跳过、沉底、屏蔽、禁言、解除禁言或恢复正常等操作
- **THEN** 系统 SHALL 要求业务原因和人工二次确认；只有全局开关及对应动作开关均启用时才调用上游，跳过 SHALL 仅完成本地审核，AI、scheduler 和 worker SHALL NOT 调用任何上游写入
