"""Admission rules for session-service refresh and credential-free login probes."""

from __future__ import annotations

from datetime import datetime
from time import sleep
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.db import IntegrityError, OperationalError, transaction

from jobs.models import BackgroundJob, JobStatus
from jobs.services import EnqueueResult
from jobs.task_types import GAME_CIRCLE_LOGIN_PROBE_TASK, GAME_CIRCLE_SESSION_REFRESH_TASK
from posts.game_catalog import ExecutionScope
from posts.models import ActiveGameScope

REFRESH_RECOVER = "RECOVER"
REFRESH_KEEPALIVE = "KEEPALIVE"
ACTIVE_STATUSES = (JobStatus.PENDING, JobStatus.RUNNING)
SHANGHAI = ZoneInfo("Asia/Shanghai")
REFRESH_SLOTS = frozenset(((3, 10), (7, 10), (11, 10), (15, 10), (19, 10), (23, 10)))
TERMINAL_STATUSES = (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELED)


def _create_refresh_job(
    *, refresh_mode: str, execution_scope: ExecutionScope, idempotency_key: str
) -> EnqueueResult:
    job, created = BackgroundJob.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "task_type": GAME_CIRCLE_SESSION_REFRESH_TASK,
            "parameter_summary": {"refresh_mode": refresh_mode},
            "checkpoint": {"execution_scope": execution_scope.as_checkpoint()},
        },
    )
    return EnqueueResult(job, created)


def _recover_admission_key(scope: ExecutionScope) -> str:
    return f"game-circle-session:recover:{scope.target_appid}:v{scope.scope_version}:active"


def _admit_recover_job(scope: ExecutionScope) -> EnqueueResult:
    """Atomically own one active recovery admission without reopening terminal jobs."""
    key = _recover_admission_key(scope)
    defaults = {
        "task_type": GAME_CIRCLE_SESSION_REFRESH_TASK,
        "parameter_summary": {"refresh_mode": REFRESH_RECOVER},
        "checkpoint": {"execution_scope": scope.as_checkpoint()},
    }
    for attempt in range(3):
        try:
            with transaction.atomic():
                job, created = BackgroundJob.objects.get_or_create(
                    idempotency_key=key,
                    defaults=defaults,
                )
                if created or job.status in ACTIVE_STATUSES:
                    return EnqueueResult(job, created)
                if job.status not in TERMINAL_STATUSES:
                    raise ValueError("恢复任务状态无效")
                archived_key = f"{key}:terminal:{job.pk}"
                changed = BackgroundJob.objects.filter(
                    pk=job.pk,
                    idempotency_key=key,
                    status__in=TERMINAL_STATUSES,
                ).update(idempotency_key=archived_key)
                if not changed:
                    continue
                next_job = BackgroundJob.objects.create(idempotency_key=key, **defaults)
                return EnqueueResult(next_job, True)
        except (IntegrityError, OperationalError):
            if attempt == 2:
                break
            # SQLite may briefly hold the unique-key write lock in another IPC
            # thread.  Retry only this short admission boundary.
            sleep(0.01 * (attempt + 1))
    # The normal Worker retry path will retry this short SQLite-contention boundary.
    raise OperationalError("游戏圈恢复任务准入暂时繁忙")


def enqueue_session_refresh(
    *,
    refresh_mode: str,
    execution_scope: ExecutionScope | None = None,
    schedule_slot: str | None = None,
) -> EnqueueResult:
    """Queue/reuse a compatible refresh; callers never touch Chrome in this path."""
    if refresh_mode not in {REFRESH_RECOVER, REFRESH_KEEPALIVE}:
        raise ValueError("会话刷新模式无效")
    if execution_scope is None:
        active = ActiveGameScope.objects.filter(pk=1).values("active_game_id", "scope_version").first()
        if active is None:
            raise ValueError("尚未配置当前操作游戏")
        execution_scope = ExecutionScope(str(active["active_game_id"]), int(active["scope_version"]))
    if schedule_slot is not None and refresh_mode != REFRESH_KEEPALIVE:
        raise ValueError("只有主动保活可以使用调度时间槽")
    saved_scope = execution_scope.as_checkpoint()
    active_jobs = list(BackgroundJob.objects.filter(task_type=GAME_CIRCLE_SESSION_REFRESH_TASK, status__in=ACTIVE_STATUSES).order_by("created_at"))
    matching = [job for job in active_jobs if (job.checkpoint or {}).get("execution_scope") == saved_scope]
    reusable = [
        job
        for job in matching
        if job.parameter_summary.get("refresh_mode") == refresh_mode
    ]
    if reusable:
        return EnqueueResult(reusable[0], False)
    if refresh_mode == REFRESH_RECOVER:
        return _admit_recover_job(execution_scope)
    key = (
        f"game-circle-session:keepalive:{execution_scope.target_appid}:v{execution_scope.scope_version}:{schedule_slot}"
        if schedule_slot is not None
        else f"game-circle-session:{refresh_mode.lower()}:{uuid4().hex}"
    )
    return _create_refresh_job(
        refresh_mode=refresh_mode,
        execution_scope=execution_scope,
        idempotency_key=key,
    )


def enqueue_login_probe(*, source: str = "manual", now: datetime | None = None) -> EnqueueResult:
    """At most one active probe exists; no appid or authentication value is recorded."""
    active = BackgroundJob.objects.filter(task_type=GAME_CIRCLE_LOGIN_PROBE_TASK, status__in=ACTIVE_STATUSES).order_by("created_at").first()
    if active:
        return EnqueueResult(active, False)
    slot = timezone_slot((now or datetime.now(SHANGHAI))) if source == "scheduler" else uuid4().hex
    job, created = BackgroundJob.objects.get_or_create(
        idempotency_key=f"game-circle-login-probe:{slot}",
        defaults={
            "task_type": GAME_CIRCLE_LOGIN_PROBE_TASK,
            "parameter_summary": {"source": source},
            "max_retries": 0,
        },
    )
    return EnqueueResult(job, created)


def timezone_slot(now: datetime) -> str:
    local = now.astimezone(SHANGHAI)
    return local.strftime("%Y%m%d%H%M")


def enqueue_scheduled_session_refresh(now: datetime) -> EnqueueResult | None:
    if now.tzinfo is None:
        raise ValueError("调度时间必须包含时区")
    local = now.astimezone(SHANGHAI)
    if (local.hour, local.minute) not in REFRESH_SLOTS:
        return None
    return enqueue_session_refresh(
        refresh_mode=REFRESH_KEEPALIVE,
        schedule_slot=local.strftime("%Y%m%d%H%M"),
    )
