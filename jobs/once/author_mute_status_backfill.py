"""One-time backfill of author identifiers and mute-status observations."""

from __future__ import annotations

from contextlib import contextmanager
from django.utils import timezone

from jobs.adapters import SourceBusinessDeletedError, SourceContractError, SourceTransportError
from jobs.models import BackgroundJob, JobStatus
from jobs.services import enqueue_job
from jobs.task_types import AUTHOR_MUTE_STATUS_BACKFILL_TASK
from posts.game_catalog import checkpoint_with_execution_scope, resolve_execution_scope
from posts.models import OriginalPost
from posts.services import sync_author_mute_status
from config.game_circle_ipc import GameCirclePostClient

AUTHOR_MUTE_STATUS_BACKFILL_IDEMPOTENCY_KEY = "once:author-mute-status-backfill:v1"
MAX_FAILURE_SAMPLES = 20


@contextmanager
def _source_client(scope):
    client = GameCirclePostClient(scope=scope)
    try:
        yield client
    finally:
        client.close()


def enqueue_author_mute_status_backfill() -> BackgroundJob:
    """Create the single operator-triggered backfill job without contacting the source."""
    job = enqueue_job(
        task_type=AUTHOR_MUTE_STATUS_BACKFILL_TASK,
        idempotency_key=AUTHOR_MUTE_STATUS_BACKFILL_IDEMPOTENCY_KEY,
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
    """Backfill one detail read per original post, retaining only a small cursor checkpoint."""
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
    try:
        scope = resolve_execution_scope(job)
        appid = scope.target_appid
    except ValueError as exc:
        raise SourceContractError(str(exc)) from exc

    def save_checkpoint(*, complete: bool) -> None:
        saved = {
            "last_topic_id": cursor,
            "processed": processed,
            "skipped": skipped,
            "complete": complete,
        }
        if failed:
            saved["failed"] = failed
            saved["failure_samples"] = failure_samples
        job.checkpoint = checkpoint_with_execution_scope(job, saved)
        job.save(update_fields=["checkpoint", "updated_at"])

    with _source_client(scope) as client:
        while post := (
            OriginalPost.objects.filter(source_appid=appid, topic_id__gt=cursor)
            .order_by("topic_id")
            .first()
        ):
            try:
                requested_at = timezone.now()
                payload = client.get_post_metrics(post.topic_id)
                sync_author_mute_status(
                    post,
                    payload,
                    source_requested_at=requested_at,
                    source_appid=appid,
                )
            except SourceBusinessDeletedError:
                skipped += 1
            except (SourceContractError, SourceTransportError) as exc:
                failed += 1
                if len(failure_samples) < MAX_FAILURE_SAMPLES:
                    failure_samples.append({"topic_id": post.topic_id, "error": str(exc)[:200]})
            else:
                processed += 1
            cursor = post.topic_id
            save_checkpoint(complete=False)
        save_checkpoint(complete=True)
