## ADDED Requirements

### Requirement: 唯一可观察受控会话页
当前 session owner SHALL 只维护一个 service-owned 受控会话页，作为外层检查、人工扫码、outer token 读取、游戏切换和 Frame 建立的唯一 HOME。该页可以由明确人工动作激活，但 target ID、完整 URL、认证材料和二维码内容 MUST NOT 输出或持久化。系统不得接管、关闭或以 URL、标题推断任意人工 target 为受控会话页。

#### Scenario: 运营人员查看受控会话页
- **WHEN** 已认证运营人员在控制台请求查看受控会话页
- **THEN** 会话服务 SHALL 激活当前 owner 已登记的唯一页面，或创建并登记一张新页面后激活
- **THEN** 运营人员可在该页面观察登录效果和 Network，重复请求不得创建第二张页面

#### Scenario: 自动检查不抢占焦点
- **WHEN** 自动维护或人工外层检查在已有受控会话页上执行
- **THEN** 系统 SHALL 复用该页面且不得激活、跳转或关闭它
- **THEN** 检查结果 SHALL 与运营人员打开该同一页面时可观察到的页面状态一致

### Requirement: 当前 owner 的 HOME 登记回收
会话服务 SHALL 在创建 HOME 前清理当前 owner 已登记但浏览器中不存在的 target。存在多个当前 owner 空闲 HOME 时，服务 SHALL 只保留一个仍存在且未被 lease 或登录等待占用的受控会话页，并关闭其余可证明归属的空闲 target。

#### Scenario: 重复检查后的 HOME 复用
- **WHEN** 运营人员连续请求外层检查且唯一受控会话页仍存在
- **THEN** 每次检查 SHALL 使用同一页面
- **THEN** 浏览器中不得因重复检查累积新的 service-owned 空闲 HOME

#### Scenario: 受控会话页被关闭
- **WHEN** 运营人员或浏览器关闭当前受控会话页
- **THEN** 服务 SHALL 清除当前 owner 对该页的登记并投影会话未就绪
- **THEN** 服务不得把任何未知人工页面作为替代页面
