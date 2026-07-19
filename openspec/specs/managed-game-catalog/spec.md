# Purpose

定义可管理游戏目录的本地缓存、受控同步与工作台可用性边界。

## Requirements

### Requirement: 可离线使用的可管理游戏目录
系统 SHALL 本地保存运营账号可管理游戏的 AppID、显示名称和最近成功同步时间，并 SHALL 内置 `wx6d2454a8afcd9706` 为“城主别慌张”。工作台渲染 SHALL 只依赖本地目录；目录同步失败、登录失效或 CDP 不可用时 SHALL 保留最后成功的列表。

#### Scenario: 首次无同步记录
- **WHEN** 本地目录尚未从上游同步
- **THEN** 系统 SHALL 至少显示并可选择“城主别慌张”

#### Scenario: 同步登录失效
- **WHEN** 运营人员请求刷新游戏列表但公众平台会话无效
- **THEN** 系统 SHALL 显示脱敏失败提示且不得清空或覆盖已有游戏目录

### Requirement: 受控的游戏目录同步
系统 SHALL 仅在已认证运营人员显式请求时，通过受控 `mp.weixin.qq.com` 页面调用 `getWxaList`，并仅持久化标准化的 AppID 与游戏名称。系统 SHALL NOT 持久化或输出请求 token、Cookie、完整认证 URL 或运营者身份。

#### Scenario: 成功同步目录
- **WHEN** 受控公众平台页面返回结构有效的可管理游戏列表
- **THEN** 系统 SHALL 创建或更新对应本地游戏记录并显示最近同步时间
