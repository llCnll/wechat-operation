# 微信小游戏圈帖子管理：原创帖拉取调研

调研日期：2026-07-18  
调研范围：微信公众平台插件中的“游戏圈 -> 内容管理 -> 帖子管理”页面。  
目标：后续仅拉取原创帖业务数据；不保存“转发帖子 + 评论”载荷。转发帖保留最小候选判定记录供统计。

## 结论

帖子列表由 `gettopiclist` 提供，但已观察到的列表响应没有可靠的原创/转发标记。`topic_type` 和列表中的 `post_type` 均不能据当前样本单独判断帖子来源。

可靠的已验证判定方式是：对列表中的每条记录，使用其 `content_id` 调用 `getcontentcarddetail`；响应中 `data.content_card.forward_topic_info` 字段存在且值不为 `null` 的记录为转发帖，应排除出运营池和原创报表。字段缺失或值为 `null` 的记录为原创帖候选。两个样本的验证结果如下：

| 样本 topic_id | 页面类型 | 判定证据 |
| --- | --- | --- |
| `1412250588` | 原创帖 | 无 `forward_topic_info`；`base_content.post_type=1`、`source_type=27`、`content_source_type=27` |
| `1412251815` | 转发帖加评论 | 存在 `forward_topic_info`，其中含 `source_topic_id`、被转发评论和引用原帖；`base_content.post_type=0`、`source_type=0`、`content_source_type=15` |

`post_type`、`source_type` 和 `content_source_type` 是上述两个样本中的观测值，未经接口文档确认；应将 `forward_topic_info` 是否存在作为正式的过滤条件。

### 2026-07-18 浏览器会话复核

已在受控 Chrome 的已登录会话中复核一个可管理测试应用的帖子列表读取流程：

1. 调用 `GET /wxamp/cgi/getWxaList?token=<mp_token>&lang=zh_CN&random=<random>` 获取当前账号可管理的小程序列表；请求返回的 AppID 和名称作为测试范围，不写入代码默认值。
2. 调用 `POST /cgi-bin/gameopenmpwap/binduseridentity?auth_type=4`，请求体携带 `biz_type=1`、目标 `appid` 和 `scene=1`，已观察到成功响应 `errcode=0`。
3. 从 `binduseridentity` 的成功响应中读取插件上下文 token，并在 `gamemp.weixin.qq.com` 的插件 iframe 上下文调用 `gettopiclist`。该 token 不等同于外层 `mp.weixin.qq.com` 页面 URL 中的 token；使用外层 token 会返回 `errcode=-2`、`errmsg=参数错误`。
4. 使用绑定响应中的插件 token 后，第一页请求成功：HTTP 200、`errcode=0`、`total=106841`、返回 10 条记录。

成功请求的最小参数形状仍为：

```json
{
  "req": {
    "page": 0,
    "page_size": 10,
    "skip_check_author": true,
    "order_by": 1,
    "is_desc": true
  }
}
```

### 2026-08 指定帖子 ID 查询约束

运营侧提供的同一受控会话请求表明，`gettopiclist` 的 `req` 可额外携带单个 `topic_id`，同时保留固定的 `page=0`、`page_size=10`、`skip_check_author=true`、`order_by=1` 与 `is_desc=true`。系统将该能力封装为固定的本地 IPC 方法：调用方只可传递正整数帖子 ID，不能传递 URL、token、AppID、页大小或任意来源参数。

Worker 只接受返回列表中与请求 ID 完全相等的唯一记录，并核验其 AppID 和非空 `content_id`；空匹配按“未找到/不可读取”结束单帖任务，不创建推测性帖子。删除的权威判定仍由已存在的 `topicsearchgetdetail` 业务错误语义提供，不能从空列表推断或保存完整响应。

已验证的第一页 `topic_id`：`1434447283`、`1434445925`、`1434445802`、`1434446926`、`1434446594`、`1434441961`、`1434443360`、`1434444026`、`1434439609`、`1434440800`。

实现要求：服务不得持久化或打印外层 token、绑定响应中的插件 token、`plugin_token`、Cookie、运营者 `openid` 或完整跳转 URL。应通过 Chrome CDP 在已授权的插件 iframe 上下文内完成应用列表读取、身份绑定和后续请求；登录失效时重新显示受控浏览器的登录页面扫码。

## 数据流

