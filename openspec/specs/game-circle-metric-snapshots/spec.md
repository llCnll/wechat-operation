# Purpose

定义当前操作游戏的游戏圈聚合指标快照、同步、调度和运营工作台的数据完整性边界。
## Requirements
### Requirement: 指标读取的执行快照和会话服务

指标 Worker SHALL 通过 `GameCircleSessionService` 的固定本地 IPC 契约读取数据，并仅提交 execution scope、指标来源和业务分页参数。它 MUST NOT 自行扫描浏览器页面、持有/跨游戏复用身份 token，或为每个指标请求调用身份绑定。

会话服务 SHALL 使用 ACTIVE SessionLease；新插件、服务/浏览器 generation 变化、插件替换和来源查询的已验证 `errcode=-2` 的身份处理均由服务完成。指标来源异常的刷新判断与帖子来源一致；`errcode=1` 和未验证业务码不得被误判为身份失效。插件失效时，指标 Worker SHALL 保留日期块/分页 checkpoint，并在会话服务返回活动刷新状态时延后，不增加业务 retry；指标 MUST NOT 写入零值、空值或猜测值，也不得覆盖已有有效快照。

#### Scenario: 同一插件 generation 的多个指标请求
- **WHEN** 多个指标请求在同一已验证 PLUGIN 和 target AppID 中连续执行
- **THEN** 它们 SHALL 由会话服务使用同一 ACTIVE SessionLease
- **THEN** 插件 generation 改变后，服务 SHALL 发布新的 ACTIVE SessionLease 后再处理新请求

### Requirement: 单游戏分来源聚合指标快照
系统 SHALL 仅为当前操作游戏保存游戏圈聚合快照，并以 `(appid, source, period_start, period_end, scope)` 作为稳定业务键。每条快照 SHALL 关联来源、业务日或周、聚合范围、最近同步时间和该来源白名单指标；`scope` 首期 SHALL 固定为 `ALL`。

已启用日来源 SHALL 包括游戏圈粉丝、内容消费、内容创作、内容分享和游戏圈活跃用户；运营效果 SHALL 按周保存。内容粉丝 SHALL 保持为独立且未启用的权限受限来源，未取得成功权限和字段样本前 MUST NOT 创建其生产快照或生产任务。不同 source MUST NOT 混写指标。

系统 MUST NOT 保存 token、Cookie、完整请求 URL、认证账号标识、作者明细、排名对象、能力卡/配置对象或原始响应。

#### Scenario: 重复同步同一指标周期
- **WHEN** 同一来源、游戏、范围和指标周期被重复同步
- **THEN** 系统 SHALL 更新该稳定业务键对应的快照和最近同步时间，且不得创建重复记录

#### Scenario: 同日读取多个分析域
- **WHEN** 内容消费、创作、分享和游戏圈活跃用户均返回同一业务日期
- **THEN** 系统 SHALL 分别以各自 source 保存该日快照，且每个快照只含该 source 白名单字段

### Requirement: 内容分析范围读取和恢复
系统 SHALL 使用内容分析详情接口的日期范围和唯一 `need_*_data` 开关读取一个日来源。Worker SHALL 校验业务码、`data.list`、每条有效 `ds` 和当前 source 的必需字段，并按 `ds` 显式排序。`data.total_count` 仅可作为分页辅助；当它与当前页记录矛盾时，Worker MUST NOT 丢弃当前已验证记录。

系统 SHALL 以每条 `data.list[].ds` 作为业务日期。它 MUST NOT 因范围内缺少日期而创建零值、空值或猜测值，也 MUST NOT 以缺失结果覆盖已有快照。

历史读取 SHALL 按 source 和至多 90 日日期块执行。checkpoint SHALL 记录来源、日期块、下一页、保存数和最后业务日；只有完整页持久化后才推进页码。分页和恢复 MUST NOT 假设单页足够。

#### Scenario: 不连续且乱序的日期记录
- **WHEN** 一个范围查询返回不连续、倒序或乱序的 `ds` 记录
- **THEN** Worker SHALL 仅为返回日期写入快照，并在写入前按 `ds` 排序
- **THEN** 范围内未返回日期不得产生快照

