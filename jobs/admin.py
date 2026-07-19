from django.contrib import admin

from config.admin_support import ReadOnlyAdmin

from .models import BackgroundJob


@admin.register(BackgroundJob)
class BackgroundJobAdmin(ReadOnlyAdmin):
    list_display = (
        "task_type",
        "status",
        "retry_count",
        "max_retries",
        "next_run_at",
        "updated_at",
    )
    list_filter = ("status", "task_type")
    search_fields = ("idempotency_key", "error_summary")
    date_hierarchy = "updated_at"
