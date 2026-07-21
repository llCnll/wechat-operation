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

## 4. 启动受控 Chrome 并扫码

受控 Chrome 必须使用独立用户目录，避免影响日常浏览器会话。

```bash
# macOS
open -na "Google Chrome" --args --remote-debugging-port=9222 --user-data-dir="$HOME/Library/Application Support/WechatOperationChrome"
```

```bash
# Linux
CHROME_BIN="$(command -v google-chrome || command -v chromium || command -v chromium-browser)"
test -n "$CHROME_BIN" || { echo '未找到 Chrome 或 Chromium' >&2; exit 1; }
"$CHROME_BIN" --remote-debugging-port=9222 --user-data-dir="$HOME/.local/share/WechatOperationChrome" >/dev/null 2>&1 &
```

```powershell
# Windows PowerShell；chrome.exe 不在 PATH 时替换为其绝对路径
$profilePath = Join-Path $env:LOCALAPPDATA 'WechatOperationChrome'
Start-Process chrome.exe -ArgumentList '--remote-debugging-port=9222', "--user-data-dir=$profilePath"
```

最小验证：macOS/Linux 执行 `curl -fsS http://127.0.0.1:9222/json/version`；Windows 执行 `(Invoke-WebRequest http://127.0.0.1:9222/json/version).Content`。随后在 `/admin/` 的“CDP 登录态”打开固定登录页，由运营人员手动扫码。不得读取、记录或伪造 Cookie、令牌、二维码或作者身份标识。

## 5. 配置能力并启用 Worker

在 `/admin/` 首页“初始化与运行状态”中：

1. 配置 AI 审核与 OCR；需要语音转写时，再配置视频审核的 ASR。
2. 确认 CDP 登录态为“已就绪”。
3. 依次启动 Scheduler、帖子采集 Worker 与 AI Worker。帖子采集节点会同时启动实时采集和人工补采。
4. 只有完成[视频审核手册](video-review-runbook.md)的预检后，才启用视频审核 Worker。

SQLite 部署中，每类 Worker 只能运行一个实例。页面只控制固定服务，不能执行任意命令。`run_scheduler`、`run_realtime_ingestion_worker`、`run_backfill_ingestion_worker`、`run_review_worker` 仅用于开发或故障诊断。

## 人工与自动上游处置

上游写操作默认关闭。人工处置必须由运营人员在内容管理工作台二次确认；AI 自动处置只有在单独完成高风险授权、规则许可与配置后才可运行。写前会读取详情核验，写后回读状态；失败时不得通过 Worker 重复发送写请求。运行恢复和最小验证见[运行与恢复手册](operations-runbook.md)。
