## Context

`game_circle_session` 是唯一可访问 CDP、outer token 与来源身份 token 的服务。现有实现把人工登录页、后台 HOME、登录等待和 Frame 候选作为不同 target 处理；手动检查可能在不可见 HOME 中短暂通过 `getappinfo`，而运营人员观察的前台标签已是扫码页。重复检查还会因过期登记优先而创建额外 HOME。`OUTER_READY` 只表达一次 HTTP 成功，不能表达“运营人员正在看的会话页可继续建立 Frame”。

认证材料、完整 URL、target ID、二维码和来源原始响应仍不可输出、持久化或经 IPC 传给 Web。Web 不能直接访问 CDP。所有 target 生命周期仍在 session 服务的 control lane 内串行执行。

## Goals / Non-Goals

**Goals:**

- 为每个 session owner 维护唯一的 service-owned“受控会话页”，它是外层会话、人工扫码、outer token 与 Frame 建立的唯一事实来源。
- 让运营人员能通过固定“查看受控会话页”动作激活同一页面，观察其页面效果与 Network；重复动作只激活或复用该页。
- 将外层验证收紧为同一次页面执行中的 token 前置、`getappinfo` 成功、token 后置与非扫码路由联合结论；并在 Frame 建立前再次验证可读 token。
- 清理当前 owner 已丢失或重复的空闲 HOME 登记，避免重复检查不断新建页面。
- 仅在同一受控会话页完成最小来源读取后发布 Frame；任一步失效都投影为可处理的未就绪状态。

**Non-Goals:**

- 不接管、扫描、关闭或把任意人工新开标签当作会话来源。
- 不显示、存储、审计或返回 token、Cookie、二维码内容、完整 URL、target ID 或原始上游响应。
- 不改变来源 IPC 的固定业务方法边界，不允许 Web 提交 JavaScript、URL 或 CDP target。
- 不承诺在外层会话恢复后自动扫码、自动执行人工写入或自动重放失败写入。

## Decisions

### 1. 一个 owner 只有一个受控会话页

用 `CONTROLLED_SESSION_HOME` 取代可累积的 `HOME_IDLE`/独立人工登录页语义。该 target 由 session owner 内存登记，既可后台创建，也可在运营人员明确请求时激活到前台；“前台”仅是可见性，不改变其服务所有权。

`ensure_controlled_home()` 在 control lane 中先枚举当前 owner 记录，剔除浏览器中已不存在的条目；在仍存在的候选中复用唯一 `CONTROLLED_SESSION_HOME` 或 `LOGIN_PENDING`。若出现多个当前 owner 的空闲候选，只保留一个稳定候选，关闭其余已证明归属且未被 lease 或登录等待使用的 target。绝不依据 URL、标题或 Chrome 枚举结果认领未知人工 target。

替代方案是让所有检查直接使用运营人员任意前台 tab。该方案会让导航、关闭、手动切换游戏和未知页面接管影响 Worker，违反现有所有权边界，因此拒绝。

### 2. 受控会话页是唯一人工观察入口

新增固定 IPC 控制意图 `OPEN_CONTROLLED_SESSION_PAGE`。它只做两件事：获得或创建唯一受控会话页，然后激活该 target。控制台的人工入口在激活成功后 SHALL 立即提交既有固定 `CHECK_LOGIN`；若页面已是扫码页，检查登记登录等待，运营人员在这张页面扫码，维护循环随后恢复；若已经有效，检查立即刷新运行态。控制台将“打开登录页”改为“查看受控会话页”，不暴露 target 标识。

手动“检查外层会话”不再另建页面；它总是使用该页。自动维护仍可后台使用同一页，不会抢占焦点。这样用户在明确打开页面后看到的效果、服务的检查结果和后续 Frame 建立都基于同一上下文。

### 3. 外层验证返回内部稳定凭据，而非一次 HTTP 结果

适配层新增一个仅供 session service 使用的原子验证方法。一次 `Runtime.evaluate` 在同一顶层页面中完成：

