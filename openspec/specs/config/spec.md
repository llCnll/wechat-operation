# Purpose

定义 config/ 与共享管理界面的运行配置、游戏圈会话、系统配置台和测试启动隔离边界。

## Requirements

### Requirement: Django 单体服务运行时
系统 SHALL 提供基于 Python 3.12 和 Django 5.2 LTS 的单体服务，包含领域应用、数据库迁移、健康检查端点以及供内部运营使用的 Django Admin 入口。

#### Scenario: 运行时配置有效时启动服务
- **WHEN** 必需的运行时配置齐全且已应用数据库迁移
- **THEN** 服务 SHALL 成功启动，提供健康检查端点，并向已认证的内部用户开放已配置的 Admin 入口

#### Scenario: 运行时配置无效
- **WHEN** 启动时缺少必需配置或配置校验失败
- **THEN** 服务 SHALL 快速失败并给出已脱敏、可处理的错误信息，且 SHALL NOT 记录缺失的密钥值

### Requirement: 运行态目录隔离
系统 SHALL 将部署配置、SQLite 数据库、收集后的静态文件、Worker 锁、Supervisor 独占锁、Supervisor 状态、Supervisor 凭据、服务 PID、心跳和可选日志统一置于项目根目录的 `runtime/`。代码包与版本控制 SHALL 排除整个目录；`.env.example` 仅作为可打包的配置模板。

#### Scenario: 初始化或打包服务
- **WHEN** 运营人员部署或打包服务代码
- **THEN** 系统 SHALL 能在缺少 `runtime/` 的代码包中初始化该目录，并从 `runtime/.env` 读取运行时配置，而不得将数据库、密钥、Supervisor 凭据、锁文件或日志混入代码目录

### Requirement: SQLite 部署边界与专用 Worker
系统 SHALL 在首期使用启用 WAL 模式、外键约束和 busy timeout 的 SQLite，并由本机 Supervisor 托管一个 Web 实例、一个 Scheduler Worker、一个实时采集 Worker、一个人工补采 Worker、一个指标同步 Worker、一个 AI 审核 Worker与一个游戏圈会话服务。Worker 与会话服务 SHALL 按职责隔离；SQLite 写入阶段会串行化，同一职责 SHALL NOT 启动多个实例。日常部署 MUST 通过 Supervisor 启动和控制这些服务；直接管理命令仅用于开发或故障诊断。

#### Scenario: 初始化数据库
- **WHEN** 服务初始化新的 SQLite 数据库
- **THEN** 它 SHALL 在正常处理任务前应用迁移，并配置 WAL 模式、外键约束和已配置的 busy timeout

#### Scenario: 专用 Worker 不互相认领任务
- **WHEN** Scheduler Worker、实时采集 Worker、人工补采 Worker、指标同步 Worker 与 AI 审核 Worker 同时运行
- **THEN** Scheduler Worker SHALL 仅创建调度任务而不认领业务任务；实时采集 Worker SHALL 仅认领 `SCHEDULED_INGEST_WINDOW` 与 `STATUS_SYNC`；人工补采 Worker SHALL 仅认领 `MANUAL_INGEST_WINDOW`、`METRICS_REFRESH_WINDOW` 与升级前遗留的 `INGEST_WINDOW`；指标同步 Worker SHALL 仅认领 `METRIC_DAILY_SYNC`、`METRIC_WEEKLY_SYNC` 与 `METRIC_BACKFILL`；AI 审核 Worker SHALL 仅认领 OCR 与 AI 评估任务，且不得将另一类任务标记为未注册或失败

#### Scenario: 人工长任务不阻塞自动采集
- **WHEN** 人工补采 Worker 正在执行历史补采或范围互动刷新，且自动调度器创建新的 15 分钟任务
- **THEN** 新的 `SCHEDULED_INGEST_WINDOW` 或 `STATUS_SYNC` SHALL 由实时采集 Worker 独立认领，不得等待人工补采 Worker 完成

#### Scenario: 需要水平扩容
- **WHEN** 任一任务类型需要多个消费者，或 SQLite 写锁持续影响任务成功率
- **THEN** 部署 SHALL 迁移到 PostgreSQL 后再扩展 Worker 实例，而不得在 SQLite 上横向扩容同一类 Worker

### Requirement: 持久化后台任务执行
系统 SHALL 在数据库任务表中持久化定时和异步工作，记录任务类型、幂等键、状态、检查点、重试次数、下次执行时间和已脱敏的错误摘要。范围指标 checkpoint SHALL 至少包含来源、日期块、下一页、保存数和最后业务日；指标 Worker SHALL 在页成功落库后推进页码。

#### Scenario: 中断的采集任务恢复
- **WHEN** 采集任务在保存页检查点后停止
- **THEN** 后续 Worker 运行 SHALL 从已持久化的检查点恢复，而不是重新处理已完成页面

#### Scenario: 可重试的任务失败
- **WHEN** 任务因可重试的外部服务错误失败且仍有重试次数
- **THEN** Worker SHALL 持久化一个有上限的指数退避下次执行时间，且 SHALL NOT 为相同幂等键创建重复任务

#### Scenario: 指标范围任务中断恢复
- **WHEN** 指标任务在已完成页后中断
- **THEN** 后续指标 Worker SHALL 从持久化的下一页和日期块恢复，不得假设单页或重新补齐缺失日期

### Requirement: 外部服务适配器边界
系统 SHALL 将原平台、OCR 和 AI 集成隔离在内部适配器接口之后，领域服务 SHALL 使用标准化内部数据传输对象而不是供应商原始响应。游戏圈私有只读接口 SHALL 隔离在指标 CDP 适配器中；领域服务 SHALL 只接收 source 特定 DTO 和白名单字段，不得持久化原始响应或认证上下文。

适配器和任务 SHALL 将登录/身份问题、权限或契约问题、传输/限流/5xx 和未知业务码区分处理。仅明确权限和契约错误可直接终态；临时错误 SHALL 按指标规格有限退避。

#### Scenario: 原平台响应字段变化
- **WHEN** 原平台响应缺少预期字段或具有不兼容结构
- **THEN** 适配器 SHALL 返回有类型的契约错误，领域服务 SHALL 记录非破坏性的失败或待处理任务状态

#### Scenario: 指标接口字段或临时状态变化
- **WHEN** 上游缺少必需字段或返回不兼容结构
- **THEN** 适配器 SHALL 返回契约错误，调用方不得覆盖既有快照
- **WHEN** 上游限流、5xx、传输失败或返回未知业务码
- **THEN** 调用方 SHALL 按指标规格退避重试并保留 checkpoint 与脱敏错误类别

### Requirement: 游戏圈来源适配器边界
游戏圈来源适配器 SHALL 仅由受管会话服务调用。调用方不得自行选择浏览器页面、持有认证材料或执行来源身份绑定。Web、Scheduler 与未确认的上游写操作不得直接访问 CDP；经认证、CSRF 二次确认的有界人工读取或写操作只可通过固定本地服务契约提交类型化业务请求。

会话不可用时，系统 SHALL 返回脱敏的可重试结果并请求恢复；人工写操作不得在恢复后自动重放。服务不得向调用方接收或返回 target ID、任意浏览器命令、JavaScript、URL 或认证材料。

#### Scenario: 有界人工操作遇到会话不可用
- **WHEN** 运营人员已二次确认单条操作，或最多 20 条的沉底/屏蔽批量操作，但登记 PLUGIN、iframe 或 lease 不可用
- **THEN** Web SHALL 不执行切换、建页或身份绑定
- **THEN** 会话服务 SHALL 创建或复用 `RECOVER` Job，并向页面返回脱敏的“会话正在恢复，请稍后重新确认”结果
- **THEN** 刷新成功后系统不得自动发送原写请求

#### Scenario: 来源会话不可用
- **WHEN** 调用方请求人工操作或来源读取但会话不可用
- **THEN** 调用方 SHALL 不直接访问浏览器，并获得不包含认证信息的可重试结果

### Requirement: 敏感配置与可观测性
系统 SHALL 仅从运行时配置或密钥存储获取会话凭据和第三方密钥，并 SHALL 输出带关联标识的结构化日志，同时脱敏令牌、Cookie、完整请求 URL 和非必要的个人身份标识。

#### Scenario: 记录外部请求失败
- **WHEN** 适配器请求失败
- **THEN** 系统 SHALL 记录任务或关联标识、适配器名称、安全错误类别和结果码，且不得记录凭据或完整的已认证请求 URL

### Requirement: 部署密钥与本机 Web 端口配置
系统 SHALL 要求部署方设置至少 32 个字符的 `DJANGO_SECRET_KEY`；不得使用源码内固定密钥作为部署默认值。系统 SHALL 支持通过 `WEB_PORT` 配置 Supervisor 托管 Web 的端口，但 Web 服务 SHALL 继续只监听 `127.0.0.1`。`.env.example` SHALL 为所有部署级 `RuntimeSettings` 字段提供名称、默认值或空值及必要说明；管理台动态开关 SHALL 与启动期设置区分标注。

