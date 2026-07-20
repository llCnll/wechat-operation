from django.contrib import admin
from django.http import JsonResponse
from django.urls import path

from config import supervisor_views
from config.cdp import CdpUnavailableError, get_browser_login_status
from posts import views as post_views
from reporting import views as reporting_views

admin.site.site_header = "游戏圈运营"
admin.site.site_title = "系统配置"
admin.site.index_title = "系统配置与追溯台"
admin.site.enable_nav_sidebar = False


def health_check(request):
    return JsonResponse({"status": "ok"})


def browser_login_status(request):
    try:
        status = get_browser_login_status()
    except CdpUnavailableError:
        return JsonResponse({"valid": False, "reason": "受控 Chrome 未启动"}, status=503)
    return JsonResponse(
        {"valid": status.valid, "reason": status.reason, "page_title": status.page_title}
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
