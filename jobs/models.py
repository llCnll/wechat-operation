from django.db import models


class JobStatus(models.TextChoices):
    PENDING = "PENDING", "待执行"
    RUNNING = "RUNNING", "执行中"
    SUCCEEDED = "SUCCEEDED", "成功"
    FAILED = "FAILED", "失败"
    CANCELED = "CANCELED", "已取消"


class BackgroundJob(models.Model):
    task_type = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=255, unique=True)
    active_coalescing_key = models.CharField(
        max_length=255,
        unique=True,
        null=True,
        blank=True,
        help_text="仅在待执行或运行中占用；终态自动释放。",
    )
    parameter_summary = models.JSONField(default=dict)
    checkpoint = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=JobStatus.choices, default=JobStatus.PENDING)
    retry_count = models.PositiveIntegerField(default=0)
    max_retries = models.PositiveIntegerField(default=3)
    next_run_at = models.DateTimeField(null=True, blank=True)
    error_summary = models.CharField(max_length=500, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["status", "next_run_at"])]
        verbose_name = "后台任务"
        verbose_name_plural = "后台任务"

    def __str__(self) -> str:
        return f"{self.task_type}:{self.idempotency_key}"


class ScheduledProducerConfig(models.Model):
    """The persisted, operator-controlled state for one built-in producer."""

    producer_key = models.CharField(max_length=64, unique=True)
    enabled = models.BooleanField(default=True)
    config_version = models.PositiveIntegerField(default=1)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_enqueued_at = models.DateTimeField(null=True, blank=True)
    last_error_category = models.CharField(max_length=64, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "自动任务生产者"
        verbose_name_plural = "自动任务生产者"

    def __str__(self) -> str:
        return self.producer_key


class ScheduledProducerRuleConfig(models.Model):
    class ScheduleKind(models.TextChoices):
        INTERVAL = "INTERVAL", "固定间隔"
        CRON = "CRON", "定时规则"

    class MisfirePolicy(models.TextChoices):
        SKIP = "SKIP", "跳过"
        COALESCE = "COALESCE", "合并"
        CATCH_UP_LIMITED = "CATCH_UP_LIMITED", "有限补跑"

    class OverlapPolicy(models.TextChoices):
        ALLOW = "ALLOW", "允许重叠"
        SKIP_WHILE_ACTIVE = "SKIP_WHILE_ACTIVE", "活动时跳过"
        COALESCE_WHILE_ACTIVE = "COALESCE_WHILE_ACTIVE", "活动时合并"

    producer = models.ForeignKey(
        ScheduledProducerConfig, on_delete=models.CASCADE, related_name="rules"
    )
    rule_type = models.CharField(max_length=64)
    rule_key = models.CharField(max_length=64)
    enabled = models.BooleanField(default=True)
    schedule_kind = models.CharField(max_length=16, choices=ScheduleKind.choices)
    schedule_expression = models.CharField(max_length=128)
    timezone_name = models.CharField(max_length=64, default="Asia/Shanghai")
    misfire_policy = models.CharField(max_length=24, choices=MisfirePolicy.choices)
    overlap_policy = models.CharField(max_length=32, choices=OverlapPolicy.choices)
    config_version = models.PositiveIntegerField(default=1)
    covered_through_at = models.DateTimeField(null=True, blank=True)
    last_skipped_from_at = models.DateTimeField(null=True, blank=True)
    last_skipped_to_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_enqueued_at = models.DateTimeField(null=True, blank=True)
    last_error_category = models.CharField(max_length=64, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("producer", "rule_key"), name="uniq_scheduled_rule")
        ]
        verbose_name = "自动任务生产规则"
        verbose_name_plural = "自动任务生产规则"

    def __str__(self) -> str:
        return f"{self.producer.producer_key}:{self.rule_key}"


class ScheduledProducerSlot(models.Model):
    class State(models.TextChoices):
        READY = "READY", "待生产"
        HELD = "HELD", "等待活动任务"
        SKIPPED = "SKIPPED", "已跳过"
        ENQUEUED = "ENQUEUED", "已入队"
        FAILED = "FAILED", "失败"

    rule = models.ForeignKey(
        ScheduledProducerRuleConfig, on_delete=models.CASCADE, related_name="slots"
    )
    utc_instance = models.DateTimeField()
    config_version = models.PositiveIntegerField()
    state = models.CharField(max_length=16, choices=State.choices, default=State.READY)
    attempt_count = models.PositiveIntegerField(default=0)
    error_category = models.CharField(max_length=64, blank=True)
    coalesced_from_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("rule", "utc_instance"), name="uniq_scheduled_slot")
        ]
        indexes = [models.Index(fields=("state", "updated_at"))]
        verbose_name = "自动任务逻辑槽"
        verbose_name_plural = "自动任务逻辑槽"


class ScheduledProducerSlotJob(models.Model):
    slot = models.ForeignKey(
        ScheduledProducerSlot, on_delete=models.CASCADE, related_name="slot_jobs"
    )
    job = models.ForeignKey(BackgroundJob, on_delete=models.PROTECT, related_name="scheduled_slots")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("slot", "job"), name="uniq_scheduled_slot_job")
        ]
