## MODIFIED Requirements

### Requirement: 固定生产者的人工一次性入队

系统 SHALL 为每个代码注册的固定自动生产者提供受认证、CSRF 保护的人工“立即执行”映射。服务端 MUST 按生产者白名单直接创建对应的一次性**业务 Job**；不得接受客户端提供的 AppID、来源、Cron、任意 Job 类型、执行命令、Worker 名称或上游参数。页面提交不得直接调用 CDP、上游、AI、OCR 或 Job handler。

每个直接创建的业务 Job SHALL 在入队事务中读取当前激活游戏与范围版本，并写入服务端构造的 `execution_scope`；页面不得接受 AppID 或 scope 参数。排队期间发生游戏切换时，系统 MUST 继续使用入队时冻结范围，不得改用执行时游戏，也不得以点击时游戏与当前游戏不一致终态失败。

帖子采集入口 MUST 只创建由人工补采 Worker 消费的 `MANUAL_CURRENT_INGEST_WINDOW`：服务端以提交时上海时区当前分钟（秒和微秒清零）为结束时间、其前 30 分钟为开始时间，并固定页大小为 100。相同 AppID、范围版本、开始时间、结束时间和页大小的 `PENDING/RUNNING` Job SHALL 被复用，终态后 SHALL 允许再次创建。该窗口不读取自动规则的启用状态、间隔或数量；暂停、修改或删除自动规则不得影响人工运行。`STATUS_SYNC` 继续由实时采集 Worker 消费并仅用于定时待审核状态巡检，不得由人工入口创建。

暂停生产者或规则 SHALL 只阻止未来自动逻辑槽入队；已认证运营人员的手动一次性业务 Job SHALL 继续可用。手动入队 MUST NOT 启用/暂停生产者、修改规则、推进 `covered_through_at`、创建 `ScheduledProducerSlot`、改变最近自动入队时间或影响下次自动触发。页面 SHALL 将自动运行投影和手动任务结果分开呈现。

固定映射 SHALL 为：帖子采集创建当前时间窗口的 `MANUAL_CURRENT_INGEST_WINDOW`；日指标创建全部启用日来源的范围工作；周指标创建运营效果范围工作；会话保活创建当前游戏 Frame 保活工作；永久禁言续期创建当前游戏续期工作。历史帖子补采和会话自动修复不属于此映射。

#### Scenario: 手动运行当前帖子采集窗口
- **WHEN** 运营人员在 10:12:34 提交帖子采集的立即执行
- **THEN** 系统 SHALL 创建或复用结束于 10:12:00、开始于 09:42:00、页大小为 100 的 `MANUAL_CURRENT_INGEST_WINDOW`
- **THEN** 系统 SHALL 冻结提交时游戏范围并仅交由人工补采 Worker 消费，不得创建 `STATUS_SYNC`
