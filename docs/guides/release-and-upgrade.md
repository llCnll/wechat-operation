# 发布与多端升级

本仓库以版本和发布清单，而非口头描述或 Git diff，确定升级步骤。每次推送到 `main` 都必须创建一个新发布版本；不得只推送代码而遗漏版本、发布清单或 tag。

## 版本与发布清单

版本格式为 `MAJOR.MINOR.PATCH`：不兼容变更递增 `MAJOR`，向后兼容的新能力递增 `MINOR`，向后兼容的修复递增 `PATCH`。

每个发布必须在同一变更中更新：

- `pyproject.toml` 和 `uv.lock` 中的版本；
- `releases/<version>.yaml` 发布清单；
- 需要时的数据库迁移、一次性 Job、静态资源和服务重启说明。

`releases/<version>.yaml` 是唯一发布事实来源，不维护面向人工阅读的 changelog，也不得由 Git diff 自动推断升级操作。发布清单须明确其 `dependencies`、`database`、`static`、`once_jobs`、`restart`、`manual`、`verification`。涉及迁移、静态资源或 Supervisor 自身代码时，必须要求重启整个 Supervisor；只有明确列出的 Worker 或 Web 变化才可使用 `services` 级重启。

`runtime/release-state.json` 是端点本地、被 Git 忽略的已确认版本状态，格式为：

```json
{
  "schema_version": 1,
  "applied_version": "1.2.0",
  "applied_commit": "<v1.2.0 tag 指向的 40 位提交 SHA>",
  "applied_at": "2026-07-21T00:00:00Z"
}
```

该文件只能由 `upgrade_init` 或 `upgrade_ack` 原子写入，不应手工编辑或从其他端点复制。`applied_version` 表示本机已完成且确认的发布版本，`applied_commit` 必须与该版本 Git tag 指向的提交一致；`applied_at` 是 UTC 确认时间，不用于决定升级顺序。迁移、一次性 Job 和服务状态分别以 Django 迁移记录、`BackgroundJob` 状态/检查点及 Supervisor 状态为准，不写入本文件。

历史基线也由发布清单维护：`1.0.0` 对应历史提交 `2d522bd932e760094bbe81fb2477f2da1462e359`；`1.0.1` 的原提交 `2905bcb04509e2ffae3877ee1a2aabcc026fa190` 因作者信息重写，当前历史提交为 `12a5a17168a27ee3088b18e708389c4a6c117510`。后续发布使用不可移动的 `v<version>` tag。

## 分支与提升策略

`main` 是唯一的生产发布分支，也是 GitHub Release 和部署端点拉取的唯一来源；它始终对应一个已经发布、可由 tag 精确定位的版本。禁止直接推送 `main`，也禁止将未准备版本号和发布清单的 `develop` 直接合入 `main`。

### `feature/*` 到 `develop`

`develop` 仅是日常集成分支：它汇集已完成评审的 feature，不触发部署、正式 tag、GitHub Release 或生产升级，也不作为 CI 状态门禁。所有 feature 从最新 `develop` 创建，并以 Pull Request 合回 `develop`：

- 每个 feature PR 必须选择 **Squash and merge**，因此在 `develop` 上恰好留下一个提交；五个 feature 合入后，应有五个可回溯的 squash 提交。
- 提交标题遵循 [AGENTS.md](../../AGENTS.md) 的规范；PR 模板中的“变更摘要”成为 squash 提交正文。
- 摘要说明用户或系统行为变化、数据库/配置/服务/升级影响、验证结果以及风险和回滚方式；无影响项明确写“无”。涉及高风险能力时，摘要必须写明隔离范围和最小验证方式。

### 非发布端点同步

跟随 `develop` 或 `feature/*` 的端点没有发布版本状态。每次 `git pull`、`git switch`、`git checkout` 或其他操作使其代码提交变化后，必须在运行任何 `uv run` 项目命令、Django 管理命令、启动 Supervisor 或重启服务前执行：