1. 读取 URL token，缺失则返回固定未就绪结论；
2. 确认当前不是固定扫码路由；
3. 使用 `credentials: include` 调用固定 `getappinfo`；
4. 校验 HTTP 200、`errcode=0` 与非空 AppID；
5. 再次读取 URL token 和路由，要求 token 仍存在、页面仍非扫码页；
6. 仅把 token 与 AppID 返回给 session service 内存，不写日志、不写数据库、不经 IPC 返回。

在 service 接收到该结果后，Frame 建立还会在同一受控会话页重新读取 token；若失败，投影 `OUTER_SESSION_UNREADY`，不得显示 `OUTER_READY`。完成 `binduseridentity` 和最小只读帖子查询后才发布 ACTIVE lease。外层检查本身不会建立 Frame，但一旦稳定验证失败必须覆盖旧 `OUTER_READY`。

不采用“只延迟数秒后再请求一次”的方案：它会增加网络调用且仍不能保证 token 与请求同一上下文。前后状态在同一个页面执行中校验，再由 Frame 建立的最小读取兜底，边界更明确。

### 4. 状态投影区分明确扫码与会话未就绪

扫码页且有扫码标识投影 `LOGIN_REQUIRED`/`LOGIN_PENDING`；token 缺失、验证期间导航或 token 前后不一致投影 `UNKNOWN + OUTER_SESSION_UNREADY`，并提供“查看受控会话页”和“重新检查”动作。只有稳定验证成功才投影 `OUTER_READY`。后续 Frame 建立遇到 token 失效必须将外层状态降为未就绪，不能保留成功时间作为当前有效证据。

控制台仅展示脱敏状态、检查时间和下一步操作。它不得用 `outer_verified_at` 单独推断当前有效；受控会话页存在且运行态在短时新鲜窗口内才可显示该验证结论。

### 5. 验证与可观察性

单元测试覆盖目标回收、唯一复用、固定激活动作、稳定验证的每一失败分支和 Frame 降级。受控浏览器最小验证必须覆盖：

- 打开受控会话页后，DevTools Network 能观察到同页的 `getappinfo`；
- 页面为扫码页时，检查与 Frame 建立均不误报成功；
- 有效页时，外层检查和 Frame 最小帖子读取均成功；
- 验证后页面跳回扫码页或 token 消失时，运行态立即降级且不调用 `binduseridentity`；
- 重复检查不新增受控 HOME。

## Risks / Trade-offs

- [用户关闭受控会话页] → 仅清理当前 owner 登记并投影未就绪；下次明确打开或需要 Bootstrap 时再创建一张，不接管其他 tab。
- [平台在请求中重定向] → 原子前后检查与 Frame 前复核阻止假阳性；页面稳定性无法由一次 API 响应替代。
- [自动维护与人工观察并发] → 两者继续在 control lane 串行，自动任务不激活页面。
- [多余旧 owner target] → 新 owner 不接管；只回收当前 owner 内存可证明归属的空闲 target。旧 generation 由 Supervisor/Chrome owner 更换处理。
- [人工检查等待较久] → 保留有界等待；控制台将请求中的状态与可观察会话页明确显示，后续可将检查改为异步运行态动作，但本变更不改变控制请求模型。

## Migration Plan

1. 部署迁移前不持久化 target，因此无需数据迁移；新增运行态枚举或动作时执行既有数据库迁移。
2. 重启 `game_circle_session` 与 `web`，由 Supervisor 更换 Chrome generation，旧 target 不复用。
3. 首次在控制台打开受控会话页并完成一次稳定外层检查；若页面提示扫码，由运营人员在该页完成扫码。
4. 建立当前游戏 Frame，确认最小只读查询成功后恢复来源 Worker。
5. 回滚代码时重启 session owner 与 Chrome generation；旧运行态只能显示未知，不能复用新版本内存 token 或 target。

## Open Questions

- 无。受控会话页默认后台创建、仅由明确人工动作激活，兼顾 Worker 不抢焦点与人工可观察性。
