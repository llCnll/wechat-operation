## MODIFIED Requirements

### Requirement: 评估输入覆盖正文、投票与 OCR 文本
系统 SHALL 将帖子标题、正文、投票标题和选项文本，以及与该帖子媒体关联的 OCR 文本一并提供给 AI 评估。每份 OCR 结果 SHALL 关联来源媒体、提供方、置信度或错误摘要。图片 OCR 的单项失败 SHALL 保存脱敏错误摘要并继续以可用文本执行 AI 评估；仅当标题、正文和投票均为空且该图片因此没有可用 OCR 文本时，后续审核任务 SHALL 按 AI 审核工作流记录 `EVIDENCE_MISSING`。

#### Scenario: OCR 无法完成
- **WHEN** 图片 OCR 失败、超时或媒体不可访问
- **THEN** 系统 SHALL 保存脱敏错误摘要并继续以可用文本执行 AI 评估；仅当标题、正文和投票均为空且该图片因此没有可用 OCR 文本时，后续审核任务 SHALL 按 AI 审核工作流记录 `EVIDENCE_MISSING`
