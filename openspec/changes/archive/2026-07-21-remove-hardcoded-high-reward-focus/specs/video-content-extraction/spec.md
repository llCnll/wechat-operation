## MODIFIED Requirements

### Requirement: 首尾高密度且感知去重的视频视觉与语音证据
系统 SHALL 仅处理时长不超过提取记录创建时保存的受控运行时上限的视频；创建提取记录时 SHALL 持久化该上限快照。探测到的实际时长即使超限也 SHALL 先被持久化，随后记录 `SKIPPED_LIMIT` 和 `video_duration_limit_exceeded`。时长不超过 6 秒的视频 SHALL 按全片每 250ms 生成候选帧；其余视频 SHALL 在前 3 秒和后 3 秒按每 250ms 生成候选帧，并在中段按每秒生成候选帧。系统 SHALL 保持既有二维码、OCR、ASR、去重和脱敏边界。

#### Scenario: 视频超过提取时保存的时长上限
- **WHEN** 视频时长超过该提取记录的受控运行时上限快照
- **THEN** 系统 SHALL 先持久化实际时长，再停止视频提取并记录 `SKIPPED_LIMIT + video_duration_limit_exceeded`；后续配置变更不得改变该历史记录的解释

### Requirement: 视频提取失败的审核降级
系统 SHALL 在视频任务达到成功、部分完成、失败、取消或资源限制等终态后允许对应 AI 评估继续执行。视频证据缺失、部分完成或失败本身 SHALL NOT 使人工审核任务标记为重点关注，亦不得自动触发上游处置或阻止运营人员查看已有文本和图片证据；仅在标题、正文和投票均为空且该视频没有可用当前媒体证据时，审核任务 SHALL 按 AI 审核工作流记录 `EVIDENCE_MISSING`。

#### Scenario: 视频超出时长限制
- **WHEN** 视频时长超过提取时保存的上限快照
- **THEN** 系统 SHALL 记录历史上限、实际时长和脱敏摘要，AI 评估 SHALL 使用其余可用内容继续执行；仅在空文字输入且该视频没有可用当前媒体证据时，审核任务 SHALL 记录 `EVIDENCE_MISSING`
