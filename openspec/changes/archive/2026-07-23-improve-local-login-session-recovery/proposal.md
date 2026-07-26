## Why

游戏圈会话是所有游戏圈来源读写的唯一认证边界。当前实现将建立会话、登录探测、故障恢复和定时保活分散在多个 Job 中；这些入口都可能创建页面或切换游戏，使基础会话可用性依赖调度，并且无法只关闭自动维护。

目标模型将会话能力收敛为一个始终可用的服务主线，并将自动修复与主动保活降为可独立启停的扩展。来源调用只消费已验证会话，永远不触发浏览器副作用。

## Product Model

`game_circle_session` 是受控 Chrome、外层身份、来源身份和 service-owned target 的唯一 owner。Supervisor 同时管理该服务和受控 Chrome；session Worker 的 owner 变更必须联动重启 Chrome，使任何 target 都不会跨 session owner 存活。

| 能力 | 默认值 | 触发方式 | 关闭或不可用时的语义 |
| --- | --- | --- | --- |
| `SessionBootstrap` | 始终可用 | 服务启动、人工检查、人工建立 Frame、切换当前游戏 | 外层或 Frame 未就绪，来源调用返回固定脱敏结果 |
| `SessionRepair` | 关闭 | 服务内部定时检查 | 仅显示 Frame 未就绪，等待人工建立 |
| `SessionKeepalive` | 关闭 | Scheduler 四小时桶创建 Job；人工也可单次执行 | 停止未来自动 Job；既有自动 Job 按原语义完成 |

一个可用会话必须同时满足：受控 Chrome 可用、service-owned HOME 的外层会话有效、当前操作游戏已确认或按许可切换、service-owned Frame 稳定、来源身份已绑定，并且最小只读来源请求成功。只有满足全部条件才发布 ACTIVE lease。

保活 Job 的成功仅表示新候选已完整验证并原子发布 ACTIVE lease；更新运行态但未发布 lease 绝不能标记 Job 成功。候选 HOME 或 Frame 在中转、切回目标或验证任一步失败时必须关闭，旧 ACTIVE lease 继续可用。旧 lease 在退役延迟届满且访问数为零时由会话服务自身回收，不依赖后续业务来源请求。

## Key Decisions and Rationale

### Bootstrap is a service capability, not a queue side effect

会话建立属于服务存活后的基础能力，必须在没有 Scheduler、没有周期 Job、没有来源调用的情况下成立。把它建模为 `SessionBootstrap` 能将“外层已登录但 Frame 未就绪”“等待人工扫码”“当前游戏尚未选择”和“已验证可调用”表达为真实状态，而不是某个 Job 的成功或失败。

因此，启动、人工检查、人工建立 Frame 和显式游戏切换共享同一个建立器；“检查登录态”在外层验证后结束，不能偷偷升级为切游戏、建 Frame 或保活。来源调用没有 lease 时只观察未就绪状态，避免一次业务读取意外创建页面或改变当前游戏。

### Session owner and browser generation are one ownership unit

target ID 不得持久化、服务也不得扫描浏览器 tab。若允许 session Worker 在旧 Chrome 上重启，新 owner 无法安全证明某个 tab 是否属于前任 owner：恢复它会扩大接管边界，忽略它又会留下无法回收的服务 tab。Chrome 与 Worker 的联动重启消除了这个二义性：旧 generation 的 target 自然销毁，新 owner 只处理自己创建的 target。

该选择的代价是 Worker 重启会中断正在扫码的登录页；这是有意的安全边界。运行态必须明确投影“需重新打开登录页”，而不是尝试从旧页面恢复认证或所有权。

### Frame remains the source boundary

外层公众平台会话有效不等于游戏圈来源可用。来源接口仍强依赖经 iframe、身份绑定和最小只读请求验证的 Frame。所谓可选 Repair 的“自动恢复”只是在 Frame 丢失时允许服务重新建立 Frame，绝不意味着来源调用可以绕过 Frame，或允许系统使用未验证的外层 token 直接请求来源。

### Optional automation has ordinary queue semantics

关闭 Repair 或 Keepalive 的含义是停止未来自动动作，不回溯撤销运营人员已经允许进入队列的工作。Keepalive 已入队 Job 继续按原有重试与取消规则执行；正在执行的 Job 自然完成。只有移除旧 consumer 的发布兼容需要显式终态化旧任务，因为新 Worker 已不再有处理它们的能力，这不是“关闭自动化”的通用语义。

