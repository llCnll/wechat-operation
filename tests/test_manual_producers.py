from datetime import date, timedelta

import pytest

from jobs.manual_producers import run_manual_producer
from jobs.models import BackgroundJob, ScheduledProducerSlot
from jobs.task_types import MANUAL_CURRENT_INGEST_WINDOW_TASK, STATUS_SYNC_TASK
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
    assert job.parameter_summary["page_size"] == 20
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
