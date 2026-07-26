## ADDED Requirements

### Requirement: 外层登录态的业务验证

系统 SHALL 仅在 service-owned HOME 的 root document 中以 `cache: 'no-store'` 调用 `getappinfo`，不得额外设置 `Cache-Control` 或 `Pragma` 请求头，并获得 HTTP 200、`errcode=0` 且非空 AppID 时，将外层会话投影为 `OUTER_READY` 并更新独立的成功验证时间。缓存的历史成功响应、URL token、页面可打开或 target 存在均不得单独证明外层登录态有效。`getWxaList` SHALL 仅用于目录读取和游戏切换前校验。

系统 SHALL 将 HTTP 401 且 `errcode=40001`（`API_Invalid_Credential`）或 `errcode=40042` 视为已验证的外层认证过期。其他 HTTP 状态或未知业务码 MUST NOT 单独投影为 `LOGIN_REQUIRED`。

系统 SHALL 区分 `LOGIN_REQUIRED` 与 `LOGIN_PENDING`。前者表示外层登录态已确认过期、尚无登录页；后者表示唯一 service-owned 登录页已由运营人员打开并等待扫码。两者 MUST 投影 `Frame=UNREADY` 并只提供固定的打开登录页操作；自动探测不得创建、显示或聚焦登录页。

“打开登录页” SHALL 是受当前运行态授权的人工命令，而非通用 target 创建接口：仅 `LOGIN_REQUIRED` 可创建唯一登录页，`LOGIN_PENDING` 只能复用仍存在的登记页。登录等待观察到外层会话恢复后，服务 SHALL 在同一 control lane 中复用该 HOME 建立 Frame；在 `OUTER_READY` 或 `BOOTSTRAPPING` 状态下到达的重复打开请求 MUST 不创建、聚焦或关闭任何 target，并返回无副作用结果。页面仅在 `available_actions` 含 `OPEN_LOGIN` 时展示该登录页入口，后端仍必须执行同一校验。

#### Scenario: URL token 尚在但登录态已过期

- **WHEN** HOME URL 仍含 token，且 `getappinfo` 返回已验证的认证拒绝或页面明确要求扫码
- **THEN** 系统 SHALL 投影 `LOGIN_REQUIRED / UNREADY`，废弃该 generation 的来源可用性
- **THEN** 系统不得显示外层或 Frame 有效，也不得自动打开登录页

#### Scenario: 暂时故障不能误报需要扫码

- **WHEN** `getappinfo` 出现网络、未知响应或非认证失败
- **THEN** 系统 MUST NOT 标记 `LOGIN_REQUIRED` 或废弃现有 lease
- **THEN** 系统不得刷新成功验证时间，超过新鲜度窗口后 SHALL 显示状态待确认

#### Scenario: 新建 HOME 跳转扫码页时上下文短暂重载

- **WHEN** 手动检查创建的 service-owned HOME 先可建立 CDP 上下文，随后在限定稳定窗口内跳转至可见扫码页
- **THEN** 系统 SHALL 仅对上下文销毁造成的短暂 CDP 不可用重试认证诊断，并在识别扫码页后投影 `LOGIN_REQUIRED / UNREADY`
- **THEN** 稳定窗口耗尽仍不可用时，系统 MUST 保持未确认故障投影，不得将其误报为需要扫码

#### Scenario: 未获得本轮外层证明即发生候选失败

- **WHEN** HOME target、页面执行或 `getappinfo` 请求在本轮成功响应前发生暂时失败
- **THEN** 系统 SHALL 投影 `UNKNOWN / UNREADY`，不得显示 `OUTER_READY`，也不得将该未知失败误判为需要扫码
- **THEN** 仅当本轮已成功验证外层，或完整 ACTIVE lease 未损坏时，候选 Frame 失败才可保留外层有效投影

#### Scenario: 扫码完成时存在排队的重复打开请求

- **WHEN** 运营人员已打开唯一登录页并完成扫码，且登录等待观察器正在或已经建立当前游戏 Frame
- **THEN** 后到达的“打开登录页”请求不得创建额外 HOME 或登录页，也不得干扰 Bootstrap
- **THEN** 已建立或正在建立的 Frame 仍由会话服务自身的恢复流程收敛

#### Scenario: 扫码等待 target 在完成前后丢失