#### Scenario: 密钥缺失或长度不足
- **WHEN** 启动期运行配置未包含至少 32 个字符的 `DJANGO_SECRET_KEY`
- **THEN** 配置加载 SHALL 快速失败并给出不包含密钥内容的可处理错误

#### Scenario: 设置有效密钥和 Web 端口
- **WHEN** 部署配置提供有效密钥和合法 `WEB_PORT`
- **THEN** Supervisor SHALL 在 `127.0.0.1:<WEB_PORT>` 启动 Web，并通过同一端口执行健康检查

#### Scenario: 使用示例模板初始化
- **WHEN** 部署方按 `.env.example` 创建 `runtime/.env`
- **THEN** 所有部署级运行设置 SHALL 可被发现，且密钥字段不得包含可直接用于部署的固定值

### Requirement: 当前操作游戏与执行范围

系统 SHALL 维护唯一的当前操作游戏，且该游戏必须来自本地可管理游戏目录。访问游戏圈来源的异步任务在首次实际访问来源前 SHALL 固化该游戏及范围版本；已固化的任务在重试、分页和后续来源调用中 MUST 保持原范围。人工入口不得对当前操作游戏以外的 AppID 创建来源任务。

#### Scenario: 切换当前操作游戏

- **WHEN** 运营人员切换当前操作游戏
- **THEN** 尚未固化范围的任务 SHALL 使用新游戏
- **THEN** 已固化范围的任务 SHALL 保持原游戏，且新游戏的会话建立可独立安排

### Requirement: 会话所有权与浏览器生命周期

Supervisor SHALL 是受控 Chrome 的唯一进程所有者；`game_circle_session` SHALL 是 CDP、认证材料与 service-owned target 的唯一使用者和登记者。部署 SHALL 保留 `127.0.0.1:9222` 仅供本项目受控 Chrome 使用的约束。Supervisor 运行期间，Chrome 健康 SHALL 由该固定回环 CDP 端点可连接且返回的 `browser_generation` 与当前已确认 generation 一致共同证明；启动器 PID 或业务子 PID 仅可用于诊断或正常关闭的辅助终止，不得单独作为 Chrome 退出、接管或替换的依据。target 所有权 SHALL 仅存在于当前 session owner 内存中；手动打开或无法证明归属的页面不得被接管、关闭或作为来源页面。

每次 Supervisor bootstrap 在启动新 Chrome 前 SHALL 先通过 `127.0.0.1:9222` 的 CDP `Browser.close` 请求关闭浏览器并等待端口释放；端口仍被监听时，才终止该监听 PID 并再次等待。此清理不需要推断监听进程归属，且不得处理该固定端口之外的浏览器或端口；CDP 不可用且无法定位监听 PID、无法终止或释放监听端口时，Supervisor MUST NOT 启动新 Chrome 或 session Worker，并 SHALL 返回固定脱敏失败状态。CDP 请求连接在浏览器退出前断开时，系统 SHALL 以端口是否释放判断关闭结果。Supervisor 异常终止时不要求立即关闭 Chrome。

替换 session Worker 前，Supervisor SHALL 终止当前受控 Chrome 并在新的 browser generation 就绪后启动新 Worker。一次配对替换 SHALL 只创建一个候选 Chrome；候选 generation 未就绪时，后续调和只可等待和核验同一候选，不得反复终止、重建 Chrome 或启动新的 session Worker。候选明确退出或 generation 检查连续失败三次后，Supervisor SHALL 停止自动恢复、记录固定脱敏熔断状态，并要求运营人员显式重启；一次成功 generation 确认 SHALL 清零连续失败计数。

#### Scenario: session Worker 重启

- **WHEN** session Worker 因发布、人工重启或异常恢复而被替换
- **THEN** Supervisor SHALL 先终止受控 Chrome 并确认旧 generation 已退出
- **THEN** 新 Worker SHALL 仅在新 generation 中创建自己的 HOME、Frame 和登录等待
- **THEN** 新 Worker MUST NOT 枚举、识别、复用或关闭前任 owner 的 target

#### Scenario: Chrome 启动器 PID 交接但 generation 未变
- **WHEN** 保存的 Chrome 启动器 PID 不再可用，但 `127.0.0.1:9222` 可连接且 CDP `browser_generation` 仍与已确认 generation 一致
- **THEN** Supervisor SHALL 将受控 Chrome 视为健康，不得替换 Chrome 或 session Worker
- **THEN** 它 SHALL 仅更新诊断状态，且不得把 PID 变化记录为浏览器异常退出

#### Scenario: 受控 Chrome 意外退出

- **WHEN** Supervisor 无法确认当前 `127.0.0.1:9222` 的 CDP generation，且游戏圈会话服务仍为 desired
- **THEN** Supervisor SHALL 将其作为会话 owner 更换，启动新 Chrome generation 后替换游戏圈会话服务
- **THEN** 新会话服务 SHALL 为当前操作游戏重新执行 Bootstrap；不得继续投影旧 generation 的 lease 为可用

#### Scenario: Supervisor 异常退出后重新启动
- **WHEN** Supervisor 异常退出且遗留 Chrome 仍监听 `127.0.0.1:9222`，随后新的 Supervisor 完成独占锁获取
- **THEN** 新 Supervisor SHALL 在启动任何新 Chrome 或 session Worker 前先请求 CDP `Browser.close`，端口仍被监听时才终止监听 PID，并等待端口释放
- **THEN** 它 SHALL 创建新 generation 和新 session owner，不得复用遗留 Chrome、其 target 或其来源会话状态

#### Scenario: Browser.close 连接在关闭期间断开
- **WHEN** Supervisor 向 `127.0.0.1:9222` 发送 CDP `Browser.close` 后连接在响应前断开
- **THEN** 它 SHALL 在受限时间内检查该端口是否已释放，释放时 SHALL 将关闭视为成功
- **THEN** 端口仍被监听时才可进入监听 PID 终止兜底，不得立即把连接断开报告为 Chrome 清理失败

#### Scenario: 会话服务重启请求

- **WHEN** 运营人员请求重启游戏圈会话服务
- **THEN** Supervisor SHALL 立即返回已提交状态，并在后台完成 Chrome generation 与 session owner 的替换
- **THEN** 控制客户端 MUST NOT 因该正常替换耗时而把已接受的请求报告为控制通道失败

#### Scenario: Chrome replacement 未在限定时间就绪

- **WHEN** Supervisor 在替换 session owner 时未能确认候选 Chrome generation
- **THEN** Supervisor SHALL 保持控制面和其他服务运行，并记录固定脱敏失败状态
- **THEN** 后续 reconcile SHALL 只重试同一候选 Chrome 的依赖恢复；除非该候选已确认退出，不得重复终止并重开浏览器或启动新的 session Worker
- **THEN** 系统不得启动新的 session Worker 或退出 Supervisor 主循环

#### Scenario: 连续 Chrome 恢复失败熔断
- **WHEN** 候选 Chrome generation 的就绪检查或候选进程退出连续失败三次
- **THEN** Supervisor SHALL 将该配对恢复置为熔断状态，停止自动启动 Chrome 和 session Worker，并报告固定脱敏原因
- **THEN** 运营人员显式重启 Chrome 或会话服务后 SHALL 开始一个新恢复周期；任一成功 generation 确认 SHALL 清零失败计数

### Requirement: SessionBootstrap 建立主线

系统 SHALL 由 SessionBootstrap 在服务启动、人工检查登录态、人工建立当前游戏 Frame 和当前游戏切换时建立会话。除明确的控制意图外，来源调用不得隐式创建恢复 Job、target 或游戏切换。运行态 SHALL 分别投影外层会话和当前游戏 Frame；只有当前 scope 存在经最小只读验证发布的 ACTIVE lease 时，Frame 才可投影为有效。外层有效不得暗示来源操作可执行，运行态仅可包含固定脱敏类别，服务停止或 generation 变更后不得继续投影旧 lease 为有效。

#### Scenario: 外层会话和当前游戏可用

- **WHEN** Chrome 可用、外层会话有效且存在当前操作游戏
- **THEN** Bootstrap SHALL 建立或复用当前 owner HOME 与 Frame，完成身份绑定和最小只读验证
- **THEN** 系统 SHALL 原子发布当前 scope ACTIVE lease，并分别投影外层会话和 Frame 为有效

#### Scenario: 切换游戏后的 HOME 回读

- **WHEN** Bootstrap 在同一 HOME 中切换当前游戏并安排页面 reload
- **THEN** 系统 SHALL 丢弃 reload 前 document 的执行上下文，并在同一 HOME 建立新上下文后回读 AppID 与 outer token
- **THEN** 系统不得因复用失效上下文而关闭候选 HOME、重试创建额外 HOME 或跳过目标 Frame 建立

#### Scenario: 没有当前操作游戏

