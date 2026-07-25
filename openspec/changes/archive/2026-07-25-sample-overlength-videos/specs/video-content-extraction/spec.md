## MODIFIED Requirements

### Requirement: 视频时长限制与候选抽帧
系统 SHALL 在创建提取记录时保存 `VIDEO_MAX_DURATION_SECONDS` 作为最大采样时长预算快照。系统 SHALL 将 `ffprobe` 返回的十进制秒数向上取整到毫秒后，以实际时长 `D` 和预算 `B` 规划采样区间。规划器 SHALL 令 `S=min(D,B)`，以前两个长度 `floor(S/3)`、末个长度 `S-2*floor(S/3)` 生成首、中、尾三个半开区间：首区间从 `0` 开始，中区间居中，尾区间以 `D` 结束；随后按时间顺序合并重叠或相邻区间，并丢弃空区间。候选帧 SHALL 只来自规划后的区间：每个区间按每秒一帧生成基础候选，实际视频的 `[0,3)` 与 `[D-3,D)` 相交部分以每 250ms 候选替换基础候选；候选时间 SHALL 去重、排序并满足 `0 ≤ t < D`。二维码检测、dHash 与 OCR SHALL 对所有规划区间走同一条既有处理管线。

#### Scenario: 预算覆盖整个视频
- **WHEN** 视频时长不大于提取记录保存的采样时长预算
- **THEN** 三个窗口 SHALL 合并为唯一的 `[0,D)` 区间，系统 SHALL 以既有完整视频节奏生成候选帧；30 秒视频 SHALL 生成恰好 48 个候选帧，60 秒视频 SHALL 生成恰好 78 个候选帧

#### Scenario: 视频超过采样时长预算
- **WHEN** 视频时长为 90 秒且提取记录保存的预算为 60 秒
- **THEN** 系统 SHALL 规划并持久化 `[0,20000)`、`[35000,55000)`、`[70000,90000)` 三个区间，以相同全局帧密度生成恰好 78 个候选帧，并不得抽取这三个区间以外的帧

#### Scenario: 分数秒刚超过预算
- **WHEN** 预算为 60 秒且探测结果为大于 60 秒的任意正分数
- **THEN** 系统 SHALL 向上取整到毫秒并规划分布式区间，不得因浮点或整数截断而将其当作完整覆盖

#### Scenario: 短视频的统一规划
- **WHEN** 视频时长不超过 6 秒
- **THEN** 规划结果 SHALL 合并为全片区间，实际首尾密集区间的并集 SHALL 使系统按每 250ms 生成全片候选帧

### Requirement: 语音与证据持久化
系统 SHALL 在规划结果为唯一 `[0,D)` 完整区间、ASR 配置完整且存在可处理音轨时提取带开始和结束时间的语音转写。规划结果包含多个区间的视频 SHALL NOT 提取整段音频或调用 ASR。每条视频证据 SHALL 保存来源类型、时间位置、文本、提供方和可用置信度；系统 SHALL NOT 持久化图像或音频二进制。

#### Scenario: 完整覆盖视频的口播
- **WHEN** 视频规划结果为唯一完整区间、存在可处理音轨且 ASR 服务配置完整
- **THEN** 系统 SHALL 持久化带开始和结束时间的转写片段，并将其与关键帧 OCR 作为独立来源保留

#### Scenario: 分布式采样视频的音轨
- **WHEN** 视频规划结果包含多个采样区间
- **THEN** 系统 SHALL 不提取音频、不调用 ASR，并将未执行 ASR 作为该提取的固定脱敏说明

### Requirement: 视频任务与提取结果的终态一致性
系统 SHALL 将视频提取记录与其 `VIDEO_EXTRACT` 后台任务的终态语义保持一致。媒体无效、读取失败、媒体工具命令失败或其他导致提取记录为 `FAILED` 的异常，后台任务 SHALL 以同一脱敏错误分类进入 `FAILED`，不得显示为成功。产生了可用部分证据或完成分布式视觉采样的 `PARTIAL`，以及因字节或处理时间资源限制完成降级的 `SKIPPED_LIMIT`，后台任务 SHALL 标记为成功。时长超过采样预算 SHALL NOT 单独进入 `SKIPPED_LIMIT`。媒体工具命令失败分类 SHALL 至少区分 `probe`、`frame_extract` 与 `audio_extract` 阶段，并可附带安全的进程退出码；持久化状态和结构化日志 SHALL NOT 包含媒体 URL、临时路径、完整命令、Cookie、令牌或原始 stderr。

#### Scenario: 首帧抽取命令失败
- **WHEN** `ffprobe` 已成功且 `ffmpeg` 在候选帧抽取阶段以非零退出码结束
- **THEN** 提取记录和后台任务 SHALL 均标记为失败，错误分类为 `video_frame_extract_command_failed:exit_<code>`，结构化日志 SHALL 记录安全的阶段与退出码

#### Scenario: 字节或处理时间资源限制
- **WHEN** 视频因既有字节或处理时间上限进入 `SKIPPED_LIMIT`
- **THEN** 提取记录 SHALL 保留对应受限原因，后台任务 SHALL 标记为成功，AI 评估可继续使用其余内容

#### Scenario: 视频时长超过采样预算
- **WHEN** 视频时长超过提取时保存的预算快照且全部分布式视觉采样步骤完成
- **THEN** 系统 SHALL 保存实际时长和采样区间，以 `PARTIAL + video_duration_distributed_sampling` 完成，后台任务 SHALL 标记为成功，AI 评估 SHALL 使用已有证据和采样范围继续执行

#### Scenario: 视频 Worker 中断后恢复
- **WHEN** 视频提取 Worker 在任务处理中意外中断
- **THEN** 系统 SHALL 按既有后台任务恢复机制重新排队该视频任务，并清理或忽略任何未完成的临时媒体文件
