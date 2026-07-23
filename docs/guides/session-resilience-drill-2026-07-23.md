# 游戏圈会话韧性演练记录（2026-07-23）

## 目的与边界

验证 Supervisor、受控 Chrome 与游戏圈会话服务在本机进程故障后的恢复能力。演练仅针对 Supervisor 已登记的本机进程；不注销真实账号、不清理 Chrome profile、不修改运行配置，不执行帖子采集、人工处置或任何上游写入。

所有命令均在项目根目录运行。输出只记录服务状态、重启计数、运行态类别和 target 角色数量；不得记录 token、Cookie、target ID、完整 URL、账号身份或来源响应。

## 演练项目

| 编号 | 故障注入 | 用户模拟动作 | 预期恢复条件 |
| --- | --- | --- | --- |
| D1 | 受控 Chrome 进程异常退出 | 使用已验证的 Supervisor 状态文件中的 Chrome PID 终止该受控进程树。 | Supervisor 拉起 Chrome；会话服务重新建立当前范围的 HOME 与 Frame；运行态为外层有效、Frame 有效。 |
| D2 | 游戏圈会话服务受控重启 | 通过固定 Supervisor 控制面请求重启 `game_circle_session`。 | Supervisor 先停止会话服务与旧 Chrome，再启动新 Chrome generation 和会话服务；不接管旧 target；当前范围重新建立 Frame。 |
| D3 | 会话未就绪的人工恢复路径 | 以固定 IPC 请求“建立当前游戏 Frame”，不发起任何来源读写。 | 请求只走会话服务 Bootstrap；成功后发布当前范围 lease，失败时保持固定未就绪状态且不产生上游写入。 |
| D4 | 受控外层登录 Cookie 失效 | 仅对 `127.0.0.1:9222` 的受控 Chrome 执行浏览器级 Cookie 清理，再重启 session owner。 | 新 session 投影需扫码，运营人员显式打开登录页并扫码；完成后复用登录 HOME 建立当前范围 Frame。 |

## 结果

演练完成后，在本节记录每项的发生时间、故障注入、系统动作、模拟用户动作、恢复证据及结论。若某项需要人工扫码，不主动注销当前真实会话制造该条件；应记录为“未执行（避免破坏真实认证状态）”。

### D1：受控 Chrome 异常退出

- 时间：14:16（修复前）与 14:24（修复后）。
- 发生的事情：对状态文件中已校验命令行和专用 profile 的受控 Chrome 主进程执行 `Stop-Process -Force`。未终止任何普通 Chrome、Web、Worker 或数据库进程。
- 系统做了什么（修复前）：Supervisor 将 Chrome 重启计数从 13 增至 14，但仍存活的 session Worker 没有更换 owner，运行态正确显示 `OUTER_READY / UNREADY / FRAME_UNREADY`，没有伪报来源可用。
- 模拟用户做了什么（修复前）：以会话 capability 调用固定 IPC `BUILD_CURRENT_GAME_FRAME`（等同工作台“建立当前游戏 Frame”）；不传入 CDP 参数，不执行来源读写或上游写入。Frame 随后恢复 `READY`。
- 修复：Chrome 意外退出且 session 服务仍为 desired 时，Supervisor 按 session owner 更换处理：启动新 Chrome generation 后替换 session Worker，由启动 Bootstrap 重建当前范围 Frame。
- 恢复证据（修复后）：再次注入相同故障后，Chrome 重启计数 16→17、session 重启计数 27→28；约 4 秒内运行态为 `OUTER_READY / READY / NONE`，用户无需操作。
- 结论：通过。Chrome 故障不再依赖默认关闭的自动修复或人工建 Frame。

### D2：会话服务重启控制

- 时间：14:18（修复前）与 14:25（修复后）。
- 模拟用户做了什么：调用固定 Supervisor 控制面 `SupervisorClient().control("game_circle_session", "restart")`，等同控制台重启服务；不直接访问 CDP。
- 系统做了什么（修复前）：实际完成 Chrome 14→15、session 25→26 和 Frame READY，但控制客户端的 0.7 秒 HTTP 超时先报告“控制通道不可用”。动作成功被误报为失败。
- 修复：Web 与 session 的长重启统一为“立即提交、后台串行执行”；重复点击在同一 Supervisor 内合并，页面以运行态确认最终结果。
- 恢复证据（修复后）：调用立即返回 `{"state":"restarting"}`；Chrome 重启计数 17→18、session 重启计数 28→29，运行态恢复 `OUTER_READY / READY / NONE`。
- 结论：通过。正常耗时的 session owner 交接不再被短控制超时误报失败。

### D3：游戏圈会话 Worker 异常退出

- 时间：14:19。
- 发生的事情：对状态文件中已校验为 `run_game_circle_session_worker --loop` 的 Worker 进程执行 `Stop-Process -Force`。
- 系统做了什么：Supervisor 在调和周期发现 Worker 退出，执行 Chrome 与 session owner 联动替换；不接管原 owner 的 target。
- 模拟用户做了什么：无。仅等待状态收敛。
- 恢复证据：Chrome 重启计数 15→16、session 重启计数 26→27；约 4 秒内运行态恢复 `OUTER_READY / READY / NONE`，可用动作仅为 `CHECK_LOGIN` 和 `KEEPALIVE`。
- 结论：通过。会话 Worker 意外退出后自动恢复当前游戏 Frame。

### D4：外层登录 Cookie 失效与人工扫码

- 时间：14:32–14:34。
- 发生的事情：仅对受控 Chrome 的公众平台页面 CDP 会话执行固定 `Network.clearBrowserCookies`。浏览器级 CDP 通道不支持该命令，第一次尝试失败且未清空 Cookie；已确认服务保持 `READY` 后，改为页面会话上下文执行，返回成功。未读取、输出或持久化 Cookie、target ID、完整 URL 或账号身份。
- 系统做了什么：Cookie 清理后通过 Supervisor 提交 session owner 重启。新会话启动 Bootstrap，投影为 `LOGIN_PENDING / UNREADY / SCAN_REQUIRED`，可用动作仅为 `OPEN_LOGIN`；未自动创建第二页、未发起来源读写或上游写入。
- 模拟用户做了什么：以固定 IPC `OPEN_CONTROLLED_LOGIN` 请求打开登录页，得到 `LOGIN_PAGE_REUSED`，即复用 Bootstrap 登记的唯一登录页。
- 运营人员动作：在复用的受控登录页完成扫码。
- 恢复证据：会话服务观察到同一 HOME 登录完成后自动建立当前范围 Frame；所有服务保持运行，最终为 `OUTER_READY / READY / NONE`，登录等待时间清空，运行态仅提供 `CHECK_LOGIN` 与 `KEEPALIVE`。运行态投影仅有一个 ACTIVE lease 的 HOME 和 PLUGIN 角色计数。
- 结论：通过。登录失效后不会误报来源可用；扫码完成后无需人工再次建立 Frame，也不会创建第二个登录页。

### 未执行的高影响场景

- Supervisor 本体异常退出：未在真实环境直接注入。该故障可能短暂留下其子服务，恢复需要重新启动 Supervisor 并核对所有已登记服务 PID；应在隔离环境进行端到端演练，不能与真实任务队列并行执行。

## 最小验证

- `tests/test_service_supervisor.py tests/test_admin_console.py`：33 passed。
- `python manage.py check`：通过。
- `openspec validate improve-local-login-session-recovery --strict`：通过。
- D1、D2、D3、D4 的真实 Windows 演练均未执行上游读写或写入，最终 Web、Chrome、session 服务均为运行中，当前范围为外层有效、Frame 有效。
