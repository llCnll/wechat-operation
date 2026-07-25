## ADDED Requirements

### Requirement: 系统配置使用共享运营主题

系统配置首页 SHALL 使用认证运营页面的共享主题根属性、导航栏主题控制和基础视觉 token，不得保留仅由系统配置页面初始化或保存的独立主题实现。运行概览、服务控制、配置、追溯和 dialog SHALL 在共享深浅主题中保持既有安全语义、CSRF 表单和固定 Supervisor 控制边界。

#### Scenario: 从运营分析进入系统配置
- **WHEN** 运营人员已在运营分析选择深色主题并打开系统配置
- **THEN** 系统配置 SHALL 使用相同主题偏好展示运行控制台
- **THEN** 页面不得因读取主题偏好而控制服务、创建 Job 或调用 CDP
