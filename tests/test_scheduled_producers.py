from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jobs.models import (
    BackgroundJob,
    ScheduledProducerConfig,
    ScheduledProducerRuleConfig,
    ScheduledProducerSlot,
)
from jobs.scheduled_producers import (
    create_rule_configuration,
    delete_rule_configuration,
    ensure_producer_configurations,
    reconcile_scheduled_producers,
    schedule_description_for_rule,
    update_producer_enabled,
    update_rule_configuration,
)
from jobs.services import enqueue_job_with_result
from posts.game_catalog import set_active_game
from posts.models import ManagedGame


@pytest.fixture
def active_game(db):
    game = ManagedGame.objects.create(appid="wx-schedule", name="调度测试")
    set_active_game(appid=game.appid)
    return game


def test_initialization_creates_only_fixed_producers_and_rules(db):
    ensure_producer_configurations()

    assert set(ScheduledProducerConfig.objects.values_list("producer_key", flat=True)) == {
        "post_ingestion",
        "metric_daily",
        "metric_weekly",
        "session_keepalive",
        "permanent_mute_renewal",
    }
    assert ScheduledProducerRuleConfig.objects.count() == 8
    assert ScheduledProducerConfig.objects.get(producer_key="session_keepalive").enabled is False


@pytest.mark.django_db
def test_rule_update_ignores_raw_schedule_kind_and_keeps_template_kind():
    ensure_producer_configurations()
    rule = ScheduledProducerRuleConfig.objects.get(producer__producer_key="post_ingestion")

    update_rule_configuration(rule_id=rule.pk, values={"schedule_kind": "COMMAND"})

    rule.refresh_from_db()
    assert rule.schedule_kind == "INTERVAL"


@pytest.mark.django_db
def test_rule_instances_are_created_from_a_time_point_with_a_fixed_timezone():
    ensure_producer_configurations()
    producer = ScheduledProducerConfig.objects.get(producer_key="metric_weekly")

    rule = create_rule_configuration(
        producer_id=producer.pk,
        values={
            "rule_type": "morning-recheck",
            "schedule_time": "11:30",
            "enabled": "true",
        },
    )

    assert rule.rule_key != rule.rule_type
    assert rule.rule_type == "morning-recheck"
    assert rule.timezone_name == "Asia/Shanghai"
    assert rule.schedule_expression == "30 11 * * 2"
    assert ScheduledProducerRuleConfig.objects.filter(producer=producer).count() == 4


@pytest.mark.django_db
def test_rule_update_rejects_a_timezone_override():
    ensure_producer_configurations()
    rule = ScheduledProducerRuleConfig.objects.get(producer__producer_key="post_ingestion")

    with pytest.raises(ValueError, match="中国标准时间"):
        update_rule_configuration(rule_id=rule.pk, values={"timezone_name": "UTC"})


@pytest.mark.django_db
def test_weekly_default_rules_use_their_template_weekdays():
    ensure_producer_configurations()

    assert (
        ScheduledProducerRuleConfig.objects.get(
            producer__producer_key="metric_weekly", rule_type="initial"
        ).schedule_expression
        == "30 17 * * 1"
    )
    assert (
        ScheduledProducerRuleConfig.objects.get(
            producer__producer_key="metric_weekly", rule_type="morning-recheck"
        ).schedule_expression
        == "0 10 * * 2"
    )


@pytest.mark.django_db
def test_schedule_labels_are_reversed_from_the_saved_expression():
    ensure_producer_configurations()
    daily = ScheduledProducerRuleConfig.objects.get(producer__producer_key="metric_daily")
    ingestion = ScheduledProducerRuleConfig.objects.get(producer__producer_key="post_ingestion")

    assert schedule_description_for_rule(daily) == "每天 07:30"
    assert schedule_description_for_rule(ingestion) == "每 15 分钟"


@pytest.mark.django_db
def test_deleted_rule_is_not_recreated_by_configuration_reconciliation():
    ensure_producer_configurations()
    rule = ScheduledProducerRuleConfig.objects.get(producer__producer_key="metric_daily")
    slot = ScheduledProducerSlot.objects.create(
        rule=rule,
        utc_instance=datetime(2026, 7, 20, tzinfo=ZoneInfo("UTC")),
        config_version=rule.config_version,
    )

    delete_rule_configuration(rule_id=rule.pk)
    ensure_producer_configurations()

    rule.refresh_from_db()
    assert rule.archived_at is not None
    assert rule.enabled is False
    assert ScheduledProducerSlot.objects.filter(pk=slot.pk, rule=rule).exists()
    assert not ScheduledProducerRuleConfig.objects.filter(
        producer__producer_key="metric_daily", archived_at__isnull=True
    ).exists()


@pytest.mark.django_db
def test_reconciler_is_idempotent_and_does_not_bootstrap_old_weekly_or_mute_work(active_game):
    now = datetime(2026, 7, 19, 10, 16, tzinfo=ZoneInfo("Asia/Shanghai"))

    first = reconcile_scheduled_producers(now)
    second = reconcile_scheduled_producers(now)

    assert first["post_ingestion:default"] == "ENQUEUED"
    assert first["metric_daily:daily"] == "ENQUEUED"
    assert first["metric_weekly:initial"] == "NOT_DUE"
    assert first["permanent_mute_renewal:midnight"] == "NOT_DUE"
    assert second["post_ingestion:default"] == "NOT_DUE"
    assert BackgroundJob.objects.filter(task_type="SCHEDULED_INGEST_WINDOW").count() == 1
    assert ScheduledProducerSlot.objects.filter(state="ENQUEUED").count() == 2


@pytest.mark.django_db
def test_paused_producer_blocks_only_future_automatic_enqueue(active_game):
    ensure_producer_configurations()
    producer = ScheduledProducerConfig.objects.get(producer_key="post_ingestion")
    update_producer_enabled(producer_id=producer.pk, enabled=False)

    result = reconcile_scheduled_producers(
        datetime(2026, 7, 19, 10, 16, tzinfo=ZoneInfo("Asia/Shanghai"))
    )

    assert result["post_ingestion:default"] == "DISABLED"
    assert not BackgroundJob.objects.filter(task_type="SCHEDULED_INGEST_WINDOW").exists()


@pytest.mark.django_db
def test_failed_multi_job_attempt_rolls_back_jobs_and_records_sanitized_slot(
    active_game, monkeypatch
):
    ensure_producer_configurations()

    def broken_enqueue(*_args, **_kwargs):
        enqueue_job_with_result(
            task_type="STATUS_SYNC", idempotency_key="rollback-test", parameter_summary={}
        )
        raise RuntimeError("provider response must not leak")

    monkeypatch.setattr("jobs.scheduled_producers._enqueue_for_slot", broken_enqueue)
    result = reconcile_scheduled_producers(
        datetime(2026, 7, 19, 10, 16, tzinfo=ZoneInfo("Asia/Shanghai"))
    )

    assert result["post_ingestion:default"] == "ENQUEUE_FAILED"
    assert not BackgroundJob.objects.filter(idempotency_key="rollback-test").exists()
    slot = ScheduledProducerSlot.objects.get(rule__producer__producer_key="post_ingestion")
    assert slot.state == "FAILED"
    assert slot.error_category == "ENQUEUE_FAILED"