- **WHEN** 外层会话有效但不存在 `ActiveGameScope`
- **THEN** 系统 SHALL 投影外层有效和 Frame 未就绪
- **THEN** 系统 MUST NOT 发布半成品 lease 或自行选择游戏

### Requirement: 人工登录页与脱敏边界

系统 SHALL 最多维护一个当前 owner 的受控会话页；该页同时承担人工登录等待与外层会话观察。仅运营人员明确请求“查看受控会话页”时系统 MAY 创建或激活该页；重复请求 SHALL 复用页面且不得创建或聚焦第二页。系统不得持久化或输出 target ID、认证材料、完整 URL、二维码、账号身份或原始来源响应。手动外层检查、outer token 读取和 Frame 建立 SHALL 复用该受控会话页，不得另建不可观察 HOME。

会话服务 SHALL 为同一 service-owned HOME 产生唯一的外层观察分类，至少区分 `VERIFIED`、`NO_USABLE_TOKEN`、`TRANSIENT_UNREADY` 与 `AUTH_EXPIRED`。固定入口 URL、页面 pathname 和扫码正文文案不得单独决定分类。当前 owner HOME 已稳定、分类为 `NO_USABLE_TOKEN` 且当前 scope 不存在 ACTIVE lease 时，系统 SHALL 登记人工登录等待并投影 `LOGIN_PENDING`；`https://mp.weixin.qq.com/` 与 `https://mp.weixin.qq.com/cgi-bin/loginpage` 均可处于该状态。Bootstrap、低频外层探测和人工打开受控会话页 SHALL 复用此分类；低频外层探测遇到 `TRANSIENT_UNREADY` SHALL 保持既有状态并在后续维护周期重试，直接建立或显式检查 SHALL 投影外层会话未就绪，且两者均不得被投影为需要扫码。

“查看受控会话页” SHALL 在会话服务的单一 control lane 内打开或复用该页并执行共享观察；Web 不得紧接以第二个控制调用触发独立检查。当前 scope 存在 ACTIVE lease 时，该操作 SHALL 只激活其 HOME，不得登记人工登录等待、改变 ACTIVE lease HOME 的 target role、撤销 lease 或将 Frame 投影为未就绪。不存在 ACTIVE lease 且观察为 `NO_USABLE_TOKEN` 时，系统 SHALL 登记人工登录等待。登记 target 在等待期间 SHALL 被保留，除 target 丢失或 Chrome generation 更换外不得被候选清理、重复打开或替换。

登录等待观察 SHALL 先检查同一 target 是否出现可用外层 token。token 缺失时系统 SHALL 保持 `LOGIN_PENDING`，不得调用 `getappinfo`、关闭该 target 或创建另一张 HOME。token 首次出现后，系统 SHALL 保持 `LOGIN_PENDING` 至少十秒，再在同一 target 调用 `getappinfo`。只有 `getappinfo` 返回成功且包含有效 AppID 时，系统才可结束等待、复用该 HOME 建立当前游戏 Frame，并在既有最小只读验证成功后发布 ACTIVE lease。页面 pathname 的变化、页面渲染中的短暂异常或非终态验证未就绪不得单独结束等待。

登录等待仅可因成功发布、等待窗口到期、已登记 target 丢失、Chrome generation 改变或 `getappinfo` 明确返回已知认证失效而结束。target 丢失或 generation 改变时，系统 SHALL 清除等待并投影 `LOGIN_REQUIRED`，且不得自动创建替代登录页；超时或明确认证失效时，系统 SHALL 清除等待并投影 `LOGIN_REQUIRED`，但 SHALL 保留当前 owner 的唯一可观察 HOME 供后续人工操作复用。CDP 短暂不可用、页面尚未稳定或非终态契约异常 SHALL 保持等待并在既有低频维护周期重新观察。

启用自动修复时，系统 SHALL 在当前 scope 不存在 ACTIVE lease、运行态为 `OUTER_READY` 且不存在 `LOGIN_PENDING` 时，尝试以既有 `REPAIR_BROKEN_FRAME` Bootstrap 补建当前游戏 Frame。该 Bootstrap SHALL 再次验证外层会话；只有验证成功和既有最小只读验证成功时才可发布 lease。`LOGIN_PENDING`、`LOGIN_REQUIRED`、`UNKNOWN` 或 Chrome 不可用状态不得触发候选建立。零 lease 的失败补建 SHALL 至少间隔十五秒后才重试；该退避不得影响已有 ACTIVE lease 的 target 损坏检测。

系统 SHALL 将最近外层观察时间与最近成功外层验证时间分开投影。只有读取 HOME 的 token、确认无 token、确认认证失效、外层过渡结果或成功 `getappinfo` 才可刷新最近外层观察时间；仅检查已登记 HOME/PLUGIN target 是否存在、Frame 建立中或 lease 生命周期变更不得刷新该时间或成功验证时间。`LOGIN_PENDING` 存在时，任何非内部登录观察的 Bootstrap SHALL 不执行且不得改写该等待投影。

来源身份被上游拒绝时，系统 SHALL 退役该被拒绝 lease；只有 `VERIFIED` 外层观察才可将运行态刷新为外层有效。`TRANSIENT_UNREADY` 观察 SHALL 保留既有外层状态和观察时间，不得作为新的外层验证或将登录状态推断为有效。

固定会话控制动作 SHALL 根据 Bootstrap 结果和当前脱敏运行态返回真实控制回执。若动作因 `LOGIN_PENDING` 的互斥保护未执行，回执 SHALL 为 `LOGIN_PENDING`；若动作未完成且不处于登录等待，回执 SHALL 为 `CONTROL_NOT_COMPLETED`。系统仅在实际完成检查或 Frame 建立时才可返回相应完成回执。

#### Scenario: 根路径入口等待扫码

- **WHEN** 运营人员通过 `https://mp.weixin.qq.com/` 打开受控会话页，且该 service-owned target 尚未提供可验证的外层 token
- **THEN** 系统 SHALL 将该 target 登记为 `LOGIN_PENDING` 并保留它供运营人员扫码
- **THEN** 系统不得要求 pathname 为 `/cgi-bin/loginpage`、读取扫码正文文案、关闭该 target 或创建第二张 HOME

#### Scenario: 启动 Bootstrap 发现根路径无 token

- **WHEN** 服务启动、低频外层探测或人工入口复用的当前 owner HOME 已稳定，且观察结果为 `NO_USABLE_TOKEN`、当前 scope 不存在 ACTIVE lease
- **THEN** 系统 SHALL 登记 `LOGIN_PENDING` 并提供查看受控会话页的恢复入口
- **THEN** 系统不得将该状态投影为 `UNKNOWN`，也不得因该 HOME 来自 Bootstrap 而要求固定 loginpage 路径或扫码正文文案

#### Scenario: loginpage 入口等待扫码

- **WHEN** 运营人员通过 `https://mp.weixin.qq.com/cgi-bin/loginpage` 打开受控会话页，且该 target 尚未提供可验证的外层 token
- **THEN** 系统 SHALL 将该 target 登记为 `LOGIN_PENDING` 并保留它供运营人员扫码
- **THEN** 重复查看受控会话页 SHALL 只激活已登记 target，不得创建或聚焦第二页

#### Scenario: 扫码完成

- **WHEN** 已登记登录等待 target 首次获得外层 token
- **THEN** 系统 SHALL 保持 `LOGIN_PENDING` 至少十秒后才在同一页面执行 `getappinfo` 验证
- **THEN** 只有验证成功且 AppID 有效时，系统 SHALL 复用该 HOME 建立当前游戏 Frame，并且只有最小只读验证成功后才可发布 ACTIVE lease

#### Scenario: 登录等待结束

- **WHEN** 登录窗口到期、已登记 target 丢失、Chrome generation 改变或 `getappinfo` 明确返回认证失效
- **THEN** 系统 SHALL 清除等待状态并投影 `LOGIN_REQUIRED`
- **THEN** 系统 SHALL 保留当前 owner 的唯一可观察 HOME 供后续人工操作复用，且 MUST NOT 自动创建下一张登录页或接管未知 tab

#### Scenario: 已登录运营人员查看受控会话页

- **WHEN** 当前 scope 存在 ACTIVE lease，运营人员请求查看受控会话页
- **THEN** 系统 SHALL 只激活该 lease 的 HOME，并保持既有外层与 Frame 投影
- **THEN** 系统不得将该 HOME 登记为 `LOGIN_PENDING`、改变其 target role、撤销 lease 或在登录等待超时路径关闭它

#### Scenario: 已验证外层会话但没有当前游戏 Frame

- **WHEN** 启用自动修复，当前 scope 的运行态为 `OUTER_READY`、不存在 `LOGIN_PENDING`，且没有 ACTIVE lease
- **THEN** 系统 SHALL 通过 `REPAIR_BROKEN_FRAME` Bootstrap 再次验证外层会话并尝试建立当前游戏 Frame
- **THEN** 验证失败不得发布 lease；非登录等待的失败重试间隔 SHALL 不少于十五秒

