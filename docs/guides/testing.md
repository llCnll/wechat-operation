# 测试执行指南

默认测试在 `config.test_settings` 中运行。该设置会把数据库、Worker/Scheduler 锁、Supervisor 文件与静态输出放入每个 worker 独立的 `.pytest-runtime/<worker-id>/`，不会读取或修改仓库 `runtime/` 与其中的 `.env`。

在新建 worktree、拉取代码或切换到 `develop`、`feature/*` 的新提交后，先执行 `uv sync --locked --all-groups`；同步失败时不要运行 pytest。完整的非发布分支边界见[根 README 的开发与集成端点同步](../../README.md#开发与集成端点同步)。

## 常用命令

```powershell
# 干净数据库的正确性基线
uv run pytest -q

# 本地循环：复用测试数据库；lease、token、日志和锁仍会在会话开始时清理
uv run pytest -q --reuse-db

# 迁移变化、数据库异常或需要验证建库流程时重建
uv run pytest -q --create-db

# 观察耗时热点
uv run pytest -q --durations=30 --durations-min=0.5

# 可选并行开发命令；固定三 worker，不使用 -n auto
uv run pytest -q -n 3
```

CI 和发布前验证使用不带 `--reuse-db` 的干净数据库命令。测试中未经 mock 的 httpx 请求会在连接前失败；需要真实本地 HTTP 集成时，用 `@pytest.mark.allow_http_hosts("127.0.0.1")` 明确列出允许主机。该 marker 仅接受 `localhost` 或 loopback IP，不能用来放行外部域名。真实模型端到端测试不属于当前测试套件。
