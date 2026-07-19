from django.contrib import admin

from config.cdp import CdpUnavailableError, list_managed_games

from .game_catalog import sync_managed_games
from .models import CandidatePost, InteractionSnapshot, ManagedGame, OriginalPost, PostMedia


@admin.register(ManagedGame)
class ManagedGameAdmin(admin.ModelAdmin):
    list_display = ("name", "appid", "last_synced_at", "updated_at")
    search_fields = ("name", "appid")
    actions = ("sync_from_authorized_platform",)

    @admin.action(description="从受控微信同步可管理游戏")
    def sync_from_authorized_platform(self, request, queryset):
        try:
            count = sync_managed_games(list_managed_games())
        except (CdpUnavailableError, ValueError):
            self.message_user(request, "同步失败，已保留本地游戏目录。", level="ERROR")
        else:
            self.message_user(request, f"已同步 {count} 个可管理游戏。")


@admin.register(CandidatePost)
class CandidatePostAdmin(admin.ModelAdmin):
    list_display = ("topic_id", "content_id", "origin_type", "classified_at", "updated_at")
    list_filter = ("origin_type",)
    search_fields = ("topic_id", "content_id")


class PostMediaInline(admin.TabularInline):
    model = PostMedia
    extra = 0


class InteractionSnapshotInline(admin.TabularInline):
    model = InteractionSnapshot
    extra = 0
    readonly_fields = ("captured_at",)


@admin.register(OriginalPost)
class OriginalPostAdmin(admin.ModelAdmin):
    list_display = ("topic_id", "title", "publish_time", "updated_at")
    list_filter = ("source_appid",)
    search_fields = ("topic_id", "title", "content_text")
    readonly_fields = ("topic_id", "content_id", "created_at", "updated_at")
    inlines = (PostMediaInline, InteractionSnapshotInline)
