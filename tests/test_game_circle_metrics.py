from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from config.cdp import CdpUnavailableError
from jobs.adapters import SourceContractError
from jobs.models import BackgroundJob, JobStatus
from jobs.services import execute_next_job
from reporting.metric_jobs import (
    DAILY_METRIC_SOURCES,
    METRIC_BACKFILL_TASK,
    METRIC_DAILY_SYNC_TASK,
    METRIC_WEEKLY_SYNC_TASK,
    enqueue_due_metric_jobs,
    enqueue_metric_backfill,
)
from reporting.metric_sources import MetricPage
from reporting.metric_sync import (
    MetricBusinessRetryableError,
    MetricRecord,
    metric_page_has_more,
    parse_source_records,
    provider_metric_page,
    upsert_metric_snapshot,
)
from reporting.metric_tasks import METRIC_WORKER_HANDLERS
from reporting.models import GameCircleMetricSnapshot, MetricPublicationState, MetricSource
from reporting.source_client import CdpGameCircleMetricClient

APPID = "wx6d2454a8afcd9706"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _page(*records, total_count=None):
    data = {"list": list(records)}
    if total_count is not None:
        data["total_count"] = total_count
    return {"errcode": 0, "data": data}


@pytest.mark.parametrize(
    ("source", "record", "expected"),
    (
        (
            MetricSource.GROUP_CONSUME_DAILY,
            {"ds": "2026-07-19", "daily_active_user": 12, "content_consume_cnt": 30},
            {"daily_active_user": 12, "content_consume_cnt": 30},
        ),
        (
            MetricSource.GROUP_PUBLISH_DAILY,
            {"ds": "2026-07-19", "publish_pv": 8, "publish_uv": 5},
            {"publish_pv": 8, "publish_uv": 5},
        ),
        (
            MetricSource.GROUP_SHARE_DAILY,
            {"ds": "2026-07-19", "share_cnt": 6, "share_uv": 4},
            {"share_cnt": 6, "share_uv": 4},
        ),
        (
            MetricSource.GROUP_RETAIN_DAILY,
            {"ds": "2026-07-19", "quanzi_dau": 11},
            {"quanzi_dau": 11},
        ),
    ),
)
def test_content_analysis_sources_project_their_own_required_fields(source, record, expected):
    records = parse_source_records(_page(record), source=source)

    assert records == [MetricRecord(date(2026, 7, 19), date(2026, 7, 19), expected)]


def test_content_analysis_rejects_missing_required_fields_and_sorts_days():
    with pytest.raises(SourceContractError):
        parse_source_records(
            _page({"ds": "2026-07-19", "daily_active_user": 12}),
            source=MetricSource.GROUP_CONSUME_DAILY,
        )

    records = parse_source_records(
        _page(
            {"ds": "2026-07-19", "daily_active_user": 12, "content_consume_cnt": 30},
            {"ds": "2026-07-17", "daily_active_user": 8, "content_consume_cnt": 20},
        ),
        source=MetricSource.GROUP_CONSUME_DAILY,
    )
    assert [record.period_start for record in records] == [date(2026, 7, 17), date(2026, 7, 19)]


def test_content_analysis_uses_a_short_page_when_total_count_is_an_unreliable_zero():
    provider_page = provider_metric_page(
        _page(
            {"ds": "2026-07-18", "share_cnt": 1, "share_uv": 1},
            {"ds": "2026-07-19", "share_cnt": 2, "share_uv": 2},
            {"ds": "2026-07-20", "share_cnt": 3, "share_uv": 3},
            total_count=0,
        )
    )

    assert metric_page_has_more(provider_page, page=0, page_size=366) is False


