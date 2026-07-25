## Context

会话服务是受控 Chrome、HOME、Frame、来源身份 token 和 lease 的唯一 owner。外层 token 属于 HOME 当前 document；Frame lease 持有来源身份 token。二者相关但不等价：正常的外层 token 刷新不会使既有 Frame token 失效，外层登录态过期则会使由该登录态建立的 Frame token 不再可信。

当前实现没有验证外层 token 的业务有效性，也在来源客户端丢失了认证拒绝的精确信号。因此控制台可能显示陈旧有效状态，Worker 只能留下“来源暂不可用”的泛化审计。

## Goals / Non-Goals

**Goals:**

- 让外层状态只由明确的 `getappinfo` 业务结果证明。
- 以状态机区分外层 token 刷新、外层登录态过期和 Frame 来源身份过期。
- 使任何已确认失效都阻断后续无效上游请求，并向运营人员和任务审计提供固定原因。
- 保留现有 lease 原子替换、访问计数和退役语义。

**Non-Goals:**

- 不把短暂网络错误、未知 HTTP 错误或任意来源失败直接解释为需要扫码。
- 不改变 Scheduler 的入队职责，也不通过来源调用隐式恢复、切换游戏或打开登录页。
- 不引入可调频率配置；初始探测周期固定为五分钟。

## Decisions

### 外层认证证明

外层探测在 service-owned HOME root document 中调用 `getappinfo`，请求必须使用浏览器 `cache: 'no-store'`，不得以 HTTP 缓存中的历史成功响应证明认证仍有效。探测不得额外设置 `Cache-Control` 或 `Pragma`：跨站请求的缓存策略由 Fetch 的 `cache` 选项表达，避免为只读认证检查引入不必要的 CORS 预检。仅本次网络响应的 HTTP 200、`errcode=0` 和非空 AppID 能确认 `OUTER_READY` 并更新 `outer_verified_at`。明确的认证拒绝进入 `LOGIN_REQUIRED`；网络、未知响应和非认证失败不更新该时间，达到十分钟新鲜度上限后只显示“状态待确认”。

当前已验证的明确认证拒绝为 HTTP 401 且 `errcode` 为 `40001`（`API_Invalid_Credential`）或 `40042`。未知 HTTP 401 业务码仍保持未确认失败，避免平台新增其他权限/风控语义时被错误要求扫码。

新建 HOME 的 root document 可能在最初可连接后继续跳转至扫码页。外层适配器 SHALL 在有限稳定窗口内重试仅由 CDP 执行上下文销毁引起的短暂不可用，并在每次重试前重新识别可见扫码标记；当前受控平台的“扫码登录”“请使用微信扫描”或“微信扫一扫”均为明确扫码标记。一旦识别扫码页即为明确 `LOGIN_REQUIRED` 事实。窗口耗尽的 CDP 不可用仍为未确认故障，不得伪报登录过期。该稳定等待属于一次手动检查或一次低频探测，不改变其平台请求频率，也不得创建第二个 target。

Bootstrap 的 target、页面执行或外层请求契约失败若发生在本轮 `getappinfo` 成功前，只能投影为 `UNKNOWN / UNREADY`，不得以“建立 Frame 失败”为由写成 `OUTER_READY`。若本轮已经得到成功外层验证，或未损坏的 ACTIVE lease 仍在，后续候选 Frame 失败可保留 `OUTER_READY / UNREADY`；这两类事实必须通过显式 `outer_verified` 边界传递，不能由异常类别隐式推断。

服务内部以显式外层读取结果传递事实，不能再用 `token is None` 混合不同状态：可见扫码页表示 `SCAN_REQUIRED`；`getappinfo` 成功且可读取 URL token 表示 `VERIFIED_WITH_TOKEN`；`getappinfo` 成功但 token 暂不可读表示 `VERIFIED_TOKEN_UNREADY`；已验证认证拒绝由专用异常表示；其他契约或传输异常保持未确认。只有前两类中的扫码事实和专用过期异常可以进入 `LOGIN_REQUIRED`。`VERIFIED_TOKEN_UNREADY` 仍可刷新外层验证时间，既有 Frame lease 不得因此失效；仅依赖新 outer token 的建 Frame、保活或目录读取以固定“外层会话暂未就绪”失败。

成功的外层读取结果携带已验证的当前 AppID；Bootstrap 在尚未发生游戏切换时直接复用它，避免对同一 HOME 重复调用 `getappinfo`。登录页等待超时不产生任何外层有效性证明，关闭等待页后必须回写 `LOGIN_REQUIRED / UNREADY`，而非 `OUTER_READY`。

登录等待页在扫码完成前后若因受控 Chrome replacement 或 target 消失而不能继续观察，服务 SHALL 清除该等待登记后重新进入常规 Bootstrap。该 Bootstrap 只根据新的 service-owned HOME 验证持久化浏览器 profile：已恢复则建立当前范围 Frame，仍失效则投影 `LOGIN_REQUIRED / UNREADY`；它不得自动创建、激活或聚焦登录页。这样等待 target 的中断不会把已完成扫码遗留为永久的 `LOGIN_REQUIRED` 旧投影。

登录等待观察器将扫码恢复建模为三个阶段：可见扫码页时只观察页面；扫码页首次消失后进入固定十秒的凭据传播稳定窗口，期间不得调用 `getappinfo`；窗口结束后才使用普通外层验证决定恢复或继续要求登录。验证时若扫码页重新出现，观察器回到第一阶段。稳定窗口内的等待不创建 target、切换游戏或发起平台接口，避免页面跳转先完成而凭据尚未落地时的短暂 401 终止人工恢复。

`getWxaList` 继续服务目录和游戏切换校验，不加入日常认证探测。