#### Scenario: 页读取中断
- **WHEN** 一个日期块已完成前一页并在下一页读取期间中断
- **THEN** Worker SHALL 保留下一页 checkpoint；恢复后 SHALL 从该页继续，允许因页内中断重复写入同一稳定业务键

#### Scenario: 开关或字段不匹配
- **WHEN** 当前 source 的响应缺少必需字段、日期不可解析或结构不符合契约
- **THEN** Worker SHALL 终态失败该读取单元，且不得覆盖已有有效快照

### Requirement: 游戏圈活跃用户口径
系统 SHALL 将 `need_content_retain_data` 返回的 `quanzi_dau` 保存并展示为游戏圈活跃用户。系统 MUST NOT 将该值命名、展示或计算为 7、14 或 30 日留存率，除非另有经过验证的字段和计算口径。

#### Scenario: 切换留存页面窗口
- **WHEN** 上游页面在 7、14 和 30 天视图之间切换并改变请求范围
- **THEN** 系统 SHALL 将其视为查询窗口变化，而非新 source 或不同留存率快照

### Requirement: 调度、复核和错误分类
Scheduler SHALL 在北京时间每日 `07:30` 后为五个已启用日 source 分别创建前一自然日的日同步任务，并在周一 `17:30` 后创建已完成周的运营效果同步任务。日任务 SHALL 复核 `D-2` 至 `D0`；周任务 SHALL 复核最近三个已完成周。Scheduler SHALL 只入队，不得建立 CDP 会话或认领任务。

同一调度时间桶 SHALL 使用稳定幂等键。相同人工回填范围的 `PENDING`/`RUNNING` 任务 SHALL 由数据库约束或租约原子复用；终态人工任务 MUST 允许新运行批次。快照 SHALL 继续按稳定业务键更新，不得因复核重复创建。

CDP 不可用、未登录或身份绑定失败时，Worker SHALL 延后 5 分钟且不增加 retry count。明确权限不足、字段缺失、日期无效或结构不兼容 SHALL 终态失败。429、5xx、传输错误 SHALL 最多指数退避重试 3 次，单次上限 5 分钟；未知非零业务码 SHALL 至少重试 1 次后再终态失败。上述错误均 MUST NOT 写入或覆盖零值、空值或猜测值。

#### Scenario: 日任务等待人工恢复登录
- **WHEN** 日任务在 `07:30` 后执行而受控 Chrome 登录态失效
- **THEN** Worker SHALL 保留“昨日”业务日期和 checkpoint，延后而不自动登录或消耗 retry count
- **WHEN** 运营人员扫码恢复登录
- **THEN** Worker SHALL 在后续轮询继续原任务

#### Scenario: 同一人工回填并发入队
- **WHEN** 两个请求同时创建相同来源和范围的人工回填
- **THEN** 系统 SHALL 只保留一个活跃任务并将其返回给两个请求
- **WHEN** 该任务进入终态后再次请求同一范围
- **THEN** 系统 SHALL 创建新的运行批次并更新已有稳定快照

### Requirement: 数据日期一致的运营工作台
系统 SHALL 将运营工作台分为每日运营和周度复盘。每日首屏的跨域 KPI SHALL 使用五个已启用日来源共同存在的最近完整业务日；周度复盘 SHALL 只展示周快照及其周期，且不得混入每日趋势。

当不存在完整业务日时，系统 SHALL 显示“不完整日数据”，每张日 KPI 卡 SHALL 显示自己的业务日期或缺失状态。系统 MUST NOT 在不完整数据上生成跨域漏斗、转化率、综合健康判断或跨域环比。单来源趋势 MAY 使用其相邻已落库日比较，但 SHALL 标示来源和业务日期。

首屏日指标 SHALL 包括粉丝存量/新增/退圈/净增、发帖数/创作人数、访问用户/浏览量、分享次数/分享人数和游戏圈活跃用户。排名、能力卡、配置和同类参照仅可作为诊断信息，MUST NOT 成为首屏 KPI 或主指标快照。

#### Scenario: 存在共同完整业务日
- **WHEN** 五个日来源均存在同一最近业务日
- **THEN** 每日首屏所有跨域 KPI SHALL 使用该业务日并显示该共同日期

