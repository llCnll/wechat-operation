from contextlib import contextmanager

import pytest
from django.db import OperationalError
from django.utils import timezone

from jobs import worker
from jobs.adapters import SourceContractError
from jobs.models import BackgroundJob, JobStatus
from jobs.task_types import task_type_label
from jobs.services import (
    cancel_job,
    claim_next_job,
    complete_job,
    enqueue_job,
    execute_next_job,
    fail_job,
    recover_interrupted_jobs,
)
from jobs.worker import consume_jobs


@pytest.mark.django_db
def test_claim_due_job_and_schedule_retry():
    job = BackgroundJob.objects.create(task_type="INGEST", idempotency_key="ingest:2026-07-18")
    claimed = claim_next_job()
    assert claimed is not None
    assert claimed.pk == job.pk
    assert claimed.status == JobStatus.RUNNING

    fail_job(claimed, "upstream timeout")
    claimed.refresh_from_db()
    assert claimed.status == JobStatus.PENDING
    assert claimed.next_run_at > timezone.now()


@pytest.mark.django_db
def test_specialized_worker_only_claims_its_own_task_types():
    review = BackgroundJob.objects.create(task_type="AI_EVALUATE", idempotency_key="review:1")
    ingestion = BackgroundJob.objects.create(task_type="INGEST_WINDOW", idempotency_key="ingest:1")

    claimed = claim_next_job(task_types={"INGEST_WINDOW"})

    assert claimed is not None
    assert claimed.pk == ingestion.pk
    review.refresh_from_db()
    assert review.status == JobStatus.PENDING


def test_task_type_labels_are_derived_from_the_shared_mapping():
    assert task_type_label("AI_EVALUATE") == "AI 内容评估"
    assert task_type_label("FUTURE_JOB") == "FUTURE_JOB"


@pytest.mark.django_db
def test_realtime_worker_claims_scheduled_work_without_waiting_for_older_manual_work():
    manual = BackgroundJob.objects.create(
        task_type="MANUAL_INGEST_WINDOW", idempotency_key="manual:older"
    )
    scheduled = BackgroundJob.objects.create(
        task_type="SCHEDULED_INGEST_WINDOW", idempotency_key="scheduled:newer"
    )

    claimed = claim_next_job(task_types={"SCHEDULED_INGEST_WINDOW", "STATUS_SYNC"})

    assert claimed is not None
    assert claimed.pk == scheduled.pk
    manual.refresh_from_db()
    assert manual.status == JobStatus.PENDING


@pytest.mark.django_db
def test_enqueue_is_idempotent_and_handler_completes_job():
    first = enqueue_job(
        task_type="REPORT", idempotency_key="report:2026-07-18", parameter_summary={}
    )
    second = enqueue_job(
        task_type="REPORT", idempotency_key="report:2026-07-18", parameter_summary={}
    )
    assert first.pk == second.pk

    job = execute_next_job({"REPORT": complete_job})
    assert job is not None
    job.refresh_from_db()
    assert job.status == JobStatus.SUCCEEDED


@pytest.mark.django_db
def test_unknown_handler_is_rescheduled_with_safe_error():
    BackgroundJob.objects.create(task_type="UNKNOWN", idempotency_key="unknown:1")
    job = execute_next_job({})
    assert job is not None
    job.refresh_from_db()
    assert job.status == JobStatus.PENDING
    assert "未注册" in job.error_summary


@pytest.mark.django_db
def test_retry_exhaustion_marks_job_failed():
    job = BackgroundJob.objects.create(
        task_type="INGEST", idempotency_key="retry:1", max_retries=0, status=JobStatus.RUNNING
    )
    fail_job(job, "contract changed")
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.finished_at is not None


@pytest.mark.django_db
def test_operator_can_cancel_job_without_marking_it_failed():
    job = BackgroundJob.objects.create(task_type="INGEST", idempotency_key="cancel:1")
    cancel_job(job, "数据已满足")
    job.refresh_from_db()
    assert job.status == JobStatus.CANCELED
    assert "数据已满足" in job.error_summary


@pytest.mark.django_db
def test_interrupted_job_is_recovered_for_checkpoint_resume():
    job = BackgroundJob.objects.create(
        task_type="INGEST_WINDOW", idempotency_key="recovery:1", status=JobStatus.RUNNING
    )
    assert recover_interrupted_jobs() == 1
    job.refresh_from_db()
    assert job.status == JobStatus.PENDING
    assert "检查点" in job.error_summary


def test_contract_errors_are_typed_and_safe():
    error = SourceContractError("missing content_card")
    assert isinstance(error, Exception)
    assert "content_card" in str(error)


def test_worker_exits_cleanly_when_database_is_temporarily_locked(monkeypatch):
    class Output:
        def __init__(self):
            self.messages = []

        def write(self, message):
            self.messages.append(message)

    class Command:
        stdout = Output()
        stderr = Output()

    monkeypatch.setattr(
        "jobs.worker.recover_interrupted_jobs",
        lambda **_kwargs: (_ for _ in ()).throw(OperationalError("database is locked")),
    )

    consume_jobs(Command(), {}, loop=False, poll_seconds=0.1)

    assert "数据库暂时繁忙" in Command.stderr.messages[0]


def test_worker_does_not_recover_or_execute_when_the_same_worker_is_already_running(monkeypatch):
    class Output:
        def __init__(self):
            self.messages = []

        def write(self, message):
            self.messages.append(message)

    class Command:
        stdout = Output()
        stderr = Output()

    @contextmanager
    def unavailable_lock(_task_types):
        yield False

    monkeypatch.setattr("jobs.worker.worker_lock", unavailable_lock)
    monkeypatch.setattr(
        "jobs.worker.recover_interrupted_jobs",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("不应恢复其他实例的任务")),
    )

    consume_jobs(Command(), {"INGEST_WINDOW": complete_job}, loop=False, poll_seconds=0.1)

    assert "同类 Worker 已在运行" in Command.stderr.messages[0]


def test_worker_does_not_claim_another_job_after_lease_revocation(monkeypatch):
    class Output:
        def write(self, _message):
            return None

    class Command:
        stdout = Output()
        stderr = Output()

    class Job:
        pk = 1
        task_type = "INGEST_WINDOW"
        status = "SUCCEEDED"

    claims = []
    monkeypatch.setattr("jobs.worker.write_worker_heartbeat", lambda: len(claims) == 0)
    monkeypatch.setattr("jobs.worker.recover_interrupted_jobs", lambda **_kwargs: 0)

    def claim(*_args, **_kwargs):
        claims.append(True)
        return Job()

    monkeypatch.setattr("jobs.worker.execute_next_job", claim)

    consume_jobs(Command(), {"INGEST_WINDOW": lambda _job: None}, loop=True, poll_seconds=0)

    assert claims == [True]


def test_worker_lock_uses_windows_byte_range_lock_when_fcntl_is_unavailable(monkeypatch):
    calls = []

    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(_fd, mode, length):
            calls.append((mode, length))

    monkeypatch.setattr(worker, "fcntl", None)
    monkeypatch.setattr(worker, "msvcrt", FakeMsvcrt)

    with worker.worker_lock(("WINDOWS_TEST",)) as acquired:
        assert acquired is True

    assert calls == [(FakeMsvcrt.LK_NBLCK, 1), (FakeMsvcrt.LK_UNLCK, 1)]
