from datetime import timedelta

from django.contrib import admin
from django.http import JsonResponse
from django.urls import path
from django.utils import timezone

from config import supervisor_views
from posts import views as post_views
from posts.models import GameCircleLoginRuntimeState
from reporting import views as reporting_views

admin.site.site_header = "游戏圈运营"
admin.site.site_title = "系统配置"
admin.site.index_title = "系统配置与追溯台"
admin.site.enable_nav_sidebar = False


def health_check(request):
    return JsonResponse({"status": "ok"})


def browser_login_status(request):
    state = GameCircleLoginRuntimeState.objects.filter(pk=1).first()
    stale = state is None or state.checked_at is None or state.checked_at < timezone.now() - timedelta(minutes=10)
    if stale:
        return JsonResponse(
            {
                "valid": False,
                "can_open_login": False,
                "reason": "状态待检查",
                "state": "UNKNOWN",
                "outer_state": "UNKNOWN",
                "frame_state": "UNKNOWN",
                "frame_valid": False,
                "frame_can_recover": False,
            }
        )
    valid = state.outer_state == GameCircleLoginRuntimeState.OuterState.OUTER_READY
    reasons = {
        GameCircleLoginRuntimeState.OuterState.OUTER_READY: "已检测到有效的受控微信外层会话",
        GameCircleLoginRuntimeState.OuterState.LOGIN_PENDING: "微信页面等待扫码登录",
        GameCircleLoginRuntimeState.OuterState.BROWSER_UNAVAILABLE: "受控 Chrome 未启动",
    }
    return JsonResponse(
        {
            "valid": valid,
            "can_open_login": GameCircleLoginRuntimeState.Action.OPEN_LOGIN in (state.available_actions or []),
            "reason": reasons.get(state.outer_state, "状态待检查"),
            "state": state.outer_state,
            "outer_state": state.outer_state,
            "frame_valid": state.frame_state == GameCircleLoginRuntimeState.FrameState.READY,
            "frame_state": state.frame_state,
            "frame_reason": state.get_error_category_display(),
            "frame_can_recover": GameCircleLoginRuntimeState.Action.BUILD_FRAME in (state.available_actions or []),
            "available_actions": state.available_actions,
            "login_wait_until": state.login_wait_until.isoformat() if state.login_wait_until else None,
        }
    )


urlpatterns = [
    path("healthz", health_check, name="health-check"),
    path("auth/status", browser_login_status, name="browser-login-status"),
    path("auth/open-login/", post_views.open_controlled_login, name="open-controlled-login"),
    path(
        "admin/runtime/services/<str:service>/logs/",
        supervisor_views.service_logs,
        name="supervisor-service-logs",
    ),
    path(
        "admin/runtime/services/<str:service>/<str:action>/",
        supervisor_views.control_service,
        name="supervisor-control-service",
    ),
    path(
        "admin/runtime/ingestion/enqueue/",
        supervisor_views.enqueue_manual_ingestion,
        name="enqueue-manual-ingestion",
    ),
    path(
        "admin/runtime/ingestion/<str:action>/",
        supervisor_views.control_ingestion_workers,
        name="supervisor-control-ingestion-workers",
    ),
    path(
        "admin/runtime/active-game/",
        supervisor_views.set_active_game_scope,
        name="set-active-game-scope",
    ),
    path(
        "admin/runtime/session-check/",
        supervisor_views.check_outer_session,
        name="check-outer-session",
    ),
    path(
        "admin/runtime/current-game-frame/",
        supervisor_views.build_current_game_frame,
        name="build-current-game-frame",
    ),
    path(
        "admin/runtime/session-keepalive/",
        supervisor_views.enqueue_session_keepalive_request,
        name="enqueue-session-keepalive",
    ),
    path(
        "admin/runtime/permanent-mute-renewal/",
        supervisor_views.enqueue_permanent_mute_renewal_request,
        name="enqueue-permanent-mute-renewal",
    ),
    path(
        "admin/runtime/game-catalog/sync/",
        supervisor_views.sync_managed_game_catalog,
        name="sync-managed-game-catalog",
    ),
    path(
        "admin/runtime/configuration/",
        supervisor_views.save_runtime_configuration,
        name="save-runtime-configuration",
    ),
    path("admin/scheduled-producers/<int:producer_id>/", supervisor_views.save_scheduled_producer, name="save-scheduled-producer"),
    path("admin/scheduled-producers/<int:producer_id>/rules/", supervisor_views.create_scheduled_producer_rule, name="create-scheduled-producer-rule"),
    path("admin/scheduled-producer-rules/<int:rule_id>/", supervisor_views.save_scheduled_producer_rule, name="save-scheduled-producer-rule"),
    path("admin/scheduled-producer-rules/<int:rule_id>/delete/", supervisor_views.delete_scheduled_producer_rule, name="delete-scheduled-producer-rule"),
    path("", post_views.operations_dashboard, name="operations-dashboard"),
    path("mute-management/", post_views.mute_management, name="mute-management"),
    path(
        "mute-management/refresh-statuses/",
        post_views.enqueue_author_mute_status_refresh_request,
        name="enqueue-author-mute-status-refresh",
    ),
    path(
        "mute-management/scan-post-authors/",
        post_views.enqueue_post_author_mute_status_sync_request,
        name="enqueue-post-author-mute-status-sync",
    ),
    path(
        "mute-management/permanent/",
        post_views.apply_permanent_mute_intent,
        name="apply-permanent-mute-intent",
    ),
    path(
        "mute-management/<int:status_id>/posts/",
        post_views.author_mute_history,
        name="author-mute-history",
    ),
    path("ai-operation-reviews/", post_views.ai_operation_reviews, name="ai-operation-reviews"),
    path(
        "ai-operation-reviews/<int:review_id>/<str:decision>/",
        post_views.update_ai_operation_review,
        name="update-ai-operation-review",
    ),
    path("reports/", reporting_views.live_operations_analysis, name="live-operations-analysis"),
    path(
        "reports/refresh-metrics/",
        reporting_views.refresh_operations_analysis_metrics,
        name="refresh-operations-analysis-metrics",
    ),
    path("posts/<int:topic_id>/", post_views.post_detail, name="operations-post-detail"),
    path(
        "posts/<int:topic_id>/refresh/",
        post_views.refresh_dashboard_post,
        name="refresh-dashboard-post",
    ),
    path(
        "posts/<int:topic_id>/operate/<str:action>/",
        post_views.confirm_dashboard_operation,
        name="confirm-dashboard-operation",
    ),
    path(
        "posts/batch-ai-recommendations/",
        post_views.execute_batch_ai_recommendations,
        name="batch-ai-recommendations",
    ),
    path(
        "posts/<int:topic_id>/requeue/",
        post_views.requeue_dashboard_post,
        name="requeue-dashboard-post",
    ),
    path(
        "posts/<int:topic_id>/reply-draft/",
        post_views.save_dashboard_reply_draft,
        name="save-dashboard-reply-draft",
    ),
    path("admin/", admin.site.urls),
]