### Scope freezes at execution, not scheduling

四小时桶仅表达“本时段需要一次保活”，不表达当时的业务游戏。调度后到消费前，运营人员可能已切换当前操作游戏；因此 scope 只能在消费者拥有 Job 后、第一项浏览器操作前冻结。冻结后的 Job 保持自身可审计和可重试的工作范围；新的当前游戏通过独立 Bootstrap 获得会话，避免旧 Job 把 UI 状态写回新游戏。

### Silent automation is a capability contract

后台创建参数只表达“请求后台创建”，不能在协议层证明操作系统永远不切前台。系统承诺的是：自动流程绝不降级为前台 target，绝不调用 activate/focus，也不通过 Web toast、modal、跳转或系统通知打断用户。跨平台焦点效果仍须在 Windows 与 macOS 人工验收。

## Operator Experience

会话状态以“外层会话”和“当前游戏 Frame”分别展示：

| 外层会话 | Frame | 页面状态 | 可用人工动作 |
| --- | --- | --- | --- |
| 有效 | 有效 | 会话有效 / Frame 有效 | 手动保活 |
| 需扫码 | 未就绪 | 需扫码登录 | 打开登录页 |
| 有效 | 建立中 | 会话有效 / 正在建立 Frame | 无 |
| 有效 | 未就绪 | 会话有效 / Frame 未就绪 | 建立当前游戏 Frame、检查登录态 |
| 不可用 | 未知 | Chrome 未启动 | 按 Supervisor 状态恢复 |

自动建立、修复和保活保持静默：不得聚焦 target、弹出 Web 对话框、发送系统通知、跳转页面或产生全局提示。运营人员明确点击“打开登录页”时，才允许显示或激活唯一的人工登录页；重复点击仅复用该页，不创建或聚焦第二页。

来源读取及已确认人工写操作均通过固定 IPC 请求 ACTIVE lease。没有 lease 时，读取返回脱敏未就绪结果；人工写操作保持待确认，恢复后必须再次由运营人员提交，绝不自动重放。

## Design Boundaries

- Web、Scheduler 和业务 Worker 不直接访问 CDP，也不传递 URL、JavaScript、target ID、CDP 方法或认证材料。
- Cookie、token、二维码、账号身份、完整认证 URL、target ID 和原始来源响应不写入数据库、运行态、Job 参数、checkpoint、日志或 Web 响应。
- 服务只管理当前 owner 在当前 browser generation 中明确登记的 target；不扫描、接管或关闭人工和未知 tab。
- session Worker 重启前，Supervisor 必须终止受控 Chrome；新 Worker 只使用新的 browser generation，新建自己的 HOME、Frame 和登录等待。
- 自动 target 必须请求 CDP 后台创建能力；后台能力缺失或失败时安全失败，不得退化为前台创建。
- 已验证旧 lease 在新 lease 完整验证并发布前持续可用；发布后按访问计数和退役延迟回收旧 owner 的 target。

## Automation Semantics

`SessionRepair` 只检查 service-owned HOME 和 Frame，并把同一 AppID 的失效合并为一次有界 `SessionBootstrap`。它不创建 BackgroundJob，不依赖 Scheduler，也不会自动打开登录页。

`SessionKeepalive` 是唯一周期性会话 Job。Scheduler 只根据维护策略和四小时桶创建或复用 Job，不接触 Chrome 或认证材料。Job 在入队时不绑定执行范围；消费者首次认领后、任何浏览器操作前，原子读取 `ActiveGameScope` 并写入 checkpoint。没有当前游戏时，Job 以 `NO_ACTIVE_GAME_SCOPE` 终态结束，不创建 target、不重试、不安排恢复。scope 冻结后发生的游戏切换不影响该 Job；它完整发布冻结 AppID 的 lease，而新当前游戏由独立 Bootstrap 建立，页面只投影新当前游戏的状态。

禁用自动化仅停止未来周期 key 的接收和入队。已入队或执行中的 Job 是可审计工作，保持既有执行、取消和重试语义；手动操作不受自动化开关影响。

历史 `GAME_CIRCLE_LOGIN_PROBE` 与 `GAME_CIRCLE_SESSION_REFRESH` 不属于新模型。发布新 consumer 前，发布脚本在旧 Worker 已停止且任务锁释放后，将这些 task type 的活动 Job 以固定发布原因终态化，保留审计行、参数和 checkpoint。该兼容识别保留本发布及后续一个发布版本。