```bash
uv sync --locked --all-groups
```

该命令成功后才能继续运行或重启服务；失败时停止排障，不得由 Supervisor、Web 或 Worker 自动安装依赖。这一约束只准备与 `uv.lock` 一致的本地环境，不创建版本、tag、发布清单或 `runtime/release-state.json`，也不替代 `main` 的 `upgrade_plan`、`upgrade_ack` 和发布清单流程。

`releases/<version>.yaml` 的 `changes` 只记录平台功能更新：`feat`、`fix`、`hotfix`、`perf` 和 `security` 的实际用户或运行行为变化。它既是升级清单的一部分，也是 GitHub Release 的“平台功能更新”唯一来源。`docs`、`ci`、`build`、`chore`、`refactor`、`style` 和 `test` 不写入 `changes`；若某项重构或维护实际改变了用户或运行行为，必须使用 `feat` 或 `fix` 标题。维护型发布没有平台功能变化时，显式写 `changes: []`，Release 页显示“本版本没有平台功能更新”。

GitHub 不能按目标分支单独强制 merge method：为保留发布后的 `main -> develop` 正常回灌，仓库仍需保留 merge commit。因此“feature 必须 squash”由 PR 审阅流程执行；验证目标是合入后的 Git 历史每个 feature 只有一个 squash 提交，而不是 GitHub 界面只显示一种合并按钮。

### GitHub CLI 操作

先安装 GitHub CLI 并完成一次登录：

```console
gh auth login
```

CLI 可创建、检查和合并 Pull Request，分支本身仍须用 `git push` 推送；它不会绕过必需检查或分支保护。仅当用户已明确授权创建对应 PR 或执行发布时，Agent 才可推送 feature 或 release 分支。个人仓库使用单个 GitHub 身份，不设置必需审批；feature 完成后由 Agent 创建、自审并 squash 合入 `develop`：

```console
git push -u origin feature/<name>
```

#### Agent 创建和自审 PR

本仓库的 feature、release 和回灌 PR 均由 Agent 使用同一个 GitHub 身份创建、自审和合并。由于不是独立身份审阅，流程不将自审称为“审批”，也不配置 GitHub 的必需审批。`.github/PULL_REQUEST_TEMPLATE.md` 只定义正文结构，GitHub CLI 不会替 Agent 判断或补全其中的内容。Agent 必须先检查实际 diff、执行过的验证和风险结论，再填写全部字段；未知或不适用的项明确写“无”，不得提交模板占位符、猜测结果或把未执行的命令写成“通过”。

涉及上游写入、会话、权限或敏感数据时，必须写明隔离范围和可复现的最小验证方式。涉及发布时，还必须说明版本、发布清单和 `changes` 的取值；`changes: []` 应明确说明为“无平台功能更新”。Agent 可以推送自己的功能或发布分支、创建 PR、报告验证结果并执行合并；不得直接推送或强推 `main`、`develop`、已发布 tag。

Agent 必须以 `.github/PULL_REQUEST_TEMPLATE.md` 为唯一正文结构，依据实际 diff、验证和风险结论填充后，保存到**工作区外**的临时文件。创建完成后删除该临时文件，避免把 PR 正文或潜在敏感信息留在仓库中。

```console
gh pr create --base develop --head "feature/<name>" --title "feat(<scope>): <简短中文摘要>" --body-file "<完整填写后的临时 Markdown 文件路径>"
```

创建 release 或回灌 PR 时沿用同一正文约定，并将 `--base`、`--head`、标题和合并策略替换为本节后续对应命令。Agent 完成自审，确认正文完整、提交标题与内容一致后，才执行相应合并。

#### Agent 自审和合并 PR

Agent 必须审查实际 diff、PR 正文与验证证据，并记录自审结论和未覆盖的风险；不能仅因标题或模板完整就合并。高风险能力的最小验证是：确认 PR 所述隔离范围与 diff 一致，并复核正文中的最小验证命令和结果。合并前用 GitHub CLI 查询并记录 PR 状态：

