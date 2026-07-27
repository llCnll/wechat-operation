## Why

当前人工“查看受控会话页”会先打开受控 HOME，再立刻执行外层检查。系统把 `/cgi-bin/loginpage` 与页面扫码文案当作登录等待的必要条件；当扫码页尚未渲染文案，或入口使用 `https://mp.weixin.qq.com/` 时，检查会误判为会话未就绪并清理该 target。运营人员因此无法完成扫码，且登录恢复流程与实际页面跳转不一致。

需要以 service-owned target 的登录等待生命周期作为唯一判断依据：入口地址可以不同，扫码后页面稳定和外层验证才是可用性的依据。

## What Changes

- 将人工打开受控会话页收敛为唯一的“打开并观察登录状态”控制动作，不再由 Web 紧接发起第二次、可能竞争 target 生命周期的外层检查。
- 将 `LOGIN_PENDING` 的判定从固定路径和页面文案改为共享的外层观察结果：当前 owner HOME 稳定但没有可用 token，且不存在 ACTIVE lease；`https://mp.weixin.qq.com/` 与 `/cgi-bin/loginpage` 均可承载该状态。
- 登录等待仅在目标页获得可用外层 token 后开始固定十秒的传播等待，再以 `getappinfo` 成功且返回有效 AppID 发布 `OUTER_READY` 并建立当前游戏 Frame。
- 明确扫码等待 target 的保留、超时、丢失、generation 变化及已确认认证失效的收敛行为；等待结束仅改变运行态，唯一可观察 HOME 保留供后续人工操作复用。
- 将可选“自动修复当前游戏 Frame”扩展为外层会话已验证但当前 scope 尚无 ACTIVE lease 时的受控补建；它不介入 `LOGIN_PENDING` 或未验证的外层会话。
- 更新控制台反馈和自动化测试，使“查看受控会话页”准确提示等待扫码或等待登录恢复，而非声称已完成检查。

## Capabilities

### New Capabilities

- 无。

### Modified Capabilities

- `game-circle-login-continuity`: 调整人工登录等待的状态判定、target 生命周期和登录完成验证约束。
- `runtime-console-orchestration`: 调整受控会话页入口的控制台行为与运营反馈约束。

## Impact

- 受影响实现位于 `config/game_circle_service.py`、`config/game_circle_adapter.py`、`posts/views.py` 及对应的会话协调器测试。
- 不新增外部依赖、持久化认证材料或新的浏览器控制权限。
- 受影响的运行能力是人工扫码登录、外层会话状态投影、扫码后的当前游戏 Frame 建立和已登录但零 lease 时的自动恢复；来源读取与写入仍只使用已发布 lease。