```text
gettopiclist（按页获取候选帖子）
  -> 取得 topic_id / content_id
  -> getcontentcarddetail（need_comment=false）
  -> forward_topic_info 存在？
       是：转发帖，仅保留最小候选判定记录
       否：原创帖，投影并保存所需字段
```

候选列表采集和原创/转发判定不依赖 `topicsearchgetdetail`，避免处理其附带的评论和评论作者信息。当前实现仅在已保留原创帖的互动快照、单帖刷新、删除判定和人工处置状态回读中调用该接口，并立即丢弃评论列表与作者身份字段。

## 1. 候选帖子列表接口

### 接口

```text
GET https://gamemp.weixin.qq.com/cgi-bin/gamecircleopenmpweb/gettopiclist
```

### 请求认证与公共参数

- `token=<plugin_token>`：插件侧会话令牌，动态且敏感。
- `auth_type=4`：本次页面请求中观察到的认证类型。
- `data=<URL 编码 JSON>`：列表查询条件。
- 浏览器还携带登录 Cookie；不要把 Cookie、令牌、运营者 `openid` 或完整请求 URL 写入代码库、日志或导出文件。

请求模板：

```json
{
  "req": {
    "page": 0,
    "page_size": 10,
    "skip_check_author": true,
    "order_by": 1,
    "is_desc": true
  }
}
```

在页面的日期筛选场景中，观察到 `req` 可额外带：

```json
{
  "start_time": 1783094400,
  "end_time": 1783180799
}
```

时间为 Unix 秒级时间戳，包含边界语义需在实现时用相邻日期验证。页面已观察到分页从 `page=0` 开始；`page_size=10` 是页面实际使用值，不应假设服务端最大值也是 10。

### 响应结构

```json
{
  "errcode": 0,
  "errmsg": "ok",
  "rsp": {
    "topic_list": ["TopicSummary"],
    "total": 0
  }
}
```

`total` 可用于计算当前筛选条件下的总页数。一次全量范围的页面响应显示总数约为 10 万量级，因此应按时间窗口分片并保存检查点，避免长任务因令牌过期或网络中断而从头开始。

### `TopicSummary` 已观察字段

| 字段 | 说明 |
| --- | --- |
| `topic_id` | 帖子数字 ID；用于帖子详情接口。 |
| `content_id` | 内容卡片 ID；用于原创/转发判定接口。 |
| `title`、`content`、`brief_content` | 标题、富文本/文本正文和摘要。`content` 可能含 HTML。 |
| `preview_pic_list`、`cover_pic_list`、`video_list` | 图片、封面和视频引用。 |
| `pic_meta_data_list` | 图片尺寸及 GIF 标识。 |
| `create_time`、`publish_time`、`last_modify_time` | Unix 秒级时间。 |
| `status`、`status_source`、`deleted`、`recommend_status` | 内容状态与推荐状态。枚举含义需以页面展示复核。 |
| `topic_type`、`post_type` | 内容类型字段；当前样本中不足以区分原创/转发。 |
| `tag_id_list`、`custom_tab_list`、`cluster_tab_list`、`cluster_index_list` | 话题、标签与圈层关联。 |
| `creater` | 发帖者对象，含昵称、头像 URL、`openid` 等个人信息。 |
| `appid` | 游戏 AppID。 |
| `ballot_list`、`content_tmpl_info`、`custom_identity_tag_list` | 投票、模板和身份标签扩展信息。 |

## 2. 原创/转发判定接口

### 接口

```text
GET https://gamemp.weixin.qq.com/cgi-bin/gamecircleopenmpweb/getcontentcarddetail
```

### 请求参数

| 参数 | 来源/用途 |
| --- | --- |
| `content_id` | 取自列表响应。 |
| `need_comment=false` | 已观察到的页面调用值；可避免请求评论列表。 |
| `preview_key` | 页面请求中为空。 |
| `token`、`auth_type` | 与列表接口相同的会话上下文。 |
| `build_version` | 页面携带的构建版本；应从当前页面请求复制，不应硬编码历史值。 |

### 响应的关键结构

```json
{
  "errcode": 0,
  "data": {
    "content_card": {
      "base_info": {
        "topic_id": 0,
        "author": {}
      },
      "base_content": {},
      "reply_info": {},
      "love_info": {},
      "share_info": {},
      "forward_topic_info": {}
    }
  }
}
```