#### Scenario: 某来源缺失
- **WHEN** 任一日来源缺少最近业务日记录
- **THEN** 工作台 SHALL 将每日首屏标为不完整，并逐卡显示日期或缺失状态
- **THEN** 工作台不得把不同业务日的指标拼接为同一漏斗或综合判断

### Requirement: 运营效果周快照发布稳定性
系统 SHALL 对运营效果周快照持久化发布状态 `VERIFIED` 或 `PENDING_RECHECK`；所有日来源快照 SHALL 为 `VERIFIED`。当运营效果一条记录的日均访问、游戏圈渗透、内容曝光、触达新进/回流/付费用户、发表率和优质内容率全部为零时，系统 MUST 将该记录标为 `PENDING_RECHECK`。好友动态曝光和点击不参与该判定。

`PENDING_RECHECK` 记录 MAY 保留来源白名单数值和任务追溯，但 MUST NOT 作为正常周度 KPI、趋势、告警或真实零值展示。若同一稳定业务键已有 `VERIFIED` 快照，待复核记录 MUST NOT 覆盖其指标、采集时间或发布状态；后续返回任一核心指标非零的有效记录 SHALL 覆盖同一稳定业务键并标记为 `VERIFIED`。

#### Scenario: 上游全零占位不污染已验证快照
- **WHEN** 某已完成周已有非零 `VERIFIED` 快照，而后一次读取返回全部核心指标为零
- **THEN** 系统 SHALL 保留既有已验证快照，且不得将其展示为零值

#### Scenario: 待复核周得到有效发布值
- **WHEN** 某周的 `PENDING_RECHECK` 快照在后续读取中返回任一非零核心指标
- **THEN** 系统 SHALL 以返回值更新同一稳定业务键并将其状态改为 `VERIFIED`

### Requirement: 日周指标的人工范围同步
系统 SHALL 允许已认证运营人员通过固定生产者手动提交日/周指标日期范围，并仅创建或复用范围型指标 Job。所有范围按 `Asia/Shanghai` 自然日解释，开始日期和结束日期均包含在内，最大跨度为 90 个自然日；系统 MUST 拒绝未来日期、无效日期和倒置范围。

日指标范围同步 SHALL 对每个已启用日指标来源创建独立的范围 Job，并以输入范围作为 `start_ds/end_ds`。周指标范围同步 SHALL 将输入开始日期归一化为其所在周周一，将输入结束日期归一化为其所在周周日，并以该完整连续范围创建或复用运营效果范围 Job。Worker SHALL 使用范围接口、现有分页和 checkpoint 处理范围；返回的每条周记录 SHALL 以自身业务周期起始日保存，不得以请求日期伪造周期。

人工范围同步 SHALL 在入队事务中创建带有服务端冻结 AppID 和范围版本的日/周指标业务 Job；指标 Worker 必须使用该 scope 创建或复用活动范围租约和范围工作。Job 创建到首次执行之间切换游戏时，系统 SHALL 继续使用入队时游戏范围，且不得以点击时游戏与当前游戏不一致终态失败。终态任务不阻止新的运行批次。人工范围 Job 不得创建自动调度逻辑槽、不改变自动日/周任务的稳定幂等键、规则覆盖进度或下次触发时间。

#### Scenario: 日指标范围按来源入队
- **WHEN** 运营人员为当前游戏提交 2026-07-01 至 2026-07-15 的有效日指标范围
- **THEN** 系统 SHALL 为每个已启用日指标来源创建或复用一个范围 Job，并使用相同的开始和结束业务日期
- **THEN** 系统不得连续创建逐日 Job，也不得包含权限未验证的内容粉丝来源

#### Scenario: 周指标范围归一化
- **WHEN** 运营人员提交 2026-07-08 至 2026-07-24 的周指标范围
- **THEN** 系统 SHALL 将实际查询范围归一化为 2026-07-06 至 2026-07-26，并创建或复用一个运营效果范围 Job
- **THEN** Worker SHALL 以一次范围读取覆盖该区间，而不得为 2026-07-06、2026-07-13、2026-07-20 分别发起上游请求

#### Scenario: 重复提交同一实际周范围
- **WHEN** 两个请求提交不同自然日输入但归一化为同一周指标实际范围，且首个范围 Job 仍为待执行或运行中
- **THEN** 系统 SHALL 返回同一活跃范围 Job
- **THEN** 系统不得增加第二次上游范围读取