#### Scenario: 扫码等待期间没有当前游戏 Frame

- **WHEN** 当前 scope 没有 ACTIVE lease，但系统已登记 `LOGIN_PENDING`
- **THEN** 自动修复 SHALL 保持人工登录等待，不得建立候选 HOME 或 PLUGIN

#### Scenario: 扫码等待期间提交其他会话动作

- **WHEN** 系统已登记 `LOGIN_PENDING`，运营人员或后台任务请求检查外层、建立 Frame 或保活
- **THEN** 系统 SHALL 保持原登录等待，不得执行 Bootstrap、切换游戏、创建候选或改写运行态
- **THEN** 对应固定控制回执 SHALL 为 `LOGIN_PENDING`，不得伪称检查或 Frame 建立已完成

#### Scenario: 健康 Frame 的 target 检查

- **WHEN** 自动修复仅确认当前 ACTIVE lease 的 HOME 和 PLUGIN target 仍存在
- **THEN** 系统不得刷新外层观察时间或成功验证时间

#### Scenario: 运营人员观察会话请求

- **WHEN** 运营人员在已登记受控会话页上提交外层检查
- **THEN** 系统 SHALL 复用该同一页面；若其处于 `LOGIN_PENDING`，系统 SHALL 保持等待而不得清理或另建 HOME
- **THEN** 登录完成后的 `getappinfo` SHALL 在该同一页面执行，系统不得将另一张后台 HOME 的结果作为该页面的会话结论

### Requirement: 来源调用与 lease 连续性

受权的来源读取或已确认人工写入 SHALL 固定 ACTIVE lease 后执行。来源 IPC MUST NOT 创建、reload、绑定或关闭 target。新 lease SHALL 在完整验证后原子替换旧 lease；旧 lease 仅在退役延迟到达且访问计数为零后关闭。`SessionBootstrap` SHALL 返回不含认证材料或 target 标识的发布结果，只有发布结果可以使 Keepalive Job 成功；会话服务的维护循环 SHALL 独立回收可关闭的退役 lease，不得等待后续来源请求。

会话服务 SHALL 通过单一 control lane 串行化 Bootstrap、登录等待检查、Repair、Keepalive 和人工控制的 target 生命周期操作。来源请求 SHALL 使用已固定 lease 或返回脱敏未就绪结果，访问计数在成功、异常、超时和取消后均须释放。自动 target SHALL 请求后台创建能力；后台创建失败 MUST NOT 退化为前台创建，且自动状态变化 MUST NOT 聚焦 target、弹出对话框、发送通知、跳转页面或写入全局页面消息。

#### Scenario: 来源会话未就绪

- **WHEN** 当前 scope 不存在 ACTIVE lease
- **THEN** 来源读取 SHALL 返回固定脱敏未就绪结果
- **THEN** 已确认人工写入 SHALL 保持待确认，恢复后 MUST NOT 自动重放

#### Scenario: 成功保活后无来源请求

- **WHEN** 新 lease 已发布、旧 lease 已超过退役延迟且没有活动访问
- **THEN** 会话服务 SHALL 在维护循环中关闭旧 lease 的 HOME 与 Frame
- **THEN** 不得等待新的来源调用才执行回收

#### Scenario: 控制操作与来源调用并发

- **WHEN** 控制操作和来源 IPC 并发到达
- **THEN** 同一时刻 SHALL 只有一个控制操作改变或检查 target 生命周期
- **THEN** 来源请求 SHALL 使用已固定 lease 或返回脱敏未就绪结果，访问计数在成功、异常、超时和取消后均须释放

### Requirement: 可选会话维护

系统 SHALL 提供默认关闭的 SessionRepair 和 SessionKeepalive。Repair 启用时 SHALL 在服务内部只检查当前 owner HOME 和 Frame，并将同一 AppID 的失效合并为一次有界 Bootstrap；它不得使用 Scheduler 或 BackgroundJob，不得自动打开登录页。Keepalive 启用时 Scheduler SHALL 为每个四小时桶创建或复用带 `trigger=schedule` 和 `automation_key=session_keepalive` 的 Job；入队 Job MUST NOT 保存 execution scope 或认证材料。

#### Scenario: Repair 被关闭

- **WHEN** service-owned Frame 失效且 Repair 未启用
- **THEN** 系统 SHALL 投影 Frame 未就绪
- **THEN** 系统 MUST NOT 创建 target 或入队会话维护 Job

#### Scenario: Repair 发现当前 Frame 丢失

- **WHEN** Repair 已启用且当前 scope ACTIVE lease 的 HOME 或 Frame target 不再存在
- **THEN** 系统 SHALL 建立并验证候选 lease，成功后才原子替换损坏 lease
- **THEN** 系统 MUST NOT 因损坏 lease 仍为 ACTIVE 而跳过 Frame 建立，亦不得在重复检查中积累未发布 HOME target
- **THEN** 浏览器已移除的当前 owner target SHALL 在退役回收时从登记中清除

#### Scenario: Repair 候选失败且旧 lease 已损坏

- **WHEN** Repair 已确认旧 lease 的 HOME 或 Frame target 丢失，且候选建立或验证失败
- **THEN** 系统 SHALL 投影当前 Frame 未就绪并提供人工建立动作
- **THEN** 系统 MUST NOT 因该旧 lease 仍在内存中而投影 Frame 有效

#### Scenario: 首次执行冻结 scope

- **WHEN** 消费者首次认领自动保活 Job
- **THEN** 消费者 SHALL 在任何浏览器操作前原子读取当前 `ActiveGameScope` 并写入 checkpoint
- **THEN** 重试和中断恢复 SHALL 使用该 checkpoint scope，idempotency key 不得包含入队时 AppID 或 scope version

#### Scenario: 没有 current scope

- **WHEN** 首次执行时不存在 `ActiveGameScope`
- **THEN** Job SHALL 以 `NO_ACTIVE_GAME_SCOPE` 终态结束
- **THEN** 系统 MUST NOT 调用 CDP、创建 target、安排恢复或重试该 Job

#### Scenario: scope 冻结后切换游戏

- **WHEN** 保活 Job 已冻结 scope，运营人员随后切换当前操作游戏
- **THEN** Job SHALL 完整执行并发布冻结 scope 的 lease，不得被改写或取消
- **THEN** 系统 SHALL 为新当前 scope 安排独立 Bootstrap，且页面不得把旧 scope 结果投影为当前 Frame 有效

#### Scenario: 禁用自动保活

- **WHEN** 运营人员关闭 SessionKeepalive
- **THEN** Scheduler SHALL 停止接受新的周期 key
- **THEN** 已入队或运行中的 Job SHALL 按既有队列语义完成，手动保活 SHALL 保持可用

#### Scenario: 保活候选失败

- **WHEN** 保活候选在中转、回切目标、Frame 稳定检查、身份绑定或最小读取任一步失败
- **THEN** 系统 SHALL 关闭未发布的候选 HOME 和 Frame，且不得保留中转游戏页面作为可复用 target
- **THEN** Job MUST NOT 标记成功；瞬态失败 SHALL 使用有限重试，需人工扫码或目标不可选 SHALL 以固定终态结束
- **THEN** 若旧 ACTIVE lease 仍适用于当前 scope，系统 SHALL 保留其可用性和 Frame ready 投影

### Requirement: 废弃会话 Job 的发布兼容

移除旧 Probe 与 Refresh consumer 的发布 SHALL 提供幂等发布工具。该工具仅在旧会话 Worker 已退出且任务锁释放后，将活动旧 Job 标记为固定发布终态；它不得物理删除 Job、参数、checkpoint 或审计记录。兼容识别 SHALL 保留本发布及后续一个发布版本。

#### Scenario: 发布时存在旧会话 Job

- **WHEN** 旧 Probe 或 Refresh Job 仍处于活动队列
- **THEN** 发布工具 SHALL 保留其审计数据并使新 Worker 不再认领
- **THEN** 重复执行发布工具 MUST NOT 重复改写已终态的记录

### Requirement: 已废弃任务隔离与当前控制台动作

旧 `GAME_CIRCLE_LOGIN_PROBE` 与 `GAME_CIRCLE_SESSION_REFRESH` SHALL 仅在发布终态化工具的兼容识别中出现。运行时任务类型表、Scheduler、Worker consumer、Web 路由、工作台文案和普通测试 SHALL NOT 生产、消费或展示这两个任务为可执行能力。

运行控制台的页面入口与去重规则 SHALL 遵守 `runtime-console-orchestration`。会话语义保持固定：切换游戏后请求建立目标 Frame；检查外层会话不建立 Frame；手动保活只创建 `GAME_CIRCLE_SESSION_KEEPALIVE`。目录同步 SHALL 以经 CSRF 保护的 POST 通过游戏圈会话 IPC 更新本地可管理游戏目录；失败 SHALL 保留既有目录并显示固定失败结果。

#### Scenario: 部分重启不会创建已废弃任务