`forward_topic_info` 为可选对象。过滤逻辑：

```js
const card = response.data.content_card;
const isForward = Object.hasOwn(card, "forward_topic_info") && card.forward_topic_info != null;
const isOriginal = !isForward;
```

### `base_content` 已观察字段

| 字段 | 说明 |
| --- | --- |
| `title`、`content`、`brief_content` | 标题、富文本正文、纯文本摘要。 |
| `image_list` | `{ url, width, height, is_gif }` 图片数组。 |
| `video_info`、`cover_pic_list` | 视频和封面信息。 |
| `ballot_info_list` | 投票列表；其 `title` 和每个 `option_item_list[].text` 都是面向用户展示的内容，必须纳入审核。 |
| `content_jump_url` | 详情跳转地址。 |
| `status`、`recommend_status`、`control_flag` | 内容与运营控制状态。 |
| `post_type`、`source_type`、`content_source_type` | 来源/类型相关枚举，仅作辅助校验。 |

### 转发帖特有字段

存在 `forward_topic_info` 时，已观察到：

| 字段 | 说明 |
| --- | --- |
| `source_topic_id` | 来源帖数字 ID。 |
| `content_list` | 转发链中的评论内容，元素含 `user_info`、`content`、`image_list`、`reply_id` 等。 |
| `quote_topic` | 被引用原帖，含作者对象、正文、图片等。 |

这些字段会带入额外用户与原帖数据，因此“只拉原创帖”任务应在识别到该对象后立即丢弃转发链载荷；系统仅保留候选帖的 `topic_id`、`content_id`、来源 AppID 和 `FORWARD` 判定，不持久化该对象本身。

### 推荐保留的数据投影

只保存后续业务确实需要的字段，避免写入会话信息和不必要的个人数据：

```json
{
  "topic_id": 0,
  "content_id": "",
  "title": "",
  "content_html": "",
  "brief_content": "",
  "images": [{ "url": "", "width": 0, "height": 0, "is_gif": false }],
  "videos": [],
  "publish_time": 0,
  "status": 0,
  "recommend_status": 0,
  "view_count": 0,
  "reply_count": 0,
  "love_count": 0,
  "share_count": 0
}
```

字段路径在当前样本中为：`data.content_card.base_info.topic_id`、`data.content_card.publish_time`、`data.content_card.reply_info.total_reply_count`、`data.content_card.love_info.total_love_count`、`data.content_card.share_info.total_share_count`。`content_id` 应继续使用列表响应中的值；实现时需对这些路径做空值兼容。

`view_count` 不在已观察到的内容卡片响应中，应从第 3 节的帖子详情接口读取。

### 投票内容投影与审核

投票不能仅视为互动组件：标题和选项文本可以承载引流、外挂、私服或其他违规传播信息。案例 `1434284478` 的投票标题明确提示“竖着看第一个字”，选项首字可拼接成独立信息；因此审核输入必须保留标题和**所有**选项文本及其原始顺序。

当前最小投影仅保存以下字段：

```text
base_content.ballot_info_list[].title
base_content.ballot_info_list[].option_item_list[].text
```

投票 `content_id`、选项 `content_id`、`is_chosen`、`is_voted`、`is_owner` 等身份或会话相关字段不持久化；`voted_count`、`voted_uv`、截止状态也不进入 AI 文本。标准化文本在工作台的“投票内容”区块呈现，并与正文、OCR 文本共同提交 AI 审核，同时支持工作台搜索。

## 3. 单帖管理详情与评论接口

```text
GET https://gamemp.weixin.qq.com/cgi-bin/gamecircleopenmpweb/topicsearchgetdetail
```

请求 `data` 的已观察字段：

```json
{
  "topic_id": 1412250588,
  "page_type": 1,
  "cur_page": 0,
  "limit": 20,
  "need_topic": true,
  "openid": "<operator_openid>"
}
```

响应包含：

- `data.topic`：帖子主体，含正文、图片、发布状态、作者、阅读/点赞/回复计数等。
- `data.reply_list`、`data.child_reply_list`：评论和子评论，包含评论者个人信息。
- `has_next`、`next_offset`、`total_count`、`more_count`：评论分页相关字段。

该接口不用于候选列表采集或原创/转发判定。即使 `need_topic=true`，示例响应仍携带评论列表；当前实现只读取帖子主体中的状态和互动计数，立即丢弃评论及作者字段。

