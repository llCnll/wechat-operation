## MODIFIED Requirements

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
