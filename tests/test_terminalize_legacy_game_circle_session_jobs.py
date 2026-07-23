from contextlib import contextmanager

import pytest
from django.core.management import call_command

from jobs.models import BackgroundJob, JobStatus


@pytest.mark.django_db
def test_release_terminalizer_preserves_legacy_job_parameters_and_checkpoint(monkeypatch):
    job = BackgroundJob.objects.create(
        task_type="GAME_CIRCLE_LOGIN_PROBE",
        idempotency_key="legacy-session-job",
        parameter_summary={"source": "historical"},
        checkpoint={"keep": "audit"},
    )

    @contextmanager
    def unlocked(_task_types):
        yield True

    monkeypatch.setattr(
        "jobs.management.commands.terminalize_legacy_game_circle_session_jobs.worker_lock", unlocked
    )
    call_command("terminalize_legacy_game_circle_session_jobs")
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.error_summary == "RELEASED_LEGACY_SESSION_JOB"
    assert job.parameter_summary == {"source": "historical"}
    assert job.checkpoint == {"keep": "audit"}

    call_command("terminalize_legacy_game_circle_session_jobs")
    job.refresh_from_db()
    assert job.error_summary == "RELEASED_LEGACY_SESSION_JOB"
