from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command

from config.game_circle_service import BootstrapResult, GameCircleSessionService
from jobs.game_circle_keepalive import enqueue_keepalive, keepalive_bucket
from jobs.models import BackgroundJob, JobStatus
from jobs.services import JobTerminalError, execute_next_job
from jobs.task_types import GAME_CIRCLE_SESSION_KEEPALIVE_TASK


@pytest.mark.django_db
def test_scheduled_keepalive_has_a_timezone_bucket_without_scope_snapshot():
    now = datetime(2026, 7, 23, 5, 42, tzinfo=ZoneInfo("Asia/Shanghai"))

    first = enqueue_keepalive(trigger="schedule", now=now)
    second = enqueue_keepalive(trigger="schedule", now=now)

    assert first.created is True
    assert second.created is False
    assert first.job.task_type == GAME_CIRCLE_SESSION_KEEPALIVE_TASK
    assert first.job.parameter_summary == {
        "trigger": "schedule",
        "automation_key": "session_keepalive",
        "bucket": "20260723T04+0800",
    }
    assert first.job.checkpoint == {}


@pytest.mark.django_db
def test_manual_keepalive_is_not_gated_by_the_automation_switch():
    job = enqueue_keepalive(trigger="manual").job

    assert job.status == JobStatus.PENDING
    assert job.parameter_summary == {"trigger": "manual"}


def test_keepalive_bucket_is_stable_across_minutes_in_one_four_hour_window():
    zone = ZoneInfo("Asia/Shanghai")
    assert keepalive_bucket(datetime(2026, 7, 23, 7, 59, tzinfo=zone)) == "20260723T04+0800"
    assert keepalive_bucket(datetime(2026, 7, 23, 8, 0, tzinfo=zone)) == "20260723T08+0800"


def test_keepalive_bucket_requires_an_explicit_timezone():
    with pytest.raises(ValueError, match="包含时区"):
        keepalive_bucket(datetime(2026, 7, 23, 8, 0))


@pytest.mark.django_db
def test_keepalive_without_current_scope_is_a_terminal_non_browser_result():
    job = enqueue_keepalive(trigger="manual").job

    with pytest.raises(JobTerminalError, match="NO_ACTIVE_GAME_SCOPE"):
        GameCircleSessionService().keepalive(job)

    assert job.checkpoint == {}


@pytest.mark.django_db
def test_keepalive_candidate_failure_uses_the_job_retry_policy(monkeypatch):
    job = enqueue_keepalive(trigger="manual").job
    scope = object()
    service = GameCircleSessionService()
    monkeypatch.setattr("config.game_circle_service.resolve_execution_scope", lambda _job: scope)
    monkeypatch.setattr(
        service,
        "bootstrap",
        lambda **_kwargs: BootstrapResult(False, "TARGET_UNAVAILABLE"),
    )

    processed = execute_next_job({GAME_CIRCLE_SESSION_KEEPALIVE_TASK: service.keepalive})

    assert processed.pk == job.pk
    job.refresh_from_db()
    assert job.status == JobStatus.PENDING
    assert job.retry_count == 1
    assert job.error_summary == "TARGET_UNAVAILABLE"


@pytest.mark.django_db
def test_keepalive_scan_required_is_terminal_not_success(monkeypatch):
    job = enqueue_keepalive(trigger="manual").job
    scope = object()
    service = GameCircleSessionService()
    monkeypatch.setattr("config.game_circle_service.resolve_execution_scope", lambda _job: scope)
    monkeypatch.setattr(
        service,
        "bootstrap",
        lambda **_kwargs: BootstrapResult(False, "SCAN_REQUIRED", terminal=True),
    )

    execute_next_job({GAME_CIRCLE_SESSION_KEEPALIVE_TASK: service.keepalive})

    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.retry_count == 0
    assert job.error_summary == "SCAN_REQUIRED"
