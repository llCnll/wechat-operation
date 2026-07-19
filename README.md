# 微信游戏圈内容运营服务

内部内容运营服务，负责原创帖子采集、AI 审核建议、人工确认处置和实时运营分析。

## Agent 快速启动提示词

将下列内容直接交给接手项目的 Agent。它覆盖基础 Web 的首次启动；Chrome 登录、外部 AI/OCR/ASR 服务配置和 Worker 启用见后文的完整初始化流程。

> 在项目根目录按 README 快速启动基础服务。先执行 `uv --version`。若未安装，macOS/Linux 执行 `curl -LsSf https://astral.sh/uv/install.sh | sh` 并将 `$HOME/.local/bin:$HOME/.cargo/bin` 加入 `PATH`；Windows PowerShell 执行 `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`，再执行 `$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"`。运行 `uv sync --python 3.12 --all-groups`。macOS/Linux 执行 `mkdir -p runtime` 和 `[ -f runtime/.env ] || cp .env.example runtime/.env`；Windows PowerShell 执行 `New-Item -ItemType Directory -Force runtime | Out-Null`，再执行 `if (-not (Test-Path runtime/.env)) { Copy-Item .env.example runtime/.env }`。随后执行 `uv run python manage.py migrate`，在本机交互终端执行 `uv run python manage.py createsuperuser`，由操作者输入凭据，Agent 不得生成、读取或记录密码。最后运行 `uv run python -m service_supervisor`，以 `http://127.0.0.1:8000/healthz` 返回 `{"status": "ok"}` 为启动成功；后台入口为 `http://127.0.0.1:8000/admin/`。基础 Web 不依赖外部模型服务；AI 审核、图片/视频 OCR 和可选 ASR 均依赖部署方提供的兼容服务地址、密钥和模型，Agent 不得编造或替换这些凭据。需要采集与审核时，再按 README 启动独立 Chrome CDP、人工扫码登录，在后台 Pipeline 配置外部 AI/OCR（需要语音时再配置 ASR），然后启动 Scheduler、采集 Worker、AI Worker 和可选视频 Worker。

## 文档导航

