# 帖子与作者运营

## 职责

管理授权游戏目录、候选与原创帖子、作者身份投影、作者治理和评论导出任务。

## 权威规格

[posts 模块规格](../../openspec/specs/posts/spec.md)。

## 代码入口

posts/，包括 ingestion.py、game_catalog.py、official_authors.py、comment_exports.py 和 views.py。

## 文档

- design/：[L1 决策](design/decisions.md)、[L2 游戏与作者边界](design/game-and-author-boundaries.md)。
- integration/：[原创帖子上游观察](integration/original-posts.md)。
- runbooks/：[作者与内容运营](runbooks/author-and-content-operations.md)。

## 缺口

2026-10-30 游戏目录与 FAQ 通用化 change 的原文尚未进入共享 Git 历史；当前授权范围决策已整理，原始方案引用本地未提交快照。
