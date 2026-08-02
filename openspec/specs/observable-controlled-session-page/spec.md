# Purpose

定义受控游戏圈会话页的唯一所有权、人工可观察边界和回收规则。

## Requirements

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

### Requirement: 受控会话 target 动作的脱敏可观察性

游戏圈会话服务 SHALL 为 service-owned HOME 与 Plugin/Frame target 的创建、复用、关闭、缺失发现和人工激活发布有界的脱敏动作摘要。每项摘要仅包含时间、目标类别（HOME 或 PLUGIN）、动作类别（创建、复用、关闭、缺失或激活）、固定触发意图（Bootstrap、Repair、Keepalive、人工打开或回收）和结果类别；不得包含 target ID、AppID、URL、页面标题、outer/source token、二维码、Cookie、capability、CDP 请求或页面正文。

该摘要 MUST 与 Supervisor 的 Chrome 进程生命周期事件独立保存和展示。会话服务的两秒维护循环、登录等待、Repair、Keepalive、target 所有权、退役回收和页面焦点语义不得因诊断摘要而改变；诊断不得新增 CDP 调用、创建/激活/关闭额外 target 或触发上游读取/写入。摘要缓冲达到上限时 SHALL 淘汰最旧动作，且不得影响会话可用性。

#### Scenario: Repair 创建会话页面不被误报为 Chrome 重启
- **WHEN** 会话服务以既有 Repair 路径创建或关闭 service-owned HOME 或 Plugin target
- **THEN** 只读追溯 SHALL 记录对应 target 动作、固定触发意图和结果类别
- **THEN** 控制台 SHALL 将其与 Chrome 进程替换分区展示，不得标记为受控 Chrome 重启

#### Scenario: 自动维护不因诊断增加页面操作
- **WHEN** 两秒维护循环执行现有登录观察、Repair、外层探测或退役回收
- **THEN** 动作摘要 SHALL 只描述既有实际 target 操作
- **THEN** 诊断本身不得创建、激活、关闭 target，也不得调用上游接口
