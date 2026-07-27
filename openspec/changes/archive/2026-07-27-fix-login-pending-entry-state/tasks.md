## 1. 登录等待状态机收敛

- [x] 1.1 重构外层页面观察接口，建立可被 Bootstrap、低频外层探测和人工入口共享的 `VERIFIED`、`NO_USABLE_TOKEN`、`TRANSIENT_UNREADY`、`AUTH_EXPIRED` 分类，不以固定路径或扫码正文决定 `LOGIN_PENDING`。
- [x] 1.2 将 `OPEN_CONTROLLED_LOGIN` 收敛为打开/复用唯一 HOME 并执行共享观察的控制动作；只有不存在 ACTIVE lease 且观察为 `NO_USABLE_TOKEN` 时才登记登录等待，保证重复请求不会创建或聚焦第二张会话页。
- [x] 1.3 在 control lane 中实现登录等待观察：无 token 保持等待、首次 token 后固定等待十秒、等待后统一以 `getappinfo` 验证并复用 HOME 建立 Frame。
- [x] 1.4 实现等待成功、超时、target 丢失、browser generation 变化和明确认证失效的状态边界；超时或认证失效不得关闭唯一可观察 HOME，非终态页面/CDP 异常不得误关 target。
- [x] 1.5 调整 Bootstrap、低频外层检查和显式外层检查，使其复用共享分类；稳定无 token 且无 ACTIVE lease 时进入 `LOGIN_PENDING`，登录等待时不触发候选清理或新 HOME 创建。
- [x] 1.6 扩展可选自动修复：当前 scope 零 ACTIVE lease 且外层已验证时复用 `REPAIR_BROKEN_FRAME` 补建；登录等待或未验证状态不介入，并对零 lease 失败使用固定退避。
- [x] 1.7 收敛运行态观察时间与 Bootstrap 边界：target 存活检查不得伪造外层验证新鲜度，`LOGIN_PENDING` 阶段阻止冲突 Bootstrap。
- [x] 1.8 收敛 `TRANSIENT_UNREADY` 的来源拒绝与低频探测语义：退役已拒绝 lease，但不将过渡观察伪造为外层验证。

## 2. 控制台入口与反馈

- [x] 2.1 移除“查看受控会话页”Web 路由中紧接的独立 `CHECK_LOGIN` 调用，使会话服务成为登录等待唯一状态所有者。
- [x] 2.2 更新操作反馈与状态投影，区分“已打开、等待扫码/恢复”“外层有效”“需要重新登录”，且不暴露认证材料或 target 信息。
- [x] 2.3 使会话服务卡仅渲染 `available_actions` 允许的会话控制，并同步自动修复说明。
- [x] 2.4 收敛固定会话控制回执与控制台反馈：扫码等待或未完成动作不得被显示为已完成检查或 Frame 建立。

## 3. 自动化验证

- [x] 3.1 补充 Bootstrap、低频探测和人工入口在根路径与 `/cgi-bin/loginpage` 均可将稳定无 token 分类为 `LOGIN_PENDING` 的单元测试，不依赖扫码正文文案。
- [x] 3.2 补充 ACTIVE lease 下查看会话页不改变 lease/target role/Frame 投影，以及无 ACTIVE lease 时重复打开复用同一 target 的测试。
- [x] 3.3 补充无 token 不请求 `getappinfo`、首次 token 后十秒等待、验证成功后建立 Frame、target 丢失、generation 变化、超时、明确 401 认证失效及非终态异常保持等待的测试；断言超时和认证失效保留唯一可观察 HOME。
- [x] 3.4 补充 Web 入口只调用单一打开控制动作并给出正确反馈的测试。
- [x] 3.5 补充零 lease 自动补建、登录等待保护和失败退避测试。
- [x] 3.6 补充 target 存活检查不刷新外层验证时间、扫码等待阻止冲突 Bootstrap 及控制台按钮收敛测试。
- [x] 3.7 补充扫码等待下固定检查/建立 Frame 控制回执和控制台提示的测试。
- [x] 3.8 补充过渡外层观察不刷新验证时间、且来源拒绝仍退役对应 lease 的测试。
- [ ] 3.9 执行相关 Django/会话协调器测试，并在 Windows 受控 Chrome 中人工验收两种入口的扫码、扫码后等待、外层有效和当前游戏 Frame 建立流程，以及外层有效零 lease 的自动补建。