### 管理队列删除的业务判定

`topicsearchgetdetail` 是帖子是否仍可在原平台运营的权威接口。已验证案例 `1434284478` 返回 HTTP `200`，但业务响应为 `errcode=-10075007`、`errmsg=System Error`，且不含 `data`。此情形应判定为“帖子已从原平台管理队列删除”，不能将 HTTP 成功或通用错误文案误判为可继续运营。

`getcontentcarddetail` 在同一案例中仍可能返回内容卡片及历史 `status`。该结果只能用于已有缓存内容的展示或原创/转发分类，**不能**推翻管理详情的删除结论，也不能作为沉底、屏蔽、禁言或恢复正常等上游处置的依据。

实现规则：刷新时优先读取 `topicsearchgetdetail`；命中该业务错误码时，在本地原创帖的 `status` 添加删除位 `128`，并保留已有屏蔽位 `4`、沉底位 `8`，不再读取或更新依赖管理详情的互动指标。`2` 仅表示正常基值，不能与删除位组合。随后仍可只读请求内容卡片，以补齐历史正文、媒体、投票等展示内容；内容卡片可读不能清除删除状态，也不能成为任何上游处置的依据。该记录仍保留在工作台、搜索和原创统计中，并明确展示“原平台已删除”状态；系统拒绝所有审核和上游处置。若日后管理详情重新成功，则以回读的原平台状态覆盖本地值并按正常刷新流程处理。

### 互动计数的精确字段

同一帖子在两个接口中的互动计数存在字段位置差异，应按用途选择：

| 数据 | `topicsearchgetdetail` 响应 | `getcontentcarddetail` 响应 | 说明 |
| --- | --- | --- | --- |
| 浏览数 | `data.topic.view_count` | 当前样本未返回对应字段 | 若需浏览数，必须调用帖子详情接口。 |
| 点赞数 | `data.topic.love_count` | `data.content_card.love_info.total_love_count` | 两者都是总点赞数；批量任务应固定一种来源，避免重复或口径混用。 |
| 评论数 | `data.topic.reply_count` | `data.content_card.reply_info.total_reply_count` | 两者都是总评论数；评论详情分页后可能与快照存在时差。 |
| 分享数 | 当前样本未确认 | `data.content_card.share_info.total_share_count` | 内容卡片接口可提供分享总数。 |

### 评论详情字段（已观察）

`data.reply_list[]` 是当前页的顶层评论列表。单条评论在样本中的字段如下：

| 字段 | 说明 |
| --- | --- |
| `local_reply_id` | 管理侧本地评论数字 ID。 |
| `reply_id` | 评论字符串 ID；用于评论相关操作和关联查询。 |
| `content` | 评论正文，可能含表情文本。 |
| `pic_url_list`、`pic_meta_list` | 评论图片 URL 及其元数据。 |
| `creator.nick_name`、`creator.head_img_url`、`creator.openid` | 评论者信息，属于个人信息。 |
| `create_time` | Unix 秒级评论时间。 |
| `status`、`state`、`block_level` | 评论审核/可见性/屏蔽相关状态；完整枚举尚未确认。 |
| `love_count` | 评论点赞数；该字段在样本中为可选字段。 |

`data.child_reply_list` 是子评论容器。其完整元素字段尚未从独立样本确认；前端在加载更多子评论时向同一接口追加 `parent_reply_id`、`offset`、`limit` 和 `need_topic=true`。返回的 `has_next`、`next_offset`、`more_count` 应作为每个父评论的分页游标处理，而不是假设页码连续。

评论数据包含正文和评论者标识。当前“仅原创帖”导出应只保留 `reply_count`，不持久化 `reply_list` 或 `child_reply_list`；若后续确有评论分析需求，应另行确定合法用途、保留期限和脱敏策略。

## 4. 原创帖操作接口

本节汇总帖子管理前端脚本中的调用点。后文明确标为“已验证”的沉底、屏蔽、恢复正常、禁言和解除禁言路径已在授权会话中复核；其余写接口仍仅为页面调用线索，未验证前不得启用。所有写操作均可能改变线上内容或运营状态，必须要求单独、明确的人工确认。

### 接口命名与请求路径

