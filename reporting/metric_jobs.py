"""Metric job creation and Shanghai-time scheduling helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from jobs.models import BackgroundJob, JobStatus
from jobs.services import EnqueueResult, enqueue_job_with_result
from jobs.task_types import METRIC_BACKFILL_TASK, METRIC_DAILY_SYNC_TASK, METRIC_WEEKLY_SYNC_TASK
from posts.ingestion_jobs import SHANGHAI
from posts.game_catalog import ExecutionScope, active_game_appid, current_execution_scope

from .models import MetricJobLease, MetricSource
from .weekly_schedule import WeeklyReleasePhase, WeeklyReleaseSchedule

DAILY_RECHECK_DAYS = 3
WEEKLY_RECHECK_WEEKS = 3
METRIC_MAX_RETRIES = 3
DAILY_METRIC_SOURCES = (
    MetricSource.CIRCLE_FANS_DAILY,
    MetricSource.GROUP_CONSUME_DAILY,
    MetricSource.GROUP_PUBLISH_DAILY,
    MetricSource.GROUP_SHARE_DAILY,
    MetricSource.GROUP_RETAIN_DAILY,
)
MAX_MANUAL_METRIC_RANGE_DAYS = 90


@dataclass(frozen=True)
class MetricScheduleResult:
    daily: tuple[EnqueueResult, ...]
    weekly: tuple[EnqueueResult, ...]


def _day_code(value: date) -> int:
    return int(value.strftime("%Y%m%d"))


def _daily_parameters(*, appid: str, run_day: date, source: MetricSource) -> dict[str, object]:
    start_day = run_day - timedelta(days=DAILY_RECHECK_DAYS - 1)
    return {
        "appid": appid,
        "source": source,
        "start_ds": _day_code(start_day),
        "end_ds": _day_code(run_day),
        "business_day": _day_code(run_day),
    }


def enqueue_daily_metric_sync(run_day: date) -> tuple[EnqueueResult, ...]:
    """Schedule every enabled daily domain under its own stable time bucket."""
    appid = active_game_appid()
    return tuple(
        enqueue_job_with_result(
            task_type=METRIC_DAILY_SYNC_TASK,
            idempotency_key=f"metric-daily:{appid}:{source}:{run_day.isoformat()}",
            parameter_summary=_daily_parameters(appid=appid, run_day=run_day, source=source),
            max_retries=METRIC_MAX_RETRIES,
        )
        for source in DAILY_METRIC_SOURCES
    )


def enqueue_weekly_metric_sync(
    *, business_week_start: date, phase: WeeklyReleasePhase
) -> EnqueueResult:
    appid = active_game_appid()
    start_day = business_week_start - timedelta(days=7 * (WEEKLY_RECHECK_WEEKS - 1))
    parameters = {
        "appid": appid,
        "source": MetricSource.OPERATION_EFFECT_WEEKLY,
        "start_ds": _day_code(start_day),
        "end_ds": _day_code(business_week_start + timedelta(days=6)),
        "business_week_start": _day_code(business_week_start),
        "release_phase": phase.key,
    }
    return enqueue_job_with_result(
        task_type=METRIC_WEEKLY_SYNC_TASK,
        idempotency_key=f"metric-weekly:{appid}:{business_week_start.isoformat()}:{phase.key}",
        parameter_summary=parameters,
        max_retries=METRIC_MAX_RETRIES,
    )


def _backfill_dedupe_key(*, appid: str, source: MetricSource, start_day: date, end_day: date) -> str:
    return f"metric-backfill:{appid}:{source}:{start_day.isoformat()}:{end_day.isoformat()}"


def validate_manual_metric_range(*, start_day: date, end_day: date, today: date | None = None) -> tuple[date, date]:
    """Validate an inclusive Shanghai business-date range before creating any job."""
    local_today = today or timezone.now().astimezone(SHANGHAI).date()
    if start_day > end_day:
        raise ValueError("指标日期范围不能倒置")
    if end_day > local_today:
        raise ValueError("指标日期范围不能包含未来日期")
    if (end_day - start_day).days + 1 > MAX_MANUAL_METRIC_RANGE_DAYS:
        raise ValueError(f"指标日期范围不能超过 {MAX_MANUAL_METRIC_RANGE_DAYS} 个自然日")
    return start_day, end_day


def _enqueue_manual_metric_range(
    *, scope: ExecutionScope, source: MetricSource, start_day: date, end_day: date
) -> EnqueueResult:
    """Create/reuse one range read with a submit-time scope and active lease."""
    if source == MetricSource.CONTENT_FANS_DAILY:
        raise ValueError("内容粉丝当前未完成权限与字段验证，禁止创建采集任务")
    dedupe_key = (
        f"metric-manual:{scope.target_appid}:{scope.scope_version}:{source}:"
        f"{start_day.isoformat()}:{end_day.isoformat()}"
    )
    parameters = {
        "appid": scope.target_appid,
        "source": source,
        "start_ds": _day_code(start_day),
        "end_ds": _day_code(end_day),
    }
    with transaction.atomic():
        lease, _ = MetricJobLease.objects.get_or_create(dedupe_key=dedupe_key)
        lease = MetricJobLease.objects.select_for_update().get(pk=lease.pk)
        if lease.active_job_id:
            active = BackgroundJob.objects.select_for_update().get(pk=lease.active_job_id)
            if active.status in (JobStatus.PENDING, JobStatus.RUNNING):
                return EnqueueResult(active, False)
        job = BackgroundJob.objects.create(
            task_type=METRIC_BACKFILL_TASK,
            idempotency_key=f"{dedupe_key}:{uuid4().hex}",
            parameter_summary=parameters,
            checkpoint={"execution_scope": scope.as_checkpoint()},
            max_retries=METRIC_MAX_RETRIES,
        )
        lease.active_job = job
        lease.save(update_fields=("active_job", "updated_at"))
    return EnqueueResult(job, True)


def enqueue_manual_daily_metric_range(*, start_day: date, end_day: date) -> tuple[EnqueueResult, ...]:
    """Queue one range read per verified daily metric source for the active scope."""
    validate_manual_metric_range(start_day=start_day, end_day=end_day)
    with transaction.atomic():
        scope = current_execution_scope()
        return tuple(
            _enqueue_manual_metric_range(
                scope=scope, source=source, start_day=start_day, end_day=end_day
            )
            for source in DAILY_METRIC_SOURCES
        )


def enqueue_manual_weekly_metric_range(*, start_day: date, end_day: date) -> tuple[EnqueueResult, date, date]:
    """Normalize natural-date input to complete business weeks and queue one read."""
    validate_manual_metric_range(start_day=start_day, end_day=end_day)
    normalized_start = start_day - timedelta(days=start_day.weekday())
    normalized_end = end_day + timedelta(days=6 - end_day.weekday())
    with transaction.atomic():
        scope = current_execution_scope()
        result = _enqueue_manual_metric_range(
            scope=scope,
            source=MetricSource.OPERATION_EFFECT_WEEKLY,
            start_day=normalized_start,
            end_day=normalized_end,
        )
    return result, normalized_start, normalized_end


def enqueue_metric_backfill(*, source: str, start_day: date, end_day: date) -> EnqueueResult:
    """Reuse only a currently active manual range; terminal runs create a new batch."""
    if start_day > end_day:
        raise ValueError("指标回填范围无效")
    normalized_source = MetricSource(source)
    appid = active_game_appid()
    if normalized_source == MetricSource.CONTENT_FANS_DAILY:
        raise ValueError("内容粉丝当前未完成权限与字段验证，禁止创建采集任务")
    parameters = {
        "appid": appid,
        "source": normalized_source,
        "start_ds": _day_code(start_day),
        "end_ds": _day_code(end_day),
    }
    dedupe_key = _backfill_dedupe_key(
        appid=appid, source=normalized_source,
        start_day=start_day,
        end_day=end_day,
    )
    with transaction.atomic():
        lease, _ = MetricJobLease.objects.get_or_create(dedupe_key=dedupe_key)
        lease = MetricJobLease.objects.select_for_update().get(pk=lease.pk)
        if lease.active_job_id is not None:
            active_job = BackgroundJob.objects.select_for_update().get(pk=lease.active_job_id)
            if active_job.status in (JobStatus.PENDING, JobStatus.RUNNING):
                return EnqueueResult(active_job, False)
        job = BackgroundJob.objects.create(
            task_type=METRIC_BACKFILL_TASK,
            idempotency_key=f"{dedupe_key}:{uuid4().hex}",
            parameter_summary=parameters,
            max_retries=METRIC_MAX_RETRIES,
        )
        lease.active_job = job
        lease.save(update_fields=["active_job", "updated_at"])
    return EnqueueResult(job, True)


def enqueue_due_metric_jobs(now: datetime | None = None) -> MetricScheduleResult:
    local_now = (now or timezone.now()).astimezone(SHANGHAI)
    daily: tuple[EnqueueResult, ...] = ()
    weekly: tuple[EnqueueResult, ...] = ()
    if (local_now.hour, local_now.minute) >= (7, 30):
        daily = enqueue_daily_metric_sync(local_now.date() - timedelta(days=1))
    weekly = tuple(
        enqueue_weekly_metric_sync(
            business_week_start=plan.business_week_start,
            phase=plan.phase,
        )
        for plan in WeeklyReleaseSchedule.due_plans(local_now)
    )
    return MetricScheduleResult(daily=daily, weekly=weekly)