旧任务只属于发布兼容边界，不属于产品运行面：运行时任务类型注册、Scheduler、Worker、Web 路由、工作台文案和测试夹具均不得再生产、消费或把它们呈现为可执行功能。兼容识别仅封装在终态化命令中，并在保留窗口结束后删除。工作台的游戏目录入口必须直接执行受控会话目录同步；不得以“同步目录”名称跳转到需要第二次操作的后台列表。

## Failure Boundaries

| 条件 | 系统行为 | 禁止行为 |
| --- | --- | --- |
| Chrome 不可用 | 投影 `BROWSER_UNAVAILABLE` | 伪报需扫码、接管普通浏览器 |
| 外层明确需扫码 | 登记唯一 `LOGIN_PENDING`，等待人工打开 | 自动扫码、创建第二页、自动聚焦 |
| 当前游戏未选择 | 投影 `OUTER_READY` / Frame 未就绪 | 自行选择游戏、发布 lease |
| 游戏不可切换或 iframe 不稳定 | 保留旧 lease，投影固定失败类别 | 发布半成品 lease、前台 fallback |
| 来源没有 lease | 返回脱敏未就绪 | 隐式 Recover、创建 target、自动重放写入 |
| Keepalive 无 scope | `NO_ACTIVE_GAME_SCOPE` 终态 | CDP、建页、恢复或重试 |
| 新 lease 验证失败 | 继续保留旧 lease | 关闭旧 lease 或把新候选标为有效 |

## Scope

### In scope

- SessionBootstrap、登录等待、状态投影、lease 和 target 所有权。
- Supervisor 中 session Worker 与受控 Chrome 的联动生命周期。
- 默认关闭的 SessionRepair 与 SessionKeepalive。
- 会话维护 Job 的来源、scope、暂停、终态和发布兼容规则。
- IPC、工作台状态和人工会话入口。

### Out of scope

- 自动登录、自动扫码、二维码识别、Cookie 或 token 持久化、导出。
- 接管任何人工 tab、普通 Chrome profile 或第三方浏览器。
- 改变帖子采集、指标、审核或人工处置的业务判定。
- 新增外部队列、Redis、浏览器扩展或 headless 模式。

## Acceptance Criteria

1. 没有启用任何自动化时，服务启动和人工操作仍可建立并发布 READY lease；Scheduler 不创建 Probe 或 Keepalive Job。
2. 来源调用在无 ACTIVE lease 时没有 Chrome、游戏切换、建页或自动恢复副作用。
3. 登录等待期间最多存在一个 service-owned 登录页；扫码成功后复用同一 HOME 建立 Frame。
4. session Worker owner 变更必然产生新的 Chrome generation；旧 target 和登录等待不被恢复、识别或接管。
5. Repair 与 Keepalive 只有在明确启用时运行，且不会影响手动会话能力、其他调度或人工写操作确认。
6. 自动保活在消费者首次执行冻结 scope；无 scope 时安全终态；冻结 scope 与当前游戏切换并存时 lease 发布和 UI 投影正确分离。
7. control lane 使 target 生命周期操作串行化，来源 IPC 固定 lease 后不竞争 target 生命周期。
8. 认证材料和受保护标识从所有持久化、对外和日志表面消失；自动行为无前台 fallback 与 UI 打断。
9. 受控 Chrome 意外退出且会话服务仍被期望运行时，Supervisor 将其视为会话 owner 更换：在新 Chrome generation 就绪后替换会话服务并恢复当前范围 Frame；不得留下旧 owner 的可用投影。
10. 需要替换 Chrome generation 的会话服务重启应立即返回“已提交”，由 Supervisor 在后台完成；控制客户端不得因正常重启耗时而把已接受的动作误报为控制通道失败。
11. 启用的 SessionRepair 发现当前 scope lease 的 HOME 或 Frame 丢失时，必须以候选 lease 替换模式建立 Frame；不得因损坏 lease 仍标记 ACTIVE 而提前返回或持续积累 HOME target。
12. 已确认损坏 lease 的 Repair 候选失败必须投影 Frame 未就绪；Chrome generation 启动超时必须保持 Supervisor 控制面运行并由后续调和恢复。
