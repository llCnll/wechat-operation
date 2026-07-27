## MODIFIED Requirements

### Requirement: 受控游戏圈连接的可观察处置入口

运行控制台 SHALL 在受控游戏圈连接节点提供唯一的“查看受控会话页”入口，以及既有的外层检查与建立 Frame 入口。该页面是会话服务登记的唯一受控会话页；控制台不得暗示任意人工浏览器标签可作为会话事实来源，也不得展示 target ID、完整 URL、Cookie 或 token。

“查看受控会话页” SHALL 只提交固定的打开并执行共享外层观察动作；控制台不得在该 HTTP 操作中紧接提交独立 `CHECK_LOGIN`。反馈 SHALL 说明受控会话页已打开，并根据服务返回的脱敏运行态提示运营人员扫码、等待登录恢复或刷新状态；不得把仅完成页面激活表述为“已完成状态检查”。当前存在 ACTIVE lease 时，该入口 SHALL 保持现有会话与 Frame 状态；不存在 ACTIVE lease 且观察为无可用 token 时，入口 SHALL 使控制台显示 `LOGIN_PENDING`。显式外层检查 SHALL 复用会话服务已登记的同一页面，并遵守游戏圈会话连续性对 `LOGIN_PENDING` 的等待规则。

会话服务卡 SHALL 仅渲染运行态 `available_actions` 明确允许的检查外层、建立当前游戏 Frame 与保活操作。`LOGIN_PENDING` 时卡片只可提供查看受控会话页和不改变会话状态的服务管理操作；不得渲染可触发 Bootstrap 的操作按钮。

控制台处理固定检查或建立 Frame 回执时，若结果为 `LOGIN_PENDING`，SHALL 明确说明正在等待扫码且动作未执行；若结果为 `CONTROL_NOT_COMPLETED`，SHALL 明确说明动作未完成并引导运营人员查看运行态。控制台不得将这两种结果显示为成功。

#### Scenario: 会话页显示扫码

- **WHEN** 运行态表明受控会话页需要人工登录或正在 `LOGIN_PENDING`
- **THEN** 控制台 SHALL 推荐查看受控会话页并在该页扫码
- **THEN** 外层检查与 Frame 建立结果 SHALL 以该同一页面为准

#### Scenario: 查看会话页后登记等待

- **WHEN** 运营人员提交查看受控会话页动作
- **THEN** 控制台 SHALL 激活并由会话服务登记该同一页面的登录等待，不得紧接提交独立 `CHECK_LOGIN`
- **THEN** 页面尚未通过外层验证时，控制台 SHALL 显示等待扫码或等待登录恢复的固定脱敏状态，且不得创建第二张会话页

#### Scenario: 会话验证未就绪

- **WHEN** 已登记会话页 token 缺失、验证中导航或 Frame 前 token 复读失败
- **THEN** 控制台 SHALL 显示固定的会话未就绪或登录等待结果，不得继续显示会话有效
- **THEN** 控制台 SHALL 提供刷新状态和查看受控会话页的可处理入口

#### Scenario: 扫码等待时的会话服务卡

- **WHEN** 服务投影为 `LOGIN_PENDING`，且 `available_actions` 仅包含查看受控会话页
- **THEN** 控制台 SHALL 隐藏检查外层、建立 Frame 与保活按钮
