from unittest.mock import patch

import pytest

from config.game_circle_ipc import GameCircleIpcError
from jobs.adapters import CandidatePostDTO, SourceBusinessDeletedError, SourceTransportError
from jobs.models import BackgroundJob
from jobs.services import execute_next_job
from posts.game_catalog import set_active_game
from posts.models import POST_STATUS_DELETED, CandidatePost, ManagedGame, OriginalPost, OriginType
from posts.tasks import (
    AUTHOR_MUTE_STATUS_BACKFILL_V2_TASK,
    BACKFILL_INGESTION_WORKER_HANDLERS,
    COMMENT_EXPORT_V1_TASK,
    FILTERED_POST_REFRESH_TASK,
    LEGACY_INGEST_WINDOW_TASK,
    MANUAL_CURRENT_INGEST_WINDOW_TASK,
    MANUAL_INGEST_WINDOW_TASK,
    METRICS_REFRESH_WINDOW_TASK,
    POST_TOPIC_BACKFILL_V1_TASK,
    REALTIME_INGESTION_WORKER_HANDLERS,
    SCHEDULED_INGEST_WINDOW_TASK,
    STATUS_SYNC_TASK,
    ingest_window_handler,
    refresh_metrics_window_handler,
    sync_pending_status_handler,
)
from reviews.models import RecommendedAction, ReviewTask


@pytest.fixture(autouse=True)
def active_scope_and_source_session(monkeypatch, db):
    game = ManagedGame.objects.create(appid="wx-test", name="测试游戏")
    set_active_game(appid=game.appid)


class OnePageSource:
    def list_candidates(self, **kwargs):
        return [CandidatePostDTO(topic_id=17, content_id="content-17", source_appid="game")]

    def get_content_card(self, content_id):
        return {"data": {"content_card": {"base_content": {"content": "原创正文"}}}}

    def get_post_metrics(self, topic_id):
        return {"data": {"topic": {"view_count": 1}}}

    def close(self):
        pass


@pytest.mark.django_db
def test_worker_ingests_full_window_and_persists_completion_checkpoint():
    job = BackgroundJob.objects.create(
        task_type=SCHEDULED_INGEST_WINDOW_TASK,
        idempotency_key="ingest:worker",
        parameter_summary={"appid": "wx-test", "start_time": 1, "end_time": 2, "page_size": 10},
    )
    with patch("posts.tasks.GameCirclePostClient", return_value=OnePageSource()):
        ingest_window_handler(job)

    job.refresh_from_db()
    assert job.checkpoint == {
        "page": 1,
        "last_processed_count": 1,
        "total_processed": 1,
        "complete": True,
        "execution_scope": {"target_appid": "wx-test", "scope_version": 1},
    }
    assert CandidatePost.objects.get(topic_id=17).origin_type == OriginType.ORIGINAL


@pytest.mark.django_db
def test_status_sync_updates_pending_posts_without_changing_review_state():
    post = OriginalPost.objects.create(
        topic_id=18, content_id="status-sync", source_appid="wx-test", status=2
    )
    task = ReviewTask.objects.create(post=post, suggested_action=RecommendedAction.HUMAN_REVIEW)
    job = BackgroundJob.objects.create(
        task_type=STATUS_SYNC_TASK,
        idempotency_key="status-sync:worker",
        parameter_summary={"appid": "wx-test", "limit": 20},
    )

    class Source:
        def get_post_metrics(self, topic_id):
            assert topic_id == post.topic_id
            return {"data": {"topic": {"view_count": 9, "status": 4}}}

        def close(self):
            pass

    with patch("posts.tasks.GameCirclePostClient", return_value=Source()):
        sync_pending_status_handler(job)

    post.refresh_from_db()
    task.refresh_from_db()
    assert post.status == 4
    assert post.interaction_snapshots.latest("captured_at").view_count == 9
    assert task.status == "PENDING"
    assert job.checkpoint == {
        "attempted": 1,
        "refreshed": 1,
        "failed": 0,
        "limit": 20,
        "execution_scope": {"target_appid": "wx-test", "scope_version": 1},
    }


@pytest.mark.django_db
def test_status_sync_marks_upstream_deleted_posts_without_changing_review_state():
    post = OriginalPost.objects.create(
        topic_id=19, content_id="status-deleted", source_appid="wx-test", status=4
    )
    task = ReviewTask.objects.create(post=post, suggested_action=RecommendedAction.HUMAN_REVIEW)
    job = BackgroundJob.objects.create(
        task_type=STATUS_SYNC_TASK,
        idempotency_key="status-sync:deleted",
        parameter_summary={"appid": "wx-test", "limit": 20},
    )

    class Source:
        def get_post_metrics(self, topic_id):
            raise SourceBusinessDeletedError("deleted")

        def close(self):
            pass

    with patch("posts.tasks.GameCirclePostClient", return_value=Source()):
        sync_pending_status_handler(job)

    post.refresh_from_db()
    task.refresh_from_db()
    assert post.status == POST_STATUS_DELETED | 4
    assert task.status == "PENDING"


