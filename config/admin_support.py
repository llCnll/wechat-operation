"""Shared presentation and access rules for the configuration console."""

from urllib.parse import urlencode

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html


class ReadOnlyAdmin(admin.ModelAdmin):
    """Expose operational records for traceability without creating an edit backdoor."""

    actions = None
    readonly_empty_value_display = "—"

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.concrete_fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        return {}


class ReadOnlyTabularInline(admin.TabularInline):
    """Inline records follow the same server-side read-only guarantee."""

    extra = 0
    can_delete = False
    show_change_link = True

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.concrete_fields)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


def operations_dashboard_url(topic_id: int) -> str:
    return f"{reverse('operations-dashboard')}?{urlencode({'topic': topic_id})}"


def operations_dashboard_link(topic_id: int):
    return format_html(
        '<a class="admin-dashboard-link" href="{}">在内容管理中查看 #{}</a>',
        operations_dashboard_url(topic_id),
        topic_id,
    )
