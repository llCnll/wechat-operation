# 部署与首次初始化

本手册说明首次部署、受控 Chrome 登录、外部能力配置及 Worker 启用。已有端点更新代码时，不要按本手册重复初始化，改用[发布与多端升级](release-and-upgrade.md)。

## 前置条件与边界

- 本项目是本机内部工具。基础 Web 不依赖 Chrome、AI、OCR、ASR 或媒体工具；采集、状态回读、游戏目录同步和人工上游处置依赖受控 Chrome 的已登录会话。
- 必须安装 Chrome 或 Chromium，并以 `--remote-debugging-port=9222` 启动独立进程；普通已运行 Chrome 不能事后开启 CDP。
- `uv` 是唯一必须预装的开发工具。项目使用 Python 3.12，`uv sync --python 3.12 --all-groups` 会下载受管解释器。
- 管理员账号只用于本系统登录。操作者在本机交互输入密码；不得生成、读取或记录密码。
- AI、OCR、ASR 均由部署方提供兼容服务地址、密钥和模型。项目不会下载、代管或猜测这些配置；密钥只可写入、不回显。

## 1. 安装 uv 与同步依赖

先执行 `uv --version`。若不可用，按操作系统使用官方安装方式：

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
uv --version
```

```powershell
# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
uv --version
```

受限网络下，若已有 Python，可使用 `python3 -m pip install --user uv`（Windows 使用 `py -m pip install --user uv`）。没有 Python 且官方脚本不可用时，由具备系统权限的操作者安装 `uv`；不要把工具或受管 Python 安装到项目 `runtime/`。

在项目根目录执行：

```bash
uv sync --python 3.12 --all-groups
```

## 2. 初始化运行态与管理员

所有可变运行态均在被 Git 忽略的 `runtime/` 下。首次初始化不会覆盖已有 `.env`、数据库或运行数据。

```bash
# macOS/Linux
mkdir -p runtime
[ -f runtime/.env ] || cp .env.example runtime/.env
uv run python manage.py migrate
uv run python manage.py createsuperuser
```

```powershell
# Windows PowerShell
New-Item -ItemType Directory -Force runtime | Out-Null
if (-not (Test-Path runtime/.env)) { Copy-Item .env.example runtime/.env }
uv run python manage.py migrate
uv run python manage.py createsuperuser
```

## 3. 启动 Supervisor 与验证

Supervisor 是 Web 和全部受托管 Worker 的唯一进程所有者；不要另行以 `runserver` 抢占 8000 端口。

```bash
uv run python -m service_supervisor
```

最小验证：访问 `http://127.0.0.1:8000/healthz`，应返回 `{"status": "ok"}`。后台入口为 `http://127.0.0.1:8000/admin/`。启动失败先检查 `runtime/supervisor/logs/preflight.log`。

## 4. 受控 Chrome 与扫码

受控 Chrome 是 Supervisor 的固定服务，使用 `runtime/controlled-chrome-profile` 独立资料目录、仅本机 `127.0.0.1` CDP 端口；不要手工以命令启动、停止或复用该资料目录。默认会自动查找 Chrome；找不到时，在 `runtime/.env` 设置 `CONTROLLED_CHROME_BIN` 的完整路径，必要时再调整 `CONTROLLED_CHROME_USER_DATA_DIR` 或 `CONTROLLED_CHROME_DEBUG_PORT`。

最小验证：启动 Supervisor 后，在 `/admin/` 确认“受控 Chrome”与“游戏圈会话服务”均运行；再在“受控 Chrome 会话”打开固定登录页并由运营人员手动扫码。不得读取、记录或伪造 Cookie、令牌、二维码或作者身份标识。

## 5. 配置能力并启用 Worker

在 `/admin/` 首页“初始化与运行状态”中：

1. 配置 AI 审核与 OCR；需要语音转写时，再配置视频审核的 ASR。
2. 确认“游戏圈会话服务”正在运行、“受控 Chrome 会话”为有效，随后同步可管理游戏并选择唯一“当前操作游戏”。
3. 保持 `game_circle_session` 服务运行；它是唯一可访问受控 Chrome、创建/回收其自有 target、读取短期会话并调用来源接口的进程。其他 Worker 和 Web 仅经带 Supervisor 注入 capability 的本机固定 IPC 调用它；不得复制 capability 或单独暴露该端口。
4. 依次启动 Scheduler、帖子采集 Worker 与 AI Worker。帖子采集节点会同时启动实时采集和人工补采。
5. 只有完成[视频审核手册](video-review-runbook.md)的预检后，才启用视频审核 Worker。

SQLite 部署中，每类 Worker 只能运行一个实例。页面只控制固定服务，不能执行任意命令。会话续接失败时，只有受控 HOME 明确显示扫码或缺少 token 才需要运营人员重新扫码；关闭并以同一用户目录重开 Chrome 时，系统会先从新 HOME 自动恢复，不会复用旧 target 或 token。`run_game_circle_session_worker` 必须由 Supervisor 启动以取得 IPC capability；其余管理命令仅用于开发或故障诊断。

## 人工与自动上游处置

上游写操作默认关闭。人工处置必须由运营人员在内容管理工作台二次确认；单条有界操作，以及当前游戏内最多 20 条的沉底/屏蔽批量操作，都会经会话服务同步完成写前核验、写入和回读。Web 不会创建、切换或关闭 Chrome target，也不执行身份绑定。会话不可用时页面只触发/复用恢复任务，并要求运营人员稍后重新确认；写请求绝不自动重放。若服务在写请求发出后、返回前中断，审计会标记“待回读确认”，不得再次提交该写操作。人工扫码仍可显式打开固定、无认证登录页，但该入口不读取认证信息或接管会话。AI 自动处置只有在单独完成高风险授权、规则许可与配置后才可运行。运行恢复和最小验证见[运行与恢复手册](operations-runbook.md)。
