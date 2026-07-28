# 文档中心

根目录 [README](../README.md) 是首次启动和已有端点升级的简明入口。本文档中心按任务提供详细手册、产品与技术资料；OpenSpec 主规格和归档位于仓库根目录 `openspec/`，不与业务文档混放。

## 按任务查找

### 启动与运行

- [README：快速启动与升级入口](../README.md)：基础 Web 首次启动、已有端点升级和常用入口。
- [部署与首次初始化](guides/deployment-and-initialization.md)：uv、运行态、Supervisor、受控 Chrome、扫码、AI/OCR 与 Worker 启用。
- [发布与多端升级](guides/release-and-upgrade.md)：版本、发布清单、端点升级、一次性 Job 和重启约束。
- [视频审核手册](guides/video-review-runbook.md)：FFmpeg、受控样本预检、ASR 降级、视频 Worker 启停与回滚。
- [系统总览](guides/system-overview.md)：模块、数据流、工作台交互和安全边界。
- [运行与恢复手册](guides/operations-runbook.md)：打包、备份、会话、Worker 与故障恢复。

### 产品与规划

- [内容运营平台需求方案](prd/content-operations-platform-requirements.md)：业务痛点、目标、范围、验收和后续规划；规划不等同于已交付能力。
- [回复 FAQ 资料维护](guides/reply-faq-guidance.md)：受控 FAQ 原始资料、运行时 JSON、版本递增、固定样本与 Worker 重启要求。

### 技术实现

- [数据库数据字典](technical/database-schema.md)：当前业务表、字段、约束、枚举和状态位。
- OpenSpec 主规格（仓库根目录 `openspec/specs/`）：当前可验证的行为契约。

### 上游接口调研

- [微信小游戏圈帖子管理调研](research/wechat-game-circle-original-posts-research.md)：已验证的原平台读取、处置与状态回读契约。
- [微信小游戏圈首页指标接口调研](research/wechat-game-circle-home-metrics-research.md)：首页概览、详细数据的字段映射、会话边界与最小验证方式。
- [微信小游戏圈每日指标接口调研](research/wechat-game-circle-daily-metrics-research.md)：内容粉丝、游戏圈粉丝的历史查询能力、字段映射与保存边界。
- [微信小游戏圈数据分析接口调研](research/wechat-game-circle-data-analysis-research.md)：内容消费、创作、分享、留存的范围查询，以及排名、能力卡和配置接口。
- [微信公众平台二维码登录调研](research/wechat-mp-qr-login-research.md)：登录页、二维码资源与登录后游戏目录的已验证边界。
- [游戏圈会话服务设计](research/game-circle-session-service-design.md)：会话所有权、受控 Chrome、Frame 和 IPC 的设计边界。
- [游戏圈会话刷新调研](research/wechat-game-circle-session-refresh-research.md)：外层 token、来源身份与 `RECOVER`/`KEEPALIVE` 刷新路径的受控验证。
- [会话韧性演练记录](guides/session-resilience-drill-2026-07-23.md)：受控 Chrome、会话服务和扫码恢复的最小演练证据。

## 建议阅读路径

1. 新部署或由 Agent 接手：先执行根目录 [README](../README.md)，再按[部署与首次初始化](guides/deployment-and-initialization.md)启用受控 Chrome、外部能力和 Worker；不要从单个 Worker 命令开始。
2. 已有端点更新：先读[发布与多端升级](guides/release-and-upgrade.md)，从 `upgrade_plan --json` 开始，不要凭提交信息猜测操作。
3. 运营人员：阅读[系统总览](guides/system-overview.md)，再按需查看[运行与恢复手册](guides/operations-runbook.md)或[视频审核手册](guides/video-review-runbook.md)。
4. 开发或排障：先查[数据库数据字典](technical/database-schema.md)，再查对应接口调研与 OpenSpec 主规格。
5. 评估范围或后续迭代：阅读[内容运营平台需求方案](prd/content-operations-platform-requirements.md)。

## 文档边界

- **当前实现事实**以代码、已执行迁移和运行中的数据库为准；数据字段以数据字典为准。
- **OpenSpec 主规格**描述当前应可验证的行为契约；实现变更应先更新主规格或对应变更产物。归档只保留历史，不应回写。
- **产品需求**描述目标状态和后续规划，不等同于已交付功能；当前能力以系统总览、数据字典和代码为准。
- **使用说明**描述当前可运行行为与人工操作流程。
- **技术文档**描述当前实现的模型、约束和运行边界。
- **接口调研**仅记录已授权浏览器会话中观察或验证的原平台私有接口，不记录 Cookie、令牌、完整认证 URL 或作者身份标识。
