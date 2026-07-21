# Purpose

定义城主别慌张游戏圈聚合指标快照、同步、调度和运营工作台的数据完整性边界。

## Requirements

### Requirement: 单游戏分来源聚合指标快照
系统 SHALL 仅为 `wx6d2454a8afcd9706` 保存游戏圈聚合快照，并以 `(appid, source, period_start, period_end, scope)` 作为稳定业务键。每条快照 SHALL 关联来源、业务日或周、聚合范围、最近同步时间和该来源白名单指标；`scope` 首期 SHALL 固定为 `ALL`。

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