@pytest.mark.django_db
def test_status_sync_retries_instead_of_succeeding_when_source_read_is_incomplete():
    post = OriginalPost.objects.create(
        topic_id=20, content_id="status-unavailable", source_appid="wx-test", status=4
    )
    ReviewTask.objects.create(post=post, suggested_action=RecommendedAction.HUMAN_REVIEW)
    job = BackgroundJob.objects.create(
        task_type=STATUS_SYNC_TASK,
        idempotency_key="status-sync:unavailable",
        parameter_summary={"appid": "wx-test", "limit": 20},
    )

    class Source:
        def get_post_metrics(self, topic_id):
            assert topic_id == post.topic_id
            raise SourceTransportError("来源暂不可用")

        def close(self):
            pass

    with patch("posts.tasks.GameCirclePostClient", return_value=Source()):
        processed = execute_next_job({STATUS_SYNC_TASK: sync_pending_status_handler})

    assert processed is not None
    job.refresh_from_db()
    assert job.status == "PENDING"
    assert job.retry_count == 1
    assert job.checkpoint == {
        "attempted": 1,
        "refreshed": 0,
        "failed": 1,
        "limit": 20,
        "execution_scope": {"target_appid": "wx-test", "scope_version": 1},
    }


@pytest.mark.django_db
def test_status_sync_defers_for_active_session_recovery_without_consuming_retry_budget():
    post = OriginalPost.objects.create(
        topic_id=21, content_id="status-session-recovering", source_appid="wx-test", status=4
    )
    ReviewTask.objects.create(post=post, suggested_action=RecommendedAction.HUMAN_REVIEW)
    job = BackgroundJob.objects.create(
        task_type=STATUS_SYNC_TASK,
        idempotency_key="status-sync:session-recovering",
        max_retries=3,
        parameter_summary={"appid": "wx-test", "limit": 20},
    )

    class Source:
        def get_post_metrics(self, topic_id):
            assert topic_id == post.topic_id
            raise GameCircleIpcError("SESSION_RECOVERING", "游戏圈会话正在恢复，请稍后重试")

        def close(self):
            pass

    with patch("posts.tasks.GameCirclePostClient", return_value=Source()):
        execute_next_job({STATUS_SYNC_TASK: sync_pending_status_handler})

    job.refresh_from_db()
    assert job.status == "PENDING"
    assert job.retry_count == 0
    assert job.next_run_at is not None


@pytest.mark.django_db
def test_metrics_refresh_updates_only_interaction_snapshots_and_skips_deleted_posts():
    refreshed = OriginalPost.objects.create(
        topic_id=30,
        content_id="metrics-refresh",
        source_appid="wx-test",
        publish_time="2026-07-18T12:00:00+08:00",
        status=4,
        content_text="unchanged",
    )
    deleted = OriginalPost.objects.create(
        topic_id=31,
        content_id="metrics-refresh-deleted",
        source_appid="wx-test",
        publish_time="2026-07-18T13:00:00+08:00",
        status=2,
    )
    job = BackgroundJob.objects.create(
        task_type=METRICS_REFRESH_WINDOW_TASK,
        idempotency_key="metrics-refresh:worker",
        parameter_summary={
            "appid": "wx-test",
            "start_time": 1784342400,
            "end_time": 1784428800,
        },
    )

    class Source:
        def get_post_metrics(self, topic_id):
            if topic_id == deleted.topic_id:
                raise SourceBusinessDeletedError("deleted")
            return {
                "data": {
                    "topic": {
                        "view_count": 9,
                        "love_count": 2,
                        "reply_count": 3,
                        "share_count": 4,
                        "status": 8,
                    }
                }
            }

        def close(self):
            pass

    with patch("posts.tasks.GameCirclePostClient", return_value=Source()):
        refresh_metrics_window_handler(job)

    refreshed.refresh_from_db()
    deleted.refresh_from_db()
    job.refresh_from_db()
    snapshot = refreshed.interaction_snapshots.get()
    assert (
        snapshot.view_count,
        snapshot.love_count,
        snapshot.reply_count,
        snapshot.share_count,
    ) == (9, 2, 3, 4)
    assert refreshed.status == 4
    assert refreshed.content_text == "unchanged"
    assert deleted.status == 2
    assert job.checkpoint == {
        "last_topic_id": 31,
        "processed": 1,
        "skipped": 1,
        "complete": True,
        "execution_scope": {"target_appid": "wx-test", "scope_version": 1},
    }
    assert (
        BACKFILL_INGESTION_WORKER_HANDLERS[METRICS_REFRESH_WINDOW_TASK]
        is refresh_metrics_window_handler
    )


