# Purpose

定义可管理游戏目录的本地缓存、受控同步与工作台可用性边界。

## Requirements

### Requirement: 可离线使用的可管理游戏目录
系统 SHALL 本地保存运营账号可管理游戏的 AppID、显示名称和最近成功同步时间。工作台渲染 SHALL 只依赖本地目录；目录同步失败、登录失效或 CDP 不可用时 SHALL 保留最后成功的列表。

#### Scenario: 首次无同步记录
- **WHEN** 本地目录尚未从上游同步
- **THEN** 系统 SHALL 显示尚未同步状态，不得以硬编码业务游戏伪造目录

#### Scenario: 同步登录失效
- **WHEN** 运营人员请求刷新游戏列表但公众平台会话无效
- **THEN** 系统 SHALL 显示脱敏失败提示且不得清空或覆盖已有游戏目录

### Requirement: 受控的游戏目录同步
系统 SHALL 仅在已认证运营人员显式请求时，通过会话服务创建的 owned candidate 调用 `getWxaList`，并仅持久化标准化的 AppID 与游戏名称。目录同步 MUST NOT 创建插件页、刷新浏览器会话、调用身份绑定或改变当前操作游戏（首次默认初始化除外）。系统 SHALL NOT 持久化或输出请求 token、Cookie、完整认证 URL 或运营者身份。

#### Scenario: 成功同步目录
- **WHEN** 受控公众平台页面返回结构有效的可管理游戏列表
- **THEN** 系统 SHALL 创建或更新对应本地游戏记录并显示最近同步时间

### Requirement: 持久目录与会话范围分离

续接协调器 SHALL 在已验证 HOME 中以外层当前项回读校验 execution scope 的 target AppID。回读结果只存在于本次调用；不得更新 `ManagedGame`、`ActiveGameScope` 或任何 Job checkpoint。目录条目不代表当前外层游戏；稳定当前项必须由独立、已受控验证的回读契约给出。

不一致时，会话服务只可确认目录中唯一包含 execution scope target 且平台明确许可后直接切换。`KEEPALIVE` 当前已为目标时从所有非目标、平台许可的目录项中随机选择受控中转；候选必须去重和稳定排序，不得使用展示字段、username、列表位置或切换面板，亦不得按上游列表顺序选择中转。

#### Scenario: 外层当前游戏与范围不一致但目标可切换
- **WHEN** 刷新 HOME 回读的当前 AppID 不等于 Job 的 `execution_scope.target_appid`，且目标唯一存在并允许切换
- **THEN** 会话服务 SHALL 对 target 执行一次直接切换，随后重新读取 token 和当前 AppID
- **THEN** 不得修改目录、当前操作游戏或 Job 快照

#### Scenario: 外层当前游戏与范围不一致且目标不可切换
- **WHEN** 外层目录缺少 target、target 不唯一或平台不允许切换
- **THEN** 会话服务 SHALL 安全失败且不得发布 lease，也不得使用页面展示字段、列表位置或其他账号作为回退
