# 微信小游戏圈内容运营服务

本机部署的微信小游戏圈内容运营服务，负责原创帖子采集、AI 审核建议、人工确认处置、实时运营分析和可追溯舆情报告。项目使用 Django、SQLite 和受控 Chrome；业务数据库、浏览器资料与运行配置保存在本机 `runtime/`。

本 README 说明首次安装、首次授权和启用能力；模块设计、运维和开发文档见[文档中心](docs/README.md)。

## 使用前确认

- 需要安装 Chrome 或 Chromium，并使用 `uv` 管理 Python 3.12 和项目依赖。
- 需要能够登录微信公众平台并管理目标小游戏游戏圈的账号；扫码由操作者在本机受控浏览器中完成。
- AI、OCR、ASR 是可选能力，由部署者提供兼容服务地址、模型和密钥。启用后，帖子文本会发送给 AI，图片/视频帧会发送给 OCR，音频会发送给 ASR。
- 随附 FAQ 是通用示例，不代表部署方的客服政策。使用回复建议前，请先审核并替换为适用的支持渠道、事实和承诺边界。
- Web、Chrome 调试端口和本机 IPC 仅供本机使用；不要通过反向代理或端口转发将它们暴露到局域网或公网。

## 快速启动

克隆仓库或解压源码 ZIP 后，在项目根目录执行。ZIP 用户先解压并进入解压后的目录，再继续后续步骤。若尚未安装 `uv`，按[部署手册](docs/01-platform/runbooks/deployment.md#1-安装-uv-与同步依赖)中的操作系统步骤安装。

```bash
git clone https://github.com/llCnll/wechat-operation.git
cd wechat-operation
uv sync --python 3.12 --all-groups
```

复制 `.env.example` 后，运行 `uv run python -c "import secrets; print(secrets.token_urlsafe(50))"`，将输出填入 `runtime/.env` 的 `DJANGO_SECRET_KEY`。每个部署实例都应生成自己的密钥；缺少至少 32 个字符的密钥时，服务会拒绝启动。

```bash
# macOS/Linux
mkdir -p runtime
[ -f runtime/.env ] || cp .env.example runtime/.env
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python -m service_supervisor
```

```powershell
# Windows PowerShell
New-Item -ItemType Directory -Force runtime | Out-Null
if (-not (Test-Path runtime/.env)) { Copy-Item .env.example runtime/.env }
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python -m service_supervisor
```

Web 默认使用 `WEB_PORT=8000`，并且只监听 `127.0.0.1`。调整端口后，将下方入口中的 `8000` 替换为配置值；不要通过修改监听地址将服务暴露到网络。

管理员密码由操作者在本机交互输入；不得生成、读取或记录。

最小验证：访问 `http://127.0.0.1:8000/healthz`，应返回 `{"status": "ok"}`；后台入口为 `http://127.0.0.1:8000/admin/`。不要另行运行 `manage.py runserver`，Supervisor 是 Web 和受管服务的进程所有者。

首次启动时，Supervisor 默认启动 Web、受控 Chrome、禁言维护 Worker 和舆情报告 Worker。游戏圈会话服务、Scheduler、帖子采集 Worker、AI 审核 Worker 和视频审核 Worker 需在后台按需启动；禁言写入和 AI 自动处置默认关闭。

## 首次使用

登录 `/admin/` 后按顺序初始化：

1. 在“游戏圈会话服务”卡片启动服务，打开受控会话页并由操作者手动扫码。
2. 登录成功后刷新“可管理游戏目录”，再选择唯一的“当前操作游戏”。新安装没有预置游戏；选择后系统会建立并验证对应 Frame。确认外层会话和当前 Frame 均就绪。
3. 如需 AI 审核，在“配置 AI 审核”中填写兼容服务地址、模型和密钥；OCR 可单独配置。密钥只可写入，不会回显。配置后按页面提示启动或重启 AI 审核 Worker。
4. 如需自动采集，先在“自动任务生产者”中核对帖子采集、日/周指标和永久禁言续期规则。新数据库创建的这些规则默认启用；只保留需要的规则，再启动 Scheduler 和“帖子采集”服务。
5. 视频审核默认关闭；完成[视频审核手册](docs/40-reviews/runbooks/video-review.md)的无 Cookie 样本预检后，才配置并启动视频审核 Worker。

不要在当前操作游戏或有效 Frame 未就绪时启动自动采集。未经确认，不要启用自动任务生产者或上游写入开关。

## 已有端点升级

Git 工作区端点拉取代码后，必须以发布清单和本机 `runtime/release-state.json` 为准执行增量升级；不要按提交信息猜测迁移、一次性 Job 或重启动作。纯 ZIP 端点不得执行 Git 命令，必须先按[发布与多端升级](docs/01-platform/runbooks/release-and-upgrade.md#源码-zip-端点升级)校验 ZIP SHA-256 与发布包 provenance，再生成升级计划。

```bash
git pull --ff-only origin main
uv sync --locked --all-groups
uv run python manage.py upgrade_plan --json
```

按计划完成迁移、一次性 Job、重启和验证后，执行 `upgrade_ack`。完整命令、状态文件格式、发布约束和最小验证见[发布与多端升级](docs/01-platform/runbooks/release-and-upgrade.md)。

## 开发与集成端点同步

跟随 `develop` 或 `feature/*` 的非发布端点，在每次拉取或切换到新提交后，必须先同步锁定依赖，再启动或重启任何服务：

```bash
git pull --ff-only
uv sync --locked --all-groups
```

依赖同步失败时停止；完整边界见[发布与多端升级](docs/01-platform/runbooks/release-and-upgrade.md#非发布端点同步)。`main` 端点仍必须按上一节的正式升级流程执行。

## 常用入口

| 入口 | 用途 |
| --- | --- |
| `http://127.0.0.1:8000/` | 内容管理工作台 |
| `http://127.0.0.1:8000/admin/` | 初始化与运行状态、系统配置、运行追溯 |
| `http://127.0.0.1:8000/healthz` | Supervisor 健康检查 |
| `http://127.0.0.1:8000/reports/` | 运营分析 |
| `http://127.0.0.1:8000/public-opinion-reports/` | 异步舆情报告 |

## 文档导航

| 目标 | 阅读文档 |
| --- | --- |
| 首次部署、受控 Chrome 扫码、AI/OCR/Worker 启用 | [部署与首次初始化](docs/01-platform/runbooks/deployment.md) |
| 版本发布、端点增量升级、一次性 Job 与重启 | [发布与多端升级](docs/01-platform/runbooks/release-and-upgrade.md) |
| FFmpeg、视频样本预检、ASR 降级与视频 Worker | [视频审核手册](docs/40-reviews/runbooks/video-review.md) |
| 备份、任务恢复、补采与故障诊断 | [运行与恢复手册](docs/01-platform/runbooks/operations.md) |
| FAQ 示例维护和部署方政策适配 | [回复 FAQ 资料维护](docs/40-reviews/runbooks/faq-data-maintenance.md) |
| 开发测试、并行执行与外部 HTTP 护栏 | [测试执行指南](docs/00-standards/runbooks/testing.md) |
| 系统模块、数据流、交互与安全边界 | [系统总览](docs/01-platform/design/system-overview.md) |
| 产品、技术、接口调研与 OpenSpec | [文档中心](docs/README.md) |

## 安全边界

- Supervisor 是受控 Chrome 和 Web/Worker 的唯一进程所有者。只通过 `/admin/` 管理服务；不要手工接管浏览器、Worker 或 `runtime/supervisor/` 中的运行态文件。Cookie、令牌和二维码内容不写入业务数据或文档。
- 配置 AI/OCR/ASR 后，帖子文本、图片或媒体提取内容会发送到对应的外部服务；部署前请确认所选服务的数据处理边界。密钥只可写入，不会回显。
- 上游写操作默认关闭。人工处置需要在工作台二次确认；AI 自动处置需要单独授权。视频审核默认关闭，启用前需完成[无 Cookie 样本预检](docs/40-reviews/runbooks/video-review.md)。
- 永久禁言是本地自动续期意图：首次上游期限为 30 天；新数据库的续期规则默认每日 00:00 和 12:00，只有 Scheduler 运行时才会调度。取消意图只停止后续续期，不会解除仍有效的上游禁言。启用前先阅读[作者与内容运营手册](docs/30-posts/runbooks/author-and-content-operations.md)的高风险最小验证。
- 舆情报告只分析本地数据；详细行为见[指标与报告模块文档](docs/50-reporting/README.md)。
