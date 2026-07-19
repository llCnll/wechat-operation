## ADDED Requirements

### Requirement: 公众平台只读页面适配器
系统 SHALL 将公众平台外层页面的只读调用隔离在专用 CDP 适配器中。适配器 SHALL 在当前受控 `mp.weixin.qq.com` 页面上下文内临时使用会话并返回标准化 DTO；它 SHALL NOT 供 scheduler、Worker 或任何上游写操作调用。

#### Scenario: 外层页面不可用
- **WHEN** 未找到已登录的受控公众平台页面或其响应结构无效
- **THEN** 适配器 SHALL 返回脱敏的可处理错误，调用方不得持久化认证上下文
