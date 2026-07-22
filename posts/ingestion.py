"""Read-only original-post ingestion orchestration."""

from __future__ import annotations

from typing import Protocol

from django.utils import timezone

from config.game_circle_ipc import is_session_recovering_error
from jobs.adapters import CandidatePostDTO, SourceContractError, SourceTransportError
from jobs.models import BackgroundJob

from .models import CandidatePost
from .game_catalog import checkpoint_with_execution_scope
from .services import classify_content_card, store_post_metrics


class ContentCardReader(Protocol):
    def list_candidates(
        self, *, page: int, page_size: int, start_time: int, end_time: int
    ) -> list[CandidatePostDTO]: ...

    def get_content_card(self, content_id: str) -> dict: ...

    def get_post_metrics(self, topic_id: int) -> dict: ...


def ingest_page(
    client: ContentCardReader, *, page: int, page_size: int, start_time: int, end_time: int
) -> int:
    """Idempotently ingest one page; never hold a database transaction across CDP I/O."""
    candidates = client.list_candidates(
        page=page, page_size=page_size, start_time=start_time, end_time=end_time
    )
    for item in candidates:
        candidate, _ = CandidatePost.objects.update_or_create(
            topic_id=item.topic_id,
            defaults={"content_id": item.content_id, "source_appid": item.source_appid},
        )
        if not candidate.content_id:
            classify_content_card(candidate, {})
            continue
        try:
            payload = client.get_content_card(candidate.content_id)
        except SourceContractError as exc:
            candidate.classification_error = str(exc)[:500]
            candidate.save(update_fields=["classification_error", "updated_at"])
            continue
        try:
            card = payload["data"]["content_card"]
        except (KeyError, TypeError):
            candidate.classification_error = "内容卡片响应结构无效"
            candidate.save(update_fields=["classification_error", "updated_at"])
            continue
        post = classify_content_card(candidate, card)
        if post is not None:
            try:
                requested_at = timezone.now()
                store_post_metrics(
                    post,
                    client.get_post_metrics(candidate.topic_id),
                    source_requested_at=requested_at,
                )
            except (SourceContractError, SourceTransportError) as exc:
                if is_session_recovering_error(exc):
                    raise
                # Metrics are optional; keep the original post even if counters are unavailable.
                pass
            from reviews.services import enqueue_evaluation_pipeline
            enqueue_evaluation_pipeline(post)
    return len(candidates)


def ingest_window(
    client: ContentCardReader, job: BackgroundJob, *, page_size: int, start_time: int, end_time: int
) -> int:
    """Process one checkpointed page and preserve progress before the next worker run."""
    page = int(job.checkpoint.get("page", 0))
    count = ingest_page(
        client, page=page, page_size=page_size, start_time=start_time, end_time=end_time
    )
    job.checkpoint = checkpoint_with_execution_scope(
        job, {"page": page + 1, "last_processed_count": count}
    )
    job.save(update_fields=["checkpoint", "updated_at"])
    return count
