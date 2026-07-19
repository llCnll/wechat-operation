# 微信游戏圈内容运营服务

内部内容运营服务，负责原创帖子采集、AI 审核建议、人工确认处置和实时运营分析。

## 系统总览

运营主入口是内容管理台，而不是 Django Admin：左侧浏览原创帖子队列，右侧查看正文、投票、媒体、互动指标与当前状态，并在人工二次确认后执行受控处置。完整模块、数据流、交互和安全边界见[系统总览](docs/guides/system-overview.md)；全部文档见[文档中心](docs/README.md)。

## 启动前确认

- 本项目是本机内部工具。基础 Web 可以在没有 Chrome 登录态的情况下启动，但帖子采集、状态回读、游戏目录同步和人工上游处置都依赖步骤 3 的受控 Chrome 会话。
- `uv` 是唯一需要预先可用的开发工具；项目要求 Python 3.12，`uv sync --python 3.12 --all-groups` 会下载受管解释器。
- 需要真实使用工作台时，先创建一个本地 Django 管理员账号。该账号只用于本系统登录，不是微信账号，也不应使用或记录微信会话信息。

## 启动流程（Agent 执行清单）

所有命令均在项目根目录执行。完成步骤 0 至 4 后，Web、帖子采集、状态巡检、OCR 和 AI 评估均可用；原平台写操作默认保持关闭，只有完成步骤 5 的显式审批配置后才可人工执行。清单不会覆盖已有 `runtime/.env`、数据库或其他运行数据。

### 0. `uv` 不可用时

先执行 `uv --version`。若命令不存在，使用当前操作系统对应的官方安装方式；它们均不依赖已安装的 Python。

macOS 或 Linux：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
uv --version
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
uv --version
```

无需预先安装系统 Python。首次执行 `uv sync --python 3.12 --all-groups` 时，`uv` 会自动下载受管的 Python 3.12；如需预下载并验证，可执行：

```bash
uv python install 3.12
uv run --python 3.12 python --version
```

若安装脚本因受限网络不可用，但系统恰好已有可用 Python 和 `pip`，可使用备用方式安装 `uv`：

```bash
# macOS/Linux；使用当前可用的 python3
python3 -m pip install --user uv
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

```powershell
# Windows PowerShell；使用当前可用的 py 启动器
py -m pip install --user uv
uv --version
```

若安装脚本不可用且系统没有 Python，需由具备系统安装权限的操作者安装 `uv` 本身：macOS 可执行 `brew install uv`，Windows PowerShell 可执行 `winget install -e --id astral-sh.uv`；Linux 应使用所在发行版的软件源或由运维提供的 `uv` 安装包。Agent 不应改写项目内的依赖锁文件或将 `uv`、受管 Python 安装到 `runtime/`。

### 1. 基础服务启动

- [ ] 确认 `uv --version` 可用；首次依赖同步会自动下载受管 Python 3.12：

```bash
uv sync --python 3.12 --all-groups
```

- [ ] 初始化运行目录。macOS/Linux：

```bash
mkdir -p runtime
[ -f runtime/.env ] || cp .env.example runtime/.env
```

Windows PowerShell：

```powershell
New-Item -ItemType Directory -Force runtime | Out-Null
if (-not (Test-Path runtime/.env)) { Copy-Item .env.example runtime/.env }
```

- [ ] 应用迁移、收集静态资源并执行校验：

```bash
uv run python manage.py migrate
uv run python manage.py ensure_default_policy
uv run python manage.py collectstatic --noinput
uv run python manage.py check
```

- [ ] 若尚未存在管理员账号，人工执行 `uv run python manage.py createsuperuser`。该步骤不影响健康检查，但登录工作台前必须完成。
- [ ] 启动单进程 Web 服务并等待健康检查成功。macOS/Linux：

```bash
nohup uv run python manage.py runserver 127.0.0.1:8000 --noreload > runtime/web.log 2>&1 &
ready=false
for _ in {1..20}; do
  if curl -fsS http://127.0.0.1:8000/healthz; then ready=true; break; fi
  sleep 1
done
$ready || { echo 'Web 服务未在 20 秒内启动；请检查 runtime/web.log' >&2; exit 1; }
```

Windows PowerShell：

```powershell
Start-Process -FilePath uv -ArgumentList @('run','python','manage.py','runserver','127.0.0.1:8000','--noreload') -WorkingDirectory (Get-Location) -RedirectStandardOutput 'runtime/web.log' -RedirectStandardError 'runtime/web-error.log'
$deadline = (Get-Date).AddSeconds(20)
do { try { $response = Invoke-WebRequest http://127.0.0.1:8000/healthz } catch { Start-Sleep -Seconds 1 } } while ((-not $response) -and (Get-Date -lt $deadline))
if (-not $response -or $response.StatusCode -ne 200) { throw 'Web 服务未在 20 秒内启动；请检查 runtime/web-error.log' }
$response.Content
```