前端将下面的逻辑服务名挂在 `gamecircleopenmpweb` 模块下。已验证的读取接口遵循“方法名转小写”的路径规则，例如 `topicSearchGetDetail` 对应 `/topicsearchgetdetail`。因此下表的“推定路径”可用于定位网络请求，但在真正实现写操作前，必须以浏览器实际抓包确认 HTTP 方法、完整公共参数和当前页面版本。

```text
https://gamemp.weixin.qq.com/cgi-bin/gamecircleopenmpweb/<lowercase-method-name>
```

认证上下文与列表接口相同：动态 `token`、`auth_type`、登录 Cookie，以及部分调用需要的 `appid` 或当前运营者 `openid`。这些值只应从当次已授权浏览器会话读取，不得写入文档、代码常量、日志或导出数据。

### 只读操作

| 前端服务名 | 推定路径 | 已观察请求体/参数 | 用途 |
| --- | --- | --- | --- |
| `getTopicDetail` | `/gettopicdetail` | `{ req: { topic_id, is_audited: true } }` | 按帖子 ID 查询管理侧详情。 |
| `topicSearchGetDetail` | `/topicsearchgetdetail` | `{ topic_id, appid, page_type, cur_page, limit, need_topic, openid, only_top_reply? }` | 获取帖子主体及评论分页；详见第 3 节。 |
| `topicSearchGetLog` | `/topicsearchgetlog` | `{ appid?, topic_id, page, page_size }` | 获取帖子操作日志，响应使用 `data.log_list` 和 `data.total`。 |
| `previewTopic` | `/previewtopic` | 页面配置明确为 `POST`；具体业务参数待抓包确认。 | 生成/读取预览信息，不应改变帖子状态。 |
| `getTopicTopConfig` | `/gettopictopconfig` | 前端将其作为置顶配置查询接口。 | 查询帖子是否存在运营置顶配置。 |
| `getIntervention` | `/getintervention` | 前端将其作为干预配置查询接口。 | 查询帖子干预配置。 |

### 会改变原创帖状态的操作

所有下列操作应先用第 2 节的 `forward_topic_info` 规则确认目标是原创帖，再以当前页面捕获到的请求为准执行。

| 操作 | 前端服务名 | 推定路径 | 已观察参数形状 | 风险与说明 |
| --- | --- | --- | --- | --- |
| 删除帖子 | `deleteTopic` | `/deletetopic` | 已发布帖：`{ req: { topic_id, group_out_id } }`；草稿：`{ req: { id } }`。`group_out_id` 在列表代码中取记录的 `appid`。 | 高风险、不可逆。页面有确认弹窗。 |
| 屏蔽帖子 | `topicSearchBlock` | `/topicsearchblock` | `{ topic_id, status: 4, block_info: { reason: <bitmask> } }` | 已验证；`reason` 是多个原因枚举按位或的结果。 |
| 取消屏蔽 | `topicSearchBlock` | `/topicsearchblock` | `{ topic_id, status: 2 }` | 已验证；恢复帖子为正常状态。 |
| 沉底/取消沉底 | `topicSearchBlock` | `/topicsearchblock` | 沉底 `{ topic_id, status: 8 }`；取消沉底 `{ topic_id, status: 2 }`。 | 已验证；影响内容分发排序。 |
| 转攻略帖/转普通帖 | `topicSearchGuide` | `/topicsearchguide` | `{ appid, topic_id, guide_info, type }`；`type` 为 `TOPIC_TYPE_GUIDE` 或 `TOPIC_TYPE_NORMAL`。 | 改变内容分类；`guide_info` 的完整结构待抓包。 |
| 设置运营标签 | `setTopicTag` | `/settopictag` | `{ req: { topic_id, tag_id_list: [tag_id] } }` | 此调用会写入标签列表；当前调用点每次传入单元素数组。 |
| 新建/更新置顶配置 | `updateTopicTopConfig` | `/updatetopictopconfig` | `{ top_config }`；下架时已观察 `{ id, biz_id: String(topic_id), biz_type: TOPIC, status: TOPIC_TOP_CONFIG_STATUS_OFFLINE }`。 | 影响置顶展示；新建配置的完整字段需从实际提交抓包。 |
| 新建/更新干预配置 | `updateIntervention` | `/updateintervention` | `{ intervention }`，已观察字段：`id?`、`topic_id`、`content_type`、`title`、`priority`、`start_time`、`expire_time`、`status`、`platform`、`enable_exposure_filter`。 | 影响曝光或运营策略；时间范围必须合法。 |
| 保存草稿/编辑 | `updateTopic` | `/updatetopic` | `{ req: { topic }, openid }`；编辑已有帖子时 `topic.topic_id` 被写入。 | 会改写正文、媒体或草稿状态；`topic` 全量字段结构需单独抓包。 |
| 发布帖子 | `publishTopic` | `/publishtopic` | `{ req: { topic }, openid }`；编辑已有帖子时 `topic.topic_id` 被写入。 | 可能发布新内容或更新已编辑内容；不属于批量拉取流程。 |

