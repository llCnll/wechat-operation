## Why

当前全量 pytest 约需 3 分 17 秒，并会因开发机的 AI 配置、外部网络状态及正在运行的 Scheduler 共享 `runtime` 文件锁而变慢或失败。测试套件必须在不依赖本机服务、外网或真实模型的前提下稳定运行；否则耗时、结果和开发反馈都不可预测。

本次变更先消除测试运行态泄漏和真实 I/O 等正确性问题，再降低重复执行的数据库准备成本；并行只在隔离方案落地并通过明确门槛后评估，避免为了速度放大 SQLite 与文件共享状态的随机失败。

## What Changes

- 为 pytest 建立受控的 `.pytest-runtime/` 测试状态根目录：数据库可按 `--reuse-db` 复用，lease、token、心跳、日志和 Worker/Scheduler 锁等易失运行态每会话隔离并清理，绝不接触仓库 `runtime/`。
- 使用 pytest-django 直接加载的测试专用 settings 模块和受路径边界验证的配置，不将运行态目录覆盖扩展为通用生产环境变量或读取开发 `.env`。
- 默认关闭可选 AI 与外部集成，并为项目的 httpx 发送边界建立快速失败护栏；模型行为使用固定脱敏响应、fake 或异常模拟覆盖。
- 提供干净数据库、`--reuse-db`、`--create-db` 和 durations 观测的标准测试命令及使用边界。
- 在串行验证稳定后，以明确的每 worker 数据库与运行态派生规则先试验 `pytest-xdist -n 2`，再以实测结果选择固定小并发；仅在无隔离问题且有实测收益时推荐 `-n 3` 为可选命令。

## Capabilities

### New Capabilities

- `test-suite-execution-isolation`: 定义 pytest 的运行态、外部 HTTP、数据库复用、性能观测与受门控并行的隔离和执行契约。

### Modified Capabilities

- 无。

## Impact

- 受影响的测试基础设施：测试专用 settings/fixture、`pyproject.toml`、测试命令文档、`.gitignore` 和可能的开发依赖锁定文件。
- 受影响的配置边界：`config.settings` 的测试专用运行态解析、`jobs.worker` 锁目录派生、`service_supervisor.runtime` 文件状态，以及 `reporting.public_opinion_conclusion` 的 HTTP 测试方式。
- 不改变生产服务的 AI、Scheduler、Worker、RuntimeStore 或既有 `runtime/.env` 行为；真实模型端到端冒烟不属于本 change，后续如需引入必须单独提案。