健康检查必须返回 `{"status": "ok"}`。若端口 `8000` 已被本服务占用，不要重复启动；直接执行健康检查。若被其他进程占用，先停止该进程或改用其他端口，并在后续命令中保持一致。

### 2. 配置 AI 与 OCR

编辑 `runtime/.env`。AI 审核必须配置 `AI_BASE_URL`、`AI_API_KEY` 和 `AI_MODEL`；图片 OCR 功能还必须配置 `OCR_BASE_URL`、`OCR_API_KEY` 和 `OCR_MODEL`。缺少 AI 配置时，AI Worker 虽能启动但评估任务会失败；缺少 OCR 配置时，图片 OCR 会记录失败并继续后续 AI 评估。

```dotenv
AI_BASE_URL=https://your-ai-endpoint/v1
AI_API_KEY=replace-with-secret
AI_MODEL=your-model
AI_TIMEOUT_SECONDS=30

OCR_BASE_URL=https://your-ocr-endpoint/v1
OCR_API_KEY=replace-with-secret
OCR_MODEL=your-vision-model
```

上游写操作开关保持默认 `false`。只有完成接口验证且运营人员明确批准后，才在同一文件启用总开关和所需单项开关；AI 永远不会自动执行处置。

运行时配置由进程启动时读取：本步骤后再启动 AI Worker 即可使 AI/OCR 配置生效；若修改了上游写操作开关，必须重启 Web 服务后再执行人工处置。

### 3. CDP 扫码登录

采集器通过 Chrome DevTools Protocol（CDP）使用独立 Chrome 的已登录插件页。启动命令如下；普通 Chrome 进程不能在启动后补加调试端口。

macOS：

```bash
open -na "Google Chrome" --args --remote-debugging-port=9222 --user-data-dir="$HOME/Library/Application Support/WechatOperationChrome"
```

Linux（已安装 `google-chrome`、`chromium` 或 `chromium-browser` 之一）：

```bash
CHROME_BIN="$(command -v google-chrome || command -v chromium || command -v chromium-browser)"
test -n "$CHROME_BIN" || { echo '未找到 Chrome 或 Chromium' >&2; exit 1; }
"$CHROME_BIN" --remote-debugging-port=9222 --user-data-dir="$HOME/.local/share/WechatOperationChrome" >/dev/null 2>&1 &
```

Windows PowerShell（若 `chrome.exe` 不在 `PATH`，替换为本机 Chrome 可执行文件路径）：

```powershell
$profilePath = Join-Path $env:LOCALAPPDATA 'WechatOperationChrome'
Start-Process chrome.exe -ArgumentList '--remote-debugging-port=9222', "--user-data-dir=$profilePath"
```

- [ ] 在独立 Chrome 打开以下插件页。二维码出现后，必须由运营人员使用微信扫码并完成确认；Agent 不得读取、记录或伪造 Cookie、令牌、二维码内容或 `openid`。

```text
https://mp.weixin.qq.com/wxamp/frame/pluginRedirect/pluginRedirect?title=&action=plugin_redirect&lang=zh_CN&plugin_uin=1029&simple=1&nosidebar=1&custom=path%3D%2Fminigame%40community%2Fminigame%2Fpage%2Fhome
```

- [ ] 保持已登录插件页打开，并验证登录态。macOS/Linux：`curl -fsS http://127.0.0.1:8000/auth/status`；Windows PowerShell：`(Invoke-WebRequest http://127.0.0.1:8000/auth/status).Content`。响应必须包含 `"valid": true`。

若工作台顶部的 `CDP` 状态胶囊显示红色“未登录，去登录”，已登录本系统的运营人员可点击它。服务只会在带调试端口的受控 Chrome 中打开固定、未认证的插件页，随后仍须由运营人员手动扫码；不会在当前工作台浏览器跳转，也不会接收或保存登录 URL、Cookie 或令牌。

### 4. 启动 Worker 并完成验证

- [ ] 在三个独立终端启动 Scheduler、采集 Worker 和 AI Worker：

```bash
uv run python manage.py run_scheduler --loop
uv run python manage.py run_ingestion_worker --loop
uv run python manage.py run_review_worker --loop
```

- [ ] 确认三个进程持续运行，`/healthz` 返回 `{"status": "ok"}`，`/auth/status` 返回 `"valid": true`。至此核心数据能力，即帖子采集、状态巡检、OCR 与 AI 评估已启用。

SQLite 部署中每类 Worker 只能运行一个实例；重复启动同类 Worker 会主动退出，不会恢复或执行正在由另一实例处理的任务。

### 启动完成判定

完成步骤 0 至 4 后，应同时满足下表。未配置 AI/OCR 时，Web 与采集仍可工作，但对应评估任务会失败，不能视为完整启用。

