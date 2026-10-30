# 后台任务与调度

## 职责

维护持久化异步任务、受限定时生产者、幂等和 checkpoint 恢复边界。

## 权威规格

[jobs 模块规格](../../openspec/specs/jobs/spec.md)。

## 代码入口

jobs/，包括 models.py、services.py、worker.py、scheduled_producers.py 和 manual_producers.py。

## 文档

- design/：[L1 决策](design/decisions.md)、[L2 调度与所有权](design/scheduling-and-task-ownership.md)。
- runbooks/：[调度操作](runbooks/scheduling.md)。

## 缺口

当前无已确认的模块级设计缺口。
