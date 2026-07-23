"""Credential-free admission for the optional session keepalive job."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from django.utils import timezone

from jobs.models import BackgroundJob, JobStatus
from jobs.services import EnqueueResult
from jobs.task_types import GAME_CIRCLE_SESSION_KEEPALIVE_TASK


SHANGHAI = ZoneInfo("Asia/Shanghai")
ACTIVE_STATUSES = (JobStatus.PENDING, JobStatus.RUNNING)


def keepalive_bucket(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("调度时间必须包含时区")
    local = now.astimezone(SHANGHAI)
    return local.replace(hour=(local.hour // 4) * 4, minute=0, second=0, microsecond=0).strftime("%Y%m%dT%H%z")


def enqueue_keepalive(*, trigger: str, now: datetime | None = None) -> EnqueueResult:
    """Queue a job without an AppID or scope snapshot; the consumer freezes it."""
    if trigger not in {"schedule", "manual"}:
        raise ValueError("会话保活触发方式无效")
    if trigger == "schedule":
        bucket = keepalive_bucket(now or timezone.now())
        key = f"game-circle-session:keepalive:{bucket}"
        parameters = {"trigger": "schedule", "automation_key": "session_keepalive", "bucket": bucket}
    else:
        key = f"game-circle-session:keepalive:manual:{timezone.now().strftime('%Y%m%d%H%M%S%f')}"
        parameters = {"trigger": "manual"}
    job, created = BackgroundJob.objects.get_or_create(
        idempotency_key=key,
        defaults={"task_type": GAME_CIRCLE_SESSION_KEEPALIVE_TASK, "parameter_summary": parameters},
    )
    return EnqueueResult(job, created)
