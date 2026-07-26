## Why

当前外层会话检查在会话服务后台创建的 HOME target 中执行，而运营人员观察和扫码处理的是另一张前台页面。两个页面的登录上下文可能不同，导致后台 `getappinfo` 短暂成功时系统显示“会话有效”，运营人员前台却已显示扫码页，随后 Frame 建立也会失败。运行态因而不能可靠指导人工处置，也无法让运营人员在同一页面的 Network 中核验请求。

## What Changes

- 将当前 owner 的唯一 service-owned HOME 定义为“受控会话页”：它仍由会话服务登记和管理，但可由明确人工动作激活并供运营人员观察。
- 外层检查、扫码等待、读取 outer token、切换游戏及 Frame 建立必须复用该受控会话页；不得为重复检查积累额外的空闲 HOME。
- 将外层会话有效条件收紧为同一受控会话页上的稳定联合验证：页面不是扫码页、outer token 可读取、`getappinfo` 成功，并且后续 Frame 建立可继续使用该 token。
- 若受控会话页已显示扫码、缺少 token、在验证窗口内导航到扫码页或 token 在后续读取时消失，运行态必须显示需要人工登录或会话未就绪，不能保留 `OUTER_READY`。
- 控制台明确将“打开/查看受控会话页”作为人工登录与请求观察的唯一入口；不接管任意人工新开页面。
- 清理当前 owner 已丢失的 target 登记，并在存在多个遗留空闲 HOME 时仅保留一个可观察受控会话页。

## Capabilities

### New Capabilities

- `observable-controlled-session-page`: 定义运营人员可观察、但仍由会话服务独占管理的受控会话页及其登录处置入口。

### Modified Capabilities

- `game-circle-login-continuity`: 收紧外层会话事实来源、HOME 复用和稳定验证要求。
- `runtime-console-orchestration`: 将游戏圈连接节点的人工操作与状态说明绑定到唯一受控会话页。

## Impact

- 影响 `config/game_circle_service.py`、`config/game_circle_adapter.py`、固定 IPC 控制意图、运行态投影、系统配置控制台和会话相关测试。
- 不新增外部依赖，不输出或持久化 target ID、Cookie、二维码、outer token 或来源身份 token。
- 需要在受控 Chrome 中验证：扫码页、有效 HOME、验证后跳回扫码页、重复检查、Frame 绑定与只读帖子查询。
