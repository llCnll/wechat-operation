## ADDED Requirements

### Requirement: 受控游戏圈连接的可观察处置入口
运行控制台 SHALL 在受控游戏圈连接节点提供唯一的“查看受控会话页”入口，以及既有的外层检查与建立 Frame 入口。该页面是会话服务登记的唯一受控会话页；控制台不得暗示任意人工浏览器标签可作为会话事实来源，也不得展示 target ID、完整 URL、Cookie 或 token。

#### Scenario: 会话页显示扫码
- **WHEN** 运行态表明受控会话页需要人工登录
- **THEN** 控制台 SHALL 推荐查看受控会话页并在该页扫码
- **THEN** 外层检查与 Frame 建立结果 SHALL 以该同一页面为准

#### Scenario: 查看会话页后刷新状态
- **WHEN** 运营人员提交查看受控会话页动作
- **THEN** 控制台 SHALL 先激活该页面，再通过固定 `CHECK_LOGIN` 在该同一页面刷新外层会话运行态
- **THEN** 已登录页面 SHALL 更新为有效，扫码页面 SHALL 登记登录等待，且不得创建第二张会话页

#### Scenario: 会话验证未就绪
- **WHEN** 页面 token 缺失、验证中导航或 Frame 前 token 复读失败
- **THEN** 控制台 SHALL 显示固定的会话未就绪结果，不得继续显示会话有效
- **THEN** 控制台 SHALL 提供重新检查和查看受控会话页的可处理入口
