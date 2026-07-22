"""Authenticated Django proxy for fixed Supervisor controls and runtime settings."""

from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from config.runtime_config import affected_services, update_runtime_config
from config.supervisor import SupervisorClient, SupervisorUnavailableError
from jobs.game_circle_refresh import (
    REFRESH_KEEPALIVE,
    REFRESH_RECOVER,
    enqueue_login_probe,
    enqueue_session_refresh,
)
from posts.game_catalog import active_game_appid
from posts.game_catalog import set_active_game
from posts.ingestion_jobs import SHANGHAI, enqueue_manual_ingestion_window
from posts.models import ManagedGame

INGESTION_WORKER_SERVICES = ("ingestion", "backfill_ingestion")
INGESTION_WORKER_ACTIONS = frozenset(("start", "stop", "restart"))
MAX_MANUAL_INGESTION_HOURS = 31 * 24


def _return_to(request) -> str:
    return request.POST.get("return_to", "/admin/")


@staff_member_required
@require_POST
def control_service(request, service: str, action: str):
    try:
        SupervisorClient().control(service, action)
    except SupervisorUnavailableError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"已提交 {service} 的 {action} 操作。")
    return redirect(_return_to(request))


@staff_member_required
@require_POST
def control_ingestion_workers(request, action: str):
    """Apply a fixed lifecycle action to both ingestion lanes and expose partial failures."""
    if action not in INGESTION_WORKER_ACTIONS:
        messages.error(request, "不支持帖子采集 Worker 操作。")
        return redirect(_return_to(request))

    failures: list[str] = []
    for service in INGESTION_WORKER_SERVICES:
        try:
            SupervisorClient().control(service, action)
        except SupervisorUnavailableError as exc:
            failures.append(f"{service}：{exc}")
    if failures:
        messages.error(request, f"帖子采集 Worker {action} 未完全成功：{'；'.join(failures)}")
    else:
        messages.success(request, f"已提交帖子采集 Worker 的 {action} 操作。")
    return redirect(_return_to(request))


@staff_member_required
@require_POST
def enqueue_manual_ingestion(request):
    """Create a bounded historical ingestion task for the backfill Worker."""
    appid = request.POST.get("appid", "").strip()
    try:
        hours = int(request.POST.get("hours", ""))
    except (TypeError, ValueError):
        messages.error(request, "补采时长必须是正整数小时。")
        return redirect(_return_to(request))

    if not 1 <= hours <= MAX_MANUAL_INGESTION_HOURS:
        messages.error(request, f"补采时长须在 1 至 {MAX_MANUAL_INGESTION_HOURS} 小时之间。")
        return redirect(_return_to(request))

    try:
        active_game_appid(appid, persist_default=False)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(_return_to(request))
    game = ManagedGame.objects.filter(appid=appid).only("appid", "name").first()
    if game is None:
        messages.error(request, "当前操作游戏不在本地目录中。")
        return redirect(_return_to(request))

    end_at = timezone.now().astimezone(SHANGHAI).replace(second=0, microsecond=0)
    start_at = end_at - timedelta(hours=hours)
    try:
        result = enqueue_manual_ingestion_window(
            appid=game.appid,
            start_time=int(start_at.timestamp()),
            end_time=int(end_at.timestamp()),
            page_size=20,
        )
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        outcome = "已创建" if result.created else "已存在"
        messages.success(
            request,
            f"{game.name}最近 {hours} 小时的历史补采任务{outcome}（任务 #{result.job.pk}）。",
        )
    return redirect(_return_to(request))


@staff_member_required
@require_POST
def set_active_game_scope(request):
    appid = request.POST.get("appid", "").strip()
    try:
        scope = set_active_game(appid=appid, updated_by=request.user)
        refresh = enqueue_session_refresh(refresh_mode=REFRESH_RECOVER)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        outcome = "已创建" if refresh.created else "已复用"
        messages.success(request, f"当前操作游戏已更新（范围版本 {scope.scope_version}）；Frame 恢复任务{outcome}。")
    return redirect(_return_to(request))


@staff_member_required
@require_POST
def enqueue_login_probe_request(request):
    try:
        refresh = enqueue_login_probe(source="manual")
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        outcome = "已创建" if refresh.created else "已复用"
        messages.success(request, f"外层会话检查任务{outcome}；不会创建当前游戏 Frame。")
    return redirect(_return_to(request))


@staff_member_required
@require_POST
def enqueue_session_recover_request(request):
    try:
        refresh = enqueue_session_refresh(refresh_mode=REFRESH_RECOVER)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        outcome = "已创建" if refresh.created else "已复用"
        messages.success(request, f"当前游戏 Frame 恢复任务{outcome}；完成后请刷新状态确认。")
    return redirect(_return_to(request))


@staff_member_required
@require_POST
def enqueue_session_keepalive_request(request):
    try:
        refresh = enqueue_session_refresh(refresh_mode=REFRESH_KEEPALIVE)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        outcome = "已创建" if refresh.created else "已复用"
        messages.success(request, f"当前游戏 Frame 保活任务{outcome}；不会修改当前操作游戏。")
    return redirect(_return_to(request))


@staff_member_required
@require_GET
def service_logs(request, service: str):
    try:
        lines = SupervisorClient().logs(service)
    except SupervisorUnavailableError as exc:
        return JsonResponse({"error": str(exc)}, status=503)
    return JsonResponse({"service": service, "lines": lines})


@staff_member_required
@require_POST
def save_runtime_configuration(request):
    values = {key: value for key, value in request.POST.items()}
    if request.POST.get("config_scope") == "writes":
        for key in (
            "ENABLE_UPSTREAM_WRITES", "ENABLE_SINK_WRITES", "ENABLE_BLOCK_WRITES",
            "ENABLE_MUTE_WRITES", "ENABLE_UNMUTE_WRITES", "ENABLE_RESTORE_WRITES",
            "ENABLE_AI_AUTOMATED_MODERATION", "ENABLE_AI_SINK_WRITES", "ENABLE_AI_BLOCK_WRITES",
        ):
            values.setdefault(key, "false")
    if request.POST.get("config_scope") == "video":
        values.setdefault("VIDEO_ENABLED", "false")
        values.setdefault("VIDEO_PREFLIGHT_VERIFIED", "false")
        values.setdefault("ASR_ENABLED", "false")
    try:
        changed = update_runtime_config(values)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(_return_to(request))
    services = affected_services(changed)
    if not changed:
        messages.info(request, "运行配置没有变化。")
    elif services:
        messages.success(
            request, f"配置已保存；请重启 {', '.join(sorted(services))} 以加载新配置。"
        )
    else:
        messages.success(request, "配置已保存。")
    return redirect(_return_to(request))
