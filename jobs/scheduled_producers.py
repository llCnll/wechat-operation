"""Database-backed reconciliation for the small, fixed set of automatic producers.

This module deliberately schedules only existing background jobs.  It never owns a
browser, makes an upstream request, or accepts arbitrary task parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from croniter import croniter
from django.db import transaction
from django.utils import timezone

from jobs.game_circle_keepalive import enqueue_keepalive
from jobs.models import (
    BackgroundJob,
    JobStatus,
    ScheduledProducerConfig,
    ScheduledProducerRuleConfig,
    ScheduledProducerSlot,
    ScheduledProducerSlotJob,
)
from jobs.permanent_mute import enqueue_permanent_mute_renewal
from jobs.services import EnqueueResult
from posts.ingestion_jobs import enqueue_scheduled_jobs
from reporting.metric_jobs import enqueue_daily_metric_sync, enqueue_weekly_metric_sync
from reporting.weekly_schedule import WeeklyReleaseSchedule

SHANGHAI = "Asia/Shanghai"
ACTIVE_JOB_STATUSES = (JobStatus.PENDING, JobStatus.RUNNING)
FRESH_CRON_SECONDS = 90


@dataclass(frozen=True)
class RuleDefinition:
    key: str
    label: str
    schedule_kind: str
    expression: str
    timezone_name: str
    misfire_policy: str
    overlap_policy: str
    allowed_misfire: tuple[str, ...]
    allowed_overlap: tuple[str, ...]
    min_interval: int | None = None
    max_interval: int | None = None


@dataclass(frozen=True)
class ProducerDefinition:
    key: str
    label: str
    templates: tuple[RuleDefinition, ...]
    enabled_by_default: bool = True


POST_RULE = RuleDefinition(
    "default",
    "帖子采集",
    "INTERVAL",
    "15",
    SHANGHAI,
    "SKIP",
    "ALLOW",
    ("SKIP",),
    ("ALLOW", "SKIP_WHILE_ACTIVE", "COALESCE_WHILE_ACTIVE"),
    5,
    1440,
)
DAILY_RULE = RuleDefinition(
    "daily",
    "每日指标",
    "CRON",
    "30 7 * * *",
    SHANGHAI,
    "COALESCE",
    "COALESCE_WHILE_ACTIVE",
    ("SKIP", "COALESCE"),
    ("SKIP_WHILE_ACTIVE", "COALESCE_WHILE_ACTIVE"),
)
WEEKLY_RULES = tuple(
    RuleDefinition(
        phase.key,
        {
            "initial": "首次发布",
            "morning-recheck": "上午复核",
            "afternoon-recheck": "下午复核",
        }[phase.key],
        "CRON",
        f"{phase.release_time.minute} {phase.release_time.hour} * * {(phase.weekday + 1) % 7}",
        SHANGHAI,
        "CATCH_UP_LIMITED",
        "COALESCE_WHILE_ACTIVE",
        ("CATCH_UP_LIMITED",),
        ("SKIP_WHILE_ACTIVE", "COALESCE_WHILE_ACTIVE"),
    )
    for phase in WeeklyReleaseSchedule.phases
)
KEEPALIVE_RULE = RuleDefinition(
    "default",
    "会话保活",
    "INTERVAL",
    "240",
    SHANGHAI,
    "SKIP",
    "SKIP_WHILE_ACTIVE",
    ("SKIP",),
    ("SKIP_WHILE_ACTIVE", "COALESCE_WHILE_ACTIVE"),
    60,
    1440,
)
MUTE_RULES = (
    RuleDefinition(
        "midnight",
        "零点续期",
        "CRON",
        "0 0 * * *",
        SHANGHAI,
        "SKIP",
        "SKIP_WHILE_ACTIVE",
        ("SKIP",),
        ("SKIP_WHILE_ACTIVE", "COALESCE_WHILE_ACTIVE"),
    ),
    RuleDefinition(
        "noon",
        "中午续期",
        "CRON",
        "0 12 * * *",
        SHANGHAI,
        "SKIP",
        "SKIP_WHILE_ACTIVE",
        ("SKIP",),
        ("SKIP_WHILE_ACTIVE", "COALESCE_WHILE_ACTIVE"),
    ),
)
DEFINITIONS = (
    ProducerDefinition("post_ingestion", "帖子采集", (POST_RULE,)),
    ProducerDefinition("metric_daily", "日指标", (DAILY_RULE,)),
    ProducerDefinition("metric_weekly", "周指标", WEEKLY_RULES),
    ProducerDefinition(
        "session_keepalive", "会话保活", (KEEPALIVE_RULE,), enabled_by_default=False
    ),
    ProducerDefinition("permanent_mute_renewal", "永久禁言续期", MUTE_RULES),
)
DEFINITION_BY_KEY = {definition.key: definition for definition in DEFINITIONS}


def definition_for_rule(rule: ScheduledProducerRuleConfig) -> RuleDefinition:
    return definition_for_template(rule.producer.producer_key, rule.rule_type)


def definition_for_template(producer_key: str, rule_type: str) -> RuleDefinition:
    try:
        definition = DEFINITION_BY_KEY[producer_key]
        return next(item for item in definition.templates if item.key == rule_type)
    except (KeyError, StopIteration) as exc:
        raise ValueError("UNKNOWN_SCHEDULED_PRODUCER") from exc


def _session_keepalive_initially_enabled() -> bool:
    # Importing the public view keeps the legacy value a one-time bootstrap input only.
    from config.runtime_config import public_runtime_config

    return public_runtime_config().get("session_keepalive_enabled") == "true"


def ensure_producer_configurations() -> None:
    """Create missing fixed definitions without overwriting operator-owned values."""
    for definition in DEFINITIONS:
        enabled = definition.enabled_by_default
        if definition.key == "session_keepalive":
            enabled = _session_keepalive_initially_enabled()
        producer, created = ScheduledProducerConfig.objects.get_or_create(
            producer_key=definition.key, defaults={"enabled": enabled}
        )
        if not created:
            continue
        for item in definition.templates:
            ScheduledProducerRuleConfig.objects.get_or_create(
                producer=producer,
                rule_key=item.key,
                defaults={
                    "rule_type": item.key,
                    "enabled": True,
                    "schedule_kind": item.schedule_kind,
                    "schedule_expression": item.expression,
                    "timezone_name": item.timezone_name,
                    "misfire_policy": item.misfire_policy,
                    "overlap_policy": item.overlap_policy,
                },
            )


def validate_rule_update(
    *, rule: ScheduledProducerRuleConfig, values: dict[str, str]
) -> dict[str, str | bool]:
    """Validate the small allow-list accepted by the administration endpoint."""
    definition = definition_for_rule(rule)
    kind = values.get("schedule_kind", rule.schedule_kind)
    expression = values.get("schedule_expression", rule.schedule_expression).strip()
    submitted_timezone = values.get("timezone_name", "").strip()
    timezone_name = definition.timezone_name
    misfire = values.get("misfire_policy", rule.misfire_policy)
    overlap = values.get("overlap_policy", rule.overlap_policy)
    if kind != definition.schedule_kind:
        raise ValueError("该规则不支持此调度方式")
    if submitted_timezone and submitted_timezone != timezone_name:
        raise ValueError("自动任务统一使用中国标准时间，不能修改时区")
    if misfire not in definition.allowed_misfire or overlap not in definition.allowed_overlap:
        raise ValueError("该规则不支持此错过触发或重叠策略")
    if kind == ScheduledProducerRuleConfig.ScheduleKind.INTERVAL:
        if not expression.isdecimal():
            raise ValueError("固定间隔必须为整数分钟")
        minutes = int(expression)
        if not definition.min_interval <= minutes <= definition.max_interval:
            raise ValueError(
                f"固定间隔必须在 {definition.min_interval} 至 {definition.max_interval} 分钟之间"
            )
    else:
        fields = expression.split()
        if len(fields) != 5 or expression.startswith("@"):
            raise ValueError("Cron 仅支持五字段表达式")
        try:
            croniter(expression, datetime.now(ZoneInfo(timezone_name)), day_or=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("Cron 表达式无效") from exc
    return {
        "schedule_kind": kind,
        "schedule_expression": expression,
        "timezone_name": timezone_name,
        "misfire_policy": misfire,
        "overlap_policy": overlap,
    }


def _time_value(expression: str) -> str:
    try:
        minute, hour, *_ = expression.split()
        return f"{int(hour):02}:{int(minute):02}"
    except (TypeError, ValueError) as exc:
        raise ValueError("现有定时规则无法转换为时点") from exc


def _cron_weekday(rule: ScheduledProducerRuleConfig) -> str:
    if rule.producer.producer_key != "metric_weekly":
        return "*"
    phase = next(item for item in WeeklyReleaseSchedule.phases if item.key == rule.rule_type)
    return str((phase.weekday + 1) % 7)


def _schedule_expression_from_values(
    *, rule: ScheduledProducerRuleConfig, values: dict[str, str], required: bool
) -> str:
    if rule.schedule_kind == ScheduledProducerRuleConfig.ScheduleKind.INTERVAL:
        value = values.get("interval_minutes", "").strip()
        if not value and required:
            raise ValueError("请填写执行间隔")
        return value or rule.schedule_expression
    time_value = values.get("schedule_time", "").strip()
    if not time_value and required:
        raise ValueError("请填写执行时点")
    time_value = time_value or _time_value(rule.schedule_expression)
    try:
        parsed = datetime.strptime(time_value, "%H:%M")
    except ValueError as exc:
        raise ValueError("执行时点必须为 HH:MM") from exc
    return f"{parsed.minute} {parsed.hour} * * {_cron_weekday(rule)}"


def schedule_description_for_rule(rule: ScheduledProducerRuleConfig) -> str:
    if rule.schedule_kind == ScheduledProducerRuleConfig.ScheduleKind.INTERVAL:
        return f"每 {int(rule.schedule_expression)} 分钟"
    try:
        minute, hour, day, month, weekday = rule.schedule_expression.split()
        time_value = f"{int(hour):02}:{int(minute):02}"
    except (TypeError, ValueError):
        return "已配置定时规则"
    if day != "*" or month != "*":
        return "已配置定时规则"
    if weekday == "*":
        return f"每天 {time_value}"
    weekdays = ("周日", "周一", "周二", "周三", "周四", "周五", "周六")
    if weekday.isdecimal() and 0 <= int(weekday) <= 6:
        return f"每{weekdays[int(weekday)]} {time_value}"
    return "已配置定时规则"


def schedule_value_for_rule(rule: ScheduledProducerRuleConfig) -> str:
    return (
        rule.schedule_expression
        if rule.schedule_kind == ScheduledProducerRuleConfig.ScheduleKind.INTERVAL
        else _time_value(rule.schedule_expression)
    )


def _clean_schedule_values(
    *, rule: ScheduledProducerRuleConfig, values: dict[str, str], required: bool
) -> dict[str, str | bool]:
    submitted_timezone = values.get("timezone_name", "").strip()
    if submitted_timezone and submitted_timezone != SHANGHAI:
        raise ValueError("自动任务统一使用中国标准时间，不能修改时区")
    definition = definition_for_rule(rule)
    return validate_rule_update(
        rule=rule,
        values={
            "schedule_kind": definition.schedule_kind,
            "schedule_expression": _schedule_expression_from_values(
                rule=rule, values=values, required=required
            ),
            "misfire_policy": definition.misfire_policy,
            "overlap_policy": definition.overlap_policy,
        },
    )


def update_rule_configuration(
    *, rule_id: int, values: dict[str, str]
) -> ScheduledProducerRuleConfig:
    with transaction.atomic():
        rule = ScheduledProducerRuleConfig.objects.select_related("producer").get(
            pk=rule_id, archived_at__isnull=True
        )
        cleaned = _clean_schedule_values(rule=rule, values=values, required=False)
        enabled = values.get("enabled", "false").lower() in {"1", "true", "on", "yes"}
        changed = (
            any(getattr(rule, key) != value for key, value in cleaned.items())
            or rule.enabled != enabled
        )
        if changed:
            for key, value in cleaned.items():
                setattr(rule, key, value)
            rule.enabled = enabled
            rule.config_version += 1
            rule.save()
        return rule


def create_rule_configuration(
    *, producer_id: int, values: dict[str, str]
) -> ScheduledProducerRuleConfig:
    with transaction.atomic():
        producer = ScheduledProducerConfig.objects.get(pk=producer_id)
        rule_type = values.get("rule_type", "").strip()
        definition = definition_for_template(producer.producer_key, rule_type)
        rule = ScheduledProducerRuleConfig(
            producer=producer,
            rule_key=f"rule-{uuid4().hex}",
            rule_type=rule_type,
            enabled=values.get("enabled", "false").lower() in {"1", "true", "on", "yes"},
            schedule_kind=definition.schedule_kind,
            schedule_expression=definition.expression,
            timezone_name=definition.timezone_name,
            misfire_policy=definition.misfire_policy,
            overlap_policy=definition.overlap_policy,
        )
        cleaned = _clean_schedule_values(rule=rule, values=values, required=True)
        for key, value in cleaned.items():
            setattr(rule, key, value)
        rule.save()
        return rule


def delete_rule_configuration(*, rule_id: int) -> None:
    with transaction.atomic():
        rule = ScheduledProducerRuleConfig.objects.get(pk=rule_id, archived_at__isnull=True)
        rule.enabled = False
        rule.archived_at = timezone.now()
        rule.config_version += 1
        rule.save(update_fields=("enabled", "archived_at", "config_version", "updated_at"))


def update_producer_enabled(*, producer_id: int, enabled: bool) -> ScheduledProducerConfig:
    with transaction.atomic():
        producer = ScheduledProducerConfig.objects.get(pk=producer_id)
        if producer.enabled != enabled:
            producer.enabled = enabled
            producer.config_version += 1
            producer.save()
        return producer


def _latest_due(rule: ScheduledProducerRuleConfig, now: datetime) -> datetime | None:
    current = now.astimezone(UTC).replace(second=0, microsecond=0)
    if rule.schedule_kind == ScheduledProducerRuleConfig.ScheduleKind.INTERVAL:
        minutes = int(rule.schedule_expression)
        epoch_minutes = int(current.timestamp() // 60)
        return datetime.fromtimestamp((epoch_minutes - epoch_minutes % minutes) * 60, tz=UTC)
    local_now = now.astimezone(ZoneInfo(rule.timezone_name))
    iterator = croniter(rule.schedule_expression, local_now + timedelta(minutes=1), day_or=True)
    candidate = iterator.get_prev(datetime)
    return candidate.astimezone(UTC).replace(second=0, microsecond=0)


def next_due_for_display(
    rule: ScheduledProducerRuleConfig, now: datetime | None = None
) -> datetime:
    moment = (now or timezone.now()).astimezone(UTC).replace(second=0, microsecond=0)
    if rule.schedule_kind == ScheduledProducerRuleConfig.ScheduleKind.INTERVAL:
        minutes = int(rule.schedule_expression)
        epoch_minutes = int(moment.timestamp() // 60)
        return datetime.fromtimestamp(
            (epoch_minutes - epoch_minutes % minutes + minutes) * 60, tz=UTC
        )
    local_now = moment.astimezone(ZoneInfo(rule.timezone_name))
    return (
        croniter(rule.schedule_expression, local_now, day_or=True)
        .get_next(datetime)
        .astimezone(UTC)
    )


def _fresh_enough(rule: ScheduledProducerRuleConfig, slot_at: datetime, now: datetime) -> bool:
    if rule.schedule_kind == ScheduledProducerRuleConfig.ScheduleKind.INTERVAL:
        return True
    return (now.astimezone(UTC) - slot_at).total_seconds() <= FRESH_CRON_SECONDS


def _allowed_on_first_observation(
    rule: ScheduledProducerRuleConfig, slot_at: datetime, now: datetime
) -> bool:
    """Mirror the old command on its first post-migration observation.

    There is no historical Scheduler cursor before this feature exists.  A fresh
    installation must not interpret last Tuesday's weekly release or midnight's
    renewal as work that was explicitly missed.
    """
    if rule.schedule_kind == ScheduledProducerRuleConfig.ScheduleKind.INTERVAL:
        return True
    local_now = now.astimezone(ZoneInfo(rule.timezone_name))
    producer = rule.producer.producer_key
    if producer == "metric_daily":
        return (local_now.hour, local_now.minute) >= (7, 30)
    if producer == "metric_weekly":
        phase = next(item for item in WeeklyReleaseSchedule.phases if item.key == rule.rule_type)
        now_time = local_now.time().replace(tzinfo=None)
        return local_now.weekday() <= 1 and (
            local_now.weekday() > phase.weekday
            or (local_now.weekday() == phase.weekday and now_time >= phase.release_time)
        )
    return _fresh_enough(rule, slot_at, now)


def _active_jobs(rule: ScheduledProducerRuleConfig) -> bool:
    return BackgroundJob.objects.filter(
        scheduled_slots__slot__rule=rule, status__in=ACTIVE_JOB_STATUSES
    ).exists()


def _enqueue_for_slot(
    rule: ScheduledProducerRuleConfig, slot_at: datetime
) -> tuple[EnqueueResult, ...]:
    producer = rule.producer.producer_key
    if producer == "post_ingestion":
        result = enqueue_scheduled_jobs(slot_at, interval_minutes=int(rule.schedule_expression))
        return (result.status_sync, result.ingestion)
    local_slot = slot_at.astimezone(ZoneInfo(rule.timezone_name))
    if producer == "metric_daily":
        return enqueue_daily_metric_sync(local_slot.date() - timedelta(days=1))
    if producer == "metric_weekly":
        phase = next(item for item in WeeklyReleaseSchedule.phases if item.key == rule.rule_type)
        business_week_start = local_slot.date() - timedelta(days=local_slot.weekday() + 7)
        return (enqueue_weekly_metric_sync(business_week_start=business_week_start, phase=phase),)
    if producer == "session_keepalive":
        return (enqueue_keepalive(trigger="schedule", now=slot_at),)
    if producer == "permanent_mute_renewal":
        return (enqueue_permanent_mute_renewal(source="scheduler", now=slot_at),)
    raise ValueError("UNKNOWN_SCHEDULED_PRODUCER")


def _record_failure(rule_id: int, slot_at: datetime, category: str) -> None:
    with transaction.atomic():
        rule = ScheduledProducerRuleConfig.objects.select_related("producer").get(pk=rule_id)
        slot, _ = ScheduledProducerSlot.objects.get_or_create(
            rule=rule, utc_instance=slot_at, defaults={"config_version": rule.config_version}
        )
        slot.state = ScheduledProducerSlot.State.FAILED
        slot.attempt_count += 1
        slot.error_category = category
        slot.save()
        rule.last_checked_at = timezone.now()
        rule.last_error_category = category
        rule.save(update_fields=("last_checked_at", "last_error_category", "updated_at"))


def _failure_slot_at(rule_id: int, now: datetime) -> datetime:
    """Return a stable failure key even if an externally corrupted rule cannot parse."""
    rule = ScheduledProducerRuleConfig.objects.get(pk=rule_id)
    try:
        return _latest_due(rule, now) or now.astimezone(UTC).replace(second=0, microsecond=0)
    except (TypeError, ValueError):
        return now.astimezone(UTC).replace(second=0, microsecond=0)


def reconcile_rule(rule_id: int, *, now: datetime) -> str:
    """Reconcile one rule.  Expected business unready states do not raise."""
    try:
        with transaction.atomic():
            rule = ScheduledProducerRuleConfig.objects.select_related("producer").get(pk=rule_id)
            checked_at = timezone.now()
            if rule.archived_at is not None or not rule.producer.enabled or not rule.enabled:
                rule.last_checked_at = checked_at
                rule.save(update_fields=("last_checked_at", "updated_at"))
                return "DISABLED"
            slot_at = _latest_due(rule, now)
            if slot_at is None or (rule.covered_through_at and slot_at <= rule.covered_through_at):
                rule.last_checked_at = checked_at
                rule.save(update_fields=("last_checked_at", "updated_at"))
                return "NOT_DUE"
            if rule.covered_through_at is None and not _allowed_on_first_observation(
                rule, slot_at, now
            ):
                rule.covered_through_at = slot_at
                rule.last_checked_at = checked_at
                rule.last_error_category = "NOT_DUE"
                rule.save()
                return "NOT_DUE"
            missed = rule.covered_through_at and slot_at > rule.covered_through_at + timedelta(
                minutes=1
            )
            if (
                missed
                and rule.misfire_policy == ScheduledProducerRuleConfig.MisfirePolicy.SKIP
                and not _fresh_enough(rule, slot_at, now)
            ):
                rule.last_skipped_from_at = rule.covered_through_at
                rule.last_skipped_to_at = slot_at
                rule.covered_through_at = slot_at
                rule.last_checked_at = checked_at
                rule.last_error_category = "MISFIRE_SKIPPED"
                rule.save()
                return "MISFIRE_SKIPPED"
            active = _active_jobs(rule)
            if (
                active
                and rule.overlap_policy
                == ScheduledProducerRuleConfig.OverlapPolicy.SKIP_WHILE_ACTIVE
            ):
                slot, _ = ScheduledProducerSlot.objects.get_or_create(
                    rule=rule,
                    utc_instance=slot_at,
                    defaults={"config_version": rule.config_version},
                )
                slot.state = ScheduledProducerSlot.State.SKIPPED
                slot.save()
                rule.covered_through_at = slot_at
                rule.last_checked_at = checked_at
                rule.last_error_category = "OVERLAP_SKIPPED"
                rule.save()
                return "OVERLAP_SKIPPED"
            if (
                active
                and rule.overlap_policy
                == ScheduledProducerRuleConfig.OverlapPolicy.COALESCE_WHILE_ACTIVE
            ):
                rule.slots.filter(state=ScheduledProducerSlot.State.HELD).exclude(
                    utc_instance=slot_at
                ).update(
                    state=ScheduledProducerSlot.State.SKIPPED,
                    error_category="OVERLAP_SUPERSEDED",
                )
                slot, _ = ScheduledProducerSlot.objects.get_or_create(
                    rule=rule,
                    utc_instance=slot_at,
                    defaults={"config_version": rule.config_version},
                )
                slot.state = ScheduledProducerSlot.State.HELD
                slot.save()
                rule.last_checked_at = checked_at
                rule.last_error_category = "OVERLAP_HELD"
                rule.save()
                return "OVERLAP_HELD"
            held = (
                rule.slots.filter(state=ScheduledProducerSlot.State.HELD)
                .order_by("-utc_instance")
                .first()
            )
            if held is not None:
                slot_at = held.utc_instance
            slot, _ = ScheduledProducerSlot.objects.get_or_create(
                rule=rule, utc_instance=slot_at, defaults={"config_version": rule.config_version}
            )
            results = _enqueue_for_slot(rule, slot_at)
            for result in results:
                ScheduledProducerSlotJob.objects.get_or_create(slot=slot, job=result.job)
            slot.state = ScheduledProducerSlot.State.ENQUEUED
            slot.attempt_count += 1
            slot.error_category = ""
            slot.save()
            rule.covered_through_at = max(slot_at, rule.covered_through_at or slot_at)
            rule.last_checked_at = checked_at
            rule.last_enqueued_at = checked_at
            rule.last_error_category = ""
            rule.save()
            rule.producer.last_checked_at = checked_at
            rule.producer.last_enqueued_at = checked_at
            rule.producer.last_error_category = ""
            rule.producer.save(
                update_fields=(
                    "last_checked_at",
                    "last_enqueued_at",
                    "last_error_category",
                    "updated_at",
                )
            )
            return "ENQUEUED"
    except ValueError as exc:
        category = (
            "NO_ACTIVE_GAME"
            if "当前操作游戏" in str(exc) or "执行范围" in str(exc)
            else "CONFIG_INVALID"
        )
        _record_failure(rule_id, _failure_slot_at(rule_id, now), category)
        return category
    except Exception:
        _record_failure(rule_id, _failure_slot_at(rule_id, now), "ENQUEUE_FAILED")
        return "ENQUEUE_FAILED"


def reconcile_scheduled_producers(now: datetime | None = None) -> dict[str, str]:
    ensure_producer_configurations()
    moment = now or timezone.now()
    results: dict[str, str] = {}
    for rule_id, key in (
        ScheduledProducerRuleConfig.objects.filter(archived_at__isnull=True)
        .values_list("pk", "producer__producer_key")
        .order_by("pk")
    ):
        rule = ScheduledProducerRuleConfig.objects.get(pk=rule_id)
        results[f"{key}:{rule.rule_key}"] = reconcile_rule(rule_id, now=moment)
    return results
