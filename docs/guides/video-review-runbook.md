# 视频审核手册

视频审核默认关闭。只有在获得授权的真实样本上完成预检后，才可启用视频 Worker。预检失败时保持关闭，不得使用 CDP Cookie 作为读取回退。

## 安装媒体工具

视频审核需要本机 `ffmpeg`、`ffprobe` 和 OpenCV。程序优先使用系统 `PATH`；如果需要项目本地可移植工具，可在 `runtime/.env` 配置：

```env
VIDEO_MEDIA_TOOLS_DIR=runtime/video-tools/ffmpeg-<version>-full_build/bin
```

相对路径按项目根目录解析，且必须位于 `runtime/` 内。显式目录必须同时包含 `ffmpeg` 与 `ffprobe`（Windows 为 `.exe`）。修改配置后重启整个 Supervisor；日后仍只需执行 `uv run python -m service_supervisor`，不需要手动修改 `PATH`。

Windows 中 WinGet 无法完成可移植安装时，将已校验的 FFmpeg ZIP 解压至 `runtime/video-tools/ffmpeg-<version>-full_build/bin`，确认 `ffmpeg.exe` 和 `ffprobe.exe` 存在，再设置上述配置。工具目录被 Git 忽略，不提交二进制文件。

macOS 已通过 `PATH` 提供工具时不需要该配置。macOS 12 Intel 无当前 Homebrew bottle 时，可使用本地 Darwin x64 回退：

```bash
npm install --prefix runtime/video-tools @ffmpeg-installer/darwin-x64@4.1.0 ffprobe-static
```

未显式配置目录且系统工具不存在时，程序会查找该本地路径。

## 受控样本预检

将已获授权的视频 URL 仅临时置于当前终端环境变量；不得写入配置、数据库或日志：

```powershell
$env:VIDEO_SAMPLE_URL = '<已授权的 HTTPS 视频样本 URL>'
```

可先用系统已识别的 `ffprobe` 验证该 URL 无 Cookie 可读取；工具在显式目录时，将 `ffprobe` 替换为该目录中的可执行文件：

```powershell
ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1 $env:VIDEO_SAMPLE_URL
```

随后使用已授权、带该视频的真实帖子完成一次视频任务处理，并确认以下最小验证均成功：

1. 无 Cookie 的 HTTPS 读取；
2. `ffprobe` 媒体探测与 `ffmpeg` 首帧提取；
3. 处理临时目录已清理；
4. 任务完成后本机临时目录已清理；
5. 预检输出、数据库和日志未记录完整视频 URL、Cookie、令牌、视频或帧。

实现会对上游重定向逐跳重新校验 HTTPS 与公网地址；不要求为启用前置条件额外准备真实重定向样本。

## 启用、降级与停止

预检通过后，在系统配置首页“视频审核 Worker”勾选“已完成受控样本无 Cookie 读取预检”，设置 `VIDEO_ENABLED=true`（默认时长上限 30 秒，最多 60 秒），保存后启动或重启视频 Worker。该页面还可设置时长/字节/视频数量/分辨率/dHash/OCR 预算、超时和 ASR 配置。

视频只在本机短生命周期目录读取。系统仅持久化带时间位置的 OCR/语音文本和固定的“检测到二维码”提示；不会保存原视频、帧、音频、完整视频 URL、Cookie 或二维码目标，也不会将它们传给 OCR、ASR 或 AI。

`ASR_ENABLED=false` 是正式降级模式，不会关闭视频审核：视频仍抽帧、检测二维码并执行 OCR，结果标记为“部分完成”并进入重点人工核验。重新启用 ASR 前，必须在支持 `/audio/transcriptions` 的受控服务上完成成功链路验收并确认数据保留边界。

停止或回滚时，先停止视频 Worker、关闭 `VIDEO_ENABLED`，再重启 Supervisor；已有本地文本证据可保留追溯。视频失败任务保留脱敏状态，供人工核验。
