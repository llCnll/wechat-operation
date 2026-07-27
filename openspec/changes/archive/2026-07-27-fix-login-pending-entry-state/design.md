## Context

游戏圈会话服务是 CDP、认证材料和 service-owned target 的唯一所有者。人工登录入口目前先打开/激活 HOME，再由 Web 立即发起 `CHECK_LOGIN`；底层外层适配器把固定 `/cgi-bin/loginpage` 路径和扫码正文文案共同作为扫码判断。实际平台允许从 `https://mp.weixin.qq.com/` 或 `https://mp.weixin.qq.com/cgi-bin/loginpage` 进入扫码流程，且登录页初始渲染、扫码完成后的跳转和 token 生效均存在短暂过渡期。

错误的路径或文案判断会把仍在进行人工操作的 target 当作失败候选而关闭；相邻的打开和检查操作也会竞争同一 target 的清理权。系统需要保留“一个 owner、一张可观察 HOME”的边界，同时把登录状态从页面表现细节收敛为明确的会话状态机。

## Goals / Non-Goals

**Goals:**

- 使用共享的外层观察分类，而不是入口 URL 或可变正文文案定义 `LOGIN_PENDING`。
- 使根路径和 `/cgi-bin/loginpage` 两种入口在扫码期间都保留同一 target，重复点击只激活该 target。
- 在页面获得 token 后固定等待十秒，再通过统一的 `getappinfo` 验证决定 `OUTER_READY`；成功后复用 HOME 建立当前游戏 Frame。
- 将 target 丢失、generation 变化、超时和明确认证失败收敛为可预测的登录状态；登录等待结束不关闭唯一可观察 HOME。
- 用单一控制动作消除 Web 与 session service 间的相邻检查竞争。

**Non-Goals:**

- 不改变受控 Chrome 或 Session Worker 的 Supervisor 所有权和重启策略。
- 不保存、展示或通过 IPC 返回 token、Cookie、target ID、完整 URL、二维码或账号信息。
- 不自动完成扫码、不接管手动 tab、不增加来源请求的隐式登录恢复。
- 不改变已发布 ACTIVE lease 的使用语义；查看受控会话页不得将已有 ACTIVE lease 或其 HOME 降级为登录等待，登录等待期间不得发布新 lease。

## Decisions

### 1. 共享外层观察分类驱动状态机

会话服务在 control lane 内为同一 service-owned HOME 产生单一的外层观察分类：`VERIFIED`、`NO_USABLE_TOKEN`、`TRANSIENT_UNREADY` 或 `AUTH_EXPIRED`。分类只读取本地 target 的稳定性和可用 token，并在 token 存在且满足固定等待后使用 `getappinfo` 完成验证；固定 pathname、扫码正文文案和入口地址均不得作为状态机条件。

当 HOME 已稳定但为 `NO_USABLE_TOKEN`，且当前 scope 不存在 ACTIVE lease 时，服务登记 `_LoginPending` 并投影 `LOGIN_PENDING`。该规则由 Bootstrap、低频外层探测和人工打开受控会话页共同使用，因此根路径和 `/cgi-bin/loginpage` 的扫码页均得到一致状态。低频探测遇到 `TRANSIENT_UNREADY` 时只保留现有投影并在后续维护周期重试；直接建立或显式检查则投影“外层会话未就绪”，但不得将其错误投影为需要扫码。

来源身份被上游拒绝时，已拒绝的 lease 必须退役，避免继续向上游提交已知无效身份；但只有 `VERIFIED` 观察才可刷新外层验证投影。`TRANSIENT_UNREADY` 下退役 lease 时保留既有外层观测时间与状态，不得将它伪造为新的 `OUTER_READY` 验证。

存在 ACTIVE lease 时，“查看受控会话页”只激活其 HOME，不得登记登录等待、修改 target role、撤销 lease 或把 Frame 投影为未就绪。已验证 lease 的外层失效仍由既有外层探测通过同一观察分类处理，而非由查看页面动作隐式改变。

这将“需要人工完成登录”的业务事实与平台入口 URL 解耦，同时隔离“观察已登录页面”和“恢复未登录页面”两类操作。替代方案是将根路径与 `loginpage` 分别映射成不同状态，或继续识别页面文字；前者无法处理平台重定向，后者依赖异步、可变且本地化的页面内容，均不能保证 target 生命周期正确。

### 2. 以可验证会话而不是地址跳转完成登录

已登记的登录等待观察分为三个阶段：

1. target 没有可用 token：保留 `LOGIN_PENDING` 并继续等待；不请求 `getappinfo`，不关闭 target。
2. target 首次出现非空 token：记录仅内存的 `settle_not_before_at`，固定等待 `LOGIN_COMPLETION_SETTLE_SECONDS`（十秒）。此阶段仍为 `LOGIN_PENDING`，避免 token 与 Cookie 传播窗口被误判为失效。
3. 等待结束：调用唯一外层验证 `getappinfo`。只有 HTTP/业务响应成功且返回有效 AppID 时，才进入常规 Bootstrap、建立 Frame 并发布 lease。

路径只能作为诊断观察，不能单独触发状态转换。页面离开登录入口但尚未稳定到工作台，或短暂验证未就绪时，仍停留在等待状态直到固定超时或出现终态。

替代方案是在 URL 变为 `/wxamp/index/index` 时立即验证。该方案无法覆盖 token 传播延迟，已在真实扫码后出现过 401 或不动作。

### 3. 明确等待的终态与资源清理

`LOGIN_PENDING` 只在以下情况结束：

