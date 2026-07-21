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

## 发布新版本

在准备合并到 `main` 的发布分支上执行：

```bash
# 更新 pyproject.toml、uv.lock 和 releases/<version>.yaml 后
uv lock
uv run python manage.py validate_releases
uv run pytest -q tests/test_release_protocol.py

git add pyproject.toml uv.lock releases/<version>.yaml <相关文件>
git commit -m "release: prepare v<version>"
git push origin <发布分支>
```

通过 Pull Request 合并到受保护的 `main` 后，`Release protocol` 工作流会创建 `v<version>`；若同名 tag 已存在，工作流会确认它仍指向当前提交。不得移动、删除或复用已发布 tag；修复必须递增版本并新增清单。

仅在工作流不可用的紧急情形，才人工创建 tag：

```bash
git tag -a v<version> -m "Release v<version>"
uv run python manage.py validate_releases --require-current-tag
git push origin main --follow-tags
```

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

发布前最小验证是 `validate_releases` 与 `tests/test_release_protocol.py` 均通过。端点升级后最小验证是：`upgrade_plan --json` 显示无待处理操作、`upgrade_ack` 成功、`/healthz` 返回 `{"status": "ok"}`；涉及一次性 Job 时还须确认其 `complete=true`。