### 已验证的帖子状态变更（2026-07-18）

以下两项由已授权浏览器会话中的人工操作验证。认证令牌和 Cookie 由当前 iframe 会话携带，未记录到本文档或服务配置中。

| 操作 | 方法与路径 | 最小 JSON 请求体 | 成功响应 | 状态回读 |
| --- | --- | --- | --- | --- |
| 沉底 | `POST /topicsearchblock` | `{ "topic_id": <id>, "status": 8 }` | `{ "errcode": 0, "errmsg": "ok" }` | 已在操作后读取到 `topicsearchgetdetail.data.topic.status = 8`。 |
| 屏蔽 | `POST /topicsearchblock` | `{ "topic_id": <id>, "status": 4, "block_info": { "reason": <bitmask> } }` | `{ "errcode": 0, "errmsg": "ok" }` | 已在操作后读取到 `topicsearchgetdetail.data.topic.status = 4`。 |
| 恢复正常 | `POST /topicsearchblock` | `{ "topic_id": <id>, "status": 2 }` | 与沉底/屏蔽一致 | 取消沉底和取消屏蔽均回读为 `status = 2`。 |

屏蔽原因 `block_info.reason` 为位图，可将多个已验证值按位或组合：`1` 其他、`2` 色情低俗、`4` 政治敏感、`8` 广告欺诈、`16` 欺诈钱财。服务只接受上述值的非空组合，未知位必须拒绝请求。上游失败响应、限流和幂等语义仍需独立验证。

已确认帖子状态枚举：`2` 正常、`4` 屏蔽、`8` 沉底、`16` 仅自己可见。恢复正常使用 `status: 2`，因此同一操作可撤销沉底或屏蔽；`16` 的恢复或其他写入语义尚未验证，系统只能展示、筛选和刷新该状态，不得发送写请求覆盖它。所有已验证处置仍要求人工二次确认、状态回读和独立能力开关。

工作台以 `topicsearchgetdetail.data.topic.status` 为正常、屏蔽和沉底的当前状态来源，并在已验证写操作的回读成功后立即同步本地状态。状态为 `8` 时展示“已沉底”且不再提供重复沉底；状态为 `4` 时展示“已屏蔽”且不再提供重复屏蔽；两种状态均只提供已验证的“恢复正常”转换。`128` 是管理详情删除错误派生的本地状态位，不会发送到原平台；它可与 `4`、`8` 组合并优先禁用全部审核处置。刷新数据仅在管理详情重新成功时清除该位，避免内容卡片中的历史状态造成误导。

### 已验证的禁言请求

禁言使用与帖子状态变更不同的私有路径：`POST /cgi-bin/gameopenmpwap/cfcommcgi/spacexcontent_gamecircletopicmanage_topicblockuser`。已授权浏览器会话中观察到的最小 JSON 请求体如下，其中身份标识只可在请求时临时取得，绝不可持久化或写入日志：

```json
{
  "account_openid": "<当前帖子作者的即时身份标识>",
  "end_time": "<运营界面选择期限对应的 Unix 秒级时间戳>",
  "block_reason_type": "<已验证的原因码>",
  "reason": "<可选的人工文字说明>"
}
```

成功响应为 `{ "errcode": 0, "errmsg": "ok", "data": {} }`，另有短期诊断关联字段，服务不依赖或持久化该字段。工作台提交禁言期限，服务端以当前 Unix 时间加所选天数计算 `end_time`；已验证选项为 1、3、7、30 天（1 个月按 30 天处理）。`block_reason_type` 与屏蔽原因码使用相同位图，服务只接受已验证值的非空组合。

写后通过 `topicsearchgetdetail.data.topic.creater` 回读禁言状态：当前游戏的 `block_info.appid` 必须等于帖子 `appid`，`block_info.block_end_time` 为当前游戏禁言截止时间；`global_block_end_time` 是独立的全局禁言截止时间。审计仅保存这两个时间戳，不保存作者身份标识。

