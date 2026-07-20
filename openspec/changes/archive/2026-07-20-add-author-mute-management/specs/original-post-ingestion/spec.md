## MODIFIED Requirements

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
