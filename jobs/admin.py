from django.contrib import admin

from config.admin_support import ReadOnlyAdmin

from .models import BackgroundJob
from .task_types import task_type_label


@admin.register(BackgroundJob)
class BackgroundJobAdmin(ReadOnlyAdmin):
    list_display = (
        "task_type_display",
        "status",
        "retry_count",
        "max_retries",
        "next_run_at",
        "updated_at",
    )
    list_filter = ("status", "task_type")
    search_fields = ("idempotency_key", "error_summary")
    date_hierarchy = "updated_at"

    @admin.display(description="任务类型", ordering="task_type")
    def task_type_display(self, obj: BackgroundJob) -> str:
        return task_type_label(obj.task_type)