### 状态与 lease 规则

| 外层状态 | Frame 状态 | 含义 |
| --- | --- | --- |
| `OUTER_READY` | `READY` | 外层和当前范围 Frame 均已验证 |
| `OUTER_READY` | `UNREADY` | 外层有效，Frame 未建立或来源身份已失效 |
| `LOGIN_REQUIRED` | `UNREADY` | 已确认登录态过期，尚未打开登录页 |
| `LOGIN_PENDING` | `UNREADY` | 唯一登录页已打开，等待扫码 |

正常的 Keepalive、切换/reload 取得新 outer token 时，只要 `getappinfo` 持续成功，旧 ACTIVE lease 不受 token 字符串变化影响；新 lease 完整验证并发布后，旧 lease 仅在退役延迟届满且访问计数为零时关闭。

`getappinfo` 确认外层登录态过期时，服务在 control lane 内撤销同 generation 所有 ACTIVE lease 的新 access 资格、保留在途 access 的有界完成和退役回收，并一次性写入 `LOGIN_REQUIRED / UNREADY`。登录页只能由运营人员显式打开；扫码成功后使用该 HOME 完整 Bootstrap，绝不复活旧 lease。

### 扫码恢复的命令边界

“打开登录页”不是通用的建页命令，而是运行态授权的人工恢复命令。它只在 `LOGIN_REQUIRED` 时创建唯一的 service-owned 登录页，或在 `LOGIN_PENDING` 且该页仍存在时聚焦同一页。登录等待观察器发现扫码成功后，复用该 HOME 在同一 control lane 内完整 Bootstrap；从外层验证成功直到 Bootstrap 成功或失败收敛前，后续“打开登录页”请求不得新建 target。

命令在获得 control lane 后必须重新读取运行态：`OUTER_READY`（包括 `Frame=BUILDING` 或 `UNREADY`）表示登录已不再是缺失条件，返回无副作用的“无需登录页”结果；`LOGIN_PENDING` 只复用其已登记 target。页面仅在 `available_actions` 含 `OPEN_LOGIN` 时展示登录页入口，但后端仍以相同授权规则兜底，避免浏览器中排队的旧 POST 在恢复完成后创建冗余 HOME。

成功发布 lease 后，HOME 的所有权从登录等待/候选状态原子变为 `LEASE_HOME`；未发布候选由既有 finally 回收。任何人工重复操作产生的已登记空闲 HOME 都必须视为可回收 owner target，不能作为新的登录恢复证据。

Frame 来源身份可在外层仍有效时单独失效。此时只撤销对应 lease 并投影当前范围 Frame 未就绪，不影响其他有效 lease 或外层状态。

### 探测与来源拒绝反馈

有 ACTIVE lease 时，服务每五分钟在 control lane 中复用已登记 HOME 执行一次只读外层探测。手动检查复用同一实现但不受间隔限制。SessionRepair 的两秒循环仍只检查 target 存在性，不调用平台接口。

外层探测只写入 HOME 实际证明的外层状态与成功验证时间，不得将 Frame 投影回写为 `READY`；Frame 的可用性只能由 Bootstrap、target 修复或来源访问结果改变。来源认证嫌疑携带产生该错误的 lease：在 control lane 中确认它仍是 ACTIVE 后才复用其 HOME 复核；外层仍有效时只退役该同一 lease，绝不按 AppID 重新选择可能刚发布的 replacement lease。相同 browser generation 的并发或固定五秒冷却窗口内认证嫌疑共享一次复核结果；外层有效时各自仅退役对应 lease，外层过期时由首个复核统一执行 generation 级失效。该短冷却仅用于合并同一波失败，不复用五分钟健康探测结果，避免掩盖之后发生的外层过期。

登录等待观察和目录读取也必须消费同一结果模型。登录等待的普通契约错误只结束本轮观察，不得退出维护循环或修改登录投影；目录读取遇到已验证过期必须执行相同的 generation 级失效和登录投影，不能被通用来源契约错误吞没。

来源客户端必须区分明确的来源身份拒绝、认证嫌疑和普通传输/契约失败。明确来源身份拒绝仅使对应 Frame 失效；认证嫌疑由会话服务合并为一次外层复核：外层有效则按 Frame 问题处理，外层过期则执行 generation 级失效。普通失败不得改写认证状态。

确认无可用 lease 后，IPC 返回 `SESSION_UNAVAILABLE` 并且不发送上游请求。Scheduler 可继续入队；Worker 的固定错误说明来源会话不可用，而不是伪装为一般平台故障。

## Risks / Trade-offs

- [认证错误特征变化] → 适配器以 HTTP/业务码的脱敏测试锁定已知特征，未知情况只让状态过期。
- [短暂时效窗口] → 五分钟周期外加来源认证嫌疑的单飞复核；不以两秒 Repair 轮询平台。
- [在途请求] → 失效后禁止新 access，但遵守既有访问计数与退役边界，不强行关闭正在使用的 Frame。
- [错误归类过宽] → 只有明确来源拒绝或外层复核成功确认过期才变更会话状态。
- [扫码完成与页面重复点击竞态] → 登录页命令按最新运行态授权，control lane 中已完成恢复的旧点击返回无副作用结果；测试覆盖不创建 target。

## Migration Plan

1. 增加 `LOGIN_REQUIRED` 和 `outer_verified_at` 迁移；旧记录因没有成功验证时间而显示待确认。
2. 重启 Supervisor，使 `game_circle_session` 和 `web` 加载新协议；启动 Bootstrap 立即执行认证验证。
3. 回滚时先替换 session Worker，再回滚应用；新增字段可由旧代码忽略，不删除任务或审计记录。

## Open Questions

无。
