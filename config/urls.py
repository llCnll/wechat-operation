from datetime import UTC, timedelta

from django.contrib import admin
from django.http import JsonResponse
from django.urls import path
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from config import supervisor_views
from config.settings import RUNTIME_DIR
from posts import views as post_views
from posts.models import ActiveGameScope, GameCircleLoginRuntimeState
from reporting import views as reporting_views
from service_supervisor.runtime import RuntimeStore

admin.site.site_header = "游戏圈运营"
admin.site.site_title = "系统配置"
admin.site.index_title = "系统配置与追溯台"
admin.site.enable_nav_sidebar = False


def health_check(request):
    return JsonResponse({"status": "ok"})


def _frame_status() -> tuple[bool, str, str]:
    """Read the current game's lease from local, credential-free runtime state only."""
    scope = ActiveGameScope.objects.filter(pk=1).only("active_game").first()
    if scope is None:
        return False, "NO_ACTIVE_SCOPE", "尚未配置当前操作游戏"

    store = RuntimeStore(RUNTIME_DIR)
    try:
        heartbeat = store.read_json(store.heartbeat_path("game_circle_session"), {})
    except OSError:
        return False, "SERVICE_UNAVAILABLE", "游戏圈会话服务状态暂不可读取"
    checked_at = parse_datetime(heartbeat.get("at", "")) if isinstance(heartbeat, dict) else None
    if checked_at is None:
        return False, "SERVICE_UNAVAILABLE", "游戏圈会话服务未运行"
    if timezone.is_naive(checked_at):
        checked_at = timezone.make_aware(checked_at, UTC)
    if checked_at < timezone.now() - timedelta(minutes=1):
        return False, "SERVICE_UNAVAILABLE", "游戏圈会话服务未运行"

    try:
        projection = store.read_json(store.game_circle_session_path, {})
    except OSError:
        return False, "SERVICE_UNAVAILABLE", "游戏圈会话服务状态暂不可读取"
    leases = projection.get("leases", []) if isinstance(projection, dict) else []
    if any(
        isinstance(lease, dict)
        and lease.get("appid") == scope.active_appid
        and lease.get("state") == "ACTIVE"
        for lease in leases
    ):
        return True, "READY", "当前操作游戏的来源 Frame 已就绪"
    return False, "NOT_READY", "当前操作游戏尚无可用来源 Frame"


def browser_login_status(request):
    state = GameCircleLoginRuntimeState.objects.filter(pk=1).first()
    frame_valid, frame_state, frame_reason = _frame_status()
    frame_can_recover = frame_state == "NOT_READY"
    stale = state is None or state.checked_at is None or state.checked_at < timezone.now() - timedelta(minutes=10)
    if stale:
        return JsonResponse(
            {
                "valid": False,
                "can_open_login": False,
                "reason": "状态待检查",
                "state": "UNKNOWN",
                "frame_valid": frame_valid,
                "frame_state": frame_state,
                "frame_reason": frame_reason,
                "frame_can_recover": frame_can_recover,
            }
        )
    valid = state.login_state == GameCircleLoginRuntimeState.LoginState.READY
    reasons = {
        GameCircleLoginRuntimeState.LoginState.READY: "已检测到有效的受控微信外层会话",
        GameCircleLoginRuntimeState.LoginState.SCAN_REQUIRED: "微信页面等待扫码登录",
        GameCircleLoginRuntimeState.LoginState.UNAVAILABLE: "受控 Chrome 未启动",
    }
    return JsonResponse(
        {
            "valid": valid,
            "can_open_login": state.login_state
            == GameCircleLoginRuntimeState.LoginState.SCAN_REQUIRED,
            "reason": reasons.get(state.login_state, "状态待检查"),
            "state": state.login_state,
            "frame_valid": frame_valid,
            "frame_state": frame_state,
            "frame_reason": frame_reason,
            "frame_can_recover": frame_can_recover,
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
        "admin/runtime/login-probe/",
        supervisor_views.enqueue_login_probe_request,
        name="enqueue-login-probe",
    ),
    path(
        "admin/runtime/session-recover/",
        supervisor_views.enqueue_session_recover_request,
        name="enqueue-session-recover",
    ),
    path(
        "admin/runtime/session-keepalive/",
        supervisor_views.enqueue_session_keepalive_request,
        name="enqueue-session-keepalive",
    ),
    path(
        "admin/runtime/configuration/",
        supervisor_views.save_runtime_configuration,
        name="save-runtime-configuration",
    ),
    path("", post_views.operations_dashboard, name="operations-dashboard"),
    path("mute-management/", post_views.mute_management, name="mute-management"),
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
