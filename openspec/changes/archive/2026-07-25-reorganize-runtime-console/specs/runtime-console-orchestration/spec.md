## ADDED Requirements

### Requirement: 单一运行快照与双视图投影
系统 SHALL 在一次系统配置首页请求中生成不含认证材料、来源响应、AppID、target ID、URL、Cookie、token、Job 参数或异常堆栈的单一运行快照。该快照 SHALL 包含固定服务状态、Supervisor 控制可达性、当前操作游戏是否存在、外层会话状态、当前游戏 Frame 状态及其本地范围版本匹配结果、审核配置与策略状态、可选能力状态及统一检查时间。

系统 SHALL 仅从该快照派生“按运行流程”和“按服务管理”两种视图以及控制台可用、来源可用、核心审核可用三个等级；模板与浏览器端不得重新推断服务依赖或就绪等级。

#### Scenario: 两个视图读取同一状态
- **WHEN** 已登录运营人员访问系统配置首页并在两个视图间切换
- **THEN** 两个视图 SHALL 显示同一检查周期内的同一服务、会话、范围和能力状态
- **THEN** 系统不得因视图切换创建 Job、调用 CDP、请求 Supervisor 控制或改变运行配置

#### Scenario: 状态暂不可用
- **WHEN** Supervisor 控制通道或某项运行状态在页面请求中不可用
- **THEN** 系统 SHALL 显示固定脱敏的“暂不可用”状态和可处理的恢复方向
- **THEN** 系统不得将未知、过期或外层会话有效状态推断为当前游戏 Frame 有效

#### Scenario: 陈旧会话投影
- **WHEN** 游戏圈运行态的 `checked_at` 超过十分钟，或受控 Chrome、游戏圈会话服务任一未运行
- **THEN** 系统 SHALL 将外层会话与 Frame 投影为未知或未就绪，并将受控游戏圈连接作为流程阻塞节点
- **THEN** 系统不得以持久化的 `OUTER_READY` 或 `READY` 继续显示来源可用或核心审核可用

### Requirement: 运行流程视图的依赖与推荐动作
系统 SHALL 将运行流程固定展示为控制台与 Supervisor、受控游戏圈连接、当前操作游戏、当前游戏 Frame、审核能力配置、核心后台服务六个有序节点。流程节点 SHALL 反映运营能力依赖，不得按 Python 模块、服务注册顺序或可选能力顺序排列。

受控游戏圈连接 SHALL 同时覆盖受控 Chrome 运行、游戏圈会话服务运行和未超过十分钟有效期的外层会话有效；当前操作游戏 SHALL 只覆盖目录与全局业务范围；当前游戏 Frame SHALL 仅在当前 scope 存在经最小只读验证发布的 ACTIVE lease、运行态中的 `frame_scope_version` 等于当前 `ActiveGameScope.scope_version` 且 Frame 投影未过期时就绪。审核能力配置 SHALL 覆盖完整 AI 配置与唯一启用且含规则的审核策略；核心后台服务 SHALL 覆盖 Scheduler、实时采集、人工补采和 AI 审核 Worker。

流程 SHALL 突出第一个未满足的必需节点及其唯一推荐动作。可选能力未配置、可选 Worker 停止、指标快照 Worker 停止或禁言维护 Worker 停止不得阻塞来源可用或核心审核可用。

#### Scenario: 外层会话有效但没有当前游戏
- **WHEN** 受控 Chrome 和外层会话有效，但不存在当前操作游戏
- **THEN** 流程 SHALL 将受控游戏圈连接显示为完成，将当前操作游戏显示为第一个阻塞节点
- **THEN** 来源可用和核心审核可用 SHALL 均为未就绪，且流程不得把 Frame 显示为有效

#### Scenario: 当前游戏没有有效 Frame
- **WHEN** 已选择当前操作游戏且外层会话有效，但当前 scope 没有 ACTIVE lease
- **THEN** 流程 SHALL 将当前游戏 Frame 显示为第一个阻塞节点，并提供建立当前游戏 Frame 的唯一恢复入口
- **THEN** 系统不得通过“外层会话有效”或旧 scope 的 lease 把当前 Frame 显示为有效