- **WHEN** Web、Scheduler 或 session Worker 已加载新版代码
- **THEN** 普通控制和调度路径不得创建 `GAME_CIRCLE_LOGIN_PROBE` 或 `GAME_CIRCLE_SESSION_REFRESH`
- **THEN** 仅发布终态化工具可以匹配这些历史记录

#### Scenario: 运营人员同步授权游戏目录

- **WHEN** 已授权运营人员提交控制台目录同步操作且外层会话可用
- **THEN** Web SHALL 请求固定 `LIST_MANAGED_GAMES` IPC 意图，并在同一请求内更新本地目录
- **THEN** 响应 SHALL 报告同步数量，且不得要求第二次 Admin 操作

### Requirement: 外层登录态验证与 Frame lease 边界

系统 SHALL 仅在当前 owner 的受控会话页于同一次稳定验证中满足以下全部条件时投影外层会话有效：页面不是固定扫码页、验证前后 URL token 均可读取、`getappinfo` 返回 HTTP 200、`errcode=0` 且带非空 AppID。URL token、页面可打开、target 存在或一次孤立 HTTP 成功均不得单独证明登录有效。Frame 建立 SHALL 在调用 `binduseridentity` 前再次从同一受控会话页读取 outer token；该读取失败、token 消失或页面变为扫码页时，系统 SHALL 投影外层会话未就绪和 Frame 未就绪，且不得保留 `OUTER_READY`。已验证的登录态过期 SHALL 投影为需要重新登录和 Frame 未就绪，并使同一 browser generation 的 ACTIVE lease 失去新 access 资格；未知或暂时错误不得误报扫码或废弃 lease。

正常 outer token 刷新不得废弃已发布 Frame lease；新 lease 发布后，旧 lease 仅按访问计数和退役延迟关闭。外层仍有效时的来源身份拒绝或 Frame target 丢失 SHALL 只废弃对应 lease，不得反推外层登录态过期。

#### Scenario: URL token 尚在但登录态过期

- **WHEN** HOME URL 仍含 token，但 `getappinfo` 返回已验证认证拒绝
- **THEN** 系统 SHALL 显示需要重新登录和当前 Frame 未就绪，并只提供人工打开登录页动作
- **THEN** 同 generation 的新来源 access 不得使用旧 token 请求上游

#### Scenario: 保活刷新 outer token

- **WHEN** Keepalive 获得新的 outer token，且 `getappinfo` 持续成功
- **THEN** 系统 MUST NOT 因 token 值变化拒绝旧 Frame lease
- **THEN** 旧 lease SHALL 仅按既有退役规则关闭

#### Scenario: 验证后会话页跳回扫码页
- **WHEN** `getappinfo` 返回成功后，同一受控会话页的 token 消失、路由变为扫码页或 Frame 前 token 复读失败
- **THEN** 系统 SHALL 覆盖此前成功投影为外层会话未就绪或需要登录，并将 Frame 投影为未就绪
- **THEN** 系统 MUST NOT 调用 `binduseridentity`、发布 lease 或执行来源帖子查询

#### Scenario: Frame 最小读取验证
- **WHEN** 同一受控会话页完成稳定外层验证且存在当前操作游戏
- **THEN** 系统 SHALL 使用仅存在于会话服务内存中的 outer token 建立插件 Frame、绑定来源身份并完成最小只读帖子查询
- **THEN** 只有该查询成功后才可将当前 scope 的 Frame 投影为有效

### Requirement: 外层会话探测与来源认证反馈

存在 ACTIVE lease 时，游戏圈会话服务 SHALL 每五分钟复用 service-owned HOME 执行一次只读外层验证；手动检查复用同一验证。SessionRepair 的短间隔循环 SHALL 只检查 target 完整性，不得轮询平台接口。来源客户端 SHALL 区分明确来源身份拒绝、认证嫌疑和普通来源错误；认证嫌疑必须合并为一次 HOME 外层复核。

来源 IPC 在登录态已确认过期、Frame 已确认失效或不存在可用 lease 时 SHALL 返回固定 `SESSION_UNAVAILABLE`，且不得发起上游请求。Scheduler 仍只按规则入队，入队成功不得表示来源读取成功。

#### Scenario: 并发认证嫌疑

- **WHEN** 多个来源请求在同一冷却窗口报告认证嫌疑
- **THEN** 服务 SHALL 最多执行一次外层复核
- **THEN** 外层有效时仅处理对应 Frame；外层过期时废弃该 generation 的全部 lease

### Requirement: 唯一可观察受控会话页
当前 session owner SHALL 只维护一个 service-owned 受控会话页，作为外层检查、人工扫码、outer token 读取、游戏切换和 Frame 建立的唯一 HOME。该页可以由明确人工动作激活，但 target ID、完整 URL、认证材料和二维码内容 MUST NOT 输出或持久化。系统不得接管、关闭或以 URL、标题推断任意人工 target 为受控会话页。

#### Scenario: 运营人员查看受控会话页
- **WHEN** 已认证运营人员在控制台请求查看受控会话页
- **THEN** 会话服务 SHALL 激活当前 owner 已登记的唯一页面，或创建并登记一张新页面后激活
- **THEN** 运营人员可在该页面观察登录效果和 Network，重复请求不得创建第二张页面

#### Scenario: 自动检查不抢占焦点
- **WHEN** 自动维护或人工外层检查在已有受控会话页上执行
- **THEN** 系统 SHALL 复用该页面且不得激活、跳转或关闭它
- **THEN** 检查结果 SHALL 与运营人员打开该同一页面时可观察到的页面状态一致

### Requirement: 当前 owner 的 HOME 登记回收
会话服务 SHALL 在创建 HOME 前清理当前 owner 已登记但浏览器中不存在的 target。存在多个当前 owner 空闲 HOME 时，服务 SHALL 只保留一个仍存在且未被 lease 或登录等待占用的受控会话页，并关闭其余可证明归属的空闲 target。

#### Scenario: 重复检查后的 HOME 复用
- **WHEN** 运营人员连续请求外层检查且唯一受控会话页仍存在
- **THEN** 每次检查 SHALL 使用同一页面
- **THEN** 浏览器中不得因重复检查累积新的 service-owned 空闲 HOME

#### Scenario: 受控会话页被关闭
- **WHEN** 运营人员或浏览器关闭当前受控会话页
- **THEN** 服务 SHALL 清除当前 owner 对该页的登记并投影会话未就绪
- **THEN** 服务不得把任何未知人工页面作为替代页面

### Requirement: 受控会话 target 动作的脱敏可观察性

游戏圈会话服务 SHALL 为 service-owned HOME 与 Plugin/Frame target 的创建、复用、关闭、缺失发现和人工激活发布有界的脱敏动作摘要。每项摘要仅包含时间、目标类别（HOME 或 PLUGIN）、动作类别（创建、复用、关闭、缺失或激活）、固定触发意图（Bootstrap、Repair、Keepalive、人工打开或回收）和结果类别；不得包含 target ID、AppID、URL、页面标题、outer/source token、二维码、Cookie、capability、CDP 请求或页面正文。

该摘要 MUST 与 Supervisor 的 Chrome 进程生命周期事件独立保存和展示。会话服务的两秒维护循环、登录等待、Repair、Keepalive、target 所有权、退役回收和页面焦点语义不得因诊断摘要而改变；诊断不得新增 CDP 调用、创建/激活/关闭额外 target 或触发上游读取/写入。摘要缓冲达到上限时 SHALL 淘汰最旧动作，且不得影响会话可用性。

#### Scenario: Repair 创建会话页面不被误报为 Chrome 重启
- **WHEN** 会话服务以既有 Repair 路径创建或关闭 service-owned HOME 或 Plugin target
- **THEN** 只读追溯 SHALL 记录对应 target 动作、固定触发意图和结果类别
- **THEN** 控制台 SHALL 将其与 Chrome 进程替换分区展示，不得标记为受控 Chrome 重启

#### Scenario: 自动维护不因诊断增加页面操作
- **WHEN** 两秒维护循环执行现有登录观察、Repair、外层探测或退役回收
- **THEN** 动作摘要 SHALL 只描述既有实际 target 操作
- **THEN** 诊断本身不得创建、激活、关闭 target，也不得调用上游接口

### Requirement: 系统配置与追溯台导航和视觉
系统 SHALL 将 `/admin/` 呈现为与内容管理工作台一致的“系统配置与追溯台”，使用中文业务术语、明确的配置/运行追溯分区和统一的视觉层级。它 SHALL 遵守 `operations-visual-system` 的共享主题、导航和无副作用边界；顶部 SHALL 提供内容管理、运营分析和系统配置的同源导航，当前页面 SHALL 明确标识为系统配置。首页 SHALL 在 Hero 下方、核心配置之前提供全宽“初始化与运行状态”Pipeline。

