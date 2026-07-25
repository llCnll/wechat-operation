## ADDED Requirements

### Requirement: 会话、工作范围和 Frame 的运行控制台归属
系统配置运行控制台 SHALL 将受控 Chrome、游戏圈会话服务和外层会话作为“受控游戏圈连接”的同一依赖链投影，并将当前操作游戏作为独立的全局工作范围投影。游戏圈运行态 SHALL 保存可空的本地 `frame_scope_version`；会话服务仅在该版本的 lease 经最小只读验证后写入并投影 Frame `READY`，其余 Frame 状态 SHALL 清空该版本。当前游戏 Frame SHALL 作为工作范围对应的来源能力结果单独显示，且仅当 `frame_scope_version` 等于当前 `ActiveGameScope.scope_version` 并且会话投影未超过十分钟有效期时有效；外层会话有效、会话服务运行或旧 scope 的 lease 均不得替代当前 Frame 有效。

登录页与外层会话检查 SHALL 仅归属受控游戏圈连接；目录同步和当前操作游戏切换 SHALL 仅归属工作范围；建立当前游戏 Frame、手动保活和自动维护配置 SHALL 仅归属游戏圈会话服务。流程视图不得重复这些操作入口，且不得新增对受控 Chrome、CDP target 或 lease 的直接控制。

#### Scenario: 登录后恢复当前范围
- **WHEN** 运营人员在受控游戏圈连接中完成扫码且存在当前操作游戏
- **THEN** 系统 SHALL 复用既有登录后的 Bootstrap 建立当前范围 Frame，并在最小只读验证成功后显示 Frame 有效
- **THEN** 系统不得要求运营人员在当前操作游戏区域重复打开登录页或手动管理浏览器页面

#### Scenario: 切换当前操作游戏
- **WHEN** 运营人员在工作范围区域切换到目录中的另一游戏
- **THEN** 系统 SHALL 使用既有范围切换和建立 Frame 能力，并将新范围的 Frame 状态独立投影
- **THEN** 已冻结 scope 的既有 Job、受控 Chrome 所有权和会话服务控制语义不得改变

#### Scenario: Frame 未就绪时恢复
- **WHEN** 当前操作游戏已选择但其 Frame 未就绪
- **THEN** 系统 SHALL 在流程和游戏圈会话服务中引导运营人员使用既有建立当前游戏 Frame 能力
- **THEN** 系统不得把会话检查、目录同步、保活或受控 Chrome 直接控制伪装为建立 Frame 的等价操作

#### Scenario: 旧范围的会话操作晚于范围切换完成
- **WHEN** 旧范围的保活、Repair 或恢复在运营人员切换当前操作游戏后才发布结果
- **THEN** 会话服务 SHALL 保留该结果的范围版本，控制台 SHALL 因版本不匹配把当前 Frame 显示为未就绪
- **THEN** 系统不得因为该旧范围结果重新显示当前范围的 Frame 有效
