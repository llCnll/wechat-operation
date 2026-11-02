# 部署与首次初始化

本手册说明首次部署、受控 Chrome 登录、外部能力配置及 Worker 启用。已有端点更新代码时，不要按本手册重复初始化，改用[发布与多端升级](release-and-upgrade.md)。

## 前置条件与边界

- 本项目在运营者本机运行，Web 仅监听 loopback。部署机必须安装 Chrome 或 Chromium；Supervisor 会用独立资料目录和 CDP 端口启动受控进程，不要手工启动或复用日常 Chrome。AI、OCR、ASR 和媒体工具只在启用相应能力时需要。
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

所有可变运行态均在被 Git 忽略的 `runtime/` 下。首次初始化不会覆盖已有 `.env`、数据库或运行数据。`.env.example` 列出部署设置与安全默认值；先复制模板，再为此实例生成唯一 Django 密钥：

```bash
# macOS/Linux
mkdir -p runtime
[ -f runtime/.env ] || cp .env.example runtime/.env
uv run python -c 'import secrets; print(secrets.token_urlsafe(50))'
# 将生成结果填入 runtime/.env 的 DJANGO_SECRET_KEY 后再执行 Django 命令。
uv run python manage.py migrate
uv run python manage.py createsuperuser
```

```powershell
# Windows PowerShell
New-Item -ItemType Directory -Force runtime | Out-Null
if (-not (Test-Path runtime/.env)) { Copy-Item .env.example runtime/.env }
uv run python -c "import secrets; print(secrets.token_urlsafe(50))"
# 将生成结果填入 runtime/.env 的 DJANGO_SECRET_KEY 后再执行 Django 命令。
uv run python manage.py migrate
uv run python manage.py createsuperuser
```

## 3. 启动 Supervisor 与验证

Supervisor 是 Web 和全部受托管 Worker 的唯一进程所有者；不要另行以 `runserver` 抢占配置的 Web 端口。默认端口为 `WEB_PORT=8000`，Web 仅绑定 `127.0.0.1`。

```bash
uv run python -m service_supervisor
```

最小验证：访问 `http://127.0.0.1:<WEB_PORT>/healthz`，应返回 `{"status": "ok"}`（默认端口为 8000）。后台入口为 `http://127.0.0.1:<WEB_PORT>/admin/`。启动失败先检查 `runtime/supervisor/logs/preflight.log`。

首次启动时，Supervisor 默认启动 Web、受控 Chrome、禁言维护 Worker 和舆情报告 Worker。游戏圈会话服务、Scheduler、采集 Worker、AI 审核 Worker 与视频审核 Worker 不自动启动。新数据库中的帖子采集、日/周指标和永久禁言续期生产者规则默认启用；启动 Scheduler 前先检查并暂停不需要的规则。

## 4. 受控 Chrome 与扫码

受控 Chrome 是 Supervisor 的固定服务，使用 `runtime/controlled-chrome-profile` 独立资料目录、仅本机 `127.0.0.1` CDP 端口；不要手工以命令启动、停止或复用该资料目录。默认会自动查找 Chrome；找不到时，在 `runtime/.env` 设置 `CONTROLLED_CHROME_BIN` 的完整路径，必要时再调整 `CONTROLLED_CHROME_USER_DATA_DIR` 或 `CONTROLLED_CHROME_DEBUG_PORT`。

最小验证：启动 Supervisor 后，在 `/admin/` 确认受控 Chrome 正在运行。不要手工接管其进程或独立资料目录。

## 5. 配置能力并启用 Worker

在 `/admin/` 首页“初始化与运行状态”中：

1. 启动“游戏圈会话服务”，打开受控会话页中的固定登录页并手动扫码；确认外层会话就绪。登录成功后同步授权游戏目录，再由管理员选择唯一“当前操作游戏”。新安装没有预置游戏，选择后系统会建立并验证对应 Frame；确认当前 Frame 就绪后再继续。
2. 保持 `game_circle_session` 服务运行；它是唯一可访问受控 Chrome、创建/回收其自有 target、读取短期会话并调用来源接口的进程。其他 Worker 和 Web 仅经带 Supervisor 注入 capability 的本机固定 IPC 调用它；不得复制 capability 或单独暴露该端口。
3. 如需 AI 审核，配置 AI 接口；OCR 可单独配置，需要视频语音转写时再配置 ASR。按页面状态启动或重启 AI 审核 Worker。
4. 如需自动采集，先检查“自动任务生产者”中的启用规则，再启动 Scheduler 和帖子采集 Worker。帖子采集节点会同时启动实时采集和人工补采；当前操作游戏及 Frame 未就绪时不要启动采集。
5. 只有完成[视频审核手册](../../40-reviews/runbooks/video-review.md)的预检后，才启用视频审核 Worker。

SQLite 部署中，每类 Worker 只能运行一个实例。页面只控制固定服务，不能执行任意命令。会话续接失败时，只有受控 HOME 明确显示扫码或缺少 token 才需要运营人员重新扫码；关闭并以同一用户目录重开 Chrome 时，系统会先从新 HOME 自动恢复，不会复用旧 target 或 token。`run_game_circle_session_worker` 必须由 Supervisor 启动以取得 IPC capability；其余管理命令仅用于开发或故障诊断。

## 人工与自动上游处置

上游写操作默认关闭。人工处置必须由运营人员在内容管理工作台二次确认；单条有界操作，以及当前游戏内最多 20 条的沉底/屏蔽批量操作，都会经会话服务同步完成写前核验、写入和回读。Web 不会创建、切换或关闭 Chrome target，也不执行身份绑定。会话不可用时来源调用只返回未就绪；运营人员先在系统配置明确建立当前游戏 Frame，再重新确认写入，绝不自动重放。若服务在写请求发出后、返回前中断，审计会标记“待回读确认”，不得再次提交该写操作。人工扫码仍可显式打开固定、无认证登录页，但该入口不读取认证信息或接管会话。AI 自动处置只有在单独完成高风险授权、规则许可与配置后才可运行。运行恢复和最小验证见[运行与恢复手册](operations.md)。
