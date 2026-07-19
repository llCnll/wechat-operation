"""Dedicated Worker handlers for AI-originated upstream moderation."""

from jobs.models import BackgroundJob
from posts.source_client import CdpGameCircleClient

from .services import (
    AI_AUTOMATED_MODERATION_TASK,
    execute_ai_automated_moderation,
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
    audit = reserve_ai_automated_moderation(task_id, assessment_id, action)
    if audit is None:
        return
    source = None
    try:
        source = CdpGameCircleClient(appid=task.post.source_appid)
    except Exception:
        # The execution service records a sanitized failure and releases the
        # reservation even when the controlled client cannot be constructed.
        execute_ai_automated_moderation(
            task_id, assessment_id, action, None, reserved_audit=audit
        )
        return
    try:
        execute_ai_automated_moderation(
            task_id, assessment_id, action, source, reserved_audit=audit
        )
    finally:
        source.close()


WORKER_HANDLERS = {AI_AUTOMATED_MODERATION_TASK: automated_moderation_handler}