@pytest.mark.django_db
def test_page_checkpoint_resumes_next_page_without_creating_missing_days(monkeypatch):
    class Source:
        def __init__(self, **_kwargs):
            pass

        def close(self):
            return None

    calls: list[int] = []
    page_one_attempts = 0

    def read_page(_client, *, page, **_kwargs):
        nonlocal page_one_attempts
        calls.append(page)
        if page == 0:
            return MetricPage(
                records=(
                    MetricRecord(
                        date(2026, 7, 17),
                        date(2026, 7, 17),
                        {"daily_active_user": 8, "content_consume_cnt": 20},
                    ),
                ),
                has_more=True,
            )
        page_one_attempts += 1
        if page_one_attempts == 1:
            raise CdpUnavailableError("offline")
        return MetricPage(
            records=(
                MetricRecord(
                    date(2026, 7, 19),
                    date(2026, 7, 19),
                    {"daily_active_user": 12, "content_consume_cnt": 30},
                ),
            ),
            has_more=False,
        )

    monkeypatch.setattr("reporting.metric_tasks.CdpGameCircleMetricClient", Source)
    monkeypatch.setattr("reporting.metric_tasks.read_metric_page", read_page)
    job = BackgroundJob.objects.create(
        task_type=METRIC_BACKFILL_TASK,
        idempotency_key="metric-page-resume",
        max_retries=3,
        parameter_summary={
            "appid": APPID,
            "source": MetricSource.GROUP_CONSUME_DAILY,
            "start_ds": 20260717,
            "end_ds": 20260719,
        },
    )

    execute_next_job(METRIC_WORKER_HANDLERS, task_types=METRIC_WORKER_HANDLERS)
    job.refresh_from_db()
    assert job.status == JobStatus.PENDING
    assert job.retry_count == 0
    assert job.checkpoint["next_page"] == 1

    job.next_run_at = timezone.now()
    job.save(update_fields=["next_run_at"])
    execute_next_job(METRIC_WORKER_HANDLERS, task_types=METRIC_WORKER_HANDLERS)
    job.refresh_from_db()

    assert job.status == JobStatus.SUCCEEDED
    assert calls == [0, 1, 1]
    assert set(GameCircleMetricSnapshot.objects.values_list("period_start", flat=True)) == {
        date(2026, 7, 17),
        date(2026, 7, 19),
    }


@pytest.mark.django_db
def test_metric_error_classification_defers_retries_or_fails_terminally(monkeypatch):
    class Source:
        def __init__(self, **_kwargs):
            pass

        def close(self):
            return None

    monkeypatch.setattr("reporting.metric_tasks.CdpGameCircleMetricClient", Source)
    job = BackgroundJob.objects.create(
        task_type=METRIC_DAILY_SYNC_TASK,
        idempotency_key="metric-error-classification",
        max_retries=3,
        parameter_summary={
            "appid": APPID,
            "source": MetricSource.GROUP_CONSUME_DAILY,
            "start_ds": 20260719,
            "end_ds": 20260719,
        },
    )

    monkeypatch.setattr(
        "reporting.metric_tasks.read_metric_page",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(MetricBusinessRetryableError("busy")),
    )
    execute_next_job(METRIC_WORKER_HANDLERS, task_types=METRIC_WORKER_HANDLERS)
    job.refresh_from_db()
    assert job.status == JobStatus.PENDING
    assert job.retry_count == 1

    job.next_run_at = timezone.now()
    job.save(update_fields=["next_run_at"])
    monkeypatch.setattr(
        "reporting.metric_tasks.read_metric_page",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SourceContractError("schema")),
    )
    execute_next_job(METRIC_WORKER_HANDLERS, task_types=METRIC_WORKER_HANDLERS)
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.retry_count == 1


@pytest.mark.django_db
def test_login_loss_defers_without_consuming_retry_budget(monkeypatch):
    monkeypatch.setattr(
        "reporting.metric_tasks.CdpGameCircleMetricClient",
        lambda **_kwargs: (_ for _ in ()).throw(CdpUnavailableError("offline")),
    )
    job = BackgroundJob.objects.create(
        task_type=METRIC_DAILY_SYNC_TASK,
        idempotency_key="metric-login-wait",
        max_retries=3,
        parameter_summary={
            "appid": APPID,
            "source": MetricSource.GROUP_SHARE_DAILY,
            "start_ds": 20260719,
            "end_ds": 20260719,
        },
    )

    execute_next_job(METRIC_WORKER_HANDLERS, task_types=METRIC_WORKER_HANDLERS)
    job.refresh_from_db()
    assert job.status == JobStatus.PENDING
    assert job.retry_count == 0
    assert job.next_run_at > timezone.now()


