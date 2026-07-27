"""Versioned one-time backfill of minimal author projections and mute state."""

from __future__ import annotations

from contextlib import contextmanager
from django.db.models import OuterRef, Subquery
from django.utils import timezone

from jobs.adapters import SourceBusinessDeletedError, SourceContractError, SourceTransportError
from jobs.models import BackgroundJob, JobStatus
from jobs.services import enqueue_job
from jobs.task_types import AUTHOR_MUTE_STATUS_BACKFILL_V2_TASK
from posts.game_catalog import checkpoint_with_execution_scope, resolve_execution_scope
from posts.models import OriginalPost
from posts.services import sync_author_mute_status, sync_author_mute_status_from_user_detail
from config.game_circle_ipc import GameCirclePostClient

AUTHOR_MUTE_STATUS_BACKFILL_V2_IDEMPOTENCY_KEY = "once:author-mute-status-backfill:v2"
LEGACY_AUTHOR_MUTE_STATUS_BACKFILL_TASK = "AUTHOR_MUTE_STATUS_BACKFILL"


@contextmanager
def _source_client(scope):
    client = GameCirclePostClient(scope=scope)
    try:
        yield client
    finally:
        client.close()


def enqueue_author_mute_status_backfill() -> BackgroundJob:
    """Create or explicitly resume the V2 backfill without any source I/O."""
    if BackgroundJob.objects.filter(
        task_type=LEGACY_AUTHOR_MUTE_STATUS_BACKFILL_TASK,
        status__in=(JobStatus.PENDING, JobStatus.RUNNING),
    ).exists():
        raise ValueError("存在未终态的旧版作者状态回填任务，请先结束后再部署 V2")
    job = enqueue_job(
        task_type=AUTHOR_MUTE_STATUS_BACKFILL_V2_TASK,
        idempotency_key=AUTHOR_MUTE_STATUS_BACKFILL_V2_IDEMPOTENCY_KEY,
        parameter_summary={"scope": "all_original_posts"},
        max_retries=0,
    )
    if job.status in (JobStatus.FAILED, JobStatus.CANCELED):
        job.status = JobStatus.PENDING
        job.retry_count = 0
        job.next_run_at = None
        job.started_at = None
        job.finished_at = None
        job.error_summary = ""
        job.save(
            update_fields=[
                "status",
                "retry_count",
                "next_run_at",
                "started_at",
                "finished_at",
                "error_summary",
                "updated_at",
            ]
        )
    return job


def author_mute_status_backfill_handler(job: BackgroundJob) -> None:
    """Backfill by saved author first, using topic detail only to discover a missing ID."""
    checkpoint = job.checkpoint or {}
    if checkpoint.get("complete"):
        return
    cursor = int(checkpoint.get("last_post_id", 0))
    processed = int(checkpoint.get("processed", 0))
    skipped = int(checkpoint.get("skipped", 0))
    failed = int(checkpoint.get("failed", 0))
    missing_author_identity = int(checkpoint.get("missing_author_identity", 0))
    identity_consistency_verified = int(checkpoint.get("identity_consistency_verified", 0))
    failure_categories = checkpoint.get("failure_categories", {})
    if not isinstance(failure_categories, dict):
        failure_categories = {}
    try:
        scope = resolve_execution_scope(job)
        appid = scope.target_appid
    except ValueError as exc:
        raise SourceContractError(str(exc)) from exc

    def save_checkpoint(*, complete: bool) -> None:
        saved = {
            "last_post_id": cursor,
            "processed": processed,
            "skipped": skipped,
            "missing_author_identity": missing_author_identity,
            "identity_consistency_verified": identity_consistency_verified,
            "failure_categories": failure_categories,
            "complete": complete,
        }
        if failed:
            saved["failed"] = failed
        job.checkpoint = checkpoint_with_execution_scope(job, saved)
        job.save(update_fields=["checkpoint", "updated_at"])

    first_post_for_author = OriginalPost.objects.filter(
        source_appid=appid,
        author_openid=OuterRef("author_openid"),
    ).exclude(author_openid="").order_by("pk")
    with _source_client(scope) as client:
        while post := (
            OriginalPost.objects.filter(source_appid=appid, pk__gt=cursor)
            .annotate(first_post_id=Subquery(first_post_for_author.values("pk")[:1]))
            .order_by("pk")
            .first()
        ):
            try:
                requested_at = timezone.now()
                if post.author_openid:
                    if post.first_post_id != post.pk:
                        skipped += 1
                    else:
                        payload = client.get_author_detail(post.author_openid)
                        sync_author_mute_status_from_user_detail(
                            payload,
                            expected_openid=post.author_openid,
                            source_requested_at=requested_at,
                            source_appid=appid,
                            post=post,
                        )
                        processed += 1
                else:
                    topic_payload = client.get_post_metrics(post.topic_id)
                    sync_author_mute_status(
                        post,
                        topic_payload,
                        source_requested_at=requested_at,
                        source_appid=appid,
                    )
                    post.refresh_from_db(fields=("author_openid",))
                    if not post.author_openid:
                        raise SourceContractError("帖子详情缺少作者用户 ID")
                    author_payload = client.get_author_detail(post.author_openid)
                    sync_author_mute_status_from_user_detail(
                        author_payload,
                        expected_openid=post.author_openid,
                        source_requested_at=timezone.now(),
                        source_appid=appid,
                        post=post,
                    )
                    identity_consistency_verified += 1
                    processed += 1
            except SourceBusinessDeletedError:
                skipped += 1
                if not post.author_openid:
                    missing_author_identity += 1
            except (SourceContractError, SourceTransportError) as exc:
                failed += 1
                category = type(exc).__name__
                failure_categories[category] = min(int(failure_categories.get(category, 0)) + 1, 9999)
            cursor = post.pk
            save_checkpoint(complete=False)
        save_checkpoint(complete=True)
