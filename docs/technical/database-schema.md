# 数据库数据字典

数据库默认文件为 `runtime/operations.sqlite3`。`runtime/` 是整体排除出版本控制与打包产物的运行态目录。下文覆盖全部业务表；每张表均由 Django 自动附带自增主键 `id`。类型中的“可空”表示数据库可存 `NULL`，“可留空”表示应用层允许空字符串或空集合。

## `posts_managedgame`（可管理游戏目录）

工作台单选游戏上下文的本地缓存。同步来源仅在管理员显式操作且受控公众平台登录有效时调用；同步失败不会清空已有记录。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `appid` | 字符串，最长 128，唯一 | 小游戏 AppID。 |
| `name` | 字符串，最长 255 | 游戏显示名称。 |
| `last_synced_at` | 日期时间，可空 | 最近一次由上游成功同步该游戏的时间；内置默认游戏可为空。 |
| `created_at` | 日期时间，自动写入 | 本地目录首次建立时间。 |
| `updated_at` | 日期时间，自动更新 | 最近一次本地更新。 |

系统内置 `wx6d2454a8afcd9706`，名称为“城主别慌张”，作为工作台缺省范围。不会保存外层 token、Cookie、完整认证 URL 或运营者身份。

## `posts_candidatepost`（候选帖子）

采集列表中的原始候选记录，用于判定原创或转发；转发帖保留在此表以便统计，但不进入工作台。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `topic_id` | 大整数，唯一 | 原平台帖子 ID。 |
| `content_id` | 字符串，最长 255，可留空 | 原平台内容 ID。 |
| `origin_type` | 字符串，最长 32，枚举，默认 `CLASSIFICATION_PENDING` | 原创/转发判定结果。 |
| `classification_error` | 字符串，最长 500，可留空 | 判定失败的脱敏摘要。 |
| `classified_at` | 日期时间，可空 | 最近一次完成判定的时间。 |
| `source_appid` | 字符串，最长 128，可留空 | 所属小游戏 AppID。 |
| `created_at` | 日期时间，自动写入 | 首次采集时间。 |
| `updated_at` | 日期时间，自动更新 | 最近更新时间。 |

`origin_type` 枚举：

| 值 | 含义 |
| --- | --- |
| `CLASSIFICATION_PENDING` | 待判定，尚未取得或解析内容卡片。 |
| `ORIGINAL` | 原创帖，已进入 `posts_originalpost`。 |
| `FORWARD` | 转发帖，包含原平台的转发信息，不进入原创工作台。 |

## `posts_originalpost`（原创帖子）

工作台的主帖子记录，只保存已判定为原创的帖子。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `topic_id` | 大整数，唯一 | 原平台帖子 ID。 |
| `content_id` | 字符串，最长 255，唯一 | 原平台内容 ID。 |
| `title` | 字符串，最长 500，可留空 | 帖子标题；游戏圈通常为空。 |
| `content_html` | 文本，可留空 | 原始富文本内容。 |
| `content_text` | 文本，可留空 | 用于展示、搜索与 AI 的纯文本内容。 |
| `ballot_text` | 文本，可留空 | 投票标题和选项拼接文本，用于识别投票内引流。 |
| `publish_time` | 日期时间，可空 | 原平台发布时间。 |
| `status` | 整数，可空 | 帖子管理状态；包含本地删除状态位。 |
| `recommend_status` | 整数，可空 | 原平台推荐状态原值，当前未确认稳定枚举。 |
| `source_appid` | 字符串，最长 128，可留空 | 所属小游戏 AppID。 |
| `author_openid` | 字符串，最长 255，可留空、有索引 | 管理详情返回的作者用户 ID；仅用于禁言状态和同作者历史原创帖关联。 |
| `author_nick_name` | 字符串，最长 255，可留空 | 经帖子详情或作者详情确认的最近作者昵称投影；仅用于内容管理显示，不参与作者搜索。 |
| `created_at` | 日期时间，自动写入 | 首次入库时间。 |
| `updated_at` | 日期时间，自动更新 | 最近同步时间。 |

`status` 枚举：

| 值 | 含义 |
| --- | --- |
| `2` | 正常。 |
| `4` | 屏蔽。 |
| `8` | 沉底。 |
| `16` | 仅自己可见；原平台可见性状态，不是本系统可写的审核动作。 |
| `128` | 原平台已删除；仅本地派生，不会写回原平台。 |
| `132` | 原平台已删除且屏蔽（`128 | 4`）。 |
| `136` | 原平台已删除且沉底（`128 | 8`）。 |
| `140` | 原平台已删除、屏蔽且沉底（`128 | 4 | 8`）。 |
| `NULL` 或其他值 | 尚未同步或原平台未知状态。 |

