# 游戏圈会话续接调研

调研日期：2026-07-22
范围：已授权受控 Chrome 的外层公众平台页、最终游戏圈插件 iframe、身份绑定、来源查询与外层游戏切换。
安全边界：本文只记录路径、字段语义、HTTP/业务结果类别和脱敏结论；不记录 token、Cookie、AppID、账号、完整认证 URL 或上游原始响应。

## 生产结论

游戏圈会话由三层不可持久化状态组成：受控 Chrome 的 Cookie 会话、外层 HOME 的运行时 token，以及最终插件 iframe 中 `binduseridentity` 返回的来源身份 token。它们不能互相替代。

两种刷新均从新 HOME 开始，但“确认当前游戏”的策略不同：

```text
新 HOME
  → 判定扫码并读取外层 token
  → 在 HOME 外层回读当前 AppID
  → 按 RECOVER 或 KEEPALIVE 的受控切换策略对齐目标
  → 每次切换后等待同一 HOME 重载，重新读取 token 与当前 AppID
  → 新建最终 PLUGIN
  → 等待 iframe 稳定
  → binduseridentity
  → 最小只读查询
  → 原子发布 lease
```

`RECOVER` 与 `KEEPALIVE` 不共享切换语义：前者在当前游戏不等于目标时，只执行一次目录/许可校验后的直接 `B → A`；当前已为 A 时不切换。后者在当前已为目标 A 时，必须执行受控 `A → B → A`，以刷新外层游戏上下文；当前为 B、目标为 A 时只执行 `B → A`。中转 B 从目录中所有非目标、平台允许切换的游戏中均匀随机选择；候选为空时安全失败。候选集合会先去重和稳定排序，选择不依赖上游列表顺序。它不创建临时插件页用于回读，也不以切换面板 DOM、显示名称或 username 推断当前游戏，且不会把中转失败降级为 `RECOVER`。

## 已验证实验与结论

### 1. 有效插件 iframe 的身份绑定和来源查询

在当前有效游戏圈 iframe 中，`binduseridentity` 返回业务成功；使用即时得到的来源身份 token 调用帖子列表接口同样返回业务成功并带有列表结构。

结论：来源身份 token 必须由最终稳定的插件上下文绑定取得。它只可存在于会话服务内存，并与 HOME/PLUGIN pair 的 lease 一起失效；每个新 pair 都必须重新绑定。

### 2. 无效外层 URL token

在隔离候选页中使用故意无效的插件路由 token，外层游戏列表请求为 HTTP 成功但业务返回 `ret=200003`，页面出现重新登录/超时标记，且 frame 树没有 `gamemp.weixin.qq.com` iframe。候选页测试后立即关闭，未改变原有受控页面。

结论：无效外层 token 时不能创建可用插件，也不能从旧 iframe 恢复新会话。固定无 token 插件路由不能作为“需要扫码”的唯一证据。

### 3. 候选页失效后旧来源身份 token 的短时只读

在浏览器 Cookie 会话仍有效的条件下，此前在有效 iframe 中即时得到的来源身份 token 可在独立 `gamemp` 上下文完成一次只读帖子查询。

结论：这只证明旧身份 token 可能短时独立于候选页 URL token；它不能补回已经消失的 iframe，也不证明真实 Cookie/登录会话失效后仍可用。因此生产实现不以此作为续接或跨 lease 复用依据。

### 4. 来源身份拒绝类别

在有效插件 iframe 中，已验证查询使用故意篡改的来源身份 token 副本时返回 `errcode=-2`；不存在的绑定目标返回 `errcode=1`，两者均没有外层扫码提示。

结论：只有已验证查询的 `errcode=-2` 可使 ACTIVE lease 丢弃身份 token，并最多在同一验证插件中单飞重绑一次。`errcode=1` 与其他未验证业务码不得被当作扫码、Cookie 失效或直接切换信号。

### 5. 关闭并重新打开受控 Chrome

停止使用共享浏览器的来源 Worker 后，正常终止受控 Chrome（保留独立 profile）再以同一 profile 启动：旧 CDP target/iframe 全部消失；新 HOME 仍能取得外层 token，且没有扫码提示。过程中没有读取、解析或记录二维码。

