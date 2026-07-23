import django.db.models.deletion
from django.db import migrations, models


def create_default_producers(apps, schema_editor):
    producer_model = apps.get_model("jobs", "ScheduledProducerConfig")
    rule_model = apps.get_model("jobs", "ScheduledProducerRuleConfig")
    try:
        from config.runtime_config import public_runtime_config
        from config.settings import SCHEDULED_INGESTION_INTERVAL_MINUTES

        keepalive_enabled = public_runtime_config().get("session_keepalive_enabled") == "true"
        ingestion_interval = str(SCHEDULED_INGESTION_INTERVAL_MINUTES)
    except Exception:  # Keep migrations usable with minimal settings.
        keepalive_enabled = False
        ingestion_interval = "15"

    definitions = (
        ("post_ingestion", True, (("default", "INTERVAL", ingestion_interval, "SKIP", "ALLOW"),)),
        (
            "metric_daily",
            True,
            (("daily", "CRON", "30 7 * * *", "COALESCE", "COALESCE_WHILE_ACTIVE"),),
        ),
        (
            "metric_weekly",
            True,
            (
                ("initial", "CRON", "30 17 * * 1", "CATCH_UP_LIMITED", "COALESCE_WHILE_ACTIVE"),
                (
                    "morning-recheck",
                    "CRON",
                    "0 10 * * 2",
                    "CATCH_UP_LIMITED",
                    "COALESCE_WHILE_ACTIVE",
                ),
                (
                    "afternoon-recheck",
                    "CRON",
                    "0 16 * * 2",
                    "CATCH_UP_LIMITED",
                    "COALESCE_WHILE_ACTIVE",
                ),
            ),
        ),
        (
            "session_keepalive",
            keepalive_enabled,
            (("default", "INTERVAL", "240", "SKIP", "SKIP_WHILE_ACTIVE"),),
        ),
        (
            "permanent_mute_renewal",
            True,
            (
                ("midnight", "CRON", "0 0 * * *", "SKIP", "SKIP_WHILE_ACTIVE"),
                ("noon", "CRON", "0 12 * * *", "SKIP", "SKIP_WHILE_ACTIVE"),
            ),
        ),
    )
    for key, enabled, rules in definitions:
        producer, _ = producer_model.objects.get_or_create(
            producer_key=key, defaults={"enabled": enabled}
        )
        for rule_key, kind, expression, misfire, overlap in rules:
            rule_model.objects.get_or_create(
                producer=producer,
                rule_key=rule_key,
                defaults={
                    "rule_type": rule_key,
                    "enabled": True,
                    "schedule_kind": kind,
                    "schedule_expression": expression,
                    "timezone_name": "Asia/Shanghai",
                    "misfire_policy": misfire,
                    "overlap_policy": overlap,
                },
            )


class Migration(migrations.Migration):
    dependencies = [("jobs", "0004_backgroundjob_active_coalescing_key")]

    operations = [
        migrations.CreateModel(
            name="ScheduledProducerConfig",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("producer_key", models.CharField(max_length=64, unique=True)),
                ("enabled", models.BooleanField(default=True)),
                ("config_version", models.PositiveIntegerField(default=1)),
                ("last_checked_at", models.DateTimeField(blank=True, null=True)),
                ("last_enqueued_at", models.DateTimeField(blank=True, null=True)),
                ("last_error_category", models.CharField(blank=True, max_length=64)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"verbose_name": "自动任务生产者", "verbose_name_plural": "自动任务生产者"},
        ),
        migrations.CreateModel(
            name="ScheduledProducerRuleConfig",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("rule_type", models.CharField(max_length=64)),
                ("rule_key", models.CharField(max_length=64)),
                ("enabled", models.BooleanField(default=True)),
                (
                    "schedule_kind",
                    models.CharField(
                        choices=[("INTERVAL", "固定间隔"), ("CRON", "定时规则")], max_length=16
                    ),
                ),
                ("schedule_expression", models.CharField(max_length=128)),
                ("timezone_name", models.CharField(default="Asia/Shanghai", max_length=64)),
                (
                    "misfire_policy",
                    models.CharField(
                        choices=[
                            ("SKIP", "跳过"),
                            ("COALESCE", "合并"),
                            ("CATCH_UP_LIMITED", "有限补跑"),
                        ],
                        max_length=24,
                    ),
                ),
                (
                    "overlap_policy",
                    models.CharField(
                        choices=[
                            ("ALLOW", "允许重叠"),
                            ("SKIP_WHILE_ACTIVE", "活动时跳过"),
                            ("COALESCE_WHILE_ACTIVE", "活动时合并"),
                        ],
                        max_length=32,
                    ),
                ),
                ("config_version", models.PositiveIntegerField(default=1)),
                ("covered_through_at", models.DateTimeField(blank=True, null=True)),
                ("last_skipped_from_at", models.DateTimeField(blank=True, null=True)),
                ("last_skipped_to_at", models.DateTimeField(blank=True, null=True)),
                ("last_checked_at", models.DateTimeField(blank=True, null=True)),
                ("last_enqueued_at", models.DateTimeField(blank=True, null=True)),
                ("last_error_category", models.CharField(blank=True, max_length=64)),
                ("archived_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "producer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rules",
                        to="jobs.scheduledproducerconfig",
                    ),
                ),
            ],
            options={"verbose_name": "自动任务生产规则", "verbose_name_plural": "自动任务生产规则"},
        ),
        migrations.CreateModel(
            name="ScheduledProducerSlot",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("utc_instance", models.DateTimeField()),
                ("config_version", models.PositiveIntegerField()),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("READY", "待生产"),
                            ("HELD", "等待活动任务"),
                            ("SKIPPED", "已跳过"),
                            ("ENQUEUED", "已入队"),
                            ("FAILED", "失败"),
                        ],
                        default="READY",
                        max_length=16,
                    ),
                ),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                ("error_category", models.CharField(blank=True, max_length=64)),
                ("coalesced_from_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "rule",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="slots",
                        to="jobs.scheduledproducerruleconfig",
                    ),
                ),
            ],
            options={"verbose_name": "自动任务逻辑槽", "verbose_name_plural": "自动任务逻辑槽"},
        ),
        migrations.CreateModel(
            name="ScheduledProducerSlotJob",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "job",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="scheduled_slots",
                        to="jobs.backgroundjob",
                    ),
                ),
                (
                    "slot",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="slot_jobs",
                        to="jobs.scheduledproducerslot",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="scheduledproducerruleconfig",
            constraint=models.UniqueConstraint(
                fields=("producer", "rule_key"), name="uniq_scheduled_rule"
            ),
        ),
        migrations.AddConstraint(
            model_name="scheduledproducerslot",
            constraint=models.UniqueConstraint(
                fields=("rule", "utc_instance"), name="uniq_scheduled_slot"
            ),
        ),
        migrations.AddIndex(
            model_name="scheduledproducerslot",
            index=models.Index(fields=["state", "updated_at"], name="jobs_schedu_state_f4d10d_idx"),
        ),
        migrations.AddConstraint(
            model_name="scheduledproducerslotjob",
            constraint=models.UniqueConstraint(
                fields=("slot", "job"), name="uniq_scheduled_slot_job"
            ),
        ),
        migrations.RunPython(create_default_producers, migrations.RunPython.noop),
    ]