`2` 是唯一正常基值，不参与按位组合。屏蔽、沉底和删除使用按位与判定；例如 `status & 128 != 0` 表示原平台已删除。`16` 是独立的上游仅自己可见状态：可在工作台筛选、查看和刷新，但系统不会发送沉底、屏蔽、恢复或禁言操作覆盖该状态。删除确认时间不再独立保存，可通过 `updated_at` 与后台任务记录辅助追溯。

## `posts_authormutestatus`（作者禁言状态）

按作者用户 ID与游戏 AppID保存最近一次管理详情观测到的禁言状态。`(openid, source_appid)` 唯一；无有效禁言时两个到期字段均为 `NULL`，表示已确认未禁言，而没有记录才表示尚未同步。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `openid` | 字符串，最长 255 | 作者平台用户 ID。 |
| `source_appid` | 字符串，最长 128 | 当前游戏禁言的作用域。 |
| `nick_name` | 字符串，最长 255，可留空 | 最近同步昵称，仅供禁言管理展示与查询。 |
| `local_muted_until` | 日期时间，可空 | 当前游戏禁言有效期至。 |
| `global_muted_until` | 日期时间，可空 | 全局禁言有效期至。 |
| `first_detected_at` | 日期时间，可空 | 首次发现有效禁言的时间，不代表上游真实开始时间。 |
| `last_synced_at` | 日期时间 | 最近成功详情同步时间。 |
| `source_requested_at` | 日期时间 | 产生当前观测的详情请求起始时间，用于拒绝乱序旧响应覆盖。 |
| `last_source_post_id` | 外键，可空 | 最近用于同步该状态的原创帖子。 |

## `posts_postmedia`（帖子媒体）

原创帖附带的图片或视频引用；不下载媒体文件。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `post_id` | 外键，指向 `posts_originalpost`，级联删除 | 所属原创帖。 |
| `url` | URL 字符串，最长 1000 | 媒体访问地址。 |
| `media_type` | 字符串，最长 16，枚举，默认 `IMAGE` | 媒体类别。 |
| `width` | 正整数，可空 | 原媒体宽度（像素）。 |
| `height` | 正整数，可空 | 原媒体高度（像素）。 |
| `is_gif` | 布尔，默认 `false` | 是否 GIF 动图。 |

`media_type` 枚举：

| 值 | 含义 |
| --- | --- |
| `IMAGE` | 图片。 |
| `VIDEO` | 视频。 |

## `posts_interactionsnapshot`（互动快照）

同一帖子在不同采集时间的互动计数，供实时运营分析和后续趋势能力使用；运营分析仅取每帖最新快照，不保存评论正文。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `post_id` | 外键，指向 `posts_originalpost`，级联删除 | 所属原创帖。 |
| `captured_at` | 日期时间，自动写入 | 本次读取互动数据的时间。 |
| `view_count` | 正大整数，可空 | 浏览数。 |
| `reply_count` | 正大整数，默认 `0` | 评论/回复数。 |
| `love_count` | 正大整数，默认 `0` | 点赞数。 |
| `share_count` | 正大整数，默认 `0` | 分享数。 |
| `source` | 字符串，最长 64 | 数据来源接口或采集路径标识。 |

## `reviews_ocrextraction`（OCR 提取结果）

图片文字识别结果，为图片内的违规或引流信息提供 AI 输入。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `post_id` | 外键，指向 `posts_originalpost`，级联删除 | 所属原创帖。 |
| `media_id` | 外键，指向 `posts_postmedia`，级联删除 | 被识别的图片媒体。 |
| `text` | 文本，可留空 | OCR 提取文字。 |
| `confidence` | 浮点数，可空 | OCR 置信度。 |
| `provider` | 字符串，最长 100 | OCR 服务或模型标识。 |
| `error_summary` | 字符串，最长 500，可留空 | 识别失败的脱敏摘要。 |
| `created_at` | 日期时间，自动写入 | 识别完成时间。 |

## `reviews_policyversion`（审核策略版本）

一组可追溯的审核策略版本。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `name` | 字符串，最长 100，唯一 | 策略版本名称。 |
| `version` | 字符串，最长 100 | 人工可读的版本号。 |
| `is_active` | 布尔，默认 `false` | 是否当前启用。 |
| `created_at` | 日期时间，自动写入 | 创建时间。 |

