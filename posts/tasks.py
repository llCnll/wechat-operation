"""Background-task handlers for read-only original-post ingestion."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any

from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone

from config.game_circle_ipc import (
    GameCircleIpcError,
    GameCirclePostClient,
    is_session_recovering_error,
)
from jobs.adapters import SourceBusinessDeletedError, SourceContractError, SourceTransportError
from jobs.models import BackgroundJob
from jobs.once.author_mute_status_backfill import (
    author_mute_status_backfill_handler,
)
from jobs.once.backfill_post_topics import post_topic_backfill_handler
from jobs.services import JobDeferred, JobTerminalError
from jobs.task_types import (
    AUTHOR_MUTE_STATUS_BACKFILL_V2_TASK,
    COMMENT_EXPORT_V1_TASK,
    FILTERED_POST_REFRESH_TASK,
    LEGACY_INGEST_WINDOW_TASK,
    MANUAL_CURRENT_INGEST_WINDOW_TASK,
    MANUAL_INGEST_WINDOW_TASK,
    MANUAL_TOPIC_INGEST_TASK,
    METRICS_REFRESH_WINDOW_TASK,
    POST_TOPIC_BACKFILL_V1_TASK,
    SCHEDULED_INGEST_WINDOW_TASK,
    STATUS_SYNC_TASK,
)
from reviews.models import ReviewStatus

from .comment_exports import comment_export_handler
from .dashboard_filters import filter_original_posts, original_post_filter_from_parameters
from .game_catalog import (
    checkpoint_with_execution_scope,
    resolve_execution_scope,
    resolve_fixed_execution_scope,
)
from .ingestion import ingest_page
from .models import CandidatePost, InteractionSnapshot, OriginType, OriginalPost
from .services import (
    classify_content_card,
    refresh_one_post_from_source,
    store_post_interaction_metrics,
    store_post_metrics,
)

MAX_METRICS_REFRESH_FAILURE_SAMPLES = 20
MAX_MANUAL_TOPIC_ID = 2**63 - 1


def _raise_if_session_recovering(error: Exception) -> None:
    """Keep source checkpoints intact while the sole session service rebuilds a Frame."""
    if is_session_recovering_error(error):
        raise JobDeferred("等待游戏圈会话恢复", delay_seconds=5) from error


@contextmanager
def _source_client(scope):
    client = GameCirclePostClient(scope=scope)
    try:
        yield client
    except GameCircleIpcError as exc:
        _raise_if_session_recovering(exc)
        raise
    finally:
        client.close()


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
    try:
        scope = resolve_execution_scope(job)
    except ValueError as exc:
        raise SourceContractError(str(exc)) from exc
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
    with _source_client(scope) as client:
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
            job.checkpoint = checkpoint_with_execution_scope(
                job,
                {
                    "page": page + 1,
                    "last_processed_count": count,
                    "total_processed": total_processed,
                    "complete": is_complete,
                },
            )
            job.save(update_fields=["checkpoint", "updated_at"])
            if is_complete:
                return
            page += 1


def _save_manual_topic_checkpoint(job: BackgroundJob, *, topic_id: int, result: str) -> None:
    job.checkpoint = checkpoint_with_execution_scope(
        job, {"topic_id": topic_id, "result": result, "complete": True}
    )
    job.save(update_fields=["checkpoint", "updated_at"])


def manual_topic_ingest_handler(job: BackgroundJob) -> None:
    """Ingest one submitted topic through the fixed candidate lookup contract."""
    if (job.checkpoint or {}).get("complete"):
        return
    try:
        scope = resolve_fixed_execution_scope(job)
    except ValueError as exc:
        raise JobTerminalError(str(exc)) from exc
    topic_id = _required_int(job.parameter_summary, "topic_id")
    if job.parameter_summary.get("appid") != scope.target_appid:
        raise JobTerminalError("指定帖子任务 AppID 与冻结执行范围不一致")
    if topic_id > MAX_MANUAL_TOPIC_ID:
        raise JobTerminalError("指定帖子 ID 超出允许范围")

    try:
        with _source_client(scope) as client:
            item = client.get_candidate_by_topic_id(topic_id)
            if item is None:
                _save_manual_topic_checkpoint(job, topic_id=topic_id, result="NOT_FOUND")
                return
            if item.topic_id != topic_id:
                raise JobTerminalError("指定帖子候选响应与请求 ID 不一致")
            if item.source_appid != scope.target_appid:
                raise JobTerminalError("指定帖子不属于提交时的当前操作游戏")
            if not item.content_id:
                raise JobTerminalError("指定帖子候选响应缺少 content ID")

            candidate, _ = CandidatePost.objects.update_or_create(
                topic_id=item.topic_id,
                defaults={"content_id": item.content_id, "source_appid": item.source_appid},
            )
            payload = client.get_content_card(candidate.content_id)
            try:
                card = payload["data"]["content_card"]
            except (KeyError, TypeError) as exc:
                raise JobTerminalError("内容卡片响应结构无效") from exc
            if not isinstance(card, dict):
                raise JobTerminalError("内容卡片响应结构无效")
            post = classify_content_card(candidate, card)
            if post is None and candidate.origin_type != OriginType.FORWARD:
                raise JobTerminalError("内容卡片无法完成帖子分类")
            if post is not None:
                try:
                    requested_at = timezone.now()
                    store_post_metrics(
                        post,
                        client.get_post_metrics(topic_id),
                        source_requested_at=requested_at,
                    )
                except (SourceContractError, SourceTransportError) as exc:
                    _raise_if_session_recovering(exc)
                    # Metrics remain optional after a valid original content-card projection.
                    pass
                from reviews.services import enqueue_evaluation_pipeline

                enqueue_evaluation_pipeline(post)
            _save_manual_topic_checkpoint(
                job,
                topic_id=topic_id,
                result="ORIGINAL" if post is not None else "FORWARD",
            )
    except SourceBusinessDeletedError:
        _save_manual_topic_checkpoint(job, topic_id=topic_id, result="DELETED")
    except GameCircleIpcError as exc:
        if exc.category == "SOURCE_DELETED":
            _save_manual_topic_checkpoint(job, topic_id=topic_id, result="DELETED")
            return
        _raise_if_session_recovering(exc)
        raise
    except SourceContractError as exc:
        raise JobTerminalError(str(exc)) from exc


def sync_pending_status_handler(job: BackgroundJob) -> None:
    """Refresh a bounded, rotating batch of pending posts without changing review decisions."""
    try:
        scope = resolve_execution_scope(job)
        appid = scope.target_appid
    except ValueError as exc:
        raise SourceContractError(str(exc)) from exc
    limit = _required_int(job.parameter_summary, "limit")
    if not appid:
        raise SourceContractError("状态巡检任务缺少 appid")
    if not 1 <= limit <= 50:
        raise SourceContractError("状态巡检任务 limit 必须在1至50之间")

    app_filter = Q(source_appid=appid)
    posts = list(
        OriginalPost.objects.filter(app_filter, review_task__status=ReviewStatus.PENDING).order_by(
            "updated_at", "topic_id"
        )[:limit]
    )
    attempted = 0
    refreshed = 0
    failed = 0
    fallback_author_openids: set[str] = set()
    with _source_client(scope) as client:
        for post in posts:
            attempted += 1
            try:
                refresh_one_post_from_source(
                    client,
                    post,
                    source_appid=appid,
                    fallback_author_openids=fallback_author_openids,
                )
            except (SourceContractError, SourceTransportError) as exc:
                _raise_if_session_recovering(exc)
                failed += 1
                continue
            # Rotate the next scan toward older pending posts when the status is unchanged.
            post.save(update_fields=["updated_at"])
            refreshed += 1
    job.checkpoint = checkpoint_with_execution_scope(
        job,
        {
            "attempted": attempted,
            "refreshed": refreshed,
            "failed": failed,
            "limit": limit,
        },
    )
    job.save(update_fields=["checkpoint", "updated_at"])
    if failed:
        raise SourceTransportError("状态巡检存在未完成的来源读取")


def filtered_post_refresh_handler(job: BackgroundJob) -> None:
    """Refresh all posts matching the submit-time predicate with a stable topic cursor."""
    try:
        scope = resolve_fixed_execution_scope(job)
        filters = original_post_filter_from_parameters((job.parameter_summary or {}).get("filters"))
    except ValueError as exc:
        raise SourceContractError(str(exc)) from exc
    checkpoint = job.checkpoint or {}
    cursor = int(checkpoint.get("last_topic_id", 0))
    attempted = int(checkpoint.get("attempted", 0))
    refreshed = int(checkpoint.get("refreshed", 0))
    deleted = int(checkpoint.get("deleted", 0))
    author_fallback = int(checkpoint.get("author_fallback", 0))
    skipped_missing_author = int(checkpoint.get("skipped_missing_author", 0))
    failed = int(checkpoint.get("failed", 0))
    fallback_author_openids: set[str] = set()

    def save_checkpoint(*, complete: bool) -> None:
        job.checkpoint = checkpoint_with_execution_scope(
            job,
            {
                "last_topic_id": cursor,
                "attempted": attempted,
                "refreshed": refreshed,
                "deleted": deleted,
                "author_fallback": author_fallback,
                "skipped_missing_author": skipped_missing_author,
                "failed": failed,
                "complete": complete,
            },
        )
        job.save(update_fields=("checkpoint", "updated_at"))

    base = OriginalPost.objects.annotate(
        latest_view_count=Subquery(
            InteractionSnapshot.objects.filter(post_id=OuterRef("pk"))
            .order_by("-captured_at")
            .values("view_count")[:1]
        )
    )
    with _source_client(scope) as client:
        while True:
            post = (
                filter_original_posts(base, source_appid=scope.target_appid, filters=filters)
                .filter(topic_id__gt=cursor)
                .order_by("topic_id")
                .first()
            )
            if post is None:
                save_checkpoint(complete=True)
                if failed:
                    raise SourceTransportError("筛选刷新存在未完成的来源读取")
                return
            attempted += 1
            try:
                outcome = refresh_one_post_from_source(
                    client,
                    post,
                    source_appid=scope.target_appid,
                    fallback_author_openids=fallback_author_openids,
                )
            except (SourceContractError, SourceTransportError) as exc:
                _raise_if_session_recovering(exc)
                failed += 1
                # A cursor is an acknowledgement of one completed target.
                # Advancing past a retryable failure would make the final Job
                # status truthful but silently omit that post on the next
                # attempt.  Persist only aggregate progress and let the job
                # retry the same topic.
                save_checkpoint(complete=False)
                raise SourceTransportError("筛选刷新存在未完成的来源读取") from exc
            else:
                if outcome == "refreshed":
                    refreshed += 1
                elif outcome == "author_fallback":
                    deleted += 1
                    author_fallback += 1
                else:
                    deleted += 1
                    if not post.author_openid:
                        skipped_missing_author += 1
            cursor = post.topic_id
            save_checkpoint(complete=False)


def refresh_metrics_window_handler(job: BackgroundJob) -> None:
    """Refresh interaction snapshots for one range without changing post state or content."""
    try:
        scope = resolve_execution_scope(job)
        appid = scope.target_appid
    except ValueError as exc:
        raise SourceContractError(str(exc)) from exc
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
        job.checkpoint = checkpoint_with_execution_scope(job, saved_checkpoint)
        job.save(update_fields=["checkpoint", "updated_at"])

    start_at = datetime.fromtimestamp(start_time, tz=timezone.get_current_timezone())
    end_at = datetime.fromtimestamp(end_time, tz=timezone.get_current_timezone())
    app_filter = Q(source_appid=appid)
    with _source_client(scope) as client:
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
                _raise_if_session_recovering(exc)
                failed += 1
                if len(failure_samples) < MAX_METRICS_REFRESH_FAILURE_SAMPLES:
                    failure_samples.append({"topic_id": post.topic_id, "error": str(exc)[:200]})
            else:
                processed += 1
            cursor = post.topic_id
            save_checkpoint(complete=False)


REALTIME_INGESTION_WORKER_HANDLERS = {
    SCHEDULED_INGEST_WINDOW_TASK: ingest_window_handler,
    STATUS_SYNC_TASK: sync_pending_status_handler,
}

BACKFILL_INGESTION_WORKER_HANDLERS = {
    LEGACY_INGEST_WINDOW_TASK: ingest_window_handler,
    MANUAL_INGEST_WINDOW_TASK: ingest_window_handler,
    MANUAL_CURRENT_INGEST_WINDOW_TASK: ingest_window_handler,
    MANUAL_TOPIC_INGEST_TASK: manual_topic_ingest_handler,
    FILTERED_POST_REFRESH_TASK: filtered_post_refresh_handler,
    METRICS_REFRESH_WINDOW_TASK: refresh_metrics_window_handler,
    AUTHOR_MUTE_STATUS_BACKFILL_V2_TASK: author_mute_status_backfill_handler,
    POST_TOPIC_BACKFILL_V1_TASK: post_topic_backfill_handler,
    COMMENT_EXPORT_V1_TASK: comment_export_handler,
}
