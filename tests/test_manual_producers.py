from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from jobs.manual_producers import run_manual_producer
from jobs.models import BackgroundJob, JobStatus, ScheduledProducerSlot
from jobs.task_types import (
    GAME_CIRCLE_SESSION_KEEPALIVE_TASK,
    MANUAL_CURRENT_INGEST_WINDOW_TASK,
    MANUAL_INGEST_WINDOW_TASK,
    PERMANENT_MUTE_RENEWAL_TASK,
    STATUS_SYNC_TASK,
)
from posts.game_catalog import set_active_game
from posts.ingestion_jobs import enqueue_manual_historical_ingestion_window
from posts.models import ActiveGameScope, ManagedGame


def _activate(appid="manual-game"):
    game = ManagedGame.objects.create(appid=appid, name="Manual")
    ActiveGameScope.objects.create(pk=1, active_game=game)
    return game


@pytest.mark.django_db
def test_manual_current_ingestion_freezes_scope_and_never_creates_status_sync():
    game = _activate()
    result = run_manual_producer(producer_key="post_ingestion", values={})
    job = result.jobs[0]
    repeated = run_manual_producer(producer_key="post_ingestion", values={})

    assert job.task_type == MANUAL_CURRENT_INGEST_WINDOW_TASK
    assert job.checkpoint["execution_scope"] == {"target_appid": game.appid, "scope_version": 1}
    assert job.parameter_summary["page_size"] == 100
    assert repeated.jobs == (job,)
    assert repeated.created == (False,)
    assert job.active_coalescing_key
    assert not BackgroundJob.objects.filter(task_type=STATUS_SYNC_TASK).exists()
    assert not ScheduledProducerSlot.objects.exists()


@pytest.mark.django_db
def test_manual_weekly_range_normalizes_and_reuses_active_work():
    _activate()
    first = run_manual_producer(
        producer_key="metric_weekly", values={"start_date": "2026-07-08", "end_date": "2026-07-24"}
    )
    second = run_manual_producer(
        producer_key="metric_weekly", values={"start_date": "2026-07-06", "end_date": "2026-07-26"}
    )
    assert first.actual_range == (date(2026, 7, 6), date(2026, 7, 26))
    assert second.jobs[0].pk == first.jobs[0].pk
    assert second.created == (False,)


@pytest.mark.django_db
def test_manual_service_producers_reuse_their_active_scope_jobs():
    game = _activate()

    for producer_key, task_type in (
        ("session_keepalive", GAME_CIRCLE_SESSION_KEEPALIVE_TASK),
        ("permanent_mute_renewal", PERMANENT_MUTE_RENEWAL_TASK),
    ):
        first = run_manual_producer(producer_key=producer_key, values={})
        repeated = run_manual_producer(producer_key=producer_key, values={})

        assert first.jobs == repeated.jobs
        assert first.created == (True,)
        assert repeated.created == (False,)
        assert first.jobs[0].task_type == task_type
        assert first.jobs[0].status == JobStatus.PENDING
        assert first.jobs[0].checkpoint["execution_scope"] == {
            "target_appid": game.appid,
            "scope_version": 1,
        }
        assert first.jobs[0].active_coalescing_key

    assert not ScheduledProducerSlot.objects.exists()


@pytest.mark.django_db
def test_manual_historical_ingestion_uses_the_current_game_without_starting_a_worker():
    game = _activate()

    result = run_manual_producer(
        producer_key="post_ingestion", values={"mode": "history", "hours": "12"}
    )
    job = result.jobs[0]

    assert job.task_type == MANUAL_INGEST_WINDOW_TASK
    assert job.status == JobStatus.PENDING
    assert job.parameter_summary["appid"] == game.appid
    assert job.checkpoint["execution_scope"] == {"target_appid": game.appid, "scope_version": 1}
    assert job.parameter_summary["page_size"] == 100
    assert job.parameter_summary["end_time"] - job.parameter_summary["start_time"] == 12 * 60 * 60
    assert not ScheduledProducerSlot.objects.exists()
    assert not BackgroundJob.objects.filter(task_type=STATUS_SYNC_TASK).exists()


@pytest.mark.django_db
def test_manual_historical_ingestion_does_not_reuse_a_previous_scope_version():
    first = _activate("manual-game-first")
    second = ManagedGame.objects.create(appid="manual-game-second", name="Manual second")
    submitted_at = datetime(2026, 7, 28, 10, 12, 34, tzinfo=ZoneInfo("Asia/Shanghai"))

    initial = enqueue_manual_historical_ingestion_window(hours="12", now=submitted_at)
    set_active_game(appid=second.appid)
    set_active_game(appid=first.appid)
    after_scope_switch = enqueue_manual_historical_ingestion_window(hours="12", now=submitted_at)

    assert initial.created is True
    assert after_scope_switch.created is True
    assert after_scope_switch.job.pk != initial.job.pk
    assert initial.job.checkpoint["execution_scope"] == {
        "target_appid": first.appid,
        "scope_version": 1,
    }
    assert after_scope_switch.job.checkpoint["execution_scope"] == {
        "target_appid": first.appid,
        "scope_version": 3,
    }


@pytest.mark.django_db
def test_manual_historical_ingestion_rejects_invalid_hours_and_client_appid():
    _activate()

    with pytest.raises(ValueError, match="1 至 744 小时"):
        run_manual_producer(
            producer_key="post_ingestion", values={"mode": "history", "hours": "745"}
        )
    with pytest.raises(ValueError, match="不接受 AppID"):
        run_manual_producer(
            producer_key="post_ingestion",
            values={"mode": "history", "hours": "12", "appid": "other-game"},
        )

    assert not BackgroundJob.objects.exists()


@pytest.mark.django_db
def test_manual_range_rejects_future_and_unknown_producer():
    _activate()
    future = date.today() + timedelta(days=1)
    with pytest.raises(ValueError):
        run_manual_producer(
            producer_key="metric_daily",
            values={"start_date": future.isoformat(), "end_date": future.isoformat()},
        )
    with pytest.raises(ValueError):
        run_manual_producer(producer_key="unknown", values={})
