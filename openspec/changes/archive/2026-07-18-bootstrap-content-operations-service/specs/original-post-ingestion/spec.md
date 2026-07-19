## ADDED Requirements

### Requirement: 按时间窗口采集候选帖子
系统 SHALL 通过原平台列表适配器，以北京时间日期窗口采集候选帖子，并持久化包含日期窗口、页位置、处理时间、状态和失败摘要的检查点。

#### Scenario: 每日采集候选帖子
- **WHEN** 定时采集器针对一个北京时间自然日运行
- **THEN** 它 SHALL 请求该窗口的候选帖子分页，并为每个已处理页面创建或更新持久化采集检查点

#### Scenario: 重复出现的候选帖子
- **WHEN** 后续采集任务遇到已有 `topic_id` 的候选帖子
- **THEN** 系统 SHALL 幂等更新已有记录，而不是创建重复帖子

### Requirement: 已验证的原创帖子判定
系统 SHALL 仅在取得候选帖内容卡片详情后进行分类；当 `data.content_card.forward_topic_info` 存在且非空时 SHALL 分类为 `FORWARD`，仅当该字段不存在时 SHALL 分类为 `ORIGINAL`。

#### Scenario: 排除转发帖子
- **WHEN** 内容卡片详情包含非空的 `forward_topic_info`
- **THEN** 系统 SHALL 将候选帖标记为 `FORWARD`，将其排除在运营池和原创帖报表之外，且 SHALL NOT 持久化转发链载荷

#### Scenario: 原创帖子进入运营池
- **WHEN** 内容卡片详情解析成功且不包含 `forward_topic_info`
- **THEN** 系统 SHALL 将候选帖标记为 `ORIGINAL`，持久化允许的标准化帖子投影，并将其加入后续评估队列

#### Scenario: 分类数据不完整
- **WHEN** 候选帖没有 `content_id`、详情调用失败或内容卡片结构无效
- **THEN** 系统 SHALL 将候选帖标记为 `CLASSIFICATION_PENDING`，保留已脱敏的失败原因，且 SHALL NOT 将其视为原创帖

### Requirement: 最小化的原创帖子标准化记录
系统 SHALL 以 `topic_id` 为键存储标准化原创帖数据，包含 `content_id`、标题、HTML 和提取文本、投票标题与选项文本、允许的媒体引用、发布时间、状态、推荐状态、互动快照、来源应用标识、分类结果和分类时间。

#### Scenario: 标准化原创帖子详情
- **WHEN** 内容卡片详情适配器接收一条原创帖子
- **THEN** 系统 SHALL 从其 HTML 提取可读的 `content_text` 并保留必要换行和话题文本，且 SHALL 仅存储已配置的最小媒体和互动字段

#### Scenario: 作者数据不用于普通报表
- **WHEN** 原创帖子包含作者身份字段
- **THEN** 系统 SHALL 将这些字段排除在普通报表聚合和通用搜索索引之外

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
系统 SHALL 以帖子管理详情适配器的已验证业务删除响应作为帖子是否仍可运营的权威判定；内容卡片可读性 SHALL NOT 推翻该判定。

#### Scenario: 管理详情确认帖子已删除
- **WHEN** 刷新原创帖时 `topicsearchgetdetail` 返回已验证的业务删除错误码 `-10075007`
- **THEN** 系统 SHALL 标记该原创帖为业务删除并记录判定时间，停止本次依赖管理详情的指标更新；系统 MAY 只读更新内容卡片中的历史正文、媒体和投票内容，但 SHALL NOT 以此清除删除状态。在默认运营队列、搜索结果和原创统计中保留该记录并明确展示其状态，且 SHALL NOT 建立上游处置会话或写入审计

#### Scenario: 管理详情恢复可用
- **WHEN** 已标记业务删除的原创帖后续管理详情读取成功
- **THEN** 系统 SHALL 清除该业务删除标记，并按正常刷新流程更新允许的帖子投影和互动快照

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
