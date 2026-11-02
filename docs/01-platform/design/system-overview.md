# 系统全局地图

全局地图只说明业务边界、数据归属和关键依赖。进程调用细节以代码与模块规格为准。

| 模块 | 负责内容 | 主要依赖 |
| --- | --- | --- |
| config | 启动运行配置、授权来源会话、固定本机 IPC | 受控 Chrome、上游平台、OCR/AI 配置 |
| jobs | 后台任务、调度规则、Worker 消费 | SQLite、各业务模块 handler |
| posts | 游戏范围、候选和原创帖子、作者治理 | config 来源会话、jobs |
| reviews | 评估证据、审核、回复草案和受限自动处置 | posts、jobs、OCR/AI 服务 |
| reporting | 指标快照、运营分析、舆情报告 | posts、jobs、config 来源会话 |
| service_supervisor | 受管进程与恢复 | 本机运行态、固定服务清单 |

数据只由对应业务模块维护：posts 保存帖子和作者治理投影，reviews 保存评估和审核记录，jobs 保存任务生命周期，reporting 保存指标快照和报告运行。浏览器认证材料只存在 config 会话服务运行期内，不进入业务表。

可验收行为见模块主规格：[config](../../../openspec/specs/config/spec.md)、[jobs](../../../openspec/specs/jobs/spec.md)、[posts](../../../openspec/specs/posts/spec.md)、[reviews](../../../openspec/specs/reviews/spec.md)、[reporting](../../../openspec/specs/reporting/spec.md)、[service_supervisor](../../../openspec/specs/service_supervisor/spec.md)。
