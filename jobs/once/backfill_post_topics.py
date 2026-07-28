"""Versioned, local-only one-time normalization of original-post topics."""

from __future__ import annotations

from jobs.models import BackgroundJob, JobStatus
from jobs.services import enqueue_job
from jobs.task_types import POST_TOPIC_BACKFILL_V1_TASK
from posts.content import split_trailing_topics
from posts.models import OriginalPost

POST_TOPIC_BACKFILL_V1_IDEMPOTENCY_KEY = "once:post-topic-backfill:v1"


def enqueue_post_topic_backfill() -> BackgroundJob:
    """Create or explicitly resume the local-only topic backfill."""
    job = enqueue_job(
        task_type=POST_TOPIC_BACKFILL_V1_TASK,
        idempotency_key=POST_TOPIC_BACKFILL_V1_IDEMPOTENCY_KEY,
        parameter_summary={"scope": "all_original_posts", "upstream_access": False},
        max_retries=0,
    )
    if job.status in (JobStatus.FAILED, JobStatus.CANCELED):
        job.status = JobStatus.PENDING
        job.retry_count = 0
        job.next_run_at = None
        job.started_at = None
        job.finished_at = None
        job.error_summary = ""
        job.save(update_fields=("status", "retry_count", "next_run_at", "started_at", "finished_at", "error_summary", "updated_at"))
    return job


def post_topic_backfill_handler(job: BackgroundJob) -> None:
    """Use only a primary-key cursor and aggregate counters; never open a source."""
    checkpoint = job.checkpoint or {}
    if checkpoint.get("complete"):
        return
    cursor = int(checkpoint.get("last_post_id", 0))
    processed = int(checkpoint.get("processed", 0))
    updated = int(checkpoint.get("updated", 0))
    batch_size = 200

    while True:
        posts = list(OriginalPost.objects.filter(pk__gt=cursor).order_by("pk")[:batch_size])
        if not posts:
            job.checkpoint = {
                "last_post_id": cursor,
                "processed": processed,
                "updated": updated,
                "complete": True,
            }
            job.save(update_fields=("checkpoint", "updated_at"))
            return
        for post in posts:
            body, topics = split_trailing_topics(post.content_text or "")
            if post.content_text != body or list(post.topics or []) != list(topics):
                post.content_text = body
                post.topics = list(topics)
                post.save(update_fields=("content_text", "topics", "updated_at"))
                updated += 1
            processed += 1
            cursor = post.pk
        job.checkpoint = {
            "last_post_id": cursor,
            "processed": processed,
            "updated": updated,
            "complete": False,
        }
        job.save(update_fields=("checkpoint", "updated_at"))