## `reviews_policyrule`（审核策略规则）

策略版本中的单条风险处置规则。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `policy_version_id` | 外键，指向 `reviews_policyversion`，级联删除 | 所属策略版本。 |
| `risk_label` | 字符串，最长 100 | 风险标签，如引流、外挂。 |
| `min_confidence` | 浮点数，默认 `0` | 命中规则的最低置信度。 |
| `recommended_action` | 字符串，最长 32，枚举 | 规则建议的人工动作。 |
| `priority` | 正小整数，默认 `50` | 规则优先级，数值越大越优先。 |
| `exceptions` | JSON 数组，可留空，默认 `[]` | 不适用该规则的例外条件。 |
| `enabled` | 布尔，默认 `true` | 是否参与策略计算。 |
| `auto_execution_enabled` | 布尔，默认 `false` | 是否允许该规则进入受限 AI 自动沉底/屏蔽资格判断。 |

`recommended_action` 枚举也适用于 `reviews_aiassessment`、`reviews_reviewtask` 和 `reviews_operationaudit`：

| 值 | 含义 |
| --- | --- |
| `KEEP` | 保留，不修改原平台状态。 |
| `SINK` | 沉底帖子。 |
| `BLOCK` | 屏蔽帖子。 |
| `MUTE` | 禁言作者。 |
| `UNMUTE` | 解除作者禁言。 |
| `RESTORE` | 将屏蔽或沉底恢复为正常。 |
| `BLOCK_AND_MUTE` | 同时屏蔽帖子并禁言作者。 |
| `HUMAN_REVIEW` | 仅提示人工判断，不建议直接处置。 |

## `reviews_aiassessment`（AI 内容评估）

一次 AI 对原创帖的分类、风险和动作建议；结论始终需要人工审核。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `post_id` | 外键，指向 `posts_originalpost`，级联删除 | 被评估的原创帖。 |
| `category` | 字符串，最长 32，枚举 | 主内容类别。 |
| `secondary_categories` | JSON 数组，可留空，默认 `[]` | 次要内容类别集合。 |
| `sentiment` | 字符串，最长 32，可留空 | 情绪判断原值。 |
| `feedback_value` | 字符串，最长 32，可留空 | 反馈价值判断原值。 |
| `recommended_action` | 字符串，最长 32，枚举 | AI 建议的人工动作。 |
| `overall_confidence` | 浮点数 | AI 总体置信度。 |
| `needs_human_review` | 布尔，默认 `true` | 是否要求进入人工审核。 |
| `model_version` | 字符串，最长 200 | 产生结论的模型标识。 |
| `prompt_version` | 字符串，最长 100，可留空 | 产生当前评估的有效提示词版本。 |
| `policy_version_id` | 外键，可空，指向 `reviews_policyversion`，置空删除 | 评估采用的策略版本。 |
| `deciding_policy_rule_id` | 外键，可空，指向 `reviews_policyrule`，置空删除 | 产生当前建议动作的策略规则。 |
| `deciding_risk_evidence_id` | 外键，可空，指向 `reviews_riskevidence`，置空删除 | 产生当前建议动作的决定性风险证据。 |
| `raw_error_summary` | 字符串，最长 500，可留空 | 预留的 AI 调用失败脱敏摘要；当前失败信息记录在对应后台任务。 |
| `created_at` | 日期时间，自动写入 | 评估完成时间。 |

`category` 枚举：

| 值 | 含义 |
| --- | --- |
| `GUIDE` | 攻略。 |
| `FEEDBACK` | 反馈。 |
| `SUGGESTION` | 建议。 |
| `QUESTION` | 求助问答。 |
| `ACTIVITY` | 活动资讯。 |
| `SOCIAL` | 晒图社交。 |
| `OFF_TOPIC` | 无关游戏圈的内容。 |
| `OTHER` | 其他内容。 |

`reviews_riskevidence.label` 使用的一级风险标签：