| 目标 | 首先阅读 |
| --- | --- |
| 首次启动或 Agent 接手 | 本节提示词；需要完整能力时继续阅读[完整初始化流程](#完整初始化流程agent-执行清单)。 |
| 日常运营与故障恢复 | [运行与恢复手册](docs/guides/operations-runbook.md)。 |
| 了解系统、数据流与安全边界 | [系统总览](docs/guides/system-overview.md)。 |
| 查找产品、技术或接口资料 | [文档中心](docs/README.md)。 |

## 系统总览

运营主入口是内容管理台，而不是 Django Admin：左侧浏览原创帖子队列，右侧查看正文、投票、媒体、互动指标与当前状态，并在人工二次确认后执行受控处置。完整模块、数据流、交互和安全边界见[系统总览](docs/guides/system-overview.md)；全部文档见[文档中心](docs/README.md)。

## 启动前确认

- 本项目是本机内部工具。基础 Web 可以在没有 Chrome 登录态的情况下启动，但帖子采集、状态回读、游戏目录同步和人工上游处置都依赖步骤 1 启动的受控 Chrome 会话。
- Chrome DevTools Protocol（CDP）无需单独安装：它是 Chrome/Chromium 自带的调试协议。必须预先安装 Chrome 或 Chromium，并以 `--remote-debugging-port=9222` 启动独立浏览器进程；普通已运行的 Chrome 无法在启动后补加该端口。
- `uv` 是唯一需要预先可用的开发工具；项目要求 Python 3.12，`uv sync --python 3.12 --all-groups` 会下载受管解释器。
- 需要真实使用工作台时，先创建一个本地 Django 管理员账号。该账号只用于本系统登录，不是微信账号，也不应使用或记录微信会话信息。
- 本项目不内置 AI、OCR 或 ASR 模型服务。基础 Web 可在未配置它们时启动；要启用 AI 审核、图片/视频文字识别或语音转写，部署方必须提供相应的 OpenAI 兼容服务地址、访问密钥和模型名称，并确认其数据处理边界。

## 完整初始化流程（Agent 执行清单）

所有命令均在项目根目录执行。终端只负责依赖安装、首次初始化、启动 Supervisor 和受控 Chrome；步骤 2 至 5 均在 `/admin/` 的“初始化与运行状态”完成。Web、帖子采集、状态巡检、OCR 和 AI 评估均可用；原平台写操作默认保持关闭，只有完成步骤 5 的显式审批配置后才可人工执行。清单不会覆盖已有 `runtime/.env`、数据库或其他运行数据。

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

### 1. 基础服务与受控 Chrome 启动

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

- [ ] 首次部署先应用迁移并创建本地管理员账号：

```bash
uv run python manage.py migrate
uv run python manage.py createsuperuser
```

- [ ] 首次部署若尚未存在管理员账号，必须在继续启动流程前由执行 Agent 人工执行 `uv run python manage.py createsuperuser`。该账号是进入系统配置与初始化 Pipeline 的前置条件；Agent 不得跳过、自动生成或记录其密码。
- [ ] 启动 Supervisor。它会自动执行迁移、静态资源收集和默认策略检查，再启动 Web；日常运行不再分别启动 Web 或 Worker：

```bash
uv run python -m service_supervisor
```

Supervisor 必须保持运行。健康检查应返回 `{"status": "ok"}`；启动失败时先查看 `runtime/supervisor/logs/preflight.log`，不要再以独立 `runserver` 进程抢占端口 `8000`。

- [ ] 启动受控 Chrome 并开启 CDP。使用独立用户目录，避免读取或影响日常浏览器会话：

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

- [ ] 验证 CDP 端口已就绪：macOS/Linux 执行 `curl -fsS http://127.0.0.1:9222/json/version`；Windows PowerShell 执行 `(Invoke-WebRequest http://127.0.0.1:9222/json/version).Content`。成功时会返回浏览器版本与 `webSocketDebuggerUrl`，但此时尚未完成微信登录。

完成本步骤后登录 `http://127.0.0.1:8000/admin/`。后续步骤均从首页“初始化与运行状态”操作：配置 AI/OCR、打开固定登录页并扫码、启停 Scheduler 与 Worker、管理人工处置授权和查看日志。扫码动作仍由运营人员在受控 Chrome 中完成。

### 2. 配置外部 AI、OCR 与可选 ASR 服务

登录 `http://127.0.0.1:8000/admin/` 后，在首页顶部“初始化与运行状态”点击“配置 AI 审核”和“配置 OCR”。这些服务由部署方提供，项目不会下载、代管或自动猜测任何服务地址、密钥或模型。AI 审核必须填写外部服务地址、访问密钥和模型名称；图片 OCR 还需填写对应的 OCR 服务地址、访问密钥和模型名称。视频审核复用 OCR 服务处理本地抽取的帧；如需语音转写，在“配置视频审核”中另行填写兼容 ASR 服务、密钥和模型并显式启用。密钥只可写入、不会回显。

保存配置后按页面提示重启 AI 审核 Worker；修改视频或 ASR 配置后重启视频审核 Worker，使其加载新配置。`runtime/.env` 仅保留给无法进入后台时的运维恢复，不应作为日常配置入口；AI 永远不会自动执行处置。

### 3. CDP 扫码登录

采集器通过步骤 1 已启动的 Chrome DevTools Protocol（CDP）使用独立 Chrome 的已登录插件页。此步骤仅完成扫码和登录态验证。

- [ ] 在 `/admin/` 的“CDP 登录态”节点点击“打开登录页”。服务只会在带调试端口的受控 Chrome 中打开固定、未认证的插件页；二维码出现后，必须由运营人员使用微信扫码并完成确认。Agent 不得读取、记录或伪造 Cookie、令牌、二维码内容或 `openid`。
- [ ] 保持已登录插件页打开，直到 Pipeline 的“CDP 登录态”显示绿色“已就绪”。`GET /auth/status` 仅作为无法进入后台时的诊断接口，成功响应包含 `"valid": true`。

若工作台顶部的 `CDP` 状态胶囊显示红色“未登录，去登录”，已登录本系统的运营人员也可点击它打开固定登录页；不会在当前工作台浏览器跳转，也不会接收或保存登录 URL、Cookie 或令牌。

### 4. 启动 Worker 并完成验证

- [ ] 返回系统配置首页的“初始化与运行状态”，依次启动 Scheduler、帖子采集 Worker 与 AI Worker。“帖子采集 Worker”的一次启动会同时启动实时采集和人工补采两个进程。Supervisor 会持久化期望状态，后续重启后自动恢复此前启用的 Worker。
- [ ] 确认 Pipeline 的核心能力均显示“已就绪”。至此帖子采集、状态巡检、OCR 与 AI 评估已启用；`/healthz` 与 `/auth/status` 仅用于后台不可用时的诊断。

SQLite 部署中每类 Worker 只能运行一个实例。页面只能控制固定服务，不能执行任意命令；`run_scheduler`、`run_realtime_ingestion_worker`、`run_backfill_ingestion_worker` 和 `run_review_worker` 仅用于开发或故障诊断。`run_ingestion_worker` 保留为实时采集命令的兼容别名。

### 可选：视频审核

视频审核默认关闭。先安装本机 `ffmpeg`、`ffprobe` 和 OpenCV，再使用已获授权的真实视频样本验证“无 Cookie HTTPS 读取、重定向和临时文件清理”；预检失败不得启用，也不得回退到 CDP Cookie。随后在 Pipeline 的“视频审核 Worker”配置中设置 `VIDEO_ENABLED=true`（默认时长上限 30 秒，可提高至 60 秒）并启动该 Worker。

macOS 12 Intel 没有当前 Homebrew FFmpeg bottle，`brew install ffmpeg` 会从源码构建。已验证可执行的替代方案是项目本地安装 Darwin x64 的 FFmpeg 4.1 与 ffprobe 4.0.2：

```bash
npm install --prefix runtime/video-tools @ffmpeg-installer/darwin-x64@4.1.0 ffprobe-static
```

程序优先使用系统 `PATH` 中的工具；仅当系统工具不存在时才使用上述 `runtime/video-tools` 路径。该目录被忽略，不提交二进制文件。安装后仍须完成受控样本预检，才可启用视频 Worker。系统配置首页的“视频审核 Worker”→“配置视频审核”可设置启用开关、预检确认、时长/字节/视频数量/分辨率/dHash/OCR 预算、各超时和 ASR 地址、模型、密钥、超时；密钥只可写入，不会回显。

视频只在本机短生命周期目录中读取：开场和收尾每 250ms 抽帧、中段每秒抽帧，先本地检测二维码、再 dHash 去重。系统仅持久化带时间位置的 OCR/语音文本和固定的“检测到二维码”提示；不会保存原视频、帧、音频、完整视频 URL、Cookie 或二维码目标，也不会将它们传给 OCR、ASR 或 AI。

当前部署明确关闭 `ASR_ENABLED`。这不会关闭视频审核：视频仍抽帧并执行二维码检测与 OCR；因为缺少语音来源，提取结果会以“部分完成”进入人工重点核验。待办：确认支持 `/audio/transcriptions` 的受控 ASR 模型与数据保留边界后，单独完成 ASR 成功链路验收，再启用 ASR。

### 启动完成判定

完成步骤 0 至 4 后，应同时满足下表。未配置 AI/OCR 时，Web 与采集仍可工作，但对应评估任务会失败，不能视为完整启用。

| 项目 | 检查方式 | 预期结果 |
| --- | --- | --- |
| Web 服务 | Pipeline 中 Web 服务显示运行 | 由 Supervisor 托管且状态正常；`GET /healthz` 仅用于诊断。 |
| 受控微信会话 | Pipeline 中 CDP 登录态显示已就绪 | 受控 Chrome 会话有效；`GET /auth/status` 仅用于诊断。 |
| 自动调度 | Pipeline 中 Scheduler 显示运行 | 每 15 分钟只创建任务，不直接访问 Chrome。 |
| 数据采集与状态巡检 | Pipeline 中帖子采集显示两个子 Worker 都在运行 | 实时采集消费 `SCHEDULED_INGEST_WINDOW`、`STATUS_SYNC`；人工补采消费 `MANUAL_INGEST_WINDOW`、`METRICS_REFRESH_WINDOW` 和历史 `INGEST_WINDOW`。 |
| OCR 与 AI 评估 | Pipeline 中 AI Worker 显示运行 | 消费 `OCR_EXTRACT`、`AI_EVALUATE`；要求相应配置完整。 |

若工作台没有新数据，先确认 Pipeline 中“CDP 登录态”、Scheduler 与帖子采集 Worker 均显示运行或已就绪。任务状态、脱敏错误摘要和重试次数可在系统后台的“后台任务”查看；详细恢复步骤见[运行与恢复手册](docs/guides/operations-runbook.md)。

### 5. 可选：启用人工上游处置

默认不得开启此步骤。仅在上游接口已验证、运营负责人明确批准并完成风险评估后，才在 `/admin/` 的“初始化与运行状态”点击“管理授权”，启用总开关和所需动作授权。未选择的动作会保持关闭。

保存授权后按页面提示重启 Web 服务。`runtime/.env` 中的 `ENABLE_*_WRITES` 仅用于紧急恢复；即使开关开启，沉底、屏蔽、禁言、解除禁言和恢复正常仍只能由已登录运营人员在工作台人工确认后执行，AI、Scheduler 和 Worker 永远不会自动调用上游写接口。

完成对应步骤后访问：健康检查 `http://127.0.0.1:8000/healthz`，内容管理台 `http://127.0.0.1:8000/`，运营分析 `http://127.0.0.1:8000/reports/`，系统后台 `http://127.0.0.1:8000/admin/`。

### 日常操作入口

| 入口 | 用途 |
| --- | --- |
| 内容管理台 `/` | 筛选和查看原创帖，人工确认后执行审核处置。 |
| 运营分析 `/reports/` | 查看指定日期范围的实时聚合数据，并异步刷新该范围内已采集原创帖的互动指标。 |
| 系统后台 `/admin/` | 配置 AI、OCR 和人工处置授权；查看 Pipeline、启停或重启固定服务、查看服务日志和后台任务。 |
| 终端 | 仅用于首次初始化、启动 Supervisor/受控 Chrome，以及故障诊断和人工历史补采。 |

## 手动补采和故障诊断

采集器不读取或保存 Cookie、外层令牌、插件令牌或 `openid`。它通过本机已登录 Chrome 的 CDP，在 `gamemp.weixin.qq.com` 插件 iframe 内发起只读请求；请先以调试端口 `9222` 启动并登录受控 Chrome。

```bash
# 创建最近 12 小时的人工历史补采任务。请替换为目标游戏 appid。
uv run python manage.py enqueue_ingestion --appid wx6d2454a8afcd9706 --hours 12
```

日常历史补采请在 `/admin/` 的“帖子采集 Worker”点击“创建历史补采”，选择可管理游戏并输入最近小时数；该任务由人工补采 Worker 异步处理。上面的命令仅用于故障诊断。日常的互动指标刷新请在运营分析页点击“刷新本范围数据”；该操作不会保存评论正文。采集与 AI 审核分别由专用 Worker 消费；SQLite 会串行化它们的写入阶段，因此不要为同一类任务额外启动多个实例。需要更高并发或持续出现写锁竞争时，应迁移 PostgreSQL。沉底、屏蔽、禁言、解除禁言和恢复正常仅能在工作台二次确认、全局开关和对应操作开关同时启用后执行。

工作台默认显示“城主别慌张”的帖子，并在标题旁单选切换本地缓存的游戏目录。管理员可在 `/admin/` 的“可管理游戏”中选择“从受控微信同步可管理游戏”更新列表；同步失败不会影响本地帖子查看。自动采集仍固定为城主别慌张。

敏感会话信息不得写入 `runtime/.env`、数据库或日志。

## 自动采集调度

城主别慌张（`wx6d2454a8afcd9706`）由 Supervisor 托管的 Scheduler 自动入队：每 15 分钟时间桶创建一项状态巡检和一个最近 30 分钟的 `SCHEDULED_INGEST_WINDOW`。因此相邻窗口重叠 15 分钟，边界帖子由 `topic_id` 幂等更新。Scheduler 只创建任务，不连接 Chrome CDP、不执行 AI 或上游写操作；实时采集 Worker 独立消费自动任务，人工补采 Worker 不会阻塞它。

SQLite 部署仅启动一个 Scheduler Worker。`run_scheduler --once` 仅用于诊断；不再需要 cron 或 macOS `launchd`。Worker 启动顺序、故障恢复和超过 30 分钟空洞的人工补采方式见[运行与恢复手册](docs/guides/operations-runbook.md)。

## 运营分析

访问 `http://127.0.0.1:8000/reports/` 查看“运营分析”。页面按北京时间起止日期和单选游戏实时聚合数据，默认最近 7 天（含当天），单次范围最多 31 天。点击“刷新本范围数据”只会创建 `METRICS_REFRESH_WINDOW` 后台任务；保持 Pipeline 中的人工补采 Worker 运行后，它会异步回读范围内原创帖的浏览、点赞、评论和分享，并新增互动快照。刷新不修改正文、媒体、投票、审核或原平台状态。

## 文档

按产品需求、使用说明、技术文档和接口调研分类的文档入口见[docs/README.md](docs/README.md)。
