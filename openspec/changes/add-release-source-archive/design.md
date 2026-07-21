## Context

当前 `Release protocol` 在 `main` 推送通过发布清单校验后创建或核验 `v<version>` tag，但不创建 GitHub Release，也没有部署源码交付物。仓库是 Django 服务，部署所需的是与发布 tag 精确对应的源码归档，而不是可供 Python 依赖方安装的 PyPI/GitHub Packages 包。

发布包不得包含端点运行态；特别是 `runtime/` 可能含数据库、密钥、锁、日志和收集后的静态文件。现有版本协议以不可移动 tag 与发布清单为事实来源，新的产物必须延续该可追溯性边界。

## Goals / Non-Goals

**Goals:**

- 每个由受控 `main` 发布流程创建或核验的 `v<version>` tag 都有一个与 tag 内容一致的源码 ZIP 和 SHA-256 校验文件。
- 在 GitHub Release 中以可下载的附件分发该 ZIP 与校验文件。
- 产物生成、校验和发布可安全重跑；不得在已有附件与当前 tag 产物不一致时静默覆盖。
- 向运维人员提供下载、校验、解压和初始化的明确步骤。

**Non-Goals:**

- 不发布 PyPI、npm、Docker、GitHub Packages 或二进制安装包。
- 不将 `runtime/`、数据库、密钥或用户配置放入归档。
- 不改变版本号、tag 不可移动规则、升级计划或端点确认协议。

## Decisions

### 使用 GitHub Release 附件，而非 GitHub Packages

GitHub Release 与现有版本 tag 一一对应，适合交付部署源码和校验文件；页面也会提供清晰的版本下载入口。GitHub Packages 需要选择并发布具体包生态的包类型，适合可被依赖安装的库或镜像，不符合本服务当前的部署模式。

替代方案是只依赖 GitHub 为 tag 自动提供的 Source code ZIP/TAR。该方案没有自定义、可校验的附件和发布说明，无法表达运维源码包契约，因此不采用。

### 从已验证 tag 使用 `git archive` 构建 ZIP

工作流必须在 tag 创建或核验后，以 `git archive` 的 tag 引用生成 `wechat-content-operations-<version>-src.zip`，并为归档内文件加统一的顶层目录。`git archive` 只导出 Git 跟踪内容，避免将 runner 工作区或端点本地文件混入产物；构建程序还必须检查归档中没有 `runtime/`、`.git/`、`.venv/` 等禁止路径。

替代方案是使用 `Compress-Archive`/`zip` 打包工作区。它依赖排除清单，容易在新增本地目录时遗漏，且无法证明内容来自发布 tag，因此不采用。

### 发布 SHA-256 并保持附件不可静默改写

工作流为 ZIP 生成同名 `.sha256` 文件并一同上传。若 Release 已存在，工作流必须验证已有附件与当前 tag 构建结果一致；缺失附件可以补传，不一致必须失败并要求人工调查，而不是使用覆盖上传。这使重跑可恢复网络或上传中断，但不会掩盖供应链或人工修改问题。

替代方案是每次重跑以 `--clobber` 覆盖附件。虽然实现简单，但会使同一版本的交付物可变，故不采用。

### 使用 GitHub CLI 发布

Ubuntu GitHub Actions runner 自带 `gh`，且当前发布 job 已授予 `contents: write`。归档命令负责调用 `gh release create`、`gh release view`、`gh release download` 和 `gh release upload`，并将创建、补传和不一致失败的状态机纳入 Python 测试；工作流只负责 tag 校验、调用命令和提供权限，不新增第三方 Action 的供应链依赖。Release 说明使用 GitHub 生成的发布说明；升级操作仍以仓库中的 `releases/<version>.yaml` 为唯一事实来源。

替代方案是将完整发布状态机直接写在工作流 Bash 中，或引入第三方 Release Action。前者难以单测且会使 YAML 成为业务代码，后者会引入额外受信代码与版本维护成本，故不采用。

## Risks / Trade-offs

- [已有 Release 资产与 tag 重建结果不一致] → 工作流失败并保留资产，要求维护者比较 SHA-256 和 tag 指向提交后再人工处置，绝不覆盖。
- [Release 创建成功但上传中断] → 重跑时检测缺失资产并补传；发布说明和 tag 不会重建或移动。
- [未来将敏感运行态误加入 Git] → 归档构建的禁止路径检查失败；代码审查仍须阻止敏感文件提交。
- [大归档增加 Actions 时间和 Release 存储] → 仅打包跟踪文件；不增加依赖缓存、虚拟环境或运行态。
- [GitHub API/CLI 短暂失败] → 工作流失败，维护者重跑相同提交；不通过本地工作区重新打包其他提交。

## Migration Plan

1. 合并工作流、构建工具、测试和文档变更。
2. 在下一个正常版本发布中由 `main` 工作流创建 tag、Release 与两个附件。
3. 下载该 Release 的 ZIP，按校验文件验证 SHA-256，解压后执行锁定依赖安装和发布清单校验，确认其可作为部署源码。
4. 若发布步骤失败，修复工作流或 GitHub 权限后从同一 `main` 提交重跑；不得移动、删除或复用 tag。若附件不一致，停止自动化并人工调查。

回滚只需移除后续版本中的 Release 发布步骤；已经发布的 tag 和附件保持不变，不影响现有端点从 Git 拉取并执行升级协议。

工作流不可用时的人工 tag 仅恢复版本 tag，不自动生成 Release 附件；恢复工作流后必须从同一 `main` 提交重跑。工作流恢复前不存在正式的 Release 源码交付物。

## Open Questions

无。源码包文件名、Release 分发方式和校验算法已确定。