@pytest.mark.django_db
def test_schedule_and_manual_lease_have_the_required_semantics():
    before = enqueue_due_metric_jobs(datetime(2026, 7, 20, 7, 29, tzinfo=SHANGHAI))
    due = enqueue_due_metric_jobs(datetime(2026, 7, 20, 17, 30, tzinfo=SHANGHAI))

    assert before.daily == () and before.weekly == ()
    assert len(due.daily) == len(DAILY_METRIC_SOURCES) == 5
    assert {result.job.parameter_summary["source"] for result in due.daily} == set(
        DAILY_METRIC_SOURCES
    )
    assert len(due.weekly) == 1
    assert due.weekly[0].job.parameter_summary["start_ds"] == 20260629
    assert due.weekly[0].job.parameter_summary["end_ds"] == 20260719
    assert due.weekly[0].job.parameter_summary["release_phase"] == "initial"

    late_start = enqueue_due_metric_jobs(datetime(2026, 7, 21, 16, 0, tzinfo=SHANGHAI))
    assert len(late_start.weekly) == 3
    assert {result.job.parameter_summary["release_phase"] for result in late_start.weekly} == {
        "initial",
        "morning-recheck",
        "afternoon-recheck",
    }
    assert BackgroundJob.objects.filter(task_type="METRIC_WEEKLY_SYNC").count() == 3

    first = enqueue_metric_backfill(
        source=MetricSource.GROUP_SHARE_DAILY,
        start_day=date(2026, 7, 1),
        end_day=date(2026, 7, 2),
    )
    duplicate = enqueue_metric_backfill(
        source=MetricSource.GROUP_SHARE_DAILY,
        start_day=date(2026, 7, 1),
        end_day=date(2026, 7, 2),
    )
    assert duplicate.created is False
    assert duplicate.job.pk == first.job.pk
    first.job.status = JobStatus.FAILED
    first.job.save(update_fields=["status"])
    next_batch = enqueue_metric_backfill(
        source=MetricSource.GROUP_SHARE_DAILY,
        start_day=date(2026, 7, 1),
        end_day=date(2026, 7, 2),
    )
    assert next_batch.created is True
    assert next_batch.job.pk != first.job.pk
    with pytest.raises(ValueError, match="内容粉丝"):
        enqueue_metric_backfill(
            source=MetricSource.CONTENT_FANS_DAILY,
            start_day=date(2026, 7, 1),
            end_day=date(2026, 7, 2),
        )


def test_metric_client_sends_one_content_analysis_flag(monkeypatch):
    client = object.__new__(CdpGameCircleMetricClient)
    client.appid = APPID
    captured = []
    monkeypatch.setattr(
        client,
        "_request",
        lambda path, **kwargs: captured.append((path, kwargs)) or _page(),
    )

    client.group_content_analysis(
        start_ds=20260701,
        end_ds=20260719,
        data_flag="need_content_consume_data",
    )

    assert captured[0][0].endswith("spacexgamemp_datacenter_getgroupconsumedetaildata")
    data = captured[0][1]["params"]["data"]
    assert '"need_content_consume_data":true' in data
    assert data.count("need_content_") == 1


@pytest.mark.django_db
def test_pending_zero_weekly_snapshot_never_overwrites_verified_data():
    verified_record = MetricRecord(
        date(2026, 7, 13),
        date(2026, 7, 19),
        {
            "avg_quanzi_dau_cur_week": 100,
            "avg_quannzi_cross_game_uv_cur_week": 0,
            "res_expose_cover_new_uv_cur_week": 0,
            "res_expose_cover_back_uv_cur_week": 0,
            "res_expose_cover_deposit_uv_cur_week": 0,
            "avg_publish_uv_rate_cur_week": 0,
            "avg_quality_publish_pv_rate_cur_week": 0,
            "avg_content_expose_cnt_cur_week": 0,
        },
    )
    job = BackgroundJob.objects.create(
        task_type=METRIC_WEEKLY_SYNC_TASK,
        idempotency_key="weekly-ok",
    )
    verified = upsert_metric_snapshot(
        appid=APPID,
        source=MetricSource.OPERATION_EFFECT_WEEKLY,
        record=verified_record,
        job=job,
    )
    zero_record = MetricRecord(
        date(2026, 7, 13),
        date(2026, 7, 19),
        {key: 0 for key in verified_record.metrics},
    )

    unchanged = upsert_metric_snapshot(
        appid=APPID,
        source=MetricSource.OPERATION_EFFECT_WEEKLY,
        record=zero_record,
        job=job,
    )

    assert unchanged.pk == verified.pk
    assert unchanged.publication_state == MetricPublicationState.VERIFIED
    assert unchanged.metrics["avg_quanzi_dau_cur_week"] == 100

    pending = upsert_metric_snapshot(
        appid=APPID,
        source=MetricSource.OPERATION_EFFECT_WEEKLY,
        record=MetricRecord(date(2026, 7, 20), date(2026, 7, 26), zero_record.metrics),
        job=job,
    )
    assert pending.publication_state == MetricPublicationState.PENDING_RECHECK

    recovered = upsert_metric_snapshot(
        appid=APPID,
        source=MetricSource.OPERATION_EFFECT_WEEKLY,
        record=MetricRecord(date(2026, 7, 20), date(2026, 7, 26), verified_record.metrics),
        job=job,
    )
    assert recovered.pk == pending.pk
    assert recovered.publication_state == MetricPublicationState.VERIFIED
