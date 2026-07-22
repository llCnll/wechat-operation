## MODIFIED Requirements

### Requirement: 明确 target 的 CDP 适配器

公众平台、插件页和游戏圈 iframe 的 CDP 适配器 SHALL 仅由 `GameCircleSessionService` 调用，并接收服务提供的明确 target ID 和经验证 iframe context。来源客户端 MUST NOT 自行查询 `/json/list` 后选择第一个页面、按域名扫描 iframe，或关闭非自己拥有的 tab。

会话服务 SHALL 提供固定 HOME 与插件路由、扫码判定、外层 token 和当前游戏回读、目录校验后的单次直接切换、iframe 检查、身份绑定及最小只读验证；认证值只能在会话服务及其 adapter 调用边界内存在。

Scheduler、未确认的上游写操作和 Django 请求 MUST NOT 直接调用 CDP 适配器。Web MUST NOT 创建、扫描、接管或关闭受会话服务管理的 target，不得打开登录页、切换游戏、重建 HOME/PLUGIN、直接调用身份绑定或在请求内执行会话刷新。经认证且经 CSRF 校验的 Web POST 可仅通过固定本地 IPC 请求 `OPEN_CONTROLLED_LOGIN`；只有运行态为 `SCAN_REQUIRED` 时页面才可提供该动作，由会话服务打开固定、无认证的公众平台登录页 `https://mp.weixin.qq.com/cgi-bin/loginpage`。无运行时 token 的插件路由不得作为登录页。Web 不得读取认证信息、登记 target 或影响现有会话。

经认证、CSRF 二次确认的有界人工读取或写操作 MAY 同步调用会话服务的固定 IPC 契约。范围只可为单条帖子，或当前游戏内最多 20 条、每条独立审计的沉底/屏蔽批量操作。该调用 MUST 只传递类型化业务请求和固定 `operation_id`，不得传递 target ID、任意 CDP 命令、JavaScript、URL 或认证材料。会话不可用时，服务 SHALL 只创建/复用刷新 Job 并返回脱敏的可重试结果；人工写操作 MUST NOT 在刷新完成后自动重放。

#### Scenario: 有界人工操作遇到会话不可用
- **WHEN** 运营人员已二次确认单条操作，或最多 20 条的沉底/屏蔽批量操作，但登记 PLUGIN、iframe 或 lease 不可用
- **THEN** Web SHALL 不执行切换、建页或身份绑定
- **THEN** 会话服务 SHALL 创建或复用 `RECOVER` Job，并向页面返回脱敏的“会话正在恢复，请稍后重新确认”结果
- **THEN** 刷新成功后系统不得自动发送原写请求

#### Scenario: 同域候选页并存
- **WHEN** 浏览器中同时存在旧 HOME、旧 PLUGIN 和新建 candidate
- **THEN** adapter SHALL 只在协调器给定的 target ID 中执行
- **THEN** adapter 不得自行选择列表中的第一个同域页面

### Requirement: 受控验证的外层当前游戏协议

当前游戏回读 SHALL 在明确 HOME target 的 `mp.weixin.qq.com` 外层页面中，以绝对 `https://gamemp.weixin.qq.com/cgi-bin/gamewxagsettingwap/getappinfo` 和浏览器凭据调用。HTTP 200、业务 `errcode=0` 时从 `data.app_info.appid` 读取当前 AppID；相对路径不得使用，因为其会落到错误的外层域名。`RECOVER` 或当前不在目标的 `KEEPALIVE` 仅可在目录唯一含 target 且 `account_switch_permit` 允许时向固定外层切换端点发送一次 `{appid: target}` 请求，并等待同一 HOME 重载后重新回读当前 AppID。当前已在目标的 `KEEPALIVE` 可对受控中转 B 执行同一契约的 `A → B → A`：B 必须从非目标、平台许可的目录项中均匀随机选择，候选需先去重和稳定排序。调用失败、响应不合法、目录/许可/中转不满足或任一步回读不一致时，adapter SHALL 返回安全的契约错误；协调器不得尝试 DOM 回退、列表顺序推断或临时 iframe probe。

外层 URL token 与来源身份 token SHALL 被视为不同且短期的认证材料。适配器 MUST NOT 持久化、记录、输出或跨 target 重放任一 token。

CDP browser generation 改变时，适配器 SHALL 拒绝所有旧 target ID，并允许协调器在新的 HOME 上重新判定外层登录态。固定无 token 插件路由没有 iframe 不得被单独用作“需要扫码”的证据。

#### Scenario: 外层 token 失效
- **WHEN** 新页无有效外层 token，或有效 token 页面没有游戏圈 iframe
- **THEN** 适配器 SHALL 报告可分类的登录/插件不可用状态
- **THEN** 协调器不得复用该页作为 PLUGIN
