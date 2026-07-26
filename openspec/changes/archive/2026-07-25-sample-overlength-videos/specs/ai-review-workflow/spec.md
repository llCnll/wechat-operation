## MODIFIED Requirements

### Requirement: 视频资源限制的可解释审核证据
系统 SHALL 在内容管理的“视频审核证据”卡片中，以运营可读说明展示当前视频提取的确定性范围与受限原因。系统 SHALL 在创建提取记录时持久化本次采用的 `VIDEO_MAX_DURATION_SECONDS` 采样预算快照；媒体探测后 SHALL 保存实际时长，但仅在关键帧候选循环完整结束后才 SHALL 保存排序、互不重叠的 `sampled_intervals_ms`。对于当前记录存在多个采样区间且错误码为 `video_duration_distributed_sampling` 的情况，系统 SHALL 显示实际时长、历史采样预算、每个实际覆盖区间、未覆盖其他时间以及未执行 ASR。对于 `SKIPPED_LIMIT + video_duration_limit_exceeded` 的历史记录，系统 SHALL 保留原有“超过上限未进行提取”的解释。AI 审核输入 SHALL 对多个采样区间附加相同的范围与 ASR 限制说明，以避免模型将局部视频证据作为整段结论。展示和模型输入 SHALL 不读取当前运行配置，不包含视频 URL、认证信息、底层命令输出或未脱敏异常。

#### Scenario: 新视频超过采样预算
- **WHEN** 当前视频提取时长为 90 秒、历史采样预算为 60 秒，并以 `PARTIAL + video_duration_distributed_sampling` 完成
- **THEN** 视频审核证据卡和 AI 输入 SHALL 说明“视频时长 90 秒，采样预算 60 秒；已覆盖 0:00–0:20、0:35–0:55、1:10–1:30，未覆盖其他时间，未执行 ASR”，并保留该范围内已得到的视频证据

#### Scenario: 预算内完整覆盖视频
- **WHEN** 当前视频提取的采样区间为唯一 `[0,D)`
- **THEN** 视频审核证据卡和 AI 输入 SHALL 将该视频视为完整覆盖，不得添加“未覆盖其他时间”或“未执行 ASR”的分布式采样说明

#### Scenario: 历史视频时长超过配置上限而未提取
- **WHEN** 当前视频提取以 `SKIPPED_LIMIT + video_duration_limit_exceeded` 完成
- **THEN** 视频审核证据卡 SHALL 显示“视频时长 {实际时长}，超过提取时资源与采样时长上限 {配置时长}，未进行提取”，同时不显示通用无证据提示作为该原因的替代

#### Scenario: 配置变更后查看历史时长结果
- **WHEN** 视频提取完成后，运行配置的 `VIDEO_MAX_DURATION_SECONDS` 被修改
- **THEN** 视频审核证据卡与 AI 输入 SHALL 继续使用该提取记录保存的历史预算快照和采样区间，不得使用修改后的当前配置解释历史结果

#### Scenario: 其他无视频证据的情况
- **WHEN** 当前视频提取没有可展示的视频证据，且并非已识别的时长超限历史记录
- **THEN** 系统 SHALL 保留现有的通用人工核验提示或相应的其他脱敏原因说明，不得将其误标为时长超限