#### Scenario: 运营人员访问系统配置首页
- **WHEN** 已登录运营人员访问 `/admin/`
- **THEN** 系统 SHALL 先展示初始化与运行状态 Pipeline，再展示配置入口与只读追溯入口的分区说明，并提供内容管理和运营分析的导航链接，而不得仅呈现 Django 默认应用索引

### Requirement: 运行数据只读追溯
系统 SHALL 将候选帖子、原创帖子、帖子媒体、互动快照、OCR 提取、AI 评估、风险证据、人工审核任务、回复草案、上游操作审计和后台任务作为只读追溯数据。系统 SHALL 保留这些数据的列表、搜索、筛选、详情和关联跳转，但 MUST 在服务端拒绝新增、修改、删除及默认批量删除操作。

#### Scenario: 查看原创帖的追溯详情
- **WHEN** 运营人员从系统配置台打开原创帖或其关联审核数据
- **THEN** 系统 SHALL 展示已采集字段和关联记录，并不得显示或接受保存、删除、内联新增或批量删除操作

#### Scenario: 访问审核任务
- **WHEN** 运营人员从系统配置台打开人工审核任务
- **THEN** 系统 SHALL 仅展示审核、AI 和审计追溯信息，并提供前往内容管理工作台的帖子链接，而不得提供沉底、屏蔽、禁言、恢复或其他上游处置入口

### Requirement: 受控配置编辑与游戏目录同步
系统 SHALL 仅允许审核策略版本和策略规则作为可编辑的 Django 配置模型；Pipeline 另可编辑白名单 AI/OCR 运行配置和人工处置授权。可管理游戏目录 SHALL 不允许手工新增、修改或删除；系统 SHALL 仅允许已登录运营人员通过“从受控微信同步可管理游戏”动作更新目录，并在 CDP 登录态无效或同步失败时保留现有目录。

#### Scenario: 编辑审核策略
- **WHEN** 运营人员访问审核策略版本或规则
- **THEN** 系统 SHALL 提供标准的新增、修改和删除配置操作，并清晰展示启用状态、版本、优先级、风险编码及其中文释义等配置字段

#### Scenario: 同步可管理游戏
- **WHEN** 运营人员在游戏目录页面触发受控同步
- **THEN** 系统 SHALL 仅通过受控 Chrome CDP 同步可管理游戏，并在失败时显示错误且不得清空本地目录

### Requirement: 默认策略与可理解的生效规则
系统 SHALL 在 Supervisor 启动预检中幂等初始化默认审核策略，用于创建可编辑的通用内容审核策略及其基线规则。初始化 MUST 不覆盖已有规则，且 MUST 仅在没有其他启用策略时启用该默认版本。`ensure_default_policy` 管理命令仅用于诊断或恢复。默认策略及所有策略规则 SHALL 仅影响 AI 建议与审核优先级，除非运营人员在单条规则中显式开启自动执行许可，且同时完成独立 AI 自动处置授权；新增或迁移后的规则自动执行许可 MUST 默认为关闭。

系统 SHALL 在配置首页为唯一启用策略提供可折叠的规则摘要；展开后 SHALL 以无需横向滚动的响应式布局展示每条已启用规则的中文风险类型、最低置信度、建议动作、优先级和 AI 自动执行许可状态。

#### Scenario: 初始化并查看默认策略
- **WHEN** 新部署启动 Supervisor 且不存在启用策略
- **THEN** Supervisor SHALL 创建并启用通用内容审核策略及其基线规则，所有规则的自动执行许可 SHALL 保持关闭，且运营人员访问配置首页时 SHALL 看到规则数量和可展开的规则摘要

#### Scenario: 已存在运营策略时初始化默认策略
- **WHEN** 已存在其他启用策略时 Supervisor 执行默认策略预检
- **THEN** 系统 SHALL 保留该启用策略，不得启用默认策略，也不得覆盖默认策略中已有的规则或其自动执行许可

### Requirement: 运行控制台承载与受控运行配置
系统配置首页 SHALL 承载并遵守 `runtime-console-orchestration` 的运行快照、流程、服务管理和唯一入口契约，不得在本规格重复定义节点顺序、就绪等级或服务生命周期。它 SHALL 允许已认证运营人员编辑白名单 AI/OCR 配置、人工上游写入开关和默认关闭的 AI 自动处置总开关及沉底/屏蔽动作开关。已有密钥 SHALL 仅显示是否已配置；空白密钥输入 MUST 保留原值，且系统不得回显、记录或返回密钥。

#### Scenario: 保存 AI 配置并重启审核 Worker
- **WHEN** 运营人员保存完整的 AI 配置并确认重启受影响服务
- **THEN** 系统 SHALL 将白名单字段写入运行时配置、不得回显密钥，并请求 Supervisor 重启 AI Worker

#### Scenario: 保存 AI 自动处置授权
- **WHEN** 运营人员保存 AI 自动处置总开关或沉底/屏蔽动作开关
- **THEN** 系统 SHALL 仅写入白名单配置键、不得自动启用任何策略规则或人工写入开关，并提示按运行控制台的既有入口重启受影响服务

#### Scenario: 运营人员创建历史补采
- **WHEN** 已登录运营人员在帖子采集生产者的“立即执行”弹窗提交有效小时数
- **THEN** 系统 SHALL 使用提交时当前操作游戏创建或复用页大小为 100 的 `MANUAL_INGEST_WINDOW`，显示任务编号及创建结果，并由人工补采 Worker 异步处理，不得在页面请求中连接 CDP 或接受 AppID

### Requirement: 自动任务生产者的受控配置与追溯
系统 SHALL 按生产者聚合展示代码已注册自动生产者及其规则实例的脱敏运行状态。首页 SHALL 以卡片展示启用状态、规则数、最近自动入队、下次触发和最近检查；已认证运营人员 SHALL 在规则管理弹窗内从定义允许的模板新增、调整时点、启用、暂停或删除规则。规则管理 SHALL 始终按竖向单列展示规则卡片；卡片首行显示模板业务名称与启用状态，不得以“每天 07:30”“每周二 10:00”等反推时点充当规则名称。每张卡片 SHALL 以统一字段展示任务类型、执行频率（每天、每周及固定执行日，或每 X 分钟）和执行计划；Cron 规则仅填写 `HH:MM`，间隔规则仅填写分钟数，服务端反推并保存表达式。周指标的执行日由受限模板固定，不得由页面修改；页面不得展示或编辑规则名称、Cron、错过触发、重叠策略或时区。

规则管理 MUST 与 Supervisor 服务控制分离：启用或暂停生产者不得启动、停止或重启 `scheduler`；保存规则配置不得直接创建循环 Job、调用 CDP 或调用上游。密钥、Cookie、token、来源响应、完整 Job 参数及 AppID 不得出现在配置表单、运行状态或错误信息中。生产者卡的人工运行入口及其反馈遵守本规格后续“自动任务生产者的手动运行界面”要求。

#### Scenario: 查看生产者与规则的未来触发时间
- **WHEN** 运营人员打开某个生产者的规则管理弹窗
- **THEN** 每张已有规则卡片 SHALL 展示与实际 Scheduler 相同计算规则得出的下一次触发时间
- **THEN** 生产者摘要卡 SHALL 汇总其所有启用规则并按触发时间升序展示未来三次触发时间及所属任务类型
- **THEN** 两类展示 SHALL 仅用于观察，不得创建 Job、推进逻辑槽或改变规则状态

#### Scenario: 暂停日指标生产者
- **WHEN** 已认证运营人员在系统配置台暂停日指标生产者
- **THEN** 系统 SHALL 保存受限配置并在下一次 Scheduler 评估时停止其未来自动入队
- **THEN** 页面 SHALL 显示已暂停状态和最近一次运行追溯，且不得停止 Scheduler 或指标 Worker

#### Scenario: 查看生产者失败
- **WHEN** 某生产者规则因无当前游戏、无效配置或入队失败而未完成
- **THEN** 页面 SHALL 展示该规则的固定脱敏错误类别、最近检查时间和可操作的下一步
- **THEN** 页面不得显示来源响应、认证材料、内部异常堆栈或完整 Job 参数

#### Scenario: 保存无效调度配置
- **WHEN** 运营人员提交空时点、无效 `HH:MM`、子分钟间隔、超出模板边界或非中国标准时区
- **THEN** 系统 SHALL 拒绝保存并明确说明受限校验失败
- **THEN** 系统不得改变现有生效配置或产生自动 Job

### Requirement: 自动任务生产者的手动运行界面
系统配置台 SHALL 在每个自动任务生产者卡片提供一个唯一的“立即执行”入口，并根据固定生产者定义显示受限参数表单。日指标和周指标表单 SHALL 仅接受开始日期与结束日期；帖子采集 SHALL 仅提供固定当前窗口模式与仅含小时数的历史补采模式，会话保活和永久禁言续期 SHALL 不展示参数。页面必须在提交前说明“此操作仅创建后台任务，不改变自动规则”，提交后展示创建或复用的任务编号、输入范围及实际范围（如适用）。