| 值 | 含义 |
| --- | --- |
| `TRAFFIC_DIVERSION` | 引流或外部联系方式。 |
| `PRIVATE_SERVER` | 私服或盗版服务。 |
| `CHEAT` | 外挂或作弊。 |
| `PERSONAL_ATTACK` | 人身攻击或辱骂。 |
| `FRAUD_SCAM` | 欺诈或诈骗。 |
| `PORNOGRAPHY` | 色情低俗内容。 |
| `POLITICAL_SENSITIVE` | 政治敏感内容。 |
| `ADVERTISEMENT_SPAM` | 广告或垃圾内容。 |
| `TRANSACTION_RISK` | 交易或资金风险。 |
| `OFF_TOPIC` | 无关游戏圈内容。 |
| `NEGATIVE_SPAM` | 纯负面灌水。 |
| `CONTENT_ANOMALY` | 内容异常，需人工核验。 |

历史评估中可能出现更细的旧标签；展示层会映射为上述通用风险类型，原始代码仍保留在记录中以便追溯。

## `reviews_riskevidence`（风险证据）

AI 评估中的单项风险证据，可保存多段命中内容。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `assessment_id` | 外键，指向 `reviews_aiassessment`，级联删除 | 所属 AI 评估。 |
| `label` | 字符串，最长 100 | 风险标签。 |
| `confidence` | 浮点数 | 该风险的置信度。 |
| `evidence` | JSON 数组，默认 `[]` | 命中的证据片段或位置。 |
| `ocr_source_refs` | JSON 数组，默认 `[]` | 与证据匹配的 OCR 来源记录和文本指纹。 |
| `media_source_refs` | JSON 数组，默认 `[]` | 当前图片 OCR 或视频证据的本地记录标识、文本指纹和视频时间位置；不保存媒体 URL 或认证数据。 |

## `reviews_reviewtask`（人工审核任务）

每篇原创帖最多一条当前审核任务，连接 AI 建议与人工最终决定。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `post_id` | 一对一外键，指向 `posts_originalpost`，级联删除 | 被审核的原创帖。 |
| `assessment_id` | 外键，可空，指向 `reviews_aiassessment`，置空删除 | 关联的 AI 评估。 |
| `suggested_action` | 字符串，最长 32，枚举 | AI 或策略建议动作。 |
| `priority` | 正小整数，默认 `50` | 人工审核优先级，数值越大越优先。 |
| `status` | 字符串，最长 32，枚举，默认 `PENDING` | 任务审核进度。 |
| `needs_focus` | 布尔，默认 `false` | 是否需要运营重点关注。 |
| `focus_reasons` | JSON 数组，默认 `[]` | 新评估仅使用 `EVIDENCE_MISSING`：空文字输入且媒体证据不足；历史记录可保留旧原因。 |
| `final_action` | 字符串，最长 32，枚举，可留空 | 人工最终选择动作。 |
| `reviewed_by_id` | 外键，可空，指向 `auth_user`，置空删除 | 完成审核的后台用户。 |
| `reviewed_at` | 日期时间，可空 | 人工审核完成时间。 |
| `reason` | 文本，可留空 | 人工审核或处置原因。 |
| `created_at` | 日期时间，自动写入 | 任务创建时间。 |
| `updated_at` | 日期时间，自动更新 | 任务最近更新时间。 |

`status` 枚举：

| 值 | 含义 |
| --- | --- |
| `PENDING` | 待人工审核。 |
| `AUTO_EXECUTED_PENDING_REVIEW` | AI 已执行，等待人工事后复审。 |
| `AUTO_REVIEW_ESCALATED` | AI 复审异常，等待人工跟进。 |
| `REVIEWED` | 已完成人工审核。 |
| `CANCELED` | 已取消，不再处理。 |

## `reviews_replydraft`（回复草案）

安全原创帖可用的本地 AI 回复建议，覆盖反馈、建议、问题、活动、攻略和安全社区互动；命中受控 FAQ 时保存受控引用，未命中时可保存明确标识的通用草案。草案不直接发布到原平台。内容管理仅在对应帖子已有当前可用草案时提供查看、编辑、复制和标记本地采纳入口；这些操作只读写本表。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `post_id` | 一对一外键，指向 `posts_originalpost`，级联删除 | 对应原创帖。 |
| `assessment_id` | 外键，指向 `reviews_aiassessment`，级联删除 | 生成草案的 AI 评估。 |
| `intent_summary` | 文本 | 用户诉求摘要。 |
| `draft` | 文本 | 本地回复建议文本；工作台允许在 4,000 字符内编辑。 |
| `missing_information` | JSON 数组，默认 `[]` | 生成回复前还缺少的信息。 |
| `knowledge_sources` | JSON 数组，默认 `[]` | 草案引用的知识来源描述。 |
| `risk_notice` | 文本，可留空 | 发布前的风险提示。 |
| `adopted` | 布尔，默认 `false` | 是否被标记为本地采用；不代表已经回复或发布。 |
| `adopted_with_edits` | 布尔，默认 `false` | 是否在草案改动后标记本地采用。 |
| `suppressed_reason` | 字符串，最长 500，可留空 | 草案不适用于当前风险评估时的脱敏原因；未抑制时为空。 |
| `faq_rule_id` | 字符串，最长 100，可留空 | 命中的受控 FAQ 规则 ID；历史草案为空。 |
| `faq_version` | 字符串，最长 100，可留空 | 生成草案时使用的 FAQ 资料版本。 |
| `style_variants` | JSON 数组，默认 `[]` | 新 FAQ 草案的 `WARM`、`CONCISE`、`FORMAL` 三种候选正文；历史草案为空。 |
| `selected_style` | 字符串，最长 16，可留空 | 当前带入编辑、复制和本地采纳的候选风格。 |
| `updated_at` | 日期时间，自动更新 | 草案最近更新时间。 |

