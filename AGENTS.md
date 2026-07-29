# Agent 协作约定

## 方案与文档

- 高风险能力必须写清最小验证方式。
- 文档与规格优先于实现。
- 对优化项、change 和技术方案，必须先独立理解目标、约束、风险和替代方案，再决定是否执行。
- 评估方案时，优先结合仓库现状，并主动查阅官方文档、一手资料或当前最佳实践，不直接照单执行用户给定方案。
- 如果用户提出的优化或实现方案与更优方案、既有架构、兼容性、成本或风险约束冲突，可以拒绝原方案，但必须明确说明理由、权衡与替代建议。

## Git 分支规范

- 日常开发分支必须使用 `feature/<简短英文描述>` 命名，并从最新 `develop` 创建。
- 紧急修复使用 `hotfix/<简短英文描述>`，发布使用 `release/v<version>`。
- 仓库分支规范优先于工具、桌面环境或 Agent 的默认分支前缀；不得使用 `codex/*` 作为项目开发分支。

## Git 提交规范

- 提交格式为 `<type>(<scope>): <简短中文摘要>`；无 `scope` 时使用 `<type>: <简短中文摘要>`。
- `type` 使用英文：`feat`、`fix`、`hotfix`、`perf`、`security`、`docs`、`ci`、`build`、`chore`、`refactor`、`style`、`test`、`release`。
- 冒号后的摘要必须使用中文，准确描述实际改动；英文 `type` 用于发布分类和自动化处理。
- 未经用户明确要求，不得执行 `git commit`、`git commit --amend`、`git reset`、`git rebase` 或 `git push`。

## 非发布分支环境同步

- 当 Agent 在端点上拉取或切换到 `develop`、`feature/*` 分支的新代码后，必须先执行 `uv sync --locked --all-groups`，再运行任何 `uv run` 项目命令、Django 管理命令、Supervisor 启动或服务重启操作。
- `uv sync --locked --all-groups` 失败时必须停止后续启动或重启；不得将依赖安装逻辑放入 Supervisor、Worker 或 Web。
- 此约束只解决非发布分支的锁定依赖准备，不替代 `main` 的发布清单、`upgrade_plan` 与 `upgrade_ack` 正式升级流程。

## 测试执行

- 修改测试、运行项目 pytest 或调整测试运行态前，必须先阅读 `docs/guides/testing.md`，并遵循其中的运行态隔离、HTTP 护栏、数据库复用、并行度和 CI 验证规则。
- 不得使用 `-n auto`；具体测试命令以 `docs/guides/testing.md` 为唯一来源。