日/周范围表单 SHALL 拒绝空值、非法日期、开始晚于结束、未来日期和超过 90 个自然日的范围；拒绝时不得创建 Job 或改变生产者配置。页面不得暴露 Cron、任务参数原文、认证材料、AppID 或上游响应。

#### Scenario: 提交日指标范围
- **WHEN** 运营人员提交一个有效的日指标日期范围
- **THEN** 页面 SHALL 显示该输入范围与为各来源创建或复用的任务结果
- **THEN** 页面不得显示内部来源参数、认证信息或上游响应

#### Scenario: 提交无效范围
- **WHEN** 运营人员提交未来日期或超过 90 天的日期范围
- **THEN** 系统 SHALL 显示可处理的校验错误且不得创建后台任务
- **THEN** 既有自动规则、服务状态和运行投影不得变化

### Requirement: 系统配置页的局部文章目录

系统 SHALL 保留 `/admin/` 的既有 Hero、运行概览、当前操作游戏、流程/服务双视图、自动任务生产者、核心配置、运行健康和数据追溯的正文顺序与既有操作入口。系统 SHALL 为运行概览、自动任务生产者、核心配置、运行健康和数据追溯提供稳定、唯一的页内锚点。

除 Hero 外，五个主区块 SHALL 使用原生 `<details>` / `<summary>` 呈现为独立披露区。运行概览 MUST 默认展开；自动任务生产者、核心配置、运行健康和数据追溯 MUST 默认收起。每个摘要 MUST 显示区块标题和简短说明，且只能控制其自身内容的可见性；正文内既有表单、CSRF token、权限、action、反馈、dialog、服务控制和 `return_to` MUST 保持原位置、原顺序和既有行为。

系统 SHALL 将系统配置正文最大宽度限制为 `1200px`，使正文两侧留白形成一致的阅读边界；共享顶栏仍使用共用的宽度与主题开关规则。系统 SHALL 在视口宽度不小于 `1440px` 时将这五个锚点呈现为 Hero 之后、正文最大宽度外侧留白中的 Wiki 式固定“本页目录”；该目录宽度 MUST 为 `100px`，与正文最小间距 MUST 为 `12px`，外侧安全留白 MUST 为 `8px`，并且 MUST 不参与正文栅格、压缩正文、遮挡表单、dialog 或服务卡。在视口宽度小于 `1440px` 时，系统 SHALL 将同一目录呈现为 Hero 后、正文前的原生 `<details>`。目录 MUST 只包含当前页面的普通 fragment 链接，不得包含服务状态、跨页面链接、操作按钮、表单、CSRF token、POST action 或 Job 创建能力。目录不得删除、迁移、复制或重排任何正文区块。

目录链接、目标区块的 `:target` 定位反馈和响应式展开 SHALL 仅改变页面阅读定位。若通过目录点击、初次加载带 fragment 的 URL、浏览器前进或后退定位到一个收起的披露区，页面 SHALL 先展开该目标区块，再定位到对应锚点。系统 MUST NOT 实现动态目录当前项、滚动监听、自动随滚动展开或展开状态持久化。目录与披露行为 MUST NOT 创建 Job、调用 CDP、访问或控制 Supervisor、修改配置、改变表单值、触发上游请求或发起额外 HTTP 请求；页面既有共享脚本的请求不属于目录或披露行为，目录与披露不得新增或触发它们。JavaScript 不可用时，普通 fragment 链接和原生 `<details>` 仍 SHALL 可用。

系统配置页 SHALL 复用已认证运营页面共用的顶栏模板、主题切换和响应式行为。共享顶栏 MUST 显式保持横向 flex 布局并禁止换行，以覆盖 Django Admin 在 `≤1024px` 时对通用 `#header` 施加的纵向布局；视口宽度不大于 `1080px` 时，系统配置页 MUST 隐藏主导航，并保持品牌、主题开关和会话状态在同一顶栏布局内；系统配置正文样式 MUST NOT 强制主导航显示、换行或覆盖共享顶栏布局。

共享顶栏 SHALL 在浅色与深色主题中显式定义品牌、普通导航、激活导航和账户链接颜色。该共享规则 MUST 覆盖 Django Admin 的基础 `#header` 链接样式，使 `/admin/` 与其他运营页面使用相同的颜色语义。

#### Scenario: 在宽屏定位核心配置

- **WHEN** 已认证运营人员在宽屏访问 `/admin/` 并点击“核心配置”
- **THEN** 系统 SHALL 先展开核心配置披露区，再定位到同页的核心配置区块，正文操作入口及顺序保持不变
- **THEN** 该交互不得创建 Job、调用 CDP、控制服务、修改配置或发起额外 HTTP 请求

#### Scenario: 在非超宽屏打开目录

- **WHEN** 已认证运营人员以宽度小于 `1440px` 的视口访问 `/admin/`
- **THEN** 系统 SHALL 显示可展开的原生“本页目录”，且目录不得占用正文横向空间
- **THEN** 展开目录不得复制或暴露第二份操作表单，也不得造成横向溢出

#### Scenario: 在超宽屏使用外置目录

- **WHEN** 已认证运营人员以宽度不小于 `1440px` 的视口访问 `/admin/`
- **THEN** 系统 SHALL 在正文最大宽度外侧的右侧留白显示固定“本页目录”，正文保持完整可用宽度
- **THEN** 目录不得覆盖正文、表单、dialog 或服务卡，且不得改变任何操作入口的位置

#### Scenario: 无 JavaScript 访问目录

- **WHEN** 浏览器禁用 JavaScript 后访问 `/admin/`
- **THEN** 运营人员 SHALL 仍可通过普通 fragment 链接定位所有五个目录区块
- **THEN** 系统不得依赖脚本来创建、暴露或解释目录的业务状态

#### Scenario: 默认阅读状态

- **WHEN** 已认证运营人员不带 fragment 访问 `/admin/`
- **THEN** 运行概览 SHALL 展开，自动任务生产者、核心配置、运行健康和数据追溯 SHALL 收起
- **THEN** Hero 和右侧或窄屏目录 SHALL 保持可见，收起区块的摘要 SHALL 继续显示标题和简短说明

#### Scenario: 直接访问收起区块

- **WHEN** 已认证运营人员访问 `/admin/#data-traceability`
- **THEN** 系统 SHALL 展开数据追溯披露区，并定位到该区块
- **THEN** 系统不得展开其他收起区块、提交表单或发起额外 HTTP 请求

#### Scenario: 中等宽度保持共用顶栏

- **WHEN** 已认证运营人员以宽度不大于 `1080px` 的视口访问 `/admin/`
- **THEN** 系统 SHALL 与其他运营页面一样隐藏主导航，并显示单行的品牌、主题开关和会话状态
- **THEN** 系统不得因 Admin 专属样式使导航换行、遮挡状态或改变共享顶栏结构

### Requirement: 共享运营视觉系统
内容管理、禁言管理、AI 操作复审、运营分析和系统配置 SHALL 使用同一 topbar、稳定本地主题偏好和根 `data-operations-theme` 属性。主题控制 SHALL 使用仓库内 Lucide 太阳/月亮图标，只改变视觉 token、`aria-pressed`、可访问名称和本地偏好；不得创建 Job、调用 CDP、请求 Supervisor、修改筛选、表单值、滚动位置或业务数据。旧系统配置偏好仅可作为一次性兼容读取来源；JavaScript 不可用时页面 SHALL 保持默认浅色且既有 GET/POST 表单可用。

共享层 SHALL 提供导航、底色、文字、边框、焦点、通用表单、反馈和对话框 token。页面 SHALL 保留各自工作模式：内容管理为队列/详情与禁言列表，AI 复审为记录/证据/纠正，运营分析为周期/KPI/趋势/图表，系统配置为运行与配置控制。图标 MUST 使用本地 Lucide SVG；色彩不得成为高风险处置、异常纠正或服务控制的唯一确认方式。

#### Scenario: 跨页面保留主题偏好
- **WHEN** 运营人员在内容管理切换深色主题后打开运营分析或系统配置
- **THEN** 页面 SHALL 使用相同主题偏好和共享导航
- **THEN** 页面访问不得创建任务、CDP 页面、上游请求或服务控制

#### Scenario: 不同工作面保持边界
- **WHEN** 运营人员依次打开内容管理、AI 操作复审、运营分析和系统配置
- **THEN** 每个页面 SHALL 显示明确的上下文、控制带和主工作面，并保留原有业务操作边界
- **THEN** 深浅主题下的表单、表格、证据与对话框 SHALL 保持可读和可操作

### Requirement: pytest 专用运行态启动边界
系统 SHALL 在 Django settings 和业务模块加载之前，由 pytest-django 直接加载的测试专用 settings 模块建立测试专属运行态。测试运行态覆盖 MUST 使用 pytest/test 专用启动标识和环境变量，且测试目录必须解析到 `BASE_DIR/.pytest-runtime/` 下；默认生产配置不得因该机制获得通用运行态目录覆盖能力。

