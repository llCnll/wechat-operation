from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from jobs.adapters import SourceBusinessDeletedError, SourceTransportError
from jobs.models import BackgroundJob, JobStatus
from jobs.once.author_mute_status_backfill import (
    AUTHOR_MUTE_STATUS_BACKFILL_V2_IDEMPOTENCY_KEY,
    LEGACY_AUTHOR_MUTE_STATUS_BACKFILL_TASK,
    author_mute_status_backfill_handler,
    enqueue_author_mute_status_backfill,
)
from jobs.task_types import AUTHOR_MUTE_STATUS_BACKFILL_V2_TASK
from posts.game_catalog import set_active_game
from posts.models import AuthorMuteStatus, ManagedGame, OriginalPost
from posts.tasks import BACKFILL_INGESTION_WORKER_HANDLERS, REALTIME_INGESTION_WORKER_HANDLERS


def _user_detail(*, appid, openid, nick_name, local_end=0, global_end=0):
    return {
        "data": {
            "user": {
                "openid": openid,
                "nick_name": nick_name,
                "block_info": {"appid": appid, "block_end_time": local_end},
                "global_block_end_time": global_end,
            }
        }
    }


def _topic_detail(*, appid, openid, nick_name):
    return {
        "data": {
            "topic": {
                "appid": appid,
                "creater": {"openid": openid, "nick_name": nick_name},
            }
        }
    }


@pytest.mark.django_db
def test_v2_backfill_is_idempotent_and_refuses_a_nonterminal_v1_job():
    first = enqueue_author_mute_status_backfill()
    second = enqueue_author_mute_status_backfill()

    assert first.pk == second.pk
    assert first.idempotency_key == AUTHOR_MUTE_STATUS_BACKFILL_V2_IDEMPOTENCY_KEY
    assert first.task_type == AUTHOR_MUTE_STATUS_BACKFILL_V2_TASK
    assert first.max_retries == 0

    BackgroundJob.objects.create(
        task_type=LEGACY_AUTHOR_MUTE_STATUS_BACKFILL_TASK,
        idempotency_key="legacy-author-backfill",
    )
    with pytest.raises(ValueError, match="旧版"):
        enqueue_author_mute_status_backfill()


@pytest.mark.django_db
def test_v2_backfill_manually_resumes_a_failed_job_without_revealing_identity_in_checkpoint():
    job = enqueue_author_mute_status_backfill()
    job.status = JobStatus.FAILED
    job.retry_count = 1
    job.checkpoint = {
        "last_post_id": 12,
        "processed": 4,
        "skipped": 1,
        "missing_author_identity": 1,
        "identity_consistency_verified": 1,
        "failure_categories": {"GameCircleIpcError": 1},
        "complete": False,
    }
    job.save()

    resumed = enqueue_author_mute_status_backfill()

    assert resumed.pk == job.pk
    assert resumed.status == JobStatus.PENDING
    assert resumed.retry_count == 0
    assert resumed.checkpoint == job.checkpoint


@pytest.mark.django_db
def test_v2_backfill_reads_known_author_once_and_updates_only_its_game():
    game = ManagedGame.objects.create(appid="game-active", name="当前游戏")
    set_active_game(appid=game.appid)
    first = OriginalPost.objects.create(
        topic_id=801, content_id="known-first", source_appid=game.appid, author_openid="author-a"
    )
    second = OriginalPost.objects.create(
        topic_id=802, content_id="known-second", source_appid=game.appid, author_openid="author-a"
    )
    other_game = OriginalPost.objects.create(
        topic_id=803, content_id="known-other", source_appid="other", author_openid="author-a"
    )
    job = enqueue_author_mute_status_backfill()
    source = Mock()
    source.get_author_detail.return_value = _user_detail(
        appid=game.appid, openid="author-a", nick_name="当前作者",
        local_end=int((timezone.now() + timedelta(days=1)).timestamp()),
    )

    with patch("jobs.once.author_mute_status_backfill.GameCirclePostClient", return_value=source):
        author_mute_status_backfill_handler(job)

    first.refresh_from_db()
    second.refresh_from_db()
    other_game.refresh_from_db()
    assert source.get_author_detail.call_args_list == [(("author-a",), {})]
    assert source.get_post_metrics.call_count == 0
    assert first.author_nick_name == second.author_nick_name == "当前作者"
    assert other_game.author_nick_name == ""
    assert AuthorMuteStatus.objects.get(openid="author-a", source_appid=game.appid)


@pytest.mark.django_db
def test_v2_backfill_discovers_missing_author_then_verifies_with_user_detail():
    game = ManagedGame.objects.create(appid="game-a", name="当前游戏")
    set_active_game(appid=game.appid)
    post = OriginalPost.objects.create(topic_id=811, content_id="missing", source_appid=game.appid)
    job = enqueue_author_mute_status_backfill()
    source = Mock()
    source.get_post_metrics.return_value = _topic_detail(
        appid=game.appid, openid="discovered-author", nick_name="帖子昵称"
    )
    source.get_author_detail.return_value = _user_detail(
        appid=game.appid, openid="discovered-author", nick_name="作者昵称"
    )

    with patch("jobs.once.author_mute_status_backfill.GameCirclePostClient", return_value=source):
        author_mute_status_backfill_handler(job)

    post.refresh_from_db()
    job.refresh_from_db()
    assert post.author_openid == "discovered-author"
    assert post.author_nick_name == "作者昵称"
    assert job.checkpoint["identity_consistency_verified"] == 1
    assert "discovered-author" not in str(job.checkpoint)
    assert "作者昵称" not in str(job.checkpoint)


@pytest.mark.django_db
def test_v2_backfill_skips_deleted_post_without_identity_and_advances_checkpoint():
    game = ManagedGame.objects.create(appid="game-a", name="当前游戏")
    set_active_game(appid=game.appid)
    post = OriginalPost.objects.create(topic_id=821, content_id="deleted", source_appid=game.appid)
    job = enqueue_author_mute_status_backfill()
    source = Mock()
    source.get_post_metrics.side_effect = SourceBusinessDeletedError("deleted")

    with patch("jobs.once.author_mute_status_backfill.GameCirclePostClient", return_value=source):
        author_mute_status_backfill_handler(job)

    job.refresh_from_db()
    assert job.checkpoint["last_post_id"] == post.pk
    assert job.checkpoint["missing_author_identity"] == 1
    assert job.checkpoint["complete"] is True


def test_v2_backfill_is_owned_only_by_the_backfill_worker():
    assert BACKFILL_INGESTION_WORKER_HANDLERS[AUTHOR_MUTE_STATUS_BACKFILL_V2_TASK] is author_mute_status_backfill_handler
    assert LEGACY_AUTHOR_MUTE_STATUS_BACKFILL_TASK not in BACKFILL_INGESTION_WORKER_HANDLERS
    assert AUTHOR_MUTE_STATUS_BACKFILL_V2_TASK not in REALTIME_INGESTION_WORKER_HANDLERS
