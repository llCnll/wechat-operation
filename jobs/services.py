from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import BackgroundJob, JobStatus


class JobDeferred(RuntimeError):
    """A dependency is still running; reschedule without consuming a retry."""

    def __init__(self, reason: str, *, delay_seconds: int = 5):
        super().__init__(reason)
        self.delay_seconds = delay_seconds


class JobTerminalError(RuntimeError):
    """The worker classified an error as non-retryable for this job."""


@dataclass(frozen=True)
class EnqueueResult:
    """A persisted job plus whether this call created it."""

    job: BackgroundJob
    created: bool


def enqueue_job(
    *, task_type: str, idempotency_key: str, parameter_summary: dict, max_retries: int = 3
) -> BackgroundJob:
    return enqueue_job_with_result(
        task_type=task_type,
        idempotency_key=idempotency_key,
        parameter_summary=parameter_summary,
        max_retries=max_retries,
    ).job


def enqueue_job_with_result(
    *, task_type: str, idempotency_key: str, parameter_summary: dict, max_retries: int = 3
) -> EnqueueResult:
    """Create or return a job while preserving the idempotent creation result."""
    job, created = BackgroundJob.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "task_type": task_type,
            "parameter_summary": parameter_summary,
            "max_retries": max_retries,
        },
    )
    return EnqueueResult(job=job, created=created)


def claim_next_job(*, task_types: Collection[str] | None = None) -> BackgroundJob | None:
    """Atomically claim one due job, optionally limited to a specialized worker's types."""
    now = timezone.now()
    # SQLite does not implement row-level SELECT ... FOR UPDATE locking. Claim with a
    # conditional update instead, so competing processes cannot both own the same job.
    while True:
        jobs = BackgroundJob.objects.filter(status=JobStatus.PENDING)
        if task_types is not None:
            jobs = jobs.filter(task_type__in=task_types)
        job_id = (
            jobs.filter(Q(next_run_at__isnull=True) | Q(next_run_at__lte=now))
            .order_by("created_at")
            .values_list("pk", flat=True)
            .first()
        )
        if job_id is None:
            return None
        claimed = BackgroundJob.objects.filter(pk=job_id, status=JobStatus.PENDING).update(
            status=JobStatus.RUNNING,
            started_at=now,
        )
        if claimed:
            return BackgroundJob.objects.get(pk=job_id)


def recover_interrupted_jobs(*, task_types: Collection[str] | None = None) -> int:
    """Recover interrupted tasks assigned to the starting worker's task types."""
    jobs = BackgroundJob.objects.filter(status=JobStatus.RUNNING)
    if task_types is not None:
        jobs = jobs.filter(task_type__in=task_types)
    return jobs.update(
        status=JobStatus.PENDING,
        next_run_at=None,
        started_at=None,
        error_summary="Worker 中断，等待从检查点恢复",
    )


def fail_job(job: BackgroundJob, error_summary: str) -> None:
    """Schedule a finite retry without persisting provider payloads or credentials."""
    job.retry_count += 1
    job.error_summary = error_summary[:500]
    if job.retry_count > job.max_retries:
        job.status = JobStatus.FAILED
        job.finished_at = timezone.now()
    else:
        job.status = JobStatus.PENDING
        job.next_run_at = timezone.now() + timedelta(seconds=min(2**job.retry_count, 300))
    job.save()


def fail_job_terminal(job: BackgroundJob, error_summary: str) -> None:
    """Persist a classified terminal failure without consuming retry attempts."""
    job.status = JobStatus.FAILED
    job.error_summary = error_summary[:500]
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "error_summary", "finished_at", "updated_at"])


def defer_job(job: BackgroundJob, reason: str, *, delay_seconds: int = 5) -> None:
    """Release a job while its dependencies are active without treating it as a failure."""
    job.status = JobStatus.PENDING
    job.error_summary = reason[:500]
    job.next_run_at = timezone.now() + timedelta(seconds=delay_seconds)
    job.started_at = None
    job.save(update_fields=["status", "error_summary", "next_run_at", "started_at", "updated_at"])


def complete_job(job: BackgroundJob) -> None:
    job.status = JobStatus.SUCCEEDED
    job.error_summary = ""
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "error_summary", "finished_at", "updated_at"])


def cancel_job(job: BackgroundJob, reason: str) -> None:
    """Stop work at an explicit operator request without misreporting a transport failure."""
    job.status = JobStatus.CANCELED
    job.error_summary = reason[:500]
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "error_summary", "finished_at", "updated_at"])


def execute_next_job(
    handlers: dict[str, Callable[[BackgroundJob], None]],
    *,
    task_types: Collection[str] | None = None,
) -> BackgroundJob | None:
    job = claim_next_job(task_types=task_types)
    if job is None:
        return None
    handler = handlers.get(job.task_type)
    if handler is None:
        fail_job(job, f"未注册的任务类型：{job.task_type}")
        return job
    try:
        handler(job)
    except JobDeferred as exc:
        defer_job(job, str(exc), delay_seconds=exc.delay_seconds)
    except JobTerminalError as exc:
        fail_job_terminal(job, str(exc))
    except Exception as exc:  # The task error is intentionally persisted without a traceback.
        fail_job(job, str(exc))
    else:
        complete_job(job)
    return job
