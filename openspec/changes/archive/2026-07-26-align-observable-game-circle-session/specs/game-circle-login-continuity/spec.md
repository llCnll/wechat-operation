## MODIFIED Requirements

### Requirement: 人工登录页与脱敏边界
系统 SHALL 最多维护一个当前 owner 的受控会话页；该页同时承担人工登录等待与外层会话观察。仅运营人员明确请求“查看受控会话页”时系统 MAY 创建或激活该页；重复请求 SHALL 复用页面且不得创建或聚焦第二页。系统不得持久化或输出 target ID、认证材料、完整 URL、二维码、账号身份或原始来源响应。手动外层检查、outer token 读取和 Frame 建立 SHALL 复用该受控会话页，不得另建不可观察 HOME。

#### Scenario: 扫码完成
- **WHEN** 已登记受控会话页的外层会话变为有效
- **THEN** 系统 SHALL 复用该 HOME 建立当前游戏 Frame
- **THEN** 只有最小只读验证成功后才可发布 ACTIVE lease

#### Scenario: 登录等待结束
- **WHEN** 登录窗口到期、已登记 target 丢失或 Chrome generation 改变
- **THEN** 系统 SHALL 清除等待状态并仅关闭该 owner 的未发布 target
- **THEN** 系统 MUST NOT 自动创建下一张登录页或接管未知 tab

#### Scenario: 运营人员观察会话请求
- **WHEN** 运营人员激活受控会话页后提交外层检查
- **THEN** `getappinfo` SHALL 在该同一页面执行
- **THEN** 系统不得将另一张后台 HOME 的结果作为该页面的会话结论

### Requirement: 外层登录态验证与 Frame lease 边界
系统 SHALL 仅在当前 owner 的受控会话页于同一次稳定验证中满足以下全部条件时投影外层会话有效：页面不是固定扫码页、验证前后 URL token 均可读取、`getappinfo` 返回 HTTP 200、`errcode=0` 且带非空 AppID。URL token、页面可打开、target 存在或一次孤立 HTTP 成功均不得单独证明登录有效。Frame 建立 SHALL 在调用 `binduseridentity` 前再次从同一受控会话页读取 outer token；该读取失败、token 消失或页面变为扫码页时，系统 SHALL 投影外层会话未就绪和 Frame 未就绪，且不得保留 `OUTER_READY`。已验证的登录态过期 SHALL 投影为需要重新登录和 Frame 未就绪，并使同一 browser generation 的 ACTIVE lease 失去新 access 资格；未知或暂时错误不得误报扫码或废弃 lease。

正常 outer token 刷新不得废弃已发布 Frame lease；新 lease 发布后，旧 lease 仅按访问计数和退役延迟关闭。外层仍有效时的来源身份拒绝或 Frame target 丢失 SHALL 只废弃对应 lease，不得反推外层登录态过期。

#### Scenario: URL token 尚在但登录态过期
- **WHEN** HOME URL 仍含 token，但 `getappinfo` 返回已验证认证拒绝
- **THEN** 系统 SHALL 显示需要重新登录和当前 Frame 未就绪，并只提供人工打开登录页动作
- **THEN** 同 generation 的新来源 access 不得使用旧 token 请求上游

#### Scenario: 保活刷新 outer token
- **WHEN** Keepalive 获得新的 outer token，且 `getappinfo` 持续成功
- **THEN** 系统 MUST NOT 因 token 值变化拒绝旧 Frame lease
- **THEN** 旧 lease SHALL 仅按既有退役规则关闭

#### Scenario: 验证后会话页跳回扫码页
- **WHEN** `getappinfo` 返回成功后，同一受控会话页的 token 消失、路由变为扫码页或 Frame 前 token 复读失败
- **THEN** 系统 SHALL 覆盖此前成功投影为外层会话未就绪或需要登录，并将 Frame 投影为未就绪
- **THEN** 系统 MUST NOT 调用 `binduseridentity`、发布 lease 或执行来源帖子查询

#### Scenario: Frame 最小读取验证
- **WHEN** 同一受控会话页完成稳定外层验证且存在当前操作游戏
- **THEN** 系统 SHALL 使用仅存在于会话服务内存中的 outer token 建立插件 Frame、绑定来源身份并完成最小只读帖子查询
- **THEN** 只有该查询成功后才可将当前 scope 的 Frame 投影为有效
