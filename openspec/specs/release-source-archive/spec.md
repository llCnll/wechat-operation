## Purpose

为受控 `main` 发布流程提供与不可移动版本 tag 精确对应、可校验且可下载的部署源码归档。该能力将 GitHub Release 作为服务源码交付入口，不发布依赖包、容器镜像或端点运行态，并要求归档附件在重跑时保持可验证的不可静默改写边界。

## Requirements

### Requirement: 发布 tag 的源码归档
系统 SHALL 在 `main` 推送的发布校验成功且 `v<version>` tag 已创建或核验后，从该 tag 生成名为 `wechat-content-operations-<version>-src.zip` 的源码归档。归档 SHALL 只包含 tag 中的 Git 跟踪文件，并 SHALL 使用以版本命名的单一顶层目录。

#### Scenario: 新版本成功发布
- **WHEN** `main` 上的版本、发布清单和发布校验均有效，且不存在对应 tag
- **THEN** 系统创建 `v<version>` tag，并从该 tag 生成对应版本的源码 ZIP

#### Scenario: 已有正确 tag 的工作流重跑
- **WHEN** `v<version>` 已存在且指向当前 `main` 提交
- **THEN** 系统仅从该 tag 重建相同版本的源码 ZIP，且不得移动或重建 tag

### Requirement: 归档运行态隔离
系统 SHALL 在发布前验证源码归档不包含 `runtime/`、`.git/`、`.venv/`、数据库、锁、日志或其他端点本地运行态；任一禁止内容存在时，系统 MUST 使发布失败并且不得上传该归档。

#### Scenario: 归档包含禁止路径
- **WHEN** 构建后的归档包含 `runtime/`、`.git/` 或 `.venv/` 路径
- **THEN** 系统使发布 job 失败，并且不创建或覆盖该版本的源码附件

### Requirement: 源码归档完整性校验
系统 SHALL 为每个源码 ZIP 生成 SHA-256 校验文件，并将校验文件名与 ZIP 文件名关联。校验文件 SHALL 与 ZIP 一同作为版本发布附件提供。

#### Scenario: 运维下载源码包
- **WHEN** 运维人员打开已发布版本的 GitHub Release
- **THEN** 其可下载对应版本的源码 ZIP 及 SHA-256 校验文件，并用校验文件验证 ZIP

### Requirement: 幂等且不可静默改写的 Release 发布
系统 SHALL 为每个由受控 `main` 发布流程创建或核验的 `v<version>` tag 创建或复用同名 GitHub Release。Release 不存在时系统 SHALL 创建它并上传源码 ZIP 与校验文件；Release 已存在时系统 SHALL 仅补传缺失附件，并 MUST 在已有附件与从 tag 重建的结果不一致时失败，且不得覆盖已有附件。

#### Scenario: 上传中断后的安全重跑
- **WHEN** Release 已创建但源码 ZIP 或校验文件尚未上传
- **THEN** 同一提交的工作流重跑会补传缺失附件，并保留原有 tag 和 Release

#### Scenario: 检测到同名附件不一致
- **WHEN** 已有 Release 附件的校验结果与当前 tag 重建的校验结果不同
- **THEN** 系统使发布 job 失败并提示人工调查，且不得覆盖已有附件

#### Scenario: 工作流不可用时人工创建 tag
- **WHEN** 维护者因工作流不可用而按紧急流程创建发布 tag
- **THEN** 系统不承诺自动创建 Release 附件，文档要求维护者恢复工作流后从同一提交重跑

### Requirement: 发布源码包的运维指引
系统 SHALL 在发布与运行文档中说明 Release 源码包的下载、SHA-256 校验、解压、运行态初始化和最小验证步骤，并说明 GitHub Packages 不承担本服务源码包分发职责。

#### Scenario: 新端点使用 Release 源码包部署
- **WHEN** 运维人员按照文档下载并校验某版本的源码 ZIP
- **THEN** 其可在解压目录创建 `runtime/`、从示例配置初始化运行态，并完成锁定依赖安装和发布协议校验

#### Scenario: 首个自动 Release 验收
- **WHEN** 新工作流发布首个版本
- **THEN** 维护者下载两个附件，确认 SHA-256 校验通过，并在解压目录完成锁定依赖安装和发布协议校验
