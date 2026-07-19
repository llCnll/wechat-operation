## Why

当前“原平台已删除”由 `business_deleted` 和 `deleted_at` 两个字段表示，而屏蔽、沉底由 `status` 表示。同一个帖子的可运营状态分散在多个字段，工作台、筛选和刷新需要分别判断，且无法保留“删除前已屏蔽或沉底”的组合状态。

## What Changes

- **BREAKING** 删除 `posts_originalpost.business_deleted` 与 `posts_originalpost.deleted_at`。
- 将原平台已删除定义为本地 `status` 状态位 `128`；屏蔽 `4`、沉底 `8` 可与删除位组合保存。
- 将正常定义为唯一基值 `2`，不作为可组合状态位；禁止生成“正常 + 删除”等无意义组合值。
- 调整帖子刷新、工作台展示、筛选、处置保护和数据迁移逻辑，统一通过状态位判断删除、屏蔽和沉底。
- 补充数据字典和运行行为说明，明确删除状态来自管理详情的业务错误，且不能写回原平台。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `original-post-ingestion`: 将业务删除判定和刷新后的持久化状态改为 `status` 删除状态位。
- `ai-review-workflow`: 将工作台处置可用性与上游操作保护改为组合状态位判定。

## Impact

影响 `posts` 模型、迁移、刷新服务、工作台查询与模板，以及相关单元测试；影响 `reviews` 的上游操作前置校验和状态回读。SQLite 中已有删除标记的数据需要迁移到 `status`，独立的删除确认时间将不再保留。
