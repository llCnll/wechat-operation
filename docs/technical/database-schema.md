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
| `created_at` | 日期时间，自动写入 | 首次入库时间。 |
| `updated_at` | 日期时间，自动更新 | 最近同步时间。 |

`status` 枚举：

| 值 | 含义 |
| --- | --- |
| `2` | 正常。 |
| `4` | 屏蔽。 |
| `8` | 沉底。 |
| `128` | 原平台已删除；仅本地派生，不会写回原平台。 |
| `132` | 原平台已删除且屏蔽（`128 | 4`）。 |
| `136` | 原平台已删除且沉底（`128 | 8`）。 |
| `140` | 原平台已删除、屏蔽且沉底（`128 | 4 | 8`）。 |
| `NULL` 或其他值 | 尚未同步或原平台未知状态。 |

`2` 是唯一正常基值，不参与按位组合。屏蔽、沉底和删除使用按位与判定；例如 `status & 128 != 0` 表示原平台已删除。删除确认时间不再独立保存，可通过 `updated_at` 与后台任务记录辅助追溯。

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
| `prompt_version` | 字符串，最长 100，可留空 | 预留的提示词版本；当前评估流程尚未写入。 |
| `policy_version_id` | 外键，可空，指向 `reviews_policyversion`，置空删除 | 评估采用的策略版本。 |
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

## `reviews_reviewtask`（人工审核任务）

每篇原创帖最多一条当前审核任务，连接 AI 建议与人工最终决定。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `post_id` | 一对一外键，指向 `posts_originalpost`，级联删除 | 被审核的原创帖。 |
| `assessment_id` | 外键，可空，指向 `reviews_aiassessment`，置空删除 | 关联的 AI 评估。 |
| `suggested_action` | 字符串，最长 32，枚举 | AI 或策略建议动作。 |
| `priority` | 正小整数，默认 `50` | 人工审核优先级，数值越大越优先。 |
| `status` | 字符串，最长 16，枚举，默认 `PENDING` | 任务审核进度。 |
| `needs_focus` | 布尔，默认 `false` | 是否需要运营重点关注。 |
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
| `REVIEWED` | 已完成人工审核。 |
| `CANCELED` | 已取消，不再处理。 |

## `reviews_replydraft`（回复草案）

反馈或建议类帖子可用的 AI 回复建议，不直接发布到原平台。内容管理仅在对应帖子已有草案时提供查看、编辑、复制和标记本地采纳入口；这些操作只读写本表。

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
| `suppressed_reason` | 字符串，最长 500，可留空 | 预留的不生成或不采用草案原因；当前正常评估流程不写入该字段。 |
| `updated_at` | 日期时间，自动更新 | 草案最近更新时间。 |

## `reviews_operationaudit`（上游操作审计）

每次人工确认的处置尝试及状态回读结果；不保存 Cookie、令牌或作者身份标识。

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
| `executed_at` | 日期时间，可空 | 实际调用上游的时间。 |
| `created_at` | 日期时间，自动写入 | 审计记录创建时间。 |

`result_code` 常见取值包括：`EXECUTING`（确认已持久化，正在执行上游读写）、`OK`（状态回读验证成功）、`ACTION_FAILED`（上游调用或回读失败）、`SKIPPED_UPSTREAM_NOT_NORMAL`（目标已不是正常帖，跳过重复处置）和 `CAPABILITY_DISABLED`（对应上游写能力未启用）。审计用于操作防重和追溯，不作为审核任务状态的强依赖。

## `jobs_backgroundjob`（后台任务）

采集、状态巡检、AI 和 OCR 等异步任务的持久化队列；`status` 与 `next_run_at` 建有联合索引。

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | 整数，自增，主键 | 本地记录编号。 |
| `task_type` | 字符串，最长 64 | 任务类别，如采集或 AI 评估。 |
| `idempotency_key` | 字符串，最长 255，唯一 | 防止重复创建同一任务。 |
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

当前 `task_type` 取值：

| 值 | 含义 | 默认消费者 |
| --- | --- | --- |
| `INGEST_WINDOW` | 指定游戏和时间窗口的断点帖子采集。 | `run_ingestion_worker` |
| `STATUS_SYNC` | 回读一批待审核帖的上游状态与互动数据。 | `run_ingestion_worker` |
| `METRICS_REFRESH_WINDOW` | 人工触发的日期范围原创帖互动回读，只新增浏览、点赞、评论和分享快照；单帖传输或契约失败会记录到 checkpoint 样本并继续。 | `run_ingestion_worker` |
| `OCR_EXTRACT` | 原创帖图片 OCR。 | `run_review_worker` |
| `AI_EVALUATE` | 原创帖 AI 内容评估。 | `run_review_worker` |

## Django 框架表

以下表由 Django 认证、会话、后台和迁移机制维护，不是内容运营业务模型：`auth_user`、`auth_group`、`auth_permission`、`auth_user_groups`、`auth_user_user_permissions`、`auth_group_permissions`、`django_session`、`django_admin_log`、`django_content_type`、`django_migrations`。

业务表仅以外键引用 `auth_user` 的本地后台用户。系统不将浏览器 Cookie、会话令牌、完整已认证 URL、原平台作者 `openid` 或评论正文写入任意数据库表；禁言所需的作者身份仅在上游请求内存中短暂使用。