结论：Chrome 重启会使全部 target 和内存 lease 失效，但不必然使外层浏览器登录态失效。服务必须丢弃旧 browser generation，从新 HOME 重新构建；只有新 HOME 明确扫码或长期无法取得 token 时才提示人工扫码。

### 6. 当前游戏外层回读

在明确、已登录的 `mp.weixin.qq.com` HOME target 中，以浏览器凭据调用绝对地址：

```text
https://gamemp.weixin.qq.com/cgi-bin/gamewxagsettingwap/getappinfo
```

已验证成功条件是 HTTP 200、业务 `errcode=0`，当前 AppID 位于 `data.app_info.appid`。相对路径会落到 `mp.weixin.qq.com` 并返回 404。

结论：这是当前游戏的生产回读契约。回读值仅用于和 `execution_scope.target_appid` 比较，不能写回 `ManagedGame`、`ActiveGameScope` 或 Job checkpoint。

### 7. 外层目录和直接切换

受控页面的静态代码与受控实验均表明：外层目录请求返回可切换 AppID 集合与 `account_switch_permit`；切换请求使用外层运行时 token，提交目标 `appid`，业务成功后 HOME 会重载。测试中从当前游戏直接切到目标游戏后，最终 HOME 能重新读取 token，创建最终插件并完成绑定和只读查询。

结论：`RECOVER` 或当前不在目标游戏的 `KEEPALIVE`，仅允许目录中目标 AppID 唯一存在、平台许可后直接切到目标一次。当前已在目标游戏的 `KEEPALIVE`，从目录/许可内所有非目标游戏中随机选择中转 B，再执行 `A → B → A`。目录缺少目标、中转候选为空、许可缺失、切换失败或每次切换后的回读不一致都不得发布 lease。不得依据切换面板 DOM 或列表顺序做回退。

## 失败边界

| 条件 | 处理 |
| --- | --- |
| 新 HOME 明确扫码或无运行时 token | 直接失败：需要人工扫码。 |
| `RECOVER` 当前 AppID 不一致，或 `KEEPALIVE` 当前不在目标 | 直接切到目标一次，重载后重新读取 token 与当前 AppID。 |
| `KEEPALIVE` 当前已在目标 | 解析受控中转 B，执行 `A → B → A`，每一步重载并回读。 |
| 目录缺少目标/中转、无可选中转或平台不允许切换 | 安全失败，不发布 lease。 |
| CDP 浏览器不可达 | 标记受控 Chrome 不可用，交由既有有限重试。 |
| HOME/iframe 稳定性、协议、切换后回读或最小只读验证失败 | 普通可重试失败，不错误覆盖外层登录态。 |
| 已验证查询返回 `errcode=-2` | 丢弃/重绑 ACTIVE lease 一次；再次失败走恢复。 |
| 人工写操作返回前中断 | 标记待回读确认；绝不自动重放。 |

## 未验证边界

- 真实外层 Cookie/登录会话失效后，旧来源身份 token 的生命周期；
- `account_switch_permit` 缺失或为零的真实上游响应；
- 目录缺少目标或目标重复的真实上游响应；
- 新 HOME 真实扫码页（不在日常运营会话主动退出登录来制造）。

在这些边界上，生产实现必须使用固定安全失败或既有有限重试，不能猜测、不能绕过目录许可，也不能改用页面 DOM。

## 最小验证

此能力涉及受控浏览器和上游身份。发布前最小验证必须脱敏，且不得执行上游写入：

1. 创建新的 HOME，确认其已就绪，可读取外层 token，绝对 `getappinfo` 返回成功；
2. 验证 `RECOVER`：当前游戏不一致时，仅在目标已获授权、目录唯一匹配且平台许可时，执行一次直接切换并确认 HOME 重载后的 token 与 AppID；
3. 验证 `KEEPALIVE`：当前游戏已为目标时，在中转候选非空且平台许可通过时执行 `A → B → A`，并在两次切换后分别确认 HOME 重载、token 与当前 AppID；
4. 创建新的最终插件 target，确认 iframe 连续稳定；
5. 调用 `binduseridentity`，以 `page=0,page_size=1` 执行最小帖子列表查询；
6. 清理未发布 candidate；成功时确认只发布一组 HOME + PLUGIN lease；
7. 不执行帖子处置、禁言、屏蔽或扫码自动化。
