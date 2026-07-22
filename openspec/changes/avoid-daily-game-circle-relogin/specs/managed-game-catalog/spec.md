## MODIFIED Requirements

### Requirement: 持久目录与会话范围分离

“可管理游戏”目录 SHALL 仅由已认证运营人员显式同步，并持久化本地业务所需的 AppID、游戏名称和同步时间。目录同步 MUST NOT 创建插件页、刷新浏览器会话、调用身份绑定或改变当前操作游戏（首次默认初始化除外）。

续接协调器 SHALL 在已验证 HOME 中以外层当前项回读校验 execution scope 的 target AppID。回读结果只存在于本次调用；不得更新 `ManagedGame`、`ActiveGameScope` 或任何 Job checkpoint。

目录条目不代表当前外层游戏。稳定当前项必须由独立、已受控验证的回读契约给出；不一致时，会话服务只可确认目录中唯一包含 execution scope target 且平台明确许可后直接切换。`KEEPALIVE` 当前已为目标时从所有非目标、平台许可的目录项中随机选择受控中转；候选必须去重和稳定排序，不得使用展示字段、username、列表位置或切换面板，亦不得按上游列表顺序选择中转。

#### Scenario: 外层当前游戏与范围不一致但目标可切换
- **WHEN** 刷新 HOME 回读的当前 AppID 不等于 Job 的 `execution_scope.target_appid`
- **AND** 外层目录唯一包含 target，且平台允许切换
- **THEN** 会话服务 SHALL 对 target 执行一次直接切换，随后重新读取 token 和当前 AppID
- **THEN** 不得修改目录、当前操作游戏或 Job 快照

#### Scenario: 外层当前游戏与范围不一致且目标不可切换
- **WHEN** 外层目录缺少 target、target 不唯一或平台不允许切换
- **THEN** 会话服务 SHALL 安全失败且不得发布 lease
- **THEN** 不得使用页面展示字段、列表位置或其他账号作为回退
