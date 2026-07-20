"""Background-task handlers for read-only original-post ingestion."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db.models import Q
from django.utils import timezone

from jobs.adapters import SourceBusinessDeletedError, SourceContractError, SourceTransportError
from jobs.models import BackgroundJob
from jobs.once.author_mute_status_backfill import (
    AUTHOR_MUTE_STATUS_BACKFILL_TASK,
    author_mute_status_backfill_handler,
)
from reviews.models import ReviewStatus

from .ingestion import ingest_page
from .models import OriginalPost, mark_post_deleted
from .services import store_post_interaction_metrics, store_post_metrics
from .source_client import CdpGameCircleClient

LEGACY_INGEST_WINDOW_TASK = "INGEST_WINDOW"
SCHEDULED_INGEST_WINDOW_TASK = "SCHEDULED_INGEST_WINDOW"
MANUAL_INGEST_WINDOW_TASK = "MANUAL_INGEST_WINDOW"
STATUS_SYNC_TASK = "STATUS_SYNC"
METRICS_REFRESH_WINDOW_TASK = "METRICS_REFRESH_WINDOW"
MAX_METRICS_REFRESH_FAILURE_SAMPLES = 20


def _required_int(parameters: dict[str, Any], key: str) -> int:
    try:
        value = int(parameters[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceContractError(f"采集任务参数无效：{key}") from exc
    if value <= 0:
        raise SourceContractError(f"采集任务参数必须为正数：{key}")
    return value


def ingest_window_handler(job: BackgroundJob) -> None:
    """Run a whole window while persisting a checkpoint after every completed page."""
    parameters = job.parameter_summary
    appid = str(parameters.get("appid", ""))
    if not appid:
        raise SourceContractError("采集任务缺少 appid")
    start_time = _required_int(parameters, "start_time")
    end_time = _required_int(parameters, "end_time")
    page_size = _required_int(parameters, "page_size")
    if end_time < start_time:
        raise SourceContractError("采集时间窗口无效")

    checkpoint = job.checkpoint or {}
    if checkpoint.get("complete"):
        return
    page = int(checkpoint.get("page", 0))
    total_processed = int(checkpoint.get("total_processed", 0))
    client = CdpGameCircleClient(appid=appid)
    try:
        while True:
            count = ingest_page(
                client,
                page=page,
                page_size=page_size,
                start_time=start_time,
                end_time=end_time,
            )
            total_processed += count
            is_complete = count < page_size
            job.checkpoint = {
                "page": page + 1,
                "last_processed_count": count,
                "total_processed": total_processed,
                "complete": is_complete,
            }
            job.save(update_fields=["checkpoint", "updated_at"])
            if is_complete:
                return
            page += 1
    finally:
        client.close()


def sync_pending_status_handler(job: BackgroundJob) -> None:
    """Refresh a bounded, rotating batch of pending posts without changing review decisions."""
    appid = str(job.parameter_summary.get("appid", "")).strip()
    limit = _required_int(job.parameter_summary, "limit")
    if not appid:
        raise SourceContractError("状态巡检任务缺少 appid")
    if not 1 <= limit <= 50:
        raise SourceContractError("状态巡检任务 limit 必须在1至50之间")

    app_filter = Q(source_appid=appid)
    if appid == "wx6d2454a8afcd9706":
        app_filter |= Q(source_appid="")
    posts = list(
        OriginalPost.objects.filter(app_filter, review_task__status=ReviewStatus.PENDING)
        .order_by("updated_at", "topic_id")[:limit]
    )
    refreshed = 0
    client = CdpGameCircleClient(appid=appid)
    try:
        for post in posts:
            try:
                requested_at = timezone.now()
                payload = client.get_post_metrics(post.topic_id)
            except SourceBusinessDeletedError:
                post.status = mark_post_deleted(post.status)
                post.save(update_fields=["status", "updated_at"])
            except (SourceContractError, SourceTransportError):
                continue
            else:
                try:
                    store_post_metrics(post, payload, source_requested_at=requested_at)
                except (SourceContractError, SourceTransportError):
                    continue
                # Rotate the next scan toward older pending posts when the status is unchanged.
                post.save(update_fields=["updated_at"])
            refreshed += 1
            job.checkpoint = {"refreshed": refreshed, "limit": limit}
            job.save(update_fields=["checkpoint", "updated_at"])
    finally:
        client.close()


def refresh_metrics_window_handler(job: BackgroundJob) -> None:
    """Refresh interaction snapshots for one range without changing post state or content."""
    appid = str(job.parameter_summary.get("appid", "")).strip()
    start_time = _required_int(job.parameter_summary, "start_time")
    end_time = _required_int(job.parameter_summary, "end_time")
    if not appid:
        raise SourceContractError("互动刷新任务缺少 appid")
    if end_time <= start_time:
        raise SourceContractError("互动刷新时间范围无效")
    checkpoint = job.checkpoint or {}
    if checkpoint.get("complete"):
        return
    cursor = int(checkpoint.get("last_topic_id", 0))
    processed = int(checkpoint.get("processed", 0))
    skipped = int(checkpoint.get("skipped", 0))
    failed = int(checkpoint.get("failed", 0))
    failure_samples = checkpoint.get("failure_samples", [])
    if not isinstance(failure_samples, list):
        failure_samples = []

    def save_checkpoint(*, complete: bool) -> None:
        saved_checkpoint = {
            "last_topic_id": cursor,
            "processed": processed,
            "skipped": skipped,
            "complete": complete,
        }
        if failed:
            saved_checkpoint["failed"] = failed
            saved_checkpoint["failure_samples"] = failure_samples
        job.checkpoint = saved_checkpoint
        job.save(update_fields=["checkpoint", "updated_at"])

    start_at = datetime.fromtimestamp(start_time, tz=timezone.get_current_timezone())
    end_at = datetime.fromtimestamp(end_time, tz=timezone.get_current_timezone())
    app_filter = Q(source_appid=appid)
    if appid == "wx6d2454a8afcd9706":
        app_filter |= Q(source_appid="")
    client = CdpGameCircleClient(appid=appid)
    try:
        while True:
            post = (
                OriginalPost.objects.filter(
                    app_filter,
                    publish_time__gte=start_at,
                    publish_time__lt=end_at,
                    topic_id__gt=cursor,
                )
                .order_by("topic_id")
                .first()
            )
            if post is None:
                save_checkpoint(complete=True)
                return
            try:
                store_post_interaction_metrics(post, client.get_post_metrics(post.topic_id))
            except SourceBusinessDeletedError:
                skipped += 1
            except (SourceContractError, SourceTransportError) as exc:
                failed += 1
                if len(failure_samples) < MAX_METRICS_REFRESH_FAILURE_SAMPLES:
                    failure_samples.append(
                        {"topic_id": post.topic_id, "error": str(exc)[:200]}
                    )
            else:
                processed += 1
            cursor = post.topic_id
            save_checkpoint(complete=False)
    finally:
        client.close()


REALTIME_INGESTION_WORKER_HANDLERS = {
    SCHEDULED_INGEST_WINDOW_TASK: ingest_window_handler,
    STATUS_SYNC_TASK: sync_pending_status_handler,
}

BACKFILL_INGESTION_WORKER_HANDLERS = {
    LEGACY_INGEST_WINDOW_TASK: ingest_window_handler,
    MANUAL_INGEST_WINDOW_TASK: ingest_window_handler,
    METRICS_REFRESH_WINDOW_TASK: refresh_metrics_window_handler,
    AUTHOR_MUTE_STATUS_BACKFILL_TASK: author_mute_status_backfill_handler,
}
