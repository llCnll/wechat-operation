# 审核与媒体证据

## 职责

维护 OCR/AI 评估、策略建议、人工审核、受限自动处置、本地回复草案与视频证据。

## 权威规格

[reviews 模块规格](../../openspec/specs/reviews/spec.md)。

## 代码入口

reviews/，包括 adapters.py、services.py、tasks.py、automation_tasks.py、video.py 和 reply_faq.py。

## 文档

- design/：[L1 决策](design/decisions.md)、[AI 自动处置安全](design/automation-safety.md)、[FAQ 与视频证据边界](design/faq-video-boundaries.md)。
- runbooks/：[FAQ 资料维护](runbooks/faq-data-maintenance.md)、[视频审核](runbooks/video-review.md)。

## 缺口

视频能力默认关闭；运行前样本预检和失败降级见[视频审核手册](runbooks/video-review.md)与模块规格。2026-10-30 FAQ 通用化 change 的原文尚未进入共享 Git 历史；当前资料边界已整理，原始方案引用本地未提交快照。
