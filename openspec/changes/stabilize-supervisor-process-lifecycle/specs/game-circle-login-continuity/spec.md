## MODIFIED Requirements

### Requirement: 会话所有权与浏览器生命周期

Supervisor SHALL 是受控 Chrome 的唯一进程所有者；`game_circle_session` SHALL 是 CDP、认证材料与 service-owned target 的唯一使用者和登记者。部署 SHALL 保留 `127.0.0.1:9222` 仅供本项目受控 Chrome 使用的约束。Supervisor 运行期间，Chrome 健康 SHALL 由该固定回环 CDP 端点可连接且返回的 `browser_generation` 与当前已确认 generation 一致共同证明；启动器 PID 或业务子 PID 仅可用于诊断或正常关闭的辅助终止，不得单独作为 Chrome 退出、接管或替换的依据。target 所有权 SHALL 仅存在于当前 session owner 内存中；手动打开或无法证明归属的页面不得被接管、关闭或作为来源页面。

每次 Supervisor bootstrap 在启动新 Chrome 前 SHALL 先通过 `127.0.0.1:9222` 的 CDP `Browser.close` 请求关闭浏览器并等待端口释放；端口仍被监听时，才终止该监听 PID 并再次等待。此清理不需要推断监听进程归属，且不得处理该固定端口之外的浏览器或端口；CDP 不可用且无法定位监听 PID、无法终止或释放监听端口时，Supervisor MUST NOT 启动新 Chrome 或 session Worker，并 SHALL 返回固定脱敏失败状态。CDP 请求连接在浏览器退出前断开时，系统 SHALL 以端口是否释放判断关闭结果。Supervisor 异常终止时不要求立即关闭 Chrome。

替换 session Worker 前，Supervisor SHALL 终止当前受控 Chrome 并在新的 browser generation 就绪后启动新 Worker。一次配对替换 SHALL 只创建一个候选 Chrome；候选 generation 未就绪时，后续调和只可等待和核验同一候选，不得反复终止、重建 Chrome 或启动新的 session Worker。候选明确退出或 generation 检查连续失败三次后，Supervisor SHALL 停止自动恢复、记录固定脱敏熔断状态，并要求运营人员显式重启；一次成功 generation 确认 SHALL 清零连续失败计数。

#### Scenario: session Worker 重启

- **WHEN** session Worker 因发布、人工重启或异常恢复而被替换
- **THEN** Supervisor SHALL 先终止当前受控 Chrome 并确认旧 generation 已退出
- **THEN** 新 Worker SHALL 仅在新 generation 中创建自己的 HOME、Frame 和登录等待
- **THEN** 新 Worker MUST NOT 枚举、识别、复用或关闭前任 owner 的 target

#### Scenario: Chrome 启动器 PID 交接但 generation 未变

- **WHEN** 保存的 Chrome 启动器 PID 不再可用，但 `127.0.0.1:9222` 可连接且 CDP `browser_generation` 仍与已确认 generation 一致
- **THEN** Supervisor SHALL 将受控 Chrome 视为健康，不得替换 Chrome 或 session Worker
- **THEN** 它 SHALL 仅更新诊断状态，且不得把 PID 变化记录为浏览器异常退出

#### Scenario: 受控 Chrome 意外退出

- **WHEN** Supervisor 无法确认当前 `127.0.0.1:9222` 的 CDP generation，且游戏圈会话服务仍为 desired
- **THEN** Supervisor SHALL 将其作为会话 owner 更换，启动新 Chrome generation 后替换游戏圈会话服务
- **THEN** 新会话服务 SHALL 为当前操作游戏重新执行 Bootstrap；不得继续投影旧 generation 的 lease 为可用

#### Scenario: Supervisor 异常退出后重新启动

- **WHEN** Supervisor 异常退出且遗留 Chrome 仍监听 `127.0.0.1:9222`，随后新的 Supervisor 完成独占锁获取
- **THEN** 新 Supervisor SHALL 在启动任何新 Chrome 或 session Worker 前先请求 CDP `Browser.close`，端口仍被监听时才终止监听 PID，并等待端口释放
- **THEN** 它 SHALL 创建新 generation 和新 session owner，不得复用遗留 Chrome、其 target 或其来源会话状态

#### Scenario: Browser.close 连接在关闭期间断开

- **WHEN** Supervisor 向 `127.0.0.1:9222` 发送 CDP `Browser.close` 后连接在响应前断开
- **THEN** 它 SHALL 在受限时间内检查该端口是否已释放，释放时 SHALL 将关闭视为成功
- **THEN** 端口仍被监听时才可进入监听 PID 终止兜底，不得立即把连接断开报告为 Chrome 清理失败

#### Scenario: 会话服务重启请求

- **WHEN** 运营人员请求重启游戏圈会话服务
- **THEN** Supervisor SHALL 立即返回已提交状态，并在后台完成 Chrome generation 与 session owner 的替换
- **THEN** 控制客户端 MUST NOT 因该正常替换耗时而把已接受的请求报告为控制通道失败

#### Scenario: Chrome replacement 未在限定时间就绪

- **WHEN** Supervisor 在替换 session owner 时未能确认候选 Chrome generation
- **THEN** Supervisor SHALL 保持控制面和其他服务运行，并记录固定脱敏失败状态
- **THEN** 后续 reconcile SHALL 只重试同一候选 Chrome 的依赖恢复；除非该候选已确认退出，不得重复终止并重开浏览器或启动新的 session Worker
- **THEN** 系统不得启动新的 session Worker 或退出 Supervisor 主循环

#### Scenario: 连续 Chrome 恢复失败熔断

- **WHEN** 候选 Chrome generation 的就绪检查或候选进程退出连续失败三次
- **THEN** Supervisor SHALL 将该配对恢复置为熔断状态，停止自动启动 Chrome 和 session Worker，并报告固定脱敏原因
- **THEN** 运营人员显式重启 Chrome 或会话服务后 SHALL 开始一个新恢复周期；任一成功 generation 确认 SHALL 清零失败计数