- `getappinfo` 成功：清除等待登记，复用该 HOME 建立 Frame；成功发布 lease 后按既有规则管理资源。
- target 丢失或 browser generation 改变：清除等待登记，投影 `LOGIN_REQUIRED`；不得创建替代登录页。
- 等待窗口到期或 `getappinfo` 明确返回已知认证失效：清除等待登记并投影 `LOGIN_REQUIRED`；保留当前 owner 的唯一可观察 HOME，供运营人员再次查看或扫码时复用。

无法连接 CDP、页面尚未就绪、页面过渡或非终态契约异常不改变等待登记；维护循环只在下个周期继续观察。登录等待结束也不主动关闭该 HOME：当前 owner 最多维护一张可观察 HOME，不存在通过销毁人工上下文来控制 target 数量的必要。Chrome generation 更换或服务停止仍沿用既有 owner 生命周期清理。

### 4. Web 只调用一个会话控制动作

“查看受控会话页”对应的 Django POST 只调用 `OPEN_CONTROLLED_LOGIN`。共享观察、必要时的等待登记与异步后续观察均在 session service 内完成。页面提示只说明会话页已打开、可扫码或正在等待登录恢复，不声称同步检查已完成。

显式“检查外层会话”仍是独立固定控制动作，但它必须复用登记 HOME；当该 target 正在 `LOGIN_PENDING` 时，只返回/维持等待状态，不得清理或另建 HOME。所有固定会话控制动作都以会话服务的 Bootstrap 结果和脱敏运行态生成回执：等待期间返回 `LOGIN_PENDING`，其他未完成结果返回 `CONTROL_NOT_COMPLETED`；只有实际完成的动作才可返回其完成状态。Web 只按该回执提示，不能把被互斥保护拦截的动作表述为成功。

替代方案是保留 Web 的连续两次 IPC，再为第二次检查加特殊豁免。该做法保留时序竞争，也把 target 生命周期规则分散到 Web 和服务两端。

### 5. 自动修复覆盖已验证的零 lease 状态

“自动修复当前游戏 Frame”不是仅针对已发布 lease 的 target 存活检查。当前 scope 没有 ACTIVE lease、运行态已投影 `OUTER_READY` 且不存在 `LOGIN_PENDING` 时，自动维护应进入与人工“建立当前游戏 Frame”相同的 `REPAIR_BROKEN_FRAME` Bootstrap。Bootstrap 必须再次通过 `getappinfo` 验证外层会话，验证成功才可创建候选 HOME/PLUGIN、最小只读验证并发布 lease。

因此服务启动后的瞬时失败、来源认证拒绝后 lease 退役、或此前恢复失败而只留下外层有效投影，都能在不需要人工重复点击的情况下恢复；外层状态已过期时，二次验证将其收敛为登录状态，绝不从历史 `OUTER_READY` 投影直接发布 lease。

自动修复不得接管人工登录：存在 `LOGIN_PENDING`、`LOGIN_REQUIRED`、`UNKNOWN` 或 Chrome 不可用投影时不得尝试建立候选。零 lease 的失败尝试以进程内固定 15 秒间隔重试，避免在持续上游故障时每个维护周期都创建候选 target；健康或已发布 lease 的既有快速 target 损坏检测保持不变。该间隔不是运营配置项，避免增加仅用于内部退避的运行开关。

### 6. 运行态只记录实际观察，扫码等待是互斥控制状态

`checked_at` 表示最近一次实际外层观察，`outer_verified_at` 表示最近一次成功的 `getappinfo` 验证。仅检查 target 是否仍存在、建立中或 Frame 生命周期变更不得刷新这两个时间；否则两秒 target 修复循环会把历史 `OUTER_READY` 伪装为刚验证的登录态。

`LOGIN_PENDING` 是会话服务持有的互斥人工恢复状态。除打开/激活该唯一 HOME 与其内部观察器外，检查外层、建立 Frame、保活和自动修复均不得执行 Bootstrap 或改写投影。控制台以服务投影的 `available_actions` 作为唯一按钮可用性来源，避免 UI 暴露能破坏等待状态的固定 POST 入口。

## Risks / Trade-offs

- [登录 target 长时间停在根路径且无 token] → 以有界登录窗口管理；到期后转为 `LOGIN_REQUIRED`，但保留唯一 HOME 供下一次人工操作复用，且不创建更多 tab。
- [扫码成功后 token 生效慢于十秒] → 十秒后仅由统一验证决定；非明确认证终态保留等待直至总窗口到期，避免立刻关闭已登录页面。
- [维护循环频繁探测平台] → 无 token 阶段只读取本地页面 URL/token；`getappinfo` 只在 token 首次出现后的固定等待结束后调用，并沿用既有低频维护周期。
- [目标页被用户手动关闭] → 明确投影 `LOGIN_REQUIRED`，不自动打开新页，运营人员可再次使用唯一入口恢复。
- [真实登录流程与假设不一致] → 为两种入口、token 传播、401、超时、target 丢失和重复点击设置确定性测试，并在 Windows 受控 Chrome 上做人工扫码最小验收。

## Migration Plan

1. 先更新会话连续性和控制台规格，明确状态与入口契约。
2. 收敛适配器的页面观察 API，建立可被 Bootstrap、低频探测和人工入口复用的稳定性、token 与认证终态分类，删除扫码正文和固定登录路径对状态机的决定权。
3. 将 session service 的打开、ACTIVE lease 保护、等待登记、观察与状态收敛集中在 control lane，并移除 Web 的第二次 IPC。
4. 补齐单元测试，并在 Windows 启动 Supervisor 后执行人工扫码验收：根路径入口、`loginpage` 入口、扫码完成后十秒传播、Frame 建立、target 丢失和超时。
5. 若上线后出现回归，可回滚实现和规格；运行态不存认证材料，且未发布 target 只由当前 owner 管理，无数据迁移或持久化回滚需求。

## Open Questions

- 无。入口 URL 的两种形态、十秒稳定窗口和终态处理均已确认。
