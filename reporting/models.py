"""Persistent operation metrics and immutable public-opinion report records."""

from django.conf import settings
from django.db import models


class MetricSource(models.TextChoices):
    CIRCLE_FANS_DAILY = "CIRCLE_FANS_DAILY", "游戏圈粉丝（日）"
    GROUP_CONSUME_DAILY = "GROUP_CONSUME_DAILY", "内容消费（日）"
    GROUP_PUBLISH_DAILY = "GROUP_PUBLISH_DAILY", "内容创作（日）"
    GROUP_SHARE_DAILY = "GROUP_SHARE_DAILY", "内容分享（日）"
    GROUP_RETAIN_DAILY = "GROUP_RETAIN_DAILY", "游戏圈活跃用户（日）"
    CONTENT_FANS_DAILY = "CONTENT_FANS_DAILY", "内容粉丝（日）"
    OPERATION_EFFECT_WEEKLY = "OPERATION_EFFECT_WEEKLY", "运营效果（周）"


class MetricPublicationState(models.TextChoices):
    VERIFIED = "VERIFIED", "已验证"
    PENDING_RECHECK = "PENDING_RECHECK", "待稳定性复核"


class GameCircleMetricSnapshot(models.Model):
    """One source's aggregate metrics for one business period and scope."""

    appid = models.CharField(max_length=64)
    source = models.CharField(max_length=40, choices=MetricSource.choices)
    period_start = models.DateField()
    period_end = models.DateField()
    scope = models.CharField(max_length=16, default="ALL")
    metrics = models.JSONField(default=dict)
    publication_state = models.CharField(
        max_length=24,
        choices=MetricPublicationState.choices,
        default=MetricPublicationState.VERIFIED,
        db_default=MetricPublicationState.VERIFIED,
    )
    captured_at = models.DateTimeField()
    latest_job = models.ForeignKey(
        "jobs.BackgroundJob",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="metric_snapshots",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("appid", "source", "period_start", "period_end", "scope"),
                name="reporting_metric_snapshot_business_key",
            )
        ]
        indexes = [
            models.Index(fields=("appid", "source", "period_start")),
            models.Index(fields=("source", "captured_at")),
        ]
        ordering = ("-period_start", "source")
        verbose_name = "游戏圈指标快照"
        verbose_name_plural = "游戏圈指标快照"

    def __str__(self) -> str:
        return f"{self.appid}:{self.source}:{self.period_start}"


class MetricJobLease(models.Model):
    """One active manual metric range per semantic request key."""

    dedupe_key = models.CharField(max_length=255, unique=True)
    active_job = models.ForeignKey(
        "jobs.BackgroundJob",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="metric_job_leases",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "指标任务租约"
        verbose_name_plural = "指标任务租约"


class PublicOpinionReport(models.Model):
    """A stable report scope whose sources are locked by its first execution."""

    source_appid = models.CharField(max_length=128)
    start_day = models.DateField()
    end_day = models.DateField()
    status_rule_version = models.CharField(max_length=32)
    analysis_version = models.CharField(max_length=64)
    template_version = models.CharField(max_length=64)
    source_locked_at = models.DateTimeField(null=True, blank=True)
    retention_until = models.DateTimeField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="public_opinion_reports",
    )
    current_successful_run = models.ForeignKey(
        "PublicOpinionReportRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="current_for_reports",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(start_day__lte=models.F("end_day")),
                name="public_opinion_report_valid_days",
            )
        ]
        indexes = [
            models.Index(fields=("source_appid", "created_at")),
            models.Index(fields=("retention_until",)),
        ]
        ordering = ("-created_at", "-pk")
        verbose_name = "舆情报告"
        verbose_name_plural = "舆情报告"


class PublicOpinionReportSourceKind(models.TextChoices):
    CURRENT = "CURRENT", "本期来源"
    BASELINE = "BASELINE", "对比基线"


class PublicOpinionReportSource(models.Model):
    """The protected, immutable text and AI input for one selected post."""

    report = models.ForeignKey(
        PublicOpinionReport, on_delete=models.CASCADE, related_name="sources"
    )
    original_post = models.ForeignKey(
        "posts.OriginalPost",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="public_opinion_report_sources",
    )
    source_kind = models.CharField(max_length=16, choices=PublicOpinionReportSourceKind.choices)
    topic_id = models.BigIntegerField()
    sort_index = models.PositiveIntegerField()
    publish_time = models.DateTimeField(null=True, blank=True)
    title = models.CharField(max_length=500, blank=True)
    content_text = models.TextField(blank=True)
    topics = models.JSONField(default=list, blank=True)
    ballot_text = models.TextField(blank=True)
    content_fingerprint = models.CharField(max_length=64)
    has_ai_assessment = models.BooleanField(default=False)
    ai_category = models.CharField(max_length=32, blank=True)
    ai_sentiment = models.CharField(max_length=32, blank=True)
    ai_feedback_value = models.CharField(max_length=32, blank=True)
    ai_feedback_state = models.CharField(max_length=16, blank=True, default="MISSING")
    official_identity = models.CharField(max_length=32, blank=True, default="PLAYER")
    ai_risk_summary = models.JSONField(default=list)
    ai_model_version = models.CharField(max_length=200, blank=True)
    ai_prompt_version = models.CharField(max_length=100, blank=True)
    ai_assessed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("report", "source_kind", "topic_id"),
                name="public_opinion_report_source_topic",
            ),
            models.UniqueConstraint(
                fields=("report", "source_kind", "sort_index"),
                name="public_opinion_report_source_order",
            ),
        ]
        indexes = [
            models.Index(fields=("report", "source_kind", "sort_index")),
            models.Index(fields=("report", "topic_id")),
        ]
        ordering = ("source_kind", "sort_index")
        verbose_name = "舆情报告来源"
        verbose_name_plural = "舆情报告来源"


class PublicOpinionReportRunStatus(models.TextChoices):
    PENDING = "PENDING", "待执行"
    RUNNING = "RUNNING", "执行中"
    SUCCEEDED = "SUCCEEDED", "成功"
    FAILED = "FAILED", "失败"


class PublicOpinionReportRun(models.Model):
    """One immutable analysis output for a stable report source set."""

    report = models.ForeignKey(PublicOpinionReport, on_delete=models.CASCADE, related_name="runs")
    run_number = models.PositiveIntegerField()
    job = models.OneToOneField(
        "jobs.BackgroundJob",
        on_delete=models.PROTECT,
        related_name="public_opinion_run",
    )
    status = models.CharField(
        max_length=16,
        choices=PublicOpinionReportRunStatus.choices,
        default=PublicOpinionReportRunStatus.PENDING,
    )
    metrics_as_of = models.DateTimeField(null=True, blank=True)
    input_summary = models.JSONField(default=dict)
    data_quality = models.JSONField(default=dict)
    deterministic_payload = models.JSONField(default=dict)
    result_payload = models.JSONField(default=dict)
    conclusion_status = models.CharField(max_length=24, blank=True, default="PENDING")
    conclusion_payload = models.JSONField(default=dict)
    conclusion_attempted_at = models.DateTimeField(null=True, blank=True)
    conclusion_input_hash = models.CharField(max_length=64, blank=True)
    conclusion_error_code = models.CharField(max_length=64, blank=True)
    error_summary = models.CharField(max_length=500, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("report", "run_number"), name="public_opinion_report_run_number"
            )
        ]
        indexes = [models.Index(fields=("report", "status", "run_number"))]
        ordering = ("-run_number",)
        verbose_name = "舆情报告运行版本"
        verbose_name_plural = "舆情报告运行版本"
