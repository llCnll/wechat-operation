from datetime import timedelta

from django.contrib import admin
from django.http import JsonResponse
from django.urls import path, re_path
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
    # Freshness means that the session service has recently observed the outer
    # page, regardless of whether that observation proved a valid login or a
    # required scan.  ``outer_verified_at`` deliberately records only a
    # successful getappinfo verification, so using it here would hide a fresh,
    # confirmed LOGIN_REQUIRED state as an indeterminate one.
    stale = state is None or state.checked_at < timezone.now() - timedelta(minutes=10)
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
        GameCircleLoginRuntimeState.OuterState.LOGIN_REQUIRED: "微信登录态已失效，请重新扫码",
        GameCircleLoginRuntimeState.OuterState.BROWSER_UNAVAILABLE: "受控 Chrome 未启动",
    }
    if state.error_category == GameCircleLoginRuntimeState.ErrorCategory.OUTER_SESSION_UNREADY:
        reasons[state.outer_state] = "受控会话页未保持可用登录上下文，请查看页面后重新检查"
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
    re_path(
        r"^admin/runtime/ingestion/(?P<action>start|stop|restart)/$",
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
    path(
        "admin/scheduled-producers/<str:producer_key>/run/",
        supervisor_views.run_scheduled_producer_now,
        name="run-scheduled-producer-now",
    ),
    path("admin/scheduled-producers/<int:producer_id>/rules/", supervisor_views.create_scheduled_producer_rule, name="create-scheduled-producer-rule"),
    path("admin/scheduled-producer-rules/<int:rule_id>/", supervisor_views.save_scheduled_producer_rule, name="save-scheduled-producer-rule"),
    path("admin/scheduled-producer-rules/<int:rule_id>/delete/", supervisor_views.delete_scheduled_producer_rule, name="delete-scheduled-producer-rule"),
    path("", post_views.operations_dashboard, name="operations-dashboard"),
    path("mute-management/", post_views.mute_management, name="mute-management"),
    path("official-authors/", post_views.official_author_management, name="official-author-management"),
    path("official-authors/preview/", post_views.preview_official_author, name="preview-official-author"),
    path("official-authors/confirm/", post_views.confirm_official_author, name="confirm-official-author"),
    path("official-authors/<int:author_id>/disable/", post_views.disable_official_author_view, name="disable-official-author"),
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
        "mute-management/import-platform-blocked-users/",
        post_views.enqueue_blocked_user_list_sync_request,
        name="enqueue-blocked-user-list-sync",
    ),
    path(
        "mute-management/permanent/",
        post_views.apply_permanent_mute_intent,
        name="apply-permanent-mute-intent",
    ),
    path(
        "mute-management/<int:status_id>/operate/<str:action>/",
        post_views.confirm_author_mute_operation,
        name="confirm-author-mute-operation",
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
    path("public-opinion-reports/", reporting_views.public_opinion_reports, name="public-opinion-reports"),
    path("public-opinion-reports/create/", reporting_views.create_public_opinion_report, name="create-public-opinion-report"),
    path("public-opinion-reports/<int:report_id>/", reporting_views.public_opinion_report_detail, name="public-opinion-report-detail"),
    path("public-opinion-reports/<int:report_id>/rerun/", reporting_views.rerun_public_opinion_report, name="rerun-public-opinion-report"),
    path("public-opinion-reports/<int:report_id>/sources/", reporting_views.public_opinion_report_sources, name="public-opinion-report-sources"),
    path("public-opinion-reports/<int:report_id>/runs/<int:run_number>/word/", reporting_views.export_public_opinion_report_word, name="export-public-opinion-report-word"),
    path("posts/<int:topic_id>/", post_views.post_detail, name="operations-post-detail"),
    path(
        "posts/<int:topic_id>/refresh/",
        post_views.refresh_dashboard_post,
        name="refresh-dashboard-post",
    ),
    path(
        "posts/refresh-filtered/",
        post_views.enqueue_filtered_post_refresh_request,
        name="enqueue-filtered-post-refresh",
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
