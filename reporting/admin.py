from django.contrib import admin

from config.admin_support import ReadOnlyAdmin

from .models import GameCircleMetricSnapshot


@admin.register(GameCircleMetricSnapshot)
class GameCircleMetricSnapshotAdmin(ReadOnlyAdmin):
    list_display = (
        "source",
        "appid",
        "period_start",
        "period_end",
        "publication_state",
        "scope",
        "captured_at",
    )
    list_filter = ("source", "publication_state", "scope")
    search_fields = ("appid",)
    date_hierarchy = "period_start"