## `reviews_operationaudit`（上游操作审计）

每次人工确认或受限 AI 自动处置的尝试及状态回读结果；不保存 Cookie、令牌或作者身份标识。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `task_id` | 外键，指向 `reviews_reviewtask`，保护删除 | 所属审核任务；有审计记录时不能删除任务。 |
| `operation_id` | 字符串，最长 64，唯一 | 本次操作的业务编号。 |
| `idempotency_key` | 字符串，最长 255，唯一 | 防止同一确认重复写入上游的幂等键。 |
| `action` | 字符串，最长 32，枚举 | 实际尝试的处置动作。 |
| `reason` | 字符串，最长 500 | 本次处置原因。 |
| `policy_version_id` | 外键，可空，指向 `reviews_policyversion`，置空删除 | 处置依据的策略版本。 |
| `before_state` | JSON 对象，默认 `{}` | 操作前的帖子状态快照。 |
| `after_state` | JSON 对象，默认 `{}` | 操作后的回读状态快照。 |
| `result_code` | 字符串，最长 100，可留空 | 上游或本地结果码。 |
| `error_summary` | 字符串，最长 500，可留空 | 失败的脱敏摘要。 |
| `executor_id` | 外键，可空，指向 `auth_user`，置空删除 | 执行确认的后台用户。 |
| `execution_source` | 字符串，最长 16，枚举，默认 `HUMAN` | 处置来源：人工或 AI 自动。 |
| `automation_snapshot` | JSON 对象，默认 `{}` | AI 自动处置时的不可变授权、模型、策略和证据摘要；人工操作为空。 |
| `executed_at` | 日期时间，可空 | 实际调用上游的时间。 |
| `created_at` | 日期时间，自动写入 | 审计记录创建时间。 |

`result_code` 常见取值包括：`EXECUTING`（确认已持久化，正在执行上游读写）、`OK`（状态回读验证成功）、`ACTION_FAILED`（上游调用或回读失败）、`SKIPPED_UPSTREAM_NOT_NORMAL`（目标已不是正常帖，跳过重复处置）和 `CAPABILITY_DISABLED`（对应上游写能力未启用）。审计用于操作防重和追溯，不作为审核任务状态的强依赖。

## `reviews_aioperationreview`（AI 操作复审）

每条成功 AI 自动沉底/屏蔽审计对应一条人工事后复审记录。复审不会自动恢复上游状态；异常纠正仍走既有人工二次确认与审计。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `audit_id` | 一对一外键，指向 `reviews_operationaudit`，保护删除 | 对应 AI 自动操作审计。 |
| `status` | 字符串，最长 16，枚举，默认 `PENDING` | `PENDING`、`CONFIRMED`、`ESCALATED` 或 `RESTORED`。 |
| `reviewer_id` | 外键，可空，指向 `auth_user`，置空删除 | 完成复审的后台用户。 |
| `reviewed_at` | 日期时间，可空 | 最近复审时间。 |
| `reason` | 文本，可留空 | 复审说明或升级原因。 |
| `created_at` / `updated_at` | 日期时间，自动写入/更新 | 创建和最近更新时间。 |

## `jobs_backgroundjob`（后台任务）

