"""Credential-free admission for the optional session keepalive job."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from jobs.models import BackgroundJob, JobStatus
from jobs.services import EnqueueResult, enqueue_active_coalesced_job
from jobs.task_types import GAME_CIRCLE_SESSION_KEEPALIVE_TASK
from posts.game_catalog import current_execution_scope


SHANGHAI = ZoneInfo("Asia/Shanghai")
ACTIVE_STATUSES = (JobStatus.PENDING, JobStatus.RUNNING)


def keepalive_bucket(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("调度时间必须包含时区")
    local = now.astimezone(SHANGHAI)
    return local.replace(hour=(local.hour // 4) * 4, minute=0, second=0, microsecond=0).strftime("%Y%m%dT%H%z")


def enqueue_keepalive(*, trigger: str, now: datetime | None = None) -> EnqueueResult:
    """Queue a keepalive job with the submit-time game scope frozen in its checkpoint."""
    if trigger not in {"schedule", "manual"}:
        raise ValueError("会话保活触发方式无效")
    if trigger == "schedule":
        bucket = keepalive_bucket(now or timezone.now())
        key = f"game-circle-session:keepalive:{bucket}"
        parameters = {"trigger": "schedule", "automation_key": "session_keepalive", "bucket": bucket}
    else:
        key = f"game-circle-session:keepalive:manual:{uuid4().hex}"
        parameters = {"trigger": "manual"}
    with transaction.atomic():
        scope = current_execution_scope()
        parameters["scope_version"] = scope.scope_version
        if trigger == "manual":
            return enqueue_active_coalesced_job(
                task_type=GAME_CIRCLE_SESSION_KEEPALIVE_TASK,
                idempotency_key=key,
                active_coalescing_key=(
                    f"game-circle-session:keepalive:manual:{scope.target_appid}:v{scope.scope_version}"
                ),
                parameter_summary=parameters,
                checkpoint={"execution_scope": scope.as_checkpoint()},
            )
        job, created = BackgroundJob.objects.get_or_create(
            idempotency_key=key,
            defaults={
                "task_type": GAME_CIRCLE_SESSION_KEEPALIVE_TASK,
                "parameter_summary": parameters,
                "checkpoint": {"execution_scope": scope.as_checkpoint()},
            },
        )
    return EnqueueResult(job, created)
