from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from jobs.adapters import SourceBusinessDeletedError, SourceTransportError
from jobs.models import BackgroundJob, JobStatus
from jobs.once.author_mute_status_backfill import (
    AUTHOR_MUTE_STATUS_BACKFILL_IDEMPOTENCY_KEY,
    AUTHOR_MUTE_STATUS_BACKFILL_TASK,
    author_mute_status_backfill_handler,
    enqueue_author_mute_status_backfill,
)
from posts.game_catalog import DEFAULT_GAME_APPID
from posts.models import AuthorMuteStatus, InteractionSnapshot, OriginalPost
from posts.tasks import BACKFILL_INGESTION_WORKER_HANDLERS, REALTIME_INGESTION_WORKER_HANDLERS


def _detail(*, appid: str, openid: str, nick_name: str, local_end: int = 0, global_end: int = 0):
    return {
        "data": {
            "topic": {
                "appid": appid,
                "creater": {
                    "openid": openid,
                    "nick_name": nick_name,
                    "block_info": {"appid": appid, "block_end_time": local_end},
                    "global_block_end_time": global_end,
                },
            }
        }
    }


@pytest.mark.django_db
def test_one_time_backfill_is_idempotently_enqueued_and_command_only_enqueues():
    first = enqueue_author_mute_status_backfill()
    second = enqueue_author_mute_status_backfill()
    call_command("enqueue_author_mute_status_backfill")

    assert first.pk == second.pk
    assert first.idempotency_key == AUTHOR_MUTE_STATUS_BACKFILL_IDEMPOTENCY_KEY
    assert first.task_type == AUTHOR_MUTE_STATUS_BACKFILL_TASK
    assert first.max_retries == 0
    assert BackgroundJob.objects.filter(task_type=AUTHOR_MUTE_STATUS_BACKFILL_TASK).count() == 1


@pytest.mark.django_db
def test_one_time_backfill_manually_requeues_only_a_failed_job_from_its_checkpoint():
    job = enqueue_author_mute_status_backfill()
    job.status = JobStatus.FAILED
    job.retry_count = 1
    job.checkpoint = {"last_topic_id": 800, "processed": 3, "skipped": 1, "complete": False}
    job.save()

    resumed = enqueue_author_mute_status_backfill()

    assert resumed.pk == job.pk
    assert resumed.status == JobStatus.PENDING
    assert resumed.retry_count == 0
    assert resumed.checkpoint == {
        "last_topic_id": 800,
        "processed": 3,
        "skipped": 1,
        "complete": False,
    }


@pytest.mark.django_db
def test_one_time_backfill_uses_post_game_or_default_and_has_no_post_side_effects():
    historic = OriginalPost.objects.create(
        topic_id=801, content_id="historic-default", source_appid="", status=4, content_text="原文"
    )
    other_game = OriginalPost.objects.create(
        topic_id=802, content_id="historic-other", source_appid="game-other", status=8
    )
    job = enqueue_author_mute_status_backfill()
    future = int((timezone.now() + timedelta(days=1)).timestamp())
    created_appids: list[str] = []

    class Source:
        def __init__(self, appid):
            self.appid = appid
            created_appids.append(appid)

        def get_post_metrics(self, topic_id):
            if topic_id == historic.topic_id:
                assert self.appid == DEFAULT_GAME_APPID
                return _detail(
                    appid=DEFAULT_GAME_APPID,
                    openid="default-author",
                    nick_name="默认游戏作者",
                    local_end=future,
                )
            assert self.appid == "game-other"
            return _detail(
                appid="game-other",
                openid="other-author",
                nick_name="其他游戏作者",
                global_end=future,
            )

        def close(self):
            pass

    with patch("jobs.once.author_mute_status_backfill.CdpGameCircleClient", Source):
        author_mute_status_backfill_handler(job)

    historic.refresh_from_db()
    other_game.refresh_from_db()
    job.refresh_from_db()
    assert created_appids == [DEFAULT_GAME_APPID, "game-other"]
    assert historic.author_openid == "default-author"
    assert other_game.author_openid == "other-author"
    assert historic.status == 4
    assert historic.content_text == "原文"
    assert InteractionSnapshot.objects.count() == 0
    assert AuthorMuteStatus.objects.get(openid="default-author").source_appid == DEFAULT_GAME_APPID
    assert AuthorMuteStatus.objects.get(openid="other-author").global_muted_until is not None
    assert job.checkpoint == {"last_topic_id": 802, "processed": 2, "skipped": 0, "complete": True}


@pytest.mark.django_db
def test_one_time_backfill_skips_failures_and_advances_checkpoint_without_changing_post_state():
    deleted = OriginalPost.objects.create(
        topic_id=811, content_id="backfill-deleted", source_appid="game-a", status=4
    )
    failed = OriginalPost.objects.create(
        topic_id=812, content_id="backfill-failed", source_appid="game-a", status=8
    )
    OriginalPost.objects.create(
        topic_id=813, content_id="backfill-synced", source_appid="game-a", status=2
    )
    job = enqueue_author_mute_status_backfill()

    class Source:
        def __init__(self, appid):
            assert appid == "game-a"

        def get_post_metrics(self, topic_id):
            if topic_id == deleted.topic_id:
                raise SourceBusinessDeletedError("已删除")
            if topic_id == failed.topic_id:
                raise SourceTransportError("连接失败")
            return _detail(appid="game-a", openid="synced-author", nick_name="已同步")

        def close(self):
            pass

    with patch("jobs.once.author_mute_status_backfill.CdpGameCircleClient", Source):
        author_mute_status_backfill_handler(job)

    deleted.refresh_from_db()
    failed.refresh_from_db()
    job.refresh_from_db()
    assert deleted.status == 4
    assert failed.status == 8
    assert AuthorMuteStatus.objects.filter(openid="synced-author").exists()
    assert job.checkpoint == {
        "last_topic_id": 813,
        "processed": 1,
        "skipped": 1,
        "complete": True,
        "failed": 1,
        "failure_samples": [{"topic_id": 812, "error": "连接失败"}],
    }


def test_one_time_backfill_is_owned_by_the_backfill_worker_only():
    assert BACKFILL_INGESTION_WORKER_HANDLERS[AUTHOR_MUTE_STATUS_BACKFILL_TASK] is (
        author_mute_status_backfill_handler
    )
    assert AUTHOR_MUTE_STATUS_BACKFILL_TASK not in REALTIME_INGESTION_WORKER_HANDLERS
