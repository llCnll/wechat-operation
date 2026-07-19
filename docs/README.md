# 文档中心

本文档目录按用途组织。根目录 [README](../README.md) 是唯一的启动入口；OpenSpec 主规格和归档位于仓库根目录 `openspec/`，不与业务文档混放。

## 建议阅读顺序

1. 新部署或由 Agent 接手：先执行根目录 [README](../README.md) 的“启动流程（Agent 执行清单）”，不要从单个 Worker 命令开始。
2. 运营人员：阅读[系统总览](guides/system-overview.md)，再按需查看[运行与恢复手册](guides/operations-runbook.md)。
3. 开发或排障：先查[数据库数据字典](technical/database-schema.md)，再查对应的接口调研与 OpenSpec 主规格。
4. 评估范围或后续迭代：阅读[内容运营平台需求方案](prd/content-operations-platform-requirements.md)。其中的规划不等同于当前功能。

| 分类 | 文档 | 适用场景 |
| --- | --- | --- |
| 产品需求 | [内容运营平台需求方案](prd/content-operations-platform-requirements.md) | 明确业务痛点、目标、范围、验收与后续规划。 |
| 使用说明 | [系统总览](guides/system-overview.md) | 快速理解当前模块、数据流、工作台交互和安全边界。 |
| 使用说明 | [运行与恢复手册](guides/operations-runbook.md) | 部署、备份、会话和 Worker 故障恢复。 |
| 技术文档 | [数据库数据字典](technical/database-schema.md) | 查询当前业务表、字段、约束、枚举和状态位。 |
| 接口调研 | [微信小游戏圈帖子管理调研](research/wechat-game-circle-original-posts-research.md) | 查看已验证的原平台读取、处置与状态回读契约。 |
| 接口调研 | [微信公众平台二维码登录调研](research/wechat-mp-qr-login-research.md) | 查看登录页、二维码资源与登录后游戏目录的已验证边界。 |

## 文档边界

- **当前实现事实**以代码、已执行迁移和运行中的数据库为准；数据字段以数据字典为准。
- **OpenSpec 主规格**描述当前应可验证的行为契约；实现变更应先更新主规格或对应变更产物。归档只保留历史，不应回写。
- **产品需求**描述目标状态和后续规划，不等同于已交付功能；当前能力以系统总览、数据字典和代码为准。
- **使用说明**描述当前可运行行为与人工操作流程。
- **技术文档**描述当前实现的模型、约束和运行边界。
- **接口调研**仅记录已授权浏览器会话中观察或验证的原平台私有接口，不记录 Cookie、令牌、完整认证 URL 或作者身份标识。
