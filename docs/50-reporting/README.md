# 指标与报告

## 职责

维护按来源、游戏和业务日期保存的指标快照、实时运营分析及舆情报告。

## 权威规格

[reporting 模块规格](../../openspec/specs/reporting/spec.md)。

## 代码入口

reporting/，包括 metric_sources.py、metric_sync.py、metric_workspace.py、public_opinion_jobs.py 和 public_opinion_analysis.py。

## 文档

- design/：[L1 决策](design/decisions.md)、[快照与报告 Run](design/snapshots-and-report-runs.md)。
- integration/metrics/：[首页](integration/metrics/home.md)、[每日](integration/metrics/daily.md)、[数据分析](integration/metrics/data-analysis.md)，记录上游接口观察。
- runbooks/：[指标与报告操作](runbooks/metrics-and-reports.md)。

## 缺口

上游指标口径会变化；来源权限和平台兼容性须按发布环境复核。
