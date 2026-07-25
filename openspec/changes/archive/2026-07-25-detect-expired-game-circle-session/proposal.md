## Why

游戏圈会话当前把 HOME URL 中仍存在的 `token` 当作外层登录有效的依据。真实失效样本表明，这个参数仍在时，`getappinfo` 已返回 HTTP 401、`errcode=40001` 或 `40042`，而控制台仍显示“会话有效、Frame 有效”，定时来源任务也只能以泛化错误重试和失败。

需要把外层登录态、外层 token 刷新和 Frame 来源身份的边界明确为一套可验证的会话协议，使运行态、来源调用和任务审计都基于同一事实。

## What Changes

- 用 HOME 内 `getappinfo` 的成功响应验证外层登录态；URL token 仅是请求所需参数，不能独立证明登录有效。
- 区分正常 token 刷新与登录态过期：保活或切换得到新 outer token 时，旧 Frame lease 继续按既有退役规则可用；`getappinfo` 确认登录过期时，当前 browser generation 的 Frame lease 同时失效。
- 将外层登录态和 Frame 来源身份分别投影。Frame 可在外层仍有效时单独失效；登录态过期则固定投影为“需要登录 / Frame 未就绪”。
- 增加低频、单飞的外层验证；来源请求遇到认证拒绝时保留可判别类别，由会话服务复核外层状态后作出 Frame 局部失效或 generation 全局失效的判断。
- 已确认无可用会话后，来源 IPC 返回固定会话错误，不再以旧 token 请求上游；Scheduler 仍只负责入队。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `game-circle-login-continuity`: 定义外层登录验证、token 刷新、Frame lease 失效、来源调用隔离和低频探测的统一契约。

## Impact

- 受影响模块：外层 CDP 适配器、游戏圈会话服务、来源客户端、IPC 错误映射、登录运行态、运行控制台和相关测试。
- 需要数据库迁移保存最后一次成功外层验证时间，并在部署后重启 `game_circle_session` 与 `web`。
- 不新增 Scheduler Job、运行时配置项、自动打开登录页、上游写入或直接 CDP 入口。