- **WHEN** 已登记的唯一登录等待 target 因 Chrome replacement 或 target 消失而无法继续观察
- **THEN** 服务 SHALL 清除等待登记并执行一次常规 Bootstrap；已恢复的浏览器 profile SHALL 建立当前游戏 Frame
- **THEN** Bootstrap 不得自动创建、激活或聚焦登录页；若认证仍失效，系统 SHALL 维持 `LOGIN_REQUIRED / UNREADY`

#### Scenario: 扫码后外层凭据尚在传播

- **WHEN** 登录等待页的可见扫码标记首次消失
- **THEN** 系统 SHALL 进入固定十秒稳定窗口，期间不得调用 `getappinfo`、切换游戏、建立 Frame 或创建 target
- **THEN** 窗口结束后系统 SHALL 使用普通外层验证；若扫码标记重新出现则回到扫码等待，而非将短暂 401 视为最终登录失败

### Requirement: token 刷新与来源 lease 的连续性

系统 SHALL 把正常 outer token 刷新与外层登录态过期视为不同事实。Keepalive、切换/reload 或其他正常流程获得新 outer token 时，只要 `getappinfo` 持续成功，系统 MUST 保留此前发布的 ACTIVE Frame lease；新 lease 发布后，旧 lease SHALL 仅按既有访问计数和退役延迟关闭。

经 `getappinfo` 确认的外层登录态过期 SHALL 使同一 browser generation 的全部 ACTIVE lease 失去新 access 资格。服务 MUST 保留在途 access 的有界完成，按既有退役规则回收 owner target，并且不得让旧 lease 重新成为可用状态。外层仍有效时的来源身份拒绝或 Frame target 丢失 SHALL 仅废弃对应 lease，不得反推外层登录态过期。

#### Scenario: 保活刷新 token 后旧 Frame 仍可使用

- **WHEN** Keepalive 获得新的 outer token，且旧 lease 尚有来源 access 或尚未达到退役条件
- **THEN** 系统 MUST NOT 因 token 值变化关闭或拒绝旧 lease
- **THEN** 新 lease 发布后，旧 lease SHALL 按既有退役规则完成关闭

#### Scenario: 外层登录态过期时存在多个 lease

- **WHEN** `getappinfo` 确认外层登录态过期，且当前 generation 存在多个 AppID 的 ACTIVE lease
- **THEN** 所有 lease SHALL 同时失去新 access 资格，当前范围 Frame SHALL 投影为未就绪
- **THEN** 系统不得仅处理当前 AppID 或继续使用同一过期登录态的其他 Frame

### Requirement: 低频探测与来源任务反馈

游戏圈会话服务 SHALL 在存在 ACTIVE lease 且不处于登录恢复时，每五分钟执行一次只读外层验证；手动检查 SHALL 立即执行同一验证。两者 MUST 复用 service-owned HOME 并进入 control lane，不得创建、reload、切换、绑定、关闭或聚焦 target，也不得创建 BackgroundJob 或写入上游。

SessionRepair 的短间隔循环 SHALL 仅检查 owner target 完整性，不得调用外层验证。来源客户端 SHALL 保留认证拒绝类别：明确的来源身份拒绝只使对应 Frame 失效；认证嫌疑 SHALL 合并为一次 HOME 外层复核；普通传输或契约失败不得改写会话状态。

来源 IPC 在外层登录态已确认过期、Frame 已确认失效或不存在可用 lease 时 SHALL 返回固定 `SESSION_UNAVAILABLE`；该次调用 MUST NOT 使用旧 token 请求上游。Scheduler 仍只按规则入队，入队成功不得解释为来源读取成功。

#### Scenario: 并发认证嫌疑

- **WHEN** 多个来源请求在同一冷却窗口报告认证嫌疑
- **THEN** 服务 SHALL 合并为最多一次 HOME 外层复核
- **THEN** 外层有效时仅按 Frame 问题处理；外层过期时才废弃该 generation 的全部 lease

#### Scenario: 已确认无可用会话的定时任务

- **WHEN** 定时采集、状态同步、指标或禁言读取任务在无可用会话后被 Worker 领取
- **THEN** 任务 SHALL 获得固定会话未就绪错误，且来源 IPC 不得发起上游读取
- **THEN** 任务审计不得将该已知问题记录为泛化“来源暂不可用”
