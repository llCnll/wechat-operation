from datetime import datetime
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from jobs.models import BackgroundJob
from posts.game_catalog import set_active_game
from posts.ingestion_jobs import (
    SHANGHAI,
    enqueue_ingestion_window,
    enqueue_scheduled_ingestion,
    enqueue_scheduled_jobs,
    enqueue_scheduled_status_sync,
    scheduled_ingestion_window,
)
from posts.models import ManagedGame


def _shanghai_time(hour: int, minute: int) -> datetime:
    return datetime(2026, 7, 19, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture(autouse=True)
def active_game_scope(request):
    if request.node.get_closest_marker("django_db") is None:
        return
    game, _ = ManagedGame.objects.get_or_create(
        appid="wx6d2454a8afcd9706",
        defaults={"name": "测试游戏"},
    )
    set_active_game(appid=game.appid)


def test_scheduled_window_floors_to_a_15_minute_boundary():
    window = scheduled_ingestion_window(_shanghai_time(10, 16))

    assert datetime.fromtimestamp(window.start_time, tz=SHANGHAI) == _shanghai_time(9, 45)
    assert datetime.fromtimestamp(window.end_time, tz=SHANGHAI) == _shanghai_time(10, 15)


def test_scheduled_windows_overlap_by_15_minutes():
    first = scheduled_ingestion_window(_shanghai_time(10, 16))
    second = scheduled_ingestion_window(_shanghai_time(10, 31))

    assert first.end_time - second.start_time == 15 * 60


@pytest.mark.django_db
def test_scheduled_enqueue_is_fixed_game_and_idempotent_in_one_time_bucket():
    now = _shanghai_time(10, 16)

    first = enqueue_scheduled_ingestion(now)
    second = enqueue_scheduled_ingestion(now)

    assert first.created is True
    assert second.created is False
    assert first.job.pk == second.job.pk
    assert first.job.parameter_summary == {
        "appid": "wx6d2454a8afcd9706",
        "start_time": int(_shanghai_time(9, 45).timestamp()),
        "end_time": int(_shanghai_time(10, 15).timestamp()),
        "page_size": 20,
    }
    assert BackgroundJob.objects.filter(task_type="SCHEDULED_INGEST_WINDOW").count() == 1


@pytest.mark.django_db
def test_scheduled_status_sync_is_fixed_game_and_idempotent_in_one_time_bucket():
    now = _shanghai_time(10, 16)

    first = enqueue_scheduled_status_sync(now)
    second = enqueue_scheduled_status_sync(now)

    assert first.created is True
    assert second.created is False
    assert first.job.parameter_summary == {"appid": "wx6d2454a8afcd9706", "limit": 20}
    assert BackgroundJob.objects.filter(task_type="STATUS_SYNC").count() == 1


@pytest.mark.django_db
def test_combined_scheduler_enqueue_keeps_status_sync_ahead_of_ingestion():
    scheduled = enqueue_scheduled_jobs(_shanghai_time(10, 16))

    assert scheduled.status_sync.created is True
    assert scheduled.ingestion.created is True
    assert scheduled.status_sync.job.pk < scheduled.ingestion.job.pk


@pytest.mark.django_db
def test_manual_enqueue_uses_shared_window_validation_and_reports_existing_job():
    now = _shanghai_time(10, 16)
    created_output = StringIO()
    existing_output = StringIO()
    with patch("jobs.management.commands.enqueue_ingestion.timezone.now", return_value=now):
        call_command(
            "enqueue_ingestion",
            "--appid",
            "wx-manual",
            "--hours",
            "2",
            "--page-size",
            "30",
            stdout=created_output,
        )
        call_command(
            "enqueue_ingestion",
            "--appid",
            "wx-manual",
            "--hours",
            "2",
            "--page-size",
            "30",
            stdout=existing_output,
        )

    job = BackgroundJob.objects.get()
    assert job.task_type == "MANUAL_INGEST_WINDOW"
    assert job.parameter_summary == {
        "appid": "wx-manual",
        "start_time": int(_shanghai_time(8, 16).timestamp()),
        "end_time": int(now.timestamp()),
        "page_size": 30,
    }
    assert "已创建" in created_output.getvalue()
    assert "已存在" in existing_output.getvalue()


@pytest.mark.django_db
def test_scheduler_command_reports_window_and_duplicate_result():
    now = _shanghai_time(10, 16)
    created_output = StringIO()
    existing_output = StringIO()
    with patch("jobs.management.commands.run_scheduler.timezone.now", return_value=now):
        call_command("run_scheduler", "--once", stdout=created_output)
        call_command("run_scheduler", "--once", stdout=existing_output)

    assert "post_ingestion:default=ENQUEUED" in created_output.getvalue()
    assert "post_ingestion:default=NOT_DUE" in existing_output.getvalue()
    assert (
        BackgroundJob.objects.get(task_type="STATUS_SYNC").pk
        < BackgroundJob.objects.get(task_type="SCHEDULED_INGEST_WINDOW").pk
    )


def test_scheduler_command_requires_exactly_one_run_mode():
    with pytest.raises(CommandError, match="--once 或 --loop"):
        call_command("run_scheduler")
    with pytest.raises(CommandError, match="--once 或 --loop"):
        call_command("run_scheduler", "--once", "--loop")


@pytest.mark.django_db
def test_scheduler_without_an_active_game_keeps_running_without_creating_source_jobs():
    from posts.models import ActiveGameScope

    ActiveGameScope.objects.all().delete()
    output = StringIO()

    call_command("run_scheduler", "--once", stdout=output)

    assert "post_ingestion:default=NO_ACTIVE_GAME" in output.getvalue()
    assert not BackgroundJob.objects.exists()


@pytest.mark.django_db
def test_scheduler_loop_runs_immediately_then_waits_for_next_check():
    now = _shanghai_time(10, 16)
    with (
        patch("jobs.management.commands.run_scheduler.timezone.now", return_value=now),
        patch(
            "jobs.management.commands.run_scheduler.time.sleep",
            side_effect=KeyboardInterrupt,
        ) as sleep,
        pytest.raises(KeyboardInterrupt),
    ):
        call_command("run_scheduler", "--loop", "--poll-seconds", "12")

    assert BackgroundJob.objects.filter(task_type="STATUS_SYNC").count() == 1
    assert BackgroundJob.objects.filter(task_type="SCHEDULED_INGEST_WINDOW").count() == 1
    sleep.assert_called_once_with(12)


@pytest.mark.django_db
def test_shared_ingestion_window_rejects_invalid_parameters():
    with pytest.raises(ValueError, match="appid不能为空"):
        enqueue_ingestion_window(appid="", start_time=1, end_time=2, page_size=20)
    with pytest.raises(ValueError, match="采集时间窗口无效"):
        enqueue_ingestion_window(appid="wx-test", start_time=2, end_time=1, page_size=20)
    with pytest.raises(ValueError, match="page-size必须在1至100之间"):
        enqueue_ingestion_window(appid="wx-test", start_time=1, end_time=2, page_size=101)