#### Scenario: 切换范围后旧 lease 完成
- **WHEN** 运营人员切换当前操作游戏后，已冻结旧 scope 的保活或恢复在后台完成
- **THEN** 系统 SHALL 仅当其 `frame_scope_version` 匹配新当前 scope 时才显示当前游戏 Frame 有效
- **THEN** 旧 scope 的完成结果不得使新范围的来源可用或核心审核可用变为有效

#### Scenario: 可选能力保持关闭
- **WHEN** OCR、视频审核、人工处置授权、AI 自动处置或禁言维护未配置、未授权或未运行
- **THEN** 流程 SHALL 将其作为可选能力摘要展示
- **THEN** 系统不得降低已满足来源可用或核心审核可用的等级

### Requirement: 服务管理视图的职责、前置条件和控制边界
系统 SHALL 在服务管理视图按基础托管、游戏圈访问、调度与业务 Worker、可选能力分组展示固定服务。每项服务或服务组 SHALL 分别显示中文名称、用途、生命周期状态、业务可处理条件、当前缺失条件、缺失后的固定行为和既有控制/日志/配置入口。业务条件缺失不得改变服务是否可启动、停止或重启的固定生命周期语义。

基础托管 SHALL 包含 Web 与 Supervisor 健康；游戏圈访问 SHALL 将受控 Chrome 与游戏圈会话服务作为同一依赖组展示，并分别展示 Chrome、外层会话和 Frame 状态；调度与业务 Worker SHALL 包含 Scheduler、帖子采集（实时采集和人工补采子服务）、指标快照、AI 审核和禁言维护；可选能力 SHALL 包含 OCR、视频审核、人工处置授权和 AI 自动处置。

当前操作游戏 SHALL 在两个视图共用的工作范围区域展示，不得作为服务、Worker 或会话维护配置。自动任务生产者 SHALL 保持独立的规则管理区域，服务视图只能链接至该区域，不得复制规则编辑表单。

#### Scenario: 查看游戏圈访问服务组
- **WHEN** 运营人员打开服务管理视图中的游戏圈访问组
- **THEN** 系统 SHALL 分别展示 Chrome、外层会话和当前 Frame 的状态与缺失原因
- **THEN** 会话服务控制 SHALL 使用既有固定 Supervisor 操作，且页面不得提供独立受控 Chrome 生命周期入口、任意 CDP 命令或浏览器页面管理能力

#### Scenario: 查看 Worker 的运行前提
- **WHEN** 运营人员查看帖子采集、指标快照、AI 审核或禁言维护服务
- **THEN** 系统 SHALL 将 Supervisor 生命周期状态与当前范围、Frame、AI/策略或具体写入 Job 授权等业务可处理条件分开显示
- **THEN** Scheduler 无启用规则、来源 Worker 暂无当前范围或 Frame、AI Worker 暂无 AI/策略配置时，系统 SHALL 显示对应业务条件缺失但不得表述为该服务不能启动
- **THEN** 禁言维护 Worker SHALL 将写入授权仅作为续期 Job 的业务条件，不得据此阻止只读状态刷新 Job 的消费
- **THEN** 运行前提展示不得泄露认证材料、作者身份、来源请求或 Job 参数

### Requirement: 运行控制入口唯一归属
系统 SHALL 为每个可变运行操作保留唯一主入口：登录页与外层会话检查归受控游戏圈连接；目录同步和当前操作游戏切换归工作范围；建立 Frame、手动保活和自动修复配置归游戏圈会话服务；服务生命周期控制和日志归服务管理视图；调度规则归自动任务生产者。

流程视图可以链接至相应入口，但 MUST NOT 重复渲染同一 POST 表单、配置开关或服务控制按钮。所有既有变更操作 SHALL 保持认证、CSRF、固定路由和 Supervisor 白名单约束。

#### Scenario: 流程提示重启后台服务
- **WHEN** 流程中的核心后台服务节点成为第一个阻塞节点
- **THEN** 系统 SHALL 将运营人员引导到服务管理视图中对应服务卡
- **THEN** 流程节点不得复制启动、停止、重启或日志操作

#### Scenario: 运营人员从服务视图执行固定操作
- **WHEN** 已认证运营人员在服务视图使用启动、停止、重启、日志或配置入口
- **THEN** 系统 SHALL 复用既有受 CSRF 保护的 Django 固定路由和 Supervisor 代理
- **THEN** 系统不得接受任意服务名、进程命令、路径、PID、浏览器 target 或认证材料
