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