采集、状态巡检、AI 和 OCR 等异步任务的持久化队列；`status` 与 `next_run_at` 建有联合索引。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `task_type` | 字符串，最长 64 | 任务类别，如采集或 AI 评估。 |
| `idempotency_key` | 字符串，最长 255，唯一 | 防止重复创建同一任务。 |
| `active_coalescing_key` | 字符串，最长 255，可空、唯一 | 仅活跃任务占用的语义去重键；任务成功、失败或取消后清空，允许后续创建新一轮任务。 |
| `parameter_summary` | JSON 对象，默认 `{}` | 脱敏任务参数摘要。 |
| `checkpoint` | JSON 对象，默认 `{}` | 可恢复执行的分页或阶段检查点。 |
| `status` | 字符串，最长 16，枚举，默认 `PENDING` | 任务运行状态。 |
| `retry_count` | 正整数，默认 `0` | 已执行重试次数。 |
| `max_retries` | 正整数，默认 `3` | 允许的最大重试次数。 |
| `next_run_at` | 日期时间，可空 | 下次可认领执行的时间。 |
| `error_summary` | 字符串，最长 500，可留空 | 最近失败的脱敏摘要。 |
| `started_at` | 日期时间，可空 | 最近一次开始执行时间。 |
| `finished_at` | 日期时间，可空 | 最近一次结束执行时间。 |
| `created_at` | 日期时间，自动写入 | 任务创建时间。 |
| `updated_at` | 日期时间，自动更新 | 任务最近更新时间。 |

`status` 枚举：

| 值 | 含义 |
| --- | --- |
| `PENDING` | 待执行。 |
| `RUNNING` | 执行中。 |
| `SUCCEEDED` | 执行成功。 |
| `FAILED` | 执行失败。 |
| `CANCELED` | 已取消。 |

## `jobs_scheduledproducerconfig`（自动任务生产者）

每个代码注册自动生产者的一条运营配置与脱敏运行投影。它只控制未来自动入队，不是 Worker 或 Supervisor 服务配置。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `producer_key` | 字符串，最长 64，唯一 | 代码注册的稳定生产者键，不向运营页面展示或编辑。 |
| `enabled` | 布尔，默认 `true` | 是否允许该生产者继续自动入队。 |
| `config_version` | 正整数，默认 `1` | 运营配置版本，仅用于逻辑槽审计。 |
| `last_checked_at` / `last_enqueued_at` | 日期时间，可空 | 最近调和检查与最近成功自动入队时间。 |
| `last_error_category` | 字符串，最长 64，可空 | 固定脱敏运行结果类别。 |
| `updated_at` | 日期时间，自动更新 | 最近配置或运行投影更新时间。 |

## `jobs_scheduledproducerruleconfig`（自动任务生产规则）

生产者下的受限规则实例。页面只按模板维护运行时点或间隔；保存的表达式、时区和策略均由服务端固定或校验。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `producer_id` | 外键，指向 `jobs_scheduledproducerconfig`，级联删除 | 所属生产者。 |
| `rule_type` / `rule_key` | 字符串，最长 64 | 代码模板键与稳定规则实例键；同一生产者内 `rule_key` 唯一。 |
| `enabled` | 布尔，默认 `true` | 是否允许该规则继续自动入队。 |
| `schedule_kind` / `schedule_expression` | 字符串 | 固定间隔或五字段 Cron 及其服务端生成表达式。 |
| `timezone_name` | 字符串，默认 `Asia/Shanghai` | 固定中国标准时间，不接受运营编辑。 |
| `misfire_policy` / `overlap_policy` | 字符串 | 代码定义的错过触发与活动任务重叠策略。 |
| `config_version` | 正整数，默认 `1` | 用于逻辑槽审计，不能使已处理槽重新入队。 |
| `covered_through_at` | 日期时间，可空 | 已覆盖到的最新 UTC 调度实例。 |
| `last_skipped_from_at` / `last_skipped_to_at` | 日期时间，可空 | 最近一次跳过区间摘要。 |
| `last_checked_at` / `last_enqueued_at` / `last_error_category` | 日期时间或字符串，可空 | 脱敏运行投影。 |
| `archived_at` | 日期时间，可空 | 删除规则时写入；归档规则退出页面与调和循环，但保留审计。 |
| `updated_at` | 日期时间，自动更新 | 最近配置或运行投影更新时间。 |

## `jobs_scheduledproducerslot` 与 `jobs_scheduledproducerslotjob`（逻辑槽审计）

