"""Shared creation and scheduling helpers for original-post ingestion jobs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from jobs.models import BackgroundJob, JobStatus
from jobs.services import enqueue_active_coalesced_job, enqueue_job_with_result

from .dashboard_filters import OriginalPostFilter
from .game_catalog import active_game_appid, current_execution_scope
from .tasks import (
    FILTERED_POST_REFRESH_TASK,
    MANUAL_CURRENT_INGEST_WINDOW_TASK,
    MANUAL_INGEST_WINDOW_TASK,
    METRICS_REFRESH_WINDOW_TASK,
    SCHEDULED_INGEST_WINDOW_TASK,
    STATUS_SYNC_TASK,
)


MAX_MANUAL_INGESTION_HOURS = 31 * 24

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _fixed_ingestion_page_size() -> int:
    """Return the verified candidate-list size shared by normal ingestion entrypoints."""
    page_size = _positive_int(settings.SCHEDULED_INGESTION_PAGE_SIZE, name="默认页面大小")
    if page_size != settings.FIXED_INGESTION_PAGE_SIZE:
        raise ValueError(f"自动采集默认页面大小必须为{settings.FIXED_INGESTION_PAGE_SIZE}")
    return page_size


@dataclass(frozen=True)
class IngestionEnqueueResult:
    """The ingestion job and whether this request created it."""

    job: BackgroundJob
    created: bool


@dataclass(frozen=True)
class IngestionWindow:
    """A validated Unix-timestamp ingestion interval."""

    start_time: int
    end_time: int


@dataclass(frozen=True)
class ScheduledJobsResult:
    """The two idempotent jobs created for one scheduler time bucket."""

    status_sync: IngestionEnqueueResult
    ingestion: IngestionEnqueueResult


@dataclass(frozen=True)
class MetricsRefreshEnqueueResult:
    """A manual range refresh job and whether it was newly queued."""

    job: BackgroundJob
    created: bool


def _positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name}必须为正整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}必须为正整数") from exc
    if number <= 0:
        raise ValueError(f"{name}必须为正整数")
    return number


def validate_ingestion_window(
    *, appid: object, start_time: object, end_time: object, page_size: object
) -> tuple[str, IngestionWindow, int]:
    normalized_appid = str(appid or "").strip()
    if not normalized_appid:
        raise ValueError("appid不能为空")
    start = _positive_int(start_time, name="开始时间")
    end = _positive_int(end_time, name="结束时间")
    if end < start:
        raise ValueError("采集时间窗口无效")
    normalized_page_size = _positive_int(page_size, name="page-size")
    if not 1 <= normalized_page_size <= 100:
        raise ValueError("page-size必须在1至100之间")
    return normalized_appid, IngestionWindow(start, end), normalized_page_size


def _enqueue_ingestion_window(
    *,
    appid: object,
    start_time: object,
    end_time: object,
    page_size: object,
    task_type: str,
    idempotency_prefix: str,
    idempotency_scope_version: int | None = None,
    checkpoint: dict | None = None,
) -> IngestionEnqueueResult:
    """Create one checkpointed ingestion window for an explicit consumer lane."""
    normalized_appid, window, normalized_page_size = validate_ingestion_window(
        appid=appid, start_time=start_time, end_time=end_time, page_size=page_size
    )
    scope_key = f":v{idempotency_scope_version}" if idempotency_scope_version is not None else ""
    result = enqueue_job_with_result(
        task_type=task_type,
        idempotency_key=(
            f"{idempotency_prefix}:{normalized_appid}{scope_key}:{window.start_time}:"
            f"{window.end_time}:{normalized_page_size}"
        ),
        parameter_summary={
            "appid": normalized_appid,
            "start_time": window.start_time,
            "end_time": window.end_time,
            "page_size": normalized_page_size,
        },
        checkpoint=checkpoint,
    )
    return IngestionEnqueueResult(job=result.job, created=result.created)


def enqueue_manual_ingestion_window(
    *, appid: object, start_time: object, end_time: object, page_size: object
) -> IngestionEnqueueResult:
    """Create a checkpointed historical ingestion task for the manual lane."""
    return _enqueue_ingestion_window(
        appid=appid,
        start_time=start_time,
        end_time=end_time,
        page_size=page_size,
        task_type=MANUAL_INGEST_WINDOW_TASK,
        idempotency_prefix="manual-ingest-window",
    )


def enqueue_manual_current_ingestion_window(
    *, now: datetime | None = None
) -> IngestionEnqueueResult:
    """Queue the producer-owned, fixed current window under the submit-time scope."""
    local_now = (now or timezone.now()).astimezone(SHANGHAI).replace(second=0, microsecond=0)
    window = IngestionWindow(
        start_time=int((local_now - timedelta(minutes=30)).timestamp()),
        end_time=int(local_now.timestamp()),
    )
    page_size = _fixed_ingestion_page_size()
    with transaction.atomic():
        scope = current_execution_scope(persist_default=False)
        parameters = {
            "appid": scope.target_appid,
            "start_time": window.start_time,
            "end_time": window.end_time,
            "page_size": page_size,
        }
        result = enqueue_active_coalesced_job(
            task_type=MANUAL_CURRENT_INGEST_WINDOW_TASK,
            idempotency_key=(
                f"manual-current-ingest:{scope.target_appid}:{scope.scope_version}:"
                f"{window.start_time}:{window.end_time}:{page_size}:{uuid4().hex}"
            ),
            active_coalescing_key=(
                f"manual-current-ingest:{scope.target_appid}:v{scope.scope_version}:"
                f"{window.start_time}:{window.end_time}:{page_size}"
            ),
            parameter_summary=parameters,
            checkpoint={"execution_scope": scope.as_checkpoint()},
        )
    return IngestionEnqueueResult(job=result.job, created=result.created)


def enqueue_manual_historical_ingestion_window(
    *, hours: object, now: datetime | None = None
) -> IngestionEnqueueResult:
    """Create a bounded historical window for the current producer scope."""
    try:
        duration_hours = int(hours)
    except (TypeError, ValueError) as exc:
        raise ValueError("补采时长必须是正整数小时。") from exc
    if not 1 <= duration_hours <= MAX_MANUAL_INGESTION_HOURS:
        raise ValueError(f"补采时长须在 1 至 {MAX_MANUAL_INGESTION_HOURS} 小时之间。")

    local_now = (now or timezone.now()).astimezone(SHANGHAI).replace(second=0, microsecond=0)
    with transaction.atomic():
        scope = current_execution_scope(persist_default=False)
        return _enqueue_ingestion_window(
            appid=scope.target_appid,
            start_time=int((local_now - timedelta(hours=duration_hours)).timestamp()),
            end_time=int(local_now.timestamp()),
            page_size=_fixed_ingestion_page_size(),
            task_type=MANUAL_INGEST_WINDOW_TASK,
            idempotency_prefix="manual-ingest-window",
            idempotency_scope_version=scope.scope_version,
            checkpoint={"execution_scope": scope.as_checkpoint()},
        )


def enqueue_filtered_post_refresh(*, filters: OriginalPostFilter) -> IngestionEnqueueResult:
    """Queue a fixed predicate; the Worker resolves matching local posts when it runs."""
    with transaction.atomic():
        scope = current_execution_scope(persist_default=False)
        parameters = {"appid": scope.target_appid, "filters": filters.as_parameters()}
        predicate_key = sha256(
            json.dumps(parameters["filters"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        result = enqueue_active_coalesced_job(
            task_type=FILTERED_POST_REFRESH_TASK,
            idempotency_key=(
                f"filtered-post-refresh:{scope.target_appid}:{scope.scope_version}:{uuid4().hex}"
            ),
            active_coalescing_key=(
                f"filtered-post-refresh:{scope.target_appid}:v{scope.scope_version}:{predicate_key}"
            ),
            parameter_summary=parameters,
            checkpoint={"execution_scope": scope.as_checkpoint()},
        )
    return IngestionEnqueueResult(job=result.job, created=result.created)


def enqueue_ingestion_window(
    *, appid: object, start_time: object, end_time: object, page_size: object
) -> IngestionEnqueueResult:
    """Compatibility name for creating a new manual historical ingestion task."""
    return enqueue_manual_ingestion_window(
        appid=appid,
        start_time=start_time,
        end_time=end_time,
        page_size=page_size,
    )


def scheduled_ingestion_window(now: datetime, *, interval_minutes: int = 15) -> IngestionWindow:
    """Return one scheduler-owned window while retaining fixed safe work size."""
    if now.tzinfo is None:
        raise ValueError("调度时间必须包含时区")
    interval = _positive_int(interval_minutes, name="调度间隔")
    lookback = _positive_int(settings.SCHEDULED_INGESTION_LOOKBACK_MINUTES, name="回看时长")
    if lookback != 30:
        raise ValueError("自动采集回看时长必须为30分钟")
    local_now = now.astimezone(SHANGHAI)
    end_at = local_now.replace(
        minute=local_now.minute - (local_now.minute % interval), second=0, microsecond=0
    )
    start_at = end_at - timedelta(minutes=lookback)
    return IngestionWindow(start_time=int(start_at.timestamp()), end_time=int(end_at.timestamp()))


def enqueue_scheduled_ingestion(
    now: datetime, *, interval_minutes: int = 15
) -> IngestionEnqueueResult:
    """Enqueue the current operation scope without invoking any browser adapter."""
    appid = active_game_appid(persist_default=False)
    page_size = _fixed_ingestion_page_size()
    window = scheduled_ingestion_window(now, interval_minutes=interval_minutes)
    return _enqueue_ingestion_window(
        appid=appid,
        start_time=window.start_time,
        end_time=window.end_time,
        page_size=page_size,
        task_type=SCHEDULED_INGEST_WINDOW_TASK,
        idempotency_prefix="scheduled-ingest-window",
    )


def enqueue_scheduled_status_sync(
    now: datetime, *, interval_minutes: int = 15
) -> IngestionEnqueueResult:
    """Queue one small pending-post status scan in the same scheduler time bucket."""
    appid = active_game_appid(persist_default=False)
    window = scheduled_ingestion_window(now, interval_minutes=interval_minutes)
    result = enqueue_job_with_result(
        task_type=STATUS_SYNC_TASK,
        idempotency_key=f"status-sync:{appid}:{window.end_time}:20",
        parameter_summary={"appid": appid, "limit": 20},
    )
    return IngestionEnqueueResult(job=result.job, created=result.created)


def enqueue_scheduled_jobs(now: datetime, *, interval_minutes: int = 15) -> ScheduledJobsResult:
    """Create the complete, ordered scheduler workload for the current time bucket."""
    return ScheduledJobsResult(
        status_sync=enqueue_scheduled_status_sync(now, interval_minutes=interval_minutes),
        ingestion=enqueue_scheduled_ingestion(now, interval_minutes=interval_minutes),
    )


def _metrics_refresh_parameters(*, appid: object, start_time: object, end_time: object) -> dict:
    normalized_appid = str(appid or "").strip()
    if not normalized_appid:
        raise ValueError("appid不能为空")
    start = _positive_int(start_time, name="开始时间")
    end = _positive_int(end_time, name="结束时间")
    if end <= start:
        raise ValueError("互动刷新时间范围无效")
    return {"appid": normalized_appid, "start_time": start, "end_time": end}


def _active_metrics_refresh_job(parameters: dict) -> BackgroundJob | None:
    """Find an active refresh without making completed scopes non-repeatable."""
    for job in BackgroundJob.objects.filter(
        task_type=METRICS_REFRESH_WINDOW_TASK,
        status__in=(JobStatus.PENDING, JobStatus.RUNNING),
    ).order_by("created_at"):
        if job.parameter_summary == parameters:
            return job
    return None


def enqueue_metrics_refresh_window(
    *, appid: object, start_time: object, end_time: object
) -> MetricsRefreshEnqueueResult:
    """Queue a manual refresh, reusing the same active scope when present."""
    parameters = _metrics_refresh_parameters(appid=appid, start_time=start_time, end_time=end_time)
    active = _active_metrics_refresh_job(parameters)
    if active is not None:
        return MetricsRefreshEnqueueResult(job=active, created=False)
    result = enqueue_job_with_result(
        task_type=METRICS_REFRESH_WINDOW_TASK,
        idempotency_key=(
            "metrics-refresh:"
            f"{parameters['appid']}:{parameters['start_time']}:{parameters['end_time']}:{uuid4().hex}"
        ),
        parameter_summary=parameters,
    )
    return MetricsRefreshEnqueueResult(job=result.job, created=result.created)


def latest_metrics_refresh_job(
    *, appid: object, start_time: object, end_time: object
) -> BackgroundJob | None:
    """Return the most recent refresh result for a scope, if any."""
    parameters = _metrics_refresh_parameters(appid=appid, start_time=start_time, end_time=end_time)
    for job in BackgroundJob.objects.filter(task_type=METRICS_REFRESH_WINDOW_TASK).order_by(
        "-created_at"
    ):
        if job.parameter_summary == parameters:
            return job
    return None
