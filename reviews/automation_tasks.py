"""Dedicated Worker handlers for AI-originated upstream moderation."""

from jobs.models import BackgroundJob
from jobs.services import JobDeferred
from config.game_circle_ipc import GameCirclePostClient
from posts.game_catalog import resolve_execution_scope

from .services import (
    AI_AUTOMATED_MODERATION_TASK,
    ai_automation_eligibility,
    execute_ai_automated_moderation,
    record_ai_automation_rejection,
    reserve_ai_automated_moderation,
)




def automated_moderation_handler(job: BackgroundJob) -> None:
    summary = job.parameter_summary
    task_id = int(summary["task_id"])
    assessment_id = int(summary["assessment_id"])
    action = str(summary["action"])
    from reviews.models import ReviewTask

    task = ReviewTask.objects.select_related("post").get(pk=task_id)
    if not task.post.source_appid:
        # Keep a failed audit instead of leaving a reservation in EXECUTING when
        # historical data lacks the game identity required for a CDP session.
        execute_ai_automated_moderation(task_id, assessment_id, action, source=None)
        return
    assessment = task.assessment
    if assessment is None:
        return
    if action != task.suggested_action:
        record_ai_automation_rejection(task, assessment, "自动处置动作与当前建议不一致", {})
        return
    eligible, rejection, snapshot = ai_automation_eligibility(task, assessment=assessment)
    if not eligible:
        record_ai_automation_rejection(task, assessment, rejection, snapshot)
        return
    if task.operation_audits.filter(result_code="EXECUTING").exists():
        record_ai_automation_rejection(task, assessment, "已有操作正在执行", snapshot)
        return
    audit = None
    try:
        scope = resolve_execution_scope(job)
        if scope.target_appid != task.post.source_appid:
            raise ValueError("AI 自动处置任务游戏与当前操作范围不一致")
        # The reservation is intentionally after the scope has been resolved.
        # IPC returns a deferred recovery before any upstream write is sent.
        with GameCirclePostClient(scope=scope) as source:
            audit = reserve_ai_automated_moderation(task_id, assessment_id, action)
            if audit is None:
                return
            execute_ai_automated_moderation(
                task_id, assessment_id, action, source, reserved_audit=audit
            )
    except JobDeferred:
        raise
    except Exception:
        # The execution service records a sanitized failure and releases the
        # reservation even when the controlled client cannot be constructed.
        execute_ai_automated_moderation(
            task_id, assessment_id, action, None, reserved_audit=audit
        )
        return


WORKER_HANDLERS = {AI_AUTOMATED_MODERATION_TASK: automated_moderation_handler}