逻辑槽是实际调度事件的持久化账本。唯一键为规则与 UTC 实例，配置保存不会改变该唯一性；槽—Job 关联保留一次自动生产实际创建或复用的后台任务。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `rule_id` | 外键，指向 `jobs_scheduledproducerruleconfig`，级联删除 | 所属规则；规则日常删除使用归档，因此不会删除既有槽。 |
| `utc_instance` | 日期时间 | 真实触发实例，和规则共同唯一。 |
| `config_version` | 正整数 | 创建该槽时的规则配置快照版本。 |
| `state` | 字符串，枚举 | `READY`、`HELD`、`SKIPPED`、`ENQUEUED` 或 `FAILED`。 |
| `attempt_count` | 正整数，默认 `0` | 已进行的有限生产尝试次数。 |
| `error_category` | 字符串，最长 64，可空 | 固定脱敏失败或跳过类别。 |
| `coalesced_from_at` | 日期时间，可空 | 合并错过或重叠槽时的起始实例。 |
| `created_at` / `updated_at` | 日期时间 | 逻辑槽创建与最后更新时间。 |
| `slot_id` / `job_id` | 槽—Job 关联，联合唯一 | 同一槽与同一后台任务最多关联一次；`job_id` 保护删除。 |

当前 `task_type` 取值：

| 值 | 含义 | 默认消费者服务 |
| --- | --- | --- |
| `SCHEDULED_INGEST_WINDOW` | Scheduler 创建的 30 分钟断点帖子采集。 | 实时采集 Worker（Supervisor 服务 `ingestion`） |
| `STATUS_SYNC` | 回读一批待审核帖的上游状态与互动数据。 | 实时采集 Worker（Supervisor 服务 `ingestion`） |
| `MANUAL_INGEST_WINDOW` | 从系统配置首页创建的指定游戏和最近小时数断点帖子采集。 | 人工补采 Worker（Supervisor 服务 `backfill_ingestion`） |
| `METRICS_REFRESH_WINDOW` | 人工触发的日期范围原创帖互动回读，只新增浏览、点赞、评论和分享快照；单帖传输或契约失败会记录到 checkpoint 样本并继续。 | 人工补采 Worker（Supervisor 服务 `backfill_ingestion`） |
| `INGEST_WINDOW` | 升级前遗留的采集任务；新版本不再创建。 | 人工补采 Worker（Supervisor 服务 `backfill_ingestion`） |
| `OCR_EXTRACT` | 原创帖图片 OCR。 | AI 审核 Worker（Supervisor 服务 `review`） |
| `AI_EVALUATE` | 原创帖 AI 内容评估。 | AI 审核 Worker（Supervisor 服务 `review`） |
| `VIDEO_EXTRACT` | 原创帖视频的本地受限证据提取。任务摘要仅含本地提取记录 ID、帖子 ID、URL 指纹与版本。 | 视频提取 Worker（Supervisor 服务 `video_extraction`） |
| `AI_AUTOMATED_MODERATION` | 已授权 AI 沉底或屏蔽的一次性上游操作与复审建档。 | AI 自动处置 Worker（Supervisor 服务 `ai_moderation`） |
| `GAME_CIRCLE_SESSION_KEEPALIVE` | 可选保活任务；由自动任务生产者按受限间隔入队，参数仅含触发来源与自动化键，消费者开始执行时才冻结当前游戏范围。 | 游戏圈会话服务（Supervisor 服务 `game_circle_session`） |
| `PERMANENT_MUTE_RENEWAL` | 由自动任务生产者按受限定时规则为当前游戏临近到期或禁言时间为 0 的永久标记续期；不处理全局禁言或其他游戏。 | 禁言维护 Worker（Supervisor 服务 `mute_maintenance`） |
| `AUTHOR_MUTE_STATUS_REFRESH` | 手动刷新当前游戏禁言列表作者的禁言时间；冻结列表判断时刻与作者集合并逐位读取用户详情，不执行上游写操作；同一范围仅保留一个活跃任务。 | 禁言维护 Worker（Supervisor 服务 `mute_maintenance`） |
| `POST_AUTHOR_MUTE_STATUS_SYNC` | 扫描当前游戏全量帖子作者；按作者去重读取用户详情，只将仍禁言作者补充到禁言管理，不设置永久禁言；同一范围仅保留一个活跃任务。 | 禁言维护 Worker（Supervisor 服务 `mute_maintenance`） |
| `AUTHOR_MUTE_STATUS_BACKFILL_V2` | 一次性补全历史原创帖作者昵称与禁言状态；已知作者按游戏去重读取作者详情。旧 `AUTHOR_MUTE_STATUS_BACKFILL` 不由新版 Worker 认领。 | 人工补采 Worker（Supervisor 服务 `backfill_ingestion`） |
| `METRIC_DAILY_SYNC` / `METRIC_WEEKLY_SYNC` / `METRIC_BACKFILL` | 游戏圈日/周聚合指标同步或人工范围回填；当前工作区已实现、待发布。 | 指标快照 Worker（Supervisor 服务 `metric_sync`） |
| `PUBLIC_OPINION_REPORT` | 锁定本地正常原创帖来源后生成一份舆情报告运行版本；不调用 CDP、上游或模型。 | 舆情报告 Worker（Supervisor 服务 `public_opinion_reporting`） |

