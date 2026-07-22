# 微信游戏圈内容运营服务

内部内容运营服务，负责原创帖子采集、AI 审核建议、人工确认处置和实时运营分析。

根 README 是首次启动和已有端点升级的简明入口；按需启用 Chrome、AI/OCR、视频与 Worker 的详细步骤已迁移到 `docs/`。

## 快速启动

在项目根目录执行。首次启动仅启动基础 Web；不会自动启用采集、AI、OCR、视频审核或上游写操作。

```bash
uv sync --python 3.12 --all-groups
```

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

最小验证：访问 `http://127.0.0.1:8000/healthz`，应返回 `{"status": "ok"}`。后台入口为 `http://127.0.0.1:8000/admin/`。

若没有 `uv`，先按[部署与首次初始化](docs/guides/deployment-and-initialization.md#1-安装-uv-与同步依赖)安装。管理员密码必须由操作者在本机交互输入；不得由 Agent 生成、读取或记录。

## 已有端点升级

拉取代码后，必须以发布清单和本机 `runtime/release-state.json` 为准执行增量升级；不要按提交信息猜测迁移、一次性 Job 或重启动作。

```bash
git pull --ff-only origin main
uv sync --locked --all-groups
uv run python manage.py upgrade_plan --json
```

按计划完成迁移、一次性 Job、重启和验证后，执行 `upgrade_ack`。完整命令、状态文件格式、发布约束和最小验证见[发布与多端升级](docs/guides/release-and-upgrade.md)。

## 常用入口

| 入口 | 用途 |
| --- | --- |
| `http://127.0.0.1:8000/` | 内容管理工作台 |
| `http://127.0.0.1:8000/admin/` | 初始化与运行状态、系统配置、运行追溯 |
| `http://127.0.0.1:8000/healthz` | Supervisor 健康检查 |
| `http://127.0.0.1:8000/reports/` | 运营分析 |

## 文档导航

| 目标 | 阅读文档 |
| --- | --- |
| 首次部署、受控 Chrome 扫码、AI/OCR/Worker 启用 | [部署与首次初始化](docs/guides/deployment-and-initialization.md) |
| 版本发布、端点增量升级、一次性 Job 与重启 | [发布与多端升级](docs/guides/release-and-upgrade.md) |
| FFmpeg、视频样本预检、ASR 降级与视频 Worker | [视频审核手册](docs/guides/video-review-runbook.md) |
| 备份、任务恢复、补采与故障诊断 | [运行与恢复手册](docs/guides/operations-runbook.md) |
| 系统模块、数据流、交互与安全边界 | [系统总览](docs/guides/system-overview.md) |
| 产品、技术、接口调研与 OpenSpec | [文档中心](docs/README.md) |

## 安全边界

- 受控 Chrome 由 Supervisor 以独立 profile 与仅本机 CDP 调试端口启动；不得手工接管其进程，也不得读取、记录或伪造 Cookie、令牌或二维码内容。
- 在 `/admin/` 同步可管理游戏并选择“当前操作游戏”后，`game_circle_session` Worker 会在来源会话失效、当前操作游戏切换或六个每日保活时间槽重建一组新的受控 HOME 与插件会话。切换当前操作游戏与来源失效使用 `RECOVER`，它只会将外层当前游戏直接对齐到目标；定时或人工保活使用 `KEEPALIVE`，在已处于目标时，从当前账号目录中、平台允许切换的非目标游戏里随机选择一个中转并完成 `A → B → A`，每次切换后均重载、回读当前游戏和运行时 token。候选为空、未获许可或任一步回读失败会安全失败，绝不静默退化为 `RECOVER`。二者都只在最终插件完成绑定和最小只读验证后发布 lease；不保存 Cookie、外层 token 或来源身份 token。新 HOME 明确提示扫码或没有运行时 token 时，任务会以“需要人工扫码”结束；由运营人员在受控 Chrome 中扫码后重新触发即可。
- 游戏圈 Worker 与 Web 的本机 IPC 由 Supervisor 注入独立进程 capability；必须通过 Supervisor 启动整套服务，不得手工暴露、复制或复用 `runtime/supervisor/` 中的控制或会话能力文件。
- 项目不内置 AI、OCR 或 ASR 服务。部署方负责提供地址、密钥和模型，并确认数据处理边界；密钥只可写入、不回显。
- 视频审核默认关闭，必须先完成无 Cookie 的授权样本预检。详见[视频审核手册](docs/guides/video-review-runbook.md)。
- 上游写操作默认关闭。人工处置需在工作台二次确认；AI 自动处置需要单独的高风险授权。