```console
gh pr view <PR 编号或 URL> --json isDraft,baseRefName,headRefName,mergeStateStatus,statusCheckRollup
```

feature 合并前最小验证是：目标分支为 `develop`、来源分支是对应的 `feature/*`、PR 不是草稿、无合并冲突，且正文完整。`main` 还必须确认目标分支为 `main`、来源分支为匹配版本的 `release/v<version>`，并确认 `Release protocol / release-source` 与 `Release protocol / validate` 成功。确认后按目标分支使用固定策略合并：

```console
gh pr merge <feature PR 编号或 URL> --squash --delete-branch
gh pr merge <release PR 编号或 URL> --merge --delete-branch
gh pr merge <main 回灌 PR 编号或 URL> --merge
```

`develop` 不要求 CI 状态。若 `main` 的必需检查尚未满足且希望 GitHub 在检查通过后自动合并，Agent 可对 release PR 添加 `--auto --merge`；feature 与回灌不使用 `--auto`。不得以 `--admin` 绕过保护规则。

准备好版本号、发布清单和发布前验证后，在用户已授权发布的前提下推送并创建 `release/v<version> -> main` PR；随后按上文固定策略以普通 merge 合入 `main`：

```console
git push -u origin release/v<version>
gh pr create --base main --head release/v<version> --title "release: 准备 v<version>"
```

`main` 发布成功后，创建回灌 PR；随后按上文固定策略以普通 merge 回到 `develop`，不得删除 `main`：

```console
gh pr create --base develop --head main --title "chore: 回灌 main 到 develop"
```

### `release/v<version>` 到 `main`

每次正式发布按以下提升路径进行，而不是在 `main` 合并后再补准备发布信息：

1. 从已验证的 `develop` 创建短期 `release/v<version>` 分支并冻结其功能范围。
2. 只用功能相关的 squash 提交生成 `changes` 草稿：

   ```bash
   git log --first-parent --no-merges --extended-regexp --grep='^(feat|fix|hotfix|perf|security)(\([^)]+\))?: ' --format='- %s%n%b' v<上一版本>..develop
   ```

   该命令只汇总提交标题和正文，不能替代发布清单；必须结合实际代码、迁移、配置和服务变更，人工写入 `releases/<version>.yaml`。没有功能相关提交时写 `changes: []`。发布清单仍是升级动作的唯一事实来源。
3. 在 `release/v<version>` 更新版本号、`uv.lock` 和 `releases/<version>.yaml`，执行发布前最小验证。
4. 创建 `release/v<version> -> main` 的发布 PR。`Release protocol` 的 `release-source` 校验会要求分支名与 `pyproject.toml` 中的版本严格匹配；PR 合入 `main` 后才创建 tag、源码附件和 GitHub Release。Release 页面按以下顺序展示：

   1. **平台功能更新**：直接渲染该版本发布清单的 `changes`。
   2. **工程与文档维护**：按时间正序收录 `docs`、`ci`、`build`、`chore`、`refactor`、`style` 和 `test` 提交标题。
   3. **完整提交记录**：链接到上一个 tag 与当前 tag 的 GitHub compare 页面。

   非平台提交仍随 release 分支交付，只是不进入发布清单的 `changes` 或端点升级动作。首次发布没有上一个 tag 时，不显示“完整提交记录”。
5. 发布工作流成功后，将 `main` 正常合回 `develop`，仅用于带回发布提交、tag 对应版本和紧急修复；不要 squash 回灌，避免丢失发布祖先关系。

正式发布由 **`main` 合入已准备好的发布分支** 触发；`develop` 只承担集成。紧急修复从 `main` 创建 `hotfix/*`，完成修复后仍经匹配版本的 `release/v<version>` 进入 `main`，随后再合回 `develop`。

### GitHub 仓库设置（可选强化）

个人私有仓库以本手册前文的 Agent 约定为基础，不将 GitHub 分支保护作为流程前置条件，且**不设置必需审批**。

