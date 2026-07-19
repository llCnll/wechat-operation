from django.contrib import admin

from config.admin_support import ReadOnlyAdmin, ReadOnlyTabularInline, operations_dashboard_link
from config.cdp import CdpUnavailableError, list_managed_games

from .game_catalog import sync_managed_games
from .models import CandidatePost, InteractionSnapshot, ManagedGame, OriginalPost, PostMedia


@admin.register(ManagedGame)
class ManagedGameAdmin(ReadOnlyAdmin):
    list_display = ("name", "appid", "last_synced_at", "updated_at")
    search_fields = ("name", "appid")
    actions = ("sync_from_authorized_platform",)

    def get_actions(self, request):
        action = self.sync_from_authorized_platform
        return {action.__name__: (action, action.__name__, action.short_description)}

    @admin.action(description="从受控微信同步可管理游戏", permissions=("view",))
    def sync_from_authorized_platform(self, request, queryset):
        try:
            count = sync_managed_games(list_managed_games())
        except (CdpUnavailableError, ValueError):
            self.message_user(request, "同步失败，已保留本地游戏目录。", level="ERROR")
        else:
            self.message_user(request, f"已同步 {count} 个可管理游戏。")


@admin.register(CandidatePost)
class CandidatePostAdmin(ReadOnlyAdmin):
    list_display = (
        "topic_id",
        "content_id",
        "origin_type",
        "classified_at",
        "updated_at",
    )
    list_filter = ("origin_type",)
    search_fields = ("topic_id", "content_id")


@admin.register(PostMedia)
class PostMediaAdmin(ReadOnlyAdmin):
    list_display = ("post", "media_type", "dimensions", "is_gif")
    list_filter = ("media_type", "is_gif")
    search_fields = ("post__topic_id", "url")

    @admin.display(description="尺寸")
    def dimensions(self, obj):
        if not obj.width or not obj.height:
            return "—"
        return f"{obj.width} × {obj.height}"


@admin.register(InteractionSnapshot)
class InteractionSnapshotAdmin(ReadOnlyAdmin):
    list_display = ("post", "view_count", "love_count", "reply_count", "share_count", "captured_at")
    list_filter = ("source",)
    search_fields = ("post__topic_id",)
    date_hierarchy = "captured_at"


class PostMediaInline(ReadOnlyTabularInline):
    model = PostMedia


class InteractionSnapshotInline(ReadOnlyTabularInline):
    model = InteractionSnapshot


@admin.register(OriginalPost)
class OriginalPostAdmin(ReadOnlyAdmin):
    list_display = ("topic_id", "content_preview", "publish_time", "status", "dashboard_link")
    list_filter = ("source_appid", "status")
    search_fields = ("topic_id", "title", "content_text")
    inlines = (PostMediaInline, InteractionSnapshotInline)

    def get_readonly_fields(self, request, obj=None):
        return (*super().get_readonly_fields(request, obj), "dashboard_link")

    @admin.display(description="正文摘要")
    def content_preview(self, obj):
        content = obj.content_text or obj.title or "图片或短内容帖子"
        return content[:60]

    @admin.display(description="内容管理")
    def dashboard_link(self, obj):
        return operations_dashboard_link(obj.topic_id)