| 项目 | 检查方式 | 预期结果 |
| --- | --- | --- |
| Web 服务 | `GET /healthz` | 返回 `{"status": "ok"}`。 |
| 受控微信会话 | `GET /auth/status` | 返回中包含 `"valid": true`。 |
| 自动调度 | `run_scheduler --loop` 进程持续运行 | 每 15 分钟只创建任务，不直接访问 Chrome。 |
| 数据采集与状态巡检 | `run_ingestion_worker --loop` 进程持续运行 | 消费 `INGEST_WINDOW`、`STATUS_SYNC`、`METRICS_REFRESH_WINDOW`。 |
| OCR 与 AI 评估 | `run_review_worker --loop` 进程持续运行 | 消费 `OCR_EXTRACT`、`AI_EVALUATE`；要求相应配置完整。 |

若工作台没有新数据，先确认 Chrome 插件页仍打开且 `/auth/status` 有效，再确认 Scheduler 与采集 Worker 均在运行。任务状态、脱敏错误摘要和重试次数可在系统后台的 `Background jobs` 查看；详细恢复步骤见[运行与恢复手册](docs/guides/operations-runbook.md)。

### 5. 可选：启用人工上游处置

默认不得开启此步骤。仅在上游接口已验证、运营负责人明确批准并完成风险评估后，才在 `runtime/.env` 设置总开关和所需动作开关：

```dotenv
ENABLE_UPSTREAM_WRITES=true
ENABLE_SINK_WRITES=true
ENABLE_BLOCK_WRITES=true
ENABLE_MUTE_WRITES=true
ENABLE_UNMUTE_WRITES=true
ENABLE_RESTORE_WRITES=true
```

不需要的动作必须保持 `false`。修改任一 `ENABLE_*_WRITES` 配置后重启 Web 服务；即使开关开启，沉底、屏蔽、禁言、解除禁言和恢复正常仍只能由已登录运营人员在工作台人工确认后执行，AI、Scheduler 和 Worker 永远不会自动调用上游写接口。

完成对应步骤后访问：健康检查 `http://127.0.0.1:8000/healthz`，内容管理台 `http://127.0.0.1:8000/`，运营分析 `http://127.0.0.1:8000/reports/`，系统后台 `http://127.0.0.1:8000/admin/`。

## 只读帖子采集

采集器不读取或保存 Cookie、外层令牌、插件令牌或 `openid`。它通过本机已登录 Chrome 的 CDP，在 `gamemp.weixin.qq.com` 插件 iframe 内发起只读请求；请先以调试端口 `9222` 启动并登录受控 Chrome。

```bash
# 创建最近 12 小时采集任务。请替换为目标游戏 appid。
uv run python manage.py enqueue_ingestion --appid wx6d2454a8afcd9706 --hours 12

# 可选：刷新已采集原创帖的浏览、点赞、评论和分享数量；不会保存评论正文。
uv run python manage.py refresh_post_metrics --appid wx6d2454a8afcd9706
```

完成后在内容管理台查看：`http://127.0.0.1:8000/`。采集与 AI 审核分别由专用 Worker 消费；SQLite 会串行化它们的写入阶段，因此不要为同一类任务额外启动多个实例。需要更高并发或持续出现写锁竞争时，应迁移 PostgreSQL。沉底、屏蔽、禁言、解除禁言和恢复正常仅能在工作台二次确认、全局开关和对应操作开关同时启用后执行。

工作台默认显示“城主别慌张”的帖子，并在标题旁单选切换本地缓存的游戏目录。管理员可在 `/admin/` 的 `Managed games` 选择“从受控微信同步可管理游戏”更新列表；同步失败不会影响本地帖子查看。自动采集仍固定为城主别慌张。

敏感会话信息不得写入 `runtime/.env`、数据库或日志。

## 自动采集调度

城主别慌张（`wx6d2454a8afcd9706`）由项目内部的 `run_scheduler --loop` 自动入队：每 15 分钟时间桶创建一项状态巡检和一个最近 30 分钟的采集窗口。因此相邻窗口重叠 15 分钟，边界帖子由 `topic_id` 幂等更新。Scheduler 只创建任务，不连接 Chrome CDP、不执行 AI 或上游写操作；采集和 AI 仍由前述两个专用 Worker 消费。

SQLite 部署仅启动一个 Scheduler Worker。`run_scheduler --once` 仅用于诊断；不再需要 cron 或 macOS `launchd`。Worker 启动顺序、故障恢复和超过 30 分钟空洞的人工补采方式见[运行与恢复手册](docs/guides/operations-runbook.md)。

## 运营分析

访问 `http://127.0.0.1:8000/reports/` 查看“运营分析”。页面按北京时间起止日期和单选游戏实时聚合数据，默认最近 7 天（含当天），单次范围最多 31 天。点击“刷新本范围数据”只会创建 `METRICS_REFRESH_WINDOW` 后台任务；保持 `run_ingestion_worker --loop` 运行后，它会异步回读范围内原创帖的浏览、点赞、评论和分享，并新增互动快照。刷新不修改正文、媒体、投票、审核或原平台状态。

## 文档

按产品需求、使用说明、技术文档和接口调研分类的文档入口见[docs/README.md](docs/README.md)。
