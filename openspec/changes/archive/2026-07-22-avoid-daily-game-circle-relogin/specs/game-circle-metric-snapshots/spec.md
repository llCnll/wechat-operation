## ADDED Requirements

### Requirement: 指标读取的执行快照和会话服务

指标 Worker SHALL 通过 `GameCircleSessionService` 的固定本地 IPC 契约读取数据，并仅提交 execution scope、指标来源和业务分页参数。它 MUST NOT 自行扫描浏览器页面、持有/跨游戏复用身份 token，或为每个指标请求调用身份绑定。

会话服务 SHALL 使用 ACTIVE SessionLease；新插件、服务/浏览器 generation 变化、插件替换和来源查询的已验证 `errcode=-2` 的身份处理均由服务完成。指标来源异常的刷新判断与帖子来源一致；`errcode=1` 和未验证业务码不得被误判为身份失效。

插件失效时，指标 Worker SHALL 保留日期块/分页 checkpoint，并在会话服务返回活动刷新状态时延后，不增加业务 retry；会话服务负责创建或复用刷新 Job。指标 MUST NOT 写入零值、空值或猜测值，也不得覆盖已有有效快照。

#### Scenario: 同一插件 generation 的多个指标请求
- **WHEN** 多个指标请求在同一已验证 PLUGIN 和 target AppID 中连续执行
- **THEN** 它们 SHALL 由会话服务使用同一 ACTIVE SessionLease
- **THEN** 插件 generation 改变后，服务 SHALL 发布新的 ACTIVE SessionLease 后再处理新请求