若当前 GitHub 套餐支持私有仓库的分支保护，可额外配置下列规则作为技术强化；它们不改变上述单账号流程：

- `develop`：要求 Pull Request；启用 squash merge，并将 squash 默认消息设为 Pull Request 标题与描述。验证结果记录在 PR 模板中，但不要求 `develop` 产生或等待 CI 状态。
- `main`：要求 Pull Request、`Release protocol / release-source` 与 `Release protocol / validate`；禁止直接推送。来源分支仍须精确为 `release/v<pyproject.toml 中的版本>`。
- 不允许删除或强制推送 `main`、`develop` 及任何已发布的 `v*` tag；对管理员和拥有分支保护绕过权限的身份也应用上述规则。Agent 不得使用 `gh pr merge --admin`。

最小验证：由 Agent 创建一个仅改文档的 `feature/*` PR，使用 `gh pr view` 确认分支、草稿和冲突状态后以 squash 合入；检查 `develop` 新增恰好一个非 merge 提交且摘要完整。再创建一个包含完整版本清单、名称与版本匹配的 `release/v<version>` PR，确认 `release-source` 和 `validate` 均通过后以普通 merge 合入 `main`，并确认生成指向该提交的 `v<version>` tag。不得用生产端点或上游写入操作验证分支策略。

## 发布新版本

在准备合并到 `main` 的发布分支上执行：

```bash
# 更新 pyproject.toml、uv.lock 和 releases/<version>.yaml 后
uv lock
uv run python manage.py validate_releases
uv run pytest -q tests/test_release_protocol.py

git add pyproject.toml uv.lock releases/<version>.yaml <相关文件>
git commit -m "release: 准备 v<version>"
git push origin <发布分支>
```

通过 Pull Request 合并到受保护的 `main` 后，`Release protocol` 工作流会创建 `v<version>`，并从该 tag 生成 `wechat-content-operations-<version>-src.zip` 及同名 `.sha256` 文件，作为同名 GitHub Release 的附件发布。若同名 tag 已存在，工作流会确认它仍指向当前提交；若 Release 附件已存在，工作流只接受与当前 tag 重建结果字节一致的附件，绝不覆盖不一致附件。不得移动、删除或复用已发布 tag；修复必须递增版本并新增清单。

源码 ZIP 是本服务的部署源码交付物；GitHub Packages 不承担该职责，因此 Packages 页面为空是正常状态。

仅在工作流不可用的紧急情形，才人工创建 tag：

```bash
git tag -a v<version> -m "Release v<version>"
uv run python manage.py validate_releases --require-current-tag
git push origin main --follow-tags
```

此紧急命令只恢复 tag，不会自动创建源码 Release 附件。恢复工作流后必须从同一 `main` 提交重跑；在此之前不存在正式的 Release 源码交付物。不得用其他提交或本地工作区替代该 tag 打包。

## 下载与验证 Release 源码包

从 `v<version>` 对应的 GitHub Release 下载以下两个附件：

- `wechat-content-operations-<version>-src.zip`
- `wechat-content-operations-<version>-src.zip.sha256`

macOS/Linux 校验后解压：

```bash
sha256sum -c wechat-content-operations-<version>-src.zip.sha256
unzip wechat-content-operations-<version>-src.zip
cd wechat-content-operations-<version>
mkdir runtime
cp .env.example runtime/.env
uv sync --locked --all-groups
uv run python manage.py validate_releases --require-current-tag
```

Windows PowerShell 校验：

```powershell
$expected = (Get-Content wechat-content-operations-<version>-src.zip.sha256).Split()[0]
$actual = (Get-FileHash wechat-content-operations-<version>-src.zip -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $expected) { throw '源码包 SHA-256 校验失败' }
Expand-Archive wechat-content-operations-<version>-src.zip -DestinationPath .
```

解压后在版本目录创建 `runtime/`，复制 `.env.example` 为 `runtime/.env`，再运行锁定依赖安装和上述发布协议校验。随后按本手册的端点升级步骤执行；源码包不能替代迁移、一次性 Job、重启或 `upgrade_ack`。