### 解除禁言调研进度

已由已授权浏览器实际请求确认，解除禁言调用为：

```text
POST /cgi-bin/gameopenmpwap/cfcommcgi/spacexcontent_gamecircletopicmanage_topicunblockuser
```

最小请求体包含运行时临时取得的 `account_openid` 与固定模块标识 `module: "mmgamegamecirclemanageweb"`。作者身份标识只能在请求内存中即时使用，不得保存到数据库、审计或日志。对明确不存在的身份标识进行的安全失败探测返回非成功结果，未影响真实用户。

解除后以当前游戏 `block_info.block_end_time` 不晚于当前时间作为成功条件；全局禁言截止时间可独立保留。解除禁言通过独立能力开关控制，默认关闭，并须人工二次确认、操作幂等键和前后状态审计。会话失效与上游失败统一标记为 `ACTION_FAILED`，不自动重试写请求。

### 评论相关操作（不属于原创帖子主体操作）

前端还暴露了 `createReply`、`deleteReply`、`batchUpdateReplyStatus`、`updateReplyTopConfig` 和 `loveTopic`。它们操作评论或当前运营者的点赞状态，而不是原创帖子主体；“仅原创帖数据”任务应默认跳过。若未来启用评论治理，需要单独设计权限、审计和个人信息最小化方案。

### 安全执行顺序

1. 先从列表取 `topic_id`、`content_id`、`appid`，再调用内容卡片接口判定是否原创。
2. 对原创帖执行只读的 `getTopicDetail`、`topicSearchGetLog` 或配置查询，展示拟操作对象与当前状态。
3. 对删除、屏蔽、状态变更、标签、置顶、干预、编辑、发布等写操作，分别要求用户确认具体 `topic_id`、动作和参数。
4. 操作后重新读取详情或日志确认结果；日志中只保存操作摘要和结果码，避免保存正文、Cookie、令牌和用户身份信息。

## 推荐的后续实现流程

1. 从受控且已登录的浏览器会话读取当前请求所需的会话参数；令牌失效时停止并要求重新登录，不保存或打印凭据。
2. 按日期窗口调用 `gettopiclist`，从 `page=0` 循环到 `total` 覆盖完毕；记录窗口和页码检查点。
3. 对每条 `TopicSummary.content_id` 调用 `getcontentcarddetail?need_comment=false`。
4. 发现非 `null` 的 `forward_topic_info` 时仅保留最小候选判定记录；其余记录按最小字段投影保存为原创帖。
5. 对 `errcode != 0`、缺少 `content_id`、缺少 `content_card` 或接口结构变更的记录，写入不含敏感内容的失败清单并可重试。
6. 采用小批量、限速和指数退避；不要并发扫描全部历史记录。令牌、Cookie 或 `build_version` 变化时重新从页面抓取请求模板。
7. 以 `topic_id` 为主键做幂等去重；对编辑或状态变更场景用 `last_modify_time`、`status` 和重新拉取时间维护增量更新。

## 待验证项

- 未观察到服务端“仅原创帖”的稳定筛选参数；在未抓到页面实际请求前，不应猜测诸如 `is_original`、`post_type` 的查询参数。
- `gettopiclist` 的服务端最大 `page_size` 未验证；先沿用页面的 10，后续再在低风险窗口内测试上限。
- `status`、`recommend_status`、`post_type`、`source_type` 和 `content_source_type` 的完整枚举含义未由公开接口文档确认。
- 评论分页中 `cur_page`、`next_offset` 的精确关系需要在“允许采集评论”的单独任务中验证。
- 沉底、屏蔽和恢复正常的失败响应、限流和上游幂等行为。

## 调研与实现边界

本调研起始于当前已登录页面的请求与响应观察；随后已在受控 Chrome 的授权会话中复核只读列表、内容卡片、管理详情，以及本文明确标为“已验证”的状态变更和禁言路径。当前服务仅通过 CDP 适配器调用这些受控路径，不保存 Cookie、会话令牌、完整认证 URL、作者身份或评论正文。

接口属于后台页面私有调用，可能随页面版本、权限和会话状态变化。未明确标为已验证的写接口仍不得启用；新增或变更调用必须在具备相应账号授权的前提下重新抓包、验证请求和状态回读。
