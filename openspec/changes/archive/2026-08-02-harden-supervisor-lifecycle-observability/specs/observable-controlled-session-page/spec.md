## ADDED Requirements

### Requirement: 受控会话 target 动作的脱敏可观察性

游戏圈会话服务 SHALL 为 service-owned HOME 与 Plugin/Frame target 的创建、复用、关闭、缺失发现和人工激活发布有界的脱敏动作摘要。每项摘要只可包含时间、目标类别（HOME 或 PLUGIN）、动作类别（创建、复用、关闭、缺失或激活）、固定触发意图（Bootstrap、Repair、Keepalive、人工打开或回收）和结果类别；不得包含 target ID、AppID、URL、页面标题、outer/source token、二维码、Cookie、capability、CDP 请求或页面正文。

该摘要 MUST 与 Supervisor 的 Chrome 进程生命周期事件独立保存和展示。会话服务的两秒维护循环、登录等待、Repair、Keepalive、target 所有权、退役回收和页面焦点语义不得因诊断摘要而改变；诊断不得新增 CDP 调用、创建/激活/关闭额外 target 或触发上游读取/写入。摘要缓冲达到上限时 SHALL 淘汰最旧动作，且不得影响会话可用性。

#### Scenario: Repair 创建会话页面不被误报为 Chrome 重启
- **WHEN** 会话服务以既有 Repair 路径创建或关闭 service-owned HOME 或 Plugin target
- **THEN** 只读追溯 SHALL 记录对应 target 动作、固定触发意图和结果类别
- **THEN** 控制台 SHALL 将其与 Chrome 进程替换分区展示，不得标记为受控 Chrome 重启

#### Scenario: 自动维护不因诊断增加页面操作
- **WHEN** 两秒维护循环执行现有登录观察、Repair、外层探测或退役回收
- **THEN** 动作摘要 SHALL 只描述既有实际 target 操作
- **THEN** 诊断本身不得创建、激活、关闭 target，也不得调用上游接口

#### Scenario: 摘要不暴露会话身份
- **WHEN** 已认证运营人员查看会话 target 动作摘要或读取相关服务日志
- **THEN** 页面和日志 SHALL 仅显示固定动作/原因类别及时间
- **THEN** 不得显示 target ID、AppID、完整 URL、token、Cookie、二维码、capability 或页面内容
