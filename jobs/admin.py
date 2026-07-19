from django.contrib import admin

from .models import BackgroundJob


@admin.register(BackgroundJob)
class BackgroundJobAdmin(admin.ModelAdmin):
    list_display = ("task_type", "status", "retry_count", "next_run_at", "updated_at")
    list_filter = ("status", "task_type")
    search_fields = ("idempotency_key",)
    readonly_fields = ("created_at", "updated_at", "started_at", "finished_at")