未设置精确测试启动标识时，系统 SHALL 保持当前 `runtime/`、`runtime/.env`、数据库、静态文件和配置解析行为。设置测试标识但缺少测试运行态目录、目录越出 `.pytest-runtime/` 或路径不可安全解析时，系统 MUST 在加载 settings 时失败，而不得回退读取开发 `runtime/.env`。

#### Scenario: 默认 pytest 不继承开发模型配置
- **WHEN** 仓库 `runtime/.env` 含有 AI base URL、API key、model 和启用开关
- **THEN** 默认 pytest 会话 SHALL 不读取这些值，并显式关闭可选外部集成

#### Scenario: 未使用测试启动标识的生产配置
- **WHEN** 非 pytest 进程以现有环境启动且没有测试启动标识
- **THEN** 系统 SHALL 继续使用仓库 `runtime/` 和既有 `.env` 配置语义

#### Scenario: 非法测试运行态路径
- **WHEN** pytest 启动标识存在但测试运行态路径不在 `BASE_DIR/.pytest-runtime/` 下
- **THEN** settings 初始化 MUST 失败，且不得创建、修改或删除该路径

### Requirement: 每 worker 测试状态隔离
系统 SHALL 将每个 pytest 执行 worker 的测试数据库源路径、Worker/Scheduler 锁、Supervisor lease/heartbeat/token/logs、静态文件和其他 RuntimeStore 派生资源置于 `.pytest-runtime/<worker-id>/`。串行运行的 worker ID SHALL 为 `master`；xdist 运行时 SHALL 使用稳定且唯一的 xdist worker ID。

`runtime.database_path` MUST 在 Django settings 加载前派生到对应 worker 子树，使从其父目录派生的 `worker-locks` 同样按 worker 隔离。pytest-django 的数据库名称后缀不能替代该路径派生。

#### Scenario: Scheduler 与开发服务并存
- **WHEN** 开发 Scheduler 或 Worker 已持有仓库 `runtime/worker-locks` 中的锁并执行 `run_scheduler` 测试
- **THEN** 测试 SHALL 使用 `.pytest-runtime/master/worker-locks` 中的独立锁并通过，且仓库 `runtime/` 中的锁、lease 与其他文件均不被测试创建、修改或删除

#### Scenario: 两个 xdist worker 执行锁相关测试
- **WHEN** pytest 以 `-n 2` 运行两个会调用 `worker_lock` 的测试 worker
- **THEN** 每个 worker SHALL 使用自身 `<worker-id>/worker-locks` 下的锁，且不会因另一 worker 持锁而报告同类 Scheduler 或 Worker 已运行

#### Scenario: 测试运行态安全清理
- **WHEN** pytest 会话开始清理可再生测试状态
- **THEN** 清理逻辑 SHALL 只删除当前 worker 子树中名称受白名单约束的易失资源，并拒绝删除 `.pytest-runtime/` 根、仓库 `runtime/`、工作区根目录或任何越界路径

### Requirement: 数据库复用与易失状态生命周期
系统 SHALL 保留 `.pytest-runtime/<worker-id>/` 以支持本地测试数据库复用，但 MUST 在每次会话开始清理 lease、heartbeat、token、日志、锁和其他易失运行态。测试数据库仅在使用 `--reuse-db` 时保留；默认 pytest 与 `--create-db` MUST 重建测试数据库。

系统 SHALL 保留 `uv run pytest` 作为干净数据库正确性基线，并提供 `uv run pytest --reuse-db` 与 `uv run pytest --create-db` 的使用规则。CI MUST 使用干净数据库验证，不得以 `--reuse-db` 替代。

#### Scenario: 本地第二次复用数据库
- **WHEN** 开发者在无迁移变化的情况下连续两次执行 `uv run pytest --reuse-db`
- **THEN** 第二次执行 SHALL 复用测试数据库并得到与干净基线一致的测试结果，且不继承上一会话的锁、lease、token 或心跳

#### Scenario: 强制重建测试数据库
- **WHEN** 迁移已变化、数据库损坏或开发者执行 `uv run pytest --create-db`
- **THEN** pytest SHALL 重建测试数据库，并在不读取或修改仓库 `runtime/` 的前提下完成测试

### Requirement: 默认 HTTP 出站请求护栏
系统 SHALL 在默认 pytest 会话中阻止项目代码发起未经测试明确替身化的出站 HTTP 请求。护栏 MUST 覆盖项目使用的 httpx 真实发送入口，并在连接开始前快速失败；诊断只能包含测试节点、请求方法和安全的主机信息，不得包含 Authorization、Cookie、查询参数或请求正文。

通过 mock、fake client 或替换发送入口明确模拟请求的测试 MUST 继续可用。第一阶段护栏 SHALL 限定于 httpx，不得为了覆盖外网而全局禁用未证实需要隔离的 socket、本地 IPC 或 CDP 测试边界。

#### Scenario: 漏掉模型 HTTP mock
- **WHEN** 常规测试启用模型结论客户端但没有替换其 httpx 请求
- **THEN** 测试 MUST 在连接或读取 timeout 之前失败，且不得访问配置的模型端点

#### Scenario: 模型客户端使用固定响应
- **WHEN** 测试以固定脱敏响应或 `httpx` 异常替换模型客户端的发送入口
- **THEN** 测试 SHALL 覆盖成功、timeout、HTTP 状态异常、非 JSON 或约束异常分支，而不访问网络

#### Scenario: 已 mock 的其他 httpx 客户端
- **WHEN** 既有测试替换 `httpx.Client`、`httpx.post` 或项目客户端发送方法
- **THEN** HTTP 护栏 SHALL 不阻止该替身行为，且测试仍可断言本地客户端契约

#### Scenario: 显式本地 HTTP 集成
- **WHEN** 测试需要访问本机控制服务并使用 `allow_http_hosts` marker
- **THEN** marker MUST 仅接受 `localhost` 或 loopback IP；任何公网、内网或非本机名称 MUST 在测试建立阶段失败，且不得发起连接

### Requirement: 可选 AI 结论的确定性测试契约
系统 SHALL 将舆情模型结论测试拆分为纯函数、客户端契约和报告集成三类。默认测试 MUST 断言结构性行为，而不得使用真实模型、真实凭据或真实模型生成的自然语言。

纯函数测试 SHALL 覆盖证据白名单、输入 hash、版本快照、结论 JSON 校验、结论类型、证据引用与运营限制。客户端契约测试 SHALL 覆盖模型成功响应及连接、读取、写入、连接池 timeout，HTTP 401/403/404/429/5xx，非 JSON、响应结构和本地约束失败，并断言稳定的 `ConclusionError` 错误码。报告集成测试 SHALL 验证模型关闭或不可用时报告仍成功，模型可用时仅持久化通过本地校验的结论。

#### Scenario: 模型不可用不阻断报告
- **WHEN** 报告运行期间模型客户端返回连接错误、读取超时或无效结论
- **THEN** 报告 SHALL 保持成功完成并记录对应的固定、非敏感结论不可用状态

#### Scenario: 模型结论通过本地约束
- **WHEN** 模拟模型返回包含有效结论类型、非空已知证据 ID 且不含受限内容的 JSON
- **THEN** 客户端 SHALL 返回已规范化的可用结论，并保留其证据引用

#### Scenario: 模型结论违反约束
- **WHEN** 模拟模型返回未知证据、无效类型、数值断言、受限运营用语或非 JSON 内容
- **THEN** 客户端 SHALL 返回对应的稳定失败分类，且不得将原始上游正文、凭据或未脱敏输入暴露给测试输出或报告展示

### Requirement: 受门控的并行试验
系统 SHALL 在运行态隔离完成且串行全量 pytest 连续两次通过之前，保持默认测试命令串行。并行测试仅可作为显式、固定小并发的试验，从 `pytest-xdist -n 2` 开始。

在推荐任何并行命令之前，试验 MUST 验证每个 worker 的 Django 测试数据库、`runtime.database_path`、`worker-locks` 和 RuntimeStore 路径均位于不同的 `<worker-id>` 子树，并在相同机器上连续两次全量通过。只有相对隔离后的串行基线至少减少 20% 墙钟时间、无新增失败、重试或顺序依赖时，文档才可将实测最优的固定小并发列为可选推荐命令；本次为 `-n 3`。裸 `uv run pytest` MUST 保持串行语义，且不得使用 `-n auto` 作为默认。

#### Scenario: 并行试验发现共享状态
- **WHEN** `-n 2` 全量验证出现 SQLite 锁、共享 runtime 文件写入、跨 worker 锁冲突或顺序依赖失败
- **THEN** 并行 SHALL 不被推荐或设为默认，且问题 SHALL 作为独立隔离缺陷处理

#### Scenario: 并行试验满足门槛
- **WHEN** 固定小并发在相同机器上连续两次全量通过，并且墙钟时间比串行基线降低至少 20%
- **THEN** 文档 MAY 将实测最优值提供为可选开发命令；本次为 `-n 3`，同时保留串行默认命令