def test_ingestion_worker_lanes_are_explicit_and_keep_legacy_jobs_in_backfill():
    assert set(REALTIME_INGESTION_WORKER_HANDLERS) == {
        SCHEDULED_INGEST_WINDOW_TASK,
        STATUS_SYNC_TASK,
    }
    assert set(BACKFILL_INGESTION_WORKER_HANDLERS) == {
        LEGACY_INGEST_WINDOW_TASK,
        MANUAL_INGEST_WINDOW_TASK,
        METRICS_REFRESH_WINDOW_TASK,
        AUTHOR_MUTE_STATUS_BACKFILL_V2_TASK,
        POST_TOPIC_BACKFILL_V1_TASK,
        MANUAL_CURRENT_INGEST_WINDOW_TASK,
        FILTERED_POST_REFRESH_TASK,
        COMMENT_EXPORT_V1_TASK,
    }
    assert BACKFILL_INGESTION_WORKER_HANDLERS[LEGACY_INGEST_WINDOW_TASK] is ingest_window_handler


@pytest.mark.django_db
def test_metrics_refresh_resumes_from_its_last_topic_checkpoint():
    first = OriginalPost.objects.create(
        topic_id=40,
        content_id="metrics-resume-first",
        source_appid="wx-test",
        publish_time="2026-07-18T12:00:00+08:00",
    )
    second = OriginalPost.objects.create(
        topic_id=41,
        content_id="metrics-resume-second",
        source_appid="wx-test",
        publish_time="2026-07-18T13:00:00+08:00",
    )
    job = BackgroundJob.objects.create(
        task_type=METRICS_REFRESH_WINDOW_TASK,
        idempotency_key="metrics-refresh:resume",
        parameter_summary={
            "appid": "wx-test",
            "start_time": 1784342400,
            "end_time": 1784428800,
        },
        checkpoint={
            "last_topic_id": first.topic_id,
            "processed": 1,
            "skipped": 0,
            "complete": False,
        },
    )

    class Source:
        def get_post_metrics(self, topic_id):
            assert topic_id == second.topic_id
            return {"data": {"topic": {"view_count": 7}}}

        def close(self):
            pass

    with patch("posts.tasks.GameCirclePostClient", return_value=Source()):
        refresh_metrics_window_handler(job)

    job.refresh_from_db()
    assert first.interaction_snapshots.count() == 0
    assert second.interaction_snapshots.get().view_count == 7
    assert job.checkpoint == {
        "last_topic_id": 41,
        "processed": 2,
        "skipped": 0,
        "complete": True,
        "execution_scope": {"target_appid": "wx-test", "scope_version": 1},
    }


@pytest.mark.django_db
def test_metrics_refresh_records_one_post_failure_and_continues_the_range():
    failed_post = OriginalPost.objects.create(
        topic_id=45,
        content_id="metrics-failure",
        source_appid="wx-test",
        publish_time="2026-07-18T12:00:00+08:00",
    )
    refreshed_post = OriginalPost.objects.create(
        topic_id=46,
        content_id="metrics-after-failure",
        source_appid="wx-test",
        publish_time="2026-07-18T13:00:00+08:00",
    )
    job = BackgroundJob.objects.create(
        task_type=METRICS_REFRESH_WINDOW_TASK,
        idempotency_key="metrics-refresh:continue-after-failure",
        parameter_summary={
            "appid": "wx-test",
            "start_time": 1784342400,
            "end_time": 1784428800,
        },
    )

    class Source:
        def get_post_metrics(self, topic_id):
            if topic_id == failed_post.topic_id:
                raise SourceTransportError("上游暂时不可用")
            assert topic_id == refreshed_post.topic_id
            return {"data": {"topic": {"view_count": 8}}}

        def close(self):
            pass

    with patch("posts.tasks.GameCirclePostClient", return_value=Source()):
        refresh_metrics_window_handler(job)

    job.refresh_from_db()
    assert refreshed_post.interaction_snapshots.get().view_count == 8
    assert job.checkpoint == {
        "last_topic_id": refreshed_post.topic_id,
        "processed": 1,
        "skipped": 0,
        "failed": 1,
        "failure_samples": [{"topic_id": failed_post.topic_id, "error": "上游暂时不可用"}],
        "complete": True,
        "execution_scope": {"target_appid": "wx-test", "scope_version": 1},
    }


@pytest.mark.django_db
def test_metrics_refresh_excludes_forward_candidates_from_the_upstream_scope():
    original = OriginalPost.objects.create(
        topic_id=50,
        content_id="metrics-original",
        source_appid="wx-test",
        publish_time="2026-07-18T12:00:00+08:00",
    )
    CandidatePost.objects.create(
        topic_id=51,
        content_id="metrics-forward",
        source_appid="wx-test",
        origin_type=OriginType.FORWARD,
    )
    job = BackgroundJob.objects.create(
        task_type=METRICS_REFRESH_WINDOW_TASK,
        idempotency_key="metrics-refresh:original-only",
        parameter_summary={
            "appid": "wx-test",
            "start_time": 1784342400,
            "end_time": 1784428800,
        },
    )
    requested_topic_ids = []

    class Source:
        def get_post_metrics(self, topic_id):
            requested_topic_ids.append(topic_id)
            return {"data": {"topic": {"view_count": 1}}}

        def close(self):
            pass

    with patch("posts.tasks.GameCirclePostClient", return_value=Source()):
        refresh_metrics_window_handler(job)

    assert requested_topic_ids == [original.topic_id]
    assert original.interaction_snapshots.count() == 1
