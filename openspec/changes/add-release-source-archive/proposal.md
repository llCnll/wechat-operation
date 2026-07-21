## Why

当前发布工作流只创建不可移动的 Git tag，运维人员仍需手工打包，且无法从 GitHub Release 获取与 tag 精确对应、可校验的部署源码包。需要将源码交付物纳入发布协议，使部署端有统一、可追溯的下载入口。

## What Changes

- 在每次 `main` 发布验证并创建或核验版本 tag 后，从该 tag 生成源码 ZIP 与 SHA-256 校验文件。
- 创建或更新对应的 GitHub Release，并将源码 ZIP 和校验文件作为 Release 附件发布。
- 明确源码包仅包含 Git 跟踪内容，不包含 `runtime/`、Git 元数据、虚拟环境、数据库或其他本地运行态。
- 更新发布与运维文档，说明下载、校验、解压和初始化流程，以及工作流失败时的处置边界。

## Capabilities

### New Capabilities

- `release-source-archive`: 为每个不可移动发布 tag 生成、校验并发布可部署源码归档。

### Modified Capabilities

无。

## Impact

- 修改 `.github/workflows/release-protocol.yml` 的发布阶段及其 `contents: write` 权限使用。
- 更新 `docs/guides/release-and-upgrade.md` 与 `docs/guides/operations-runbook.md`。
- GitHub Release 成为源码包的分发入口；GitHub Packages 不在本变更范围内。