## 端点升级

拉取后始终先生成计划，不要凭提交信息猜测动作：

```bash
git pull --ff-only origin main
uv sync --locked --all-groups
uv run python manage.py upgrade_plan --json
```

按 `upgrade_plan` 输出逐项执行：

1. `database=migrate`：运行 `uv run python manage.py migrate`。
2. `static=collectstatic`：运行 `uv run python manage.py collectstatic --noinput`，或按清单重启整个 Supervisor，使其执行启动预检。
3. 每项 `once_jobs`：使用清单给出的 `enqueue` 命令入队，保持指定 Worker 运行，再执行 `upgrade_plan --json`，直到任务 `complete=true`。
4. `restart.mode=supervisor`：停止当前 Supervisor 后执行 `uv run python -m service_supervisor`；`restart.mode=services`：仅重启清单列出的服务。
5. 逐条完成 `verification` 命令和健康检查。

当发布包含 Django 数据迁移时，发布清单必须声明 `database=migrate` 与 `restart.mode=supervisor`。若发布不新增一次性 Job 或长期 Worker，应明确将 `once_jobs` 置为空并沿用既有 Supervisor 服务；执行 `migrate` 后重启既有 Supervisor 即可加载 Web 与 Worker 的同一套代码。

当发布新增或调整游戏圈会话 IPC、`GameCircleLoginRuntimeState` 或 `game_circle_session` 的常驻职责时，必须声明 `database=migrate`（如有迁移）和 `restart.mode=supervisor`。会话服务重启会主动丢弃内存中的短期 lease/Access 并重新探测；升级过程不得复制 `runtime/supervisor/game-circle-session.json`、Cookie、token 或未完成的人工写请求。人工写审计为“待回读确认”时，先回读确认状态，绝不自动重放。

本变更移除旧 `GAME_CIRCLE_LOGIN_PROBE` 和 `GAME_CIRCLE_SESSION_REFRESH` consumer。升级时，先停止旧 Supervisor/会话 Worker，完成迁移并启动新代码前运行一次：

```powershell
uv run python manage.py terminalize_legacy_game_circle_session_jobs
```

该命令会取得旧 Worker 的同一任务锁；锁仍被占用时会拒绝执行。它只把活动旧 Job 标为 `RELEASED_LEGACY_SESSION_JOB`，保留参数、checkpoint 和审计记录；可重复执行。随后重启 Supervisor，由新会话服务启动 Chrome、建立外层会话与当前游戏 Frame。最小验证：在 `/admin/` 确认外层会话与 Frame 分别显示正确状态，并对一个非写入来源读取确认无 lease 时不会创建浏览器 tab。

完成后，以计划的 `to_version` 运行确认命令，并且只传入计划实际要求的 `--confirm-*` 参数：

```bash
uv run python manage.py upgrade_ack 1.2.0 \
  --confirm-restart --confirm-verification
```

`upgrade_ack` 会拒绝未应用迁移、未完成精确一次性 Job 或缺少确认的升级，并原子更新本机状态文件。

旧端点没有 `runtime/release-state.json` 时，先人工核验其历史版本、迁移和运行态，再一次性建立基线；该命令不能覆盖已有状态文件：

```bash
uv run python manage.py upgrade_init 1.1.1 --confirmed
```

## 最小验证

发布前最小验证是 `validate_releases`、`tests/test_release_protocol.py` 与 `tests/test_release_source_archive.py` 均通过。首次启用源码 Release 的版本还必须下载两个附件，完成 SHA-256 校验、解压后的 `uv sync --locked --all-groups` 与 `validate_releases --require-current-tag`。端点升级后最小验证是：`upgrade_plan --json` 显示无待处理操作、`upgrade_ack` 成功、`/healthz` 返回 `{"status": "ok"}`；涉及一次性 Job 时还须确认其 `complete=true`。
