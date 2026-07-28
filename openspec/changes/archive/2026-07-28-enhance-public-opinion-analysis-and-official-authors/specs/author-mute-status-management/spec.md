## ADDED Requirements

### Requirement: 作者详情读取的最小主题页

系统 SHALL 通过固定作者详情接口以 `page=0`、`page_size=3` 读取作者身份和禁言状态。调用方 MUST 仅使用响应中的 `data.user` 完成 OpenID 一致性校验、昵称投影、当前游戏禁言截止和全局禁言截止同步；不得依赖、持久化或为获取更多主题列表而分页读取作者主题。

#### Scenario: 缩小作者详情主题页后完成身份核验
- **WHEN** 禁言前核验、禁言写后回读或作者状态刷新读取一个已保存 OpenID
- **THEN** 请求 SHALL 使用页大小 3，并在 `data.user.openid` 与请求 OpenID 一致时继续既有同步或作者操作语义
- **THEN** 返回主题列表的数量或内容不得影响作者身份、禁言状态或审计结论

#### Scenario: 缩页响应缺少可核验作者
- **WHEN** 页大小为 3 的作者详情响应缺失 `data.user`、OpenID 或必要禁言字段，或返回 OpenID 不一致
- **THEN** 系统 SHALL 按既有固定脱敏契约失败，不得覆盖已有作者状态或发送上游写入
