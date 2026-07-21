"""Persistent, aggregate-only game-circle metric snapshots."""

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