## `reporting_gamecirclemetricsnapshot`（游戏圈指标快照，待发布）

当前工作区的指标能力按单游戏、单来源、单业务周期保存白名单聚合数值；不保存原始响应、认证上下文、排名对象或作者明细。稳定业务键为 `(appid, source, period_start, period_end, scope)`。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `appid` | 字符串，最长 64 | 游戏 AppID。 |
| `source` | 字符串，最长 40，枚举 | 指标来源，如日粉丝、内容消费或周度运营效果。 |
| `period_start` / `period_end` | 日期 | 业务日或业务周的起止日期。 |
| `scope` | 字符串，最长 16，默认 `ALL` | 首期固定全量范围。 |
| `metrics` | JSON 对象 | 来源白名单指标值；不同来源不得混写字段。 |
| `publication_state` | 字符串，最长 24，默认 `VERIFIED` | 周度数据为 `VERIFIED` 或 `PENDING_RECHECK`；日来源固定已验证。 |
| `captured_at` | 日期时间 | 最近一次成功获取时间。 |
| `latest_job_id` | 外键，可空，指向 `jobs_backgroundjob`，置空删除 | 最近写入该快照的后台任务。 |
| `created_at` / `updated_at` | 日期时间，自动写入/更新 | 创建和最近更新时间。 |

## `reporting_metricjoblease`（指标任务租约，待发布）

同一人工指标范围只允许一个待执行或运行中的任务。终态后可创建新批次，快照仍按稳定业务键更新。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `dedupe_key` | 字符串，最长 255，唯一 | 人工范围请求的语义去重键。 |
| `active_job_id` | 外键，可空，指向 `jobs_backgroundjob`，置空删除 | 当前活跃指标任务。 |
| `updated_at` | 日期时间，自动更新 | 最近租约更新时间。 |

## `reviews_videoextraction` 与 `reviews_videoevidence`

视频审核仅持久化可复核的文本与时间位置。`VideoExtraction` 保存帖子、媒体外键、媒体 URL 的 SHA-256 指纹、提取器版本、尝试序号、状态、实际时长、提取时的采样预算快照、关键帧候选循环完成后保存的采样区间、帧数和脱敏错误；同一媒体每次显式重试创建新的尝试记录，旧证据保持可追溯。`VideoEvidence` 保存来源（帧 OCR、二维码、语音）、起止毫秒、顺序、文本、置信度和提供方。二维码文本固定为“检测到二维码”。两表均不保存视频、音频、帧、完整 URL、副本 Cookie、令牌、二维码解码字符串/目标或模型原始响应。

## Supervisor 运行态文件

Supervisor 不新增数据库表。它在 `runtime/supervisor/` 保存独占锁、仅本机使用的控制令牌、回环控制端点、每个服务的 PID/创建时间/期望状态/退出码、Worker 心跳及脱敏日志。这些文件不纳入版本控制，且不得人工编辑或复制到其他机器。

## Django 框架表

以下表由 Django 认证、会话、后台和迁移机制维护，不是内容运营业务模型：`auth_user`、`auth_group`、`auth_permission`、`auth_user_groups`、`auth_user_user_permissions`、`auth_group_permissions`、`django_session`、`django_admin_log`、`django_content_type`、`django_migrations`。

业务表仅以外键引用 `auth_user` 的本地后台用户。系统不将浏览器 Cookie、会话令牌、完整已认证 URL、运营者身份标识或评论正文写入任意数据库表。管理详情返回的发帖作者用户 ID例外：它仅保存在 `posts_originalpost.author_openid` 与 `posts_authormutestatus.openid`，用于禁言状态和同作者历史原创帖关联；不得进入普通报表、通用搜索索引、日志或导出。作者昵称仅保存为 `posts_originalpost.author_nick_name` 和禁言状态投影中的最小显示字段。
