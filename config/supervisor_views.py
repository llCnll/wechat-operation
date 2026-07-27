"""Authenticated Django proxy for fixed Supervisor controls and runtime settings."""

from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from config.game_circle_ipc import GameCircleIpcClient, GameCircleIpcError
from config.runtime_config import affected_services, display_service_names, update_runtime_config
from config.supervisor import SupervisorClient, SupervisorUnavailableError
from jobs.game_circle_keepalive import enqueue_keepalive
from jobs.models import ScheduledProducerRuleConfig
from jobs.permanent_mute import enqueue_permanent_mute_renewal
from jobs.scheduled_producers import (
    DEFINITION_BY_KEY,
    create_rule_configuration,
    delete_rule_configuration,
    schedule_description_for_rule,
    update_producer_enabled,
    update_rule_configuration,
)
from posts.game_catalog import active_game_appid, set_active_game, sync_authorized_managed_games
from posts.ingestion_jobs import SHANGHAI, enqueue_manual_ingestion_window
from posts.models import ManagedGame

INGESTION_WORKER_SERVICES = ("ingestion", "backfill_ingestion")
INGESTION_WORKER_ACTIONS = frozenset(("start", "stop", "restart"))
MAX_MANUAL_INGESTION_HOURS = 31 * 24
SESSION_CONTROL_MESSAGES = {
    "CHECK_LOGIN": {
        "completed": "已检查外层会话；不会切换游戏或建立 Frame。",
        "pending": "正在等待扫码登录；未执行外层检查。",
        "uncompleted": "外层会话当前未就绪，检查未完成；请根据会话状态处理。",
    },
    "BUILD_CURRENT_GAME_FRAME": {
        "completed": "已请求建立当前游戏 Frame；完成后请刷新状态确认。",
        "pending": "正在等待扫码登录；未执行当前游戏 Frame 建立。",
        "uncompleted": "当前游戏 Frame 尚未建立；请根据会话状态处理。",
    },
}


def _return_to(request) -> str:
    return request.POST.get("return_to", "/admin/")


def _run_session_control(request, method: str) -> None:
    """Submit one fixed control and present only its service-owned outcome."""
    message_set = SESSION_CONTROL_MESSAGES[method]
    try:
        result = GameCircleIpcClient().call(method)
    except GameCircleIpcError as exc:
        messages.error(request, str(exc))
        return
    status = result.get("status") if isinstance(result, dict) else ""
    if status == "LOGIN_PENDING":
        messages.info(request, message_set["pending"])
    elif status == "CONTROL_NOT_COMPLETED":
        messages.warning(request, message_set["uncompleted"])
    else:
        messages.success(request, message_set["completed"])


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
        GameCircleIpcClient().call("BUILD_CURRENT_GAME_FRAME")
    except (ValueError, GameCircleIpcError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request, f"当前操作游戏已更新（范围版本 {scope.scope_version}）；正在建立对应 Frame。"
        )
    return redirect(_return_to(request))


@staff_member_required
@require_POST
def check_outer_session(request):
    _run_session_control(request, "CHECK_LOGIN")
    return redirect(_return_to(request))


@staff_member_required
@require_POST
def build_current_game_frame(request):
    _run_session_control(request, "BUILD_CURRENT_GAME_FRAME")
    return redirect(_return_to(request))


@staff_member_required
@require_POST
def enqueue_session_keepalive_request(request):
    try:
        result = enqueue_keepalive(trigger="manual")
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        outcome = "已创建" if result.created else "已复用"
        messages.success(request, f"当前游戏 Frame 保活任务{outcome}。")
    return redirect(_return_to(request))


@staff_member_required
@require_POST
def enqueue_permanent_mute_renewal_request(request):
    """Queue maintenance; source reads and writes remain in its Worker."""
    try:
        result = enqueue_permanent_mute_renewal(source="manual")
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        outcome = "已创建" if result.created else "已复用"
        messages.success(
            request,
            f"永久禁言续期任务{outcome}（任务 #{result.job.pk}）；Worker 未运行时任务会等待启动。",
        )
    return redirect(_return_to(request))


@staff_member_required
@require_POST
def sync_managed_game_catalog(request):
    try:
        count = sync_authorized_managed_games()
    except (GameCircleIpcError, ValueError):
        messages.error(request, "同步可管理游戏失败，已保留本地目录。")
    else:
        messages.success(request, f"已同步 {count} 个可管理游戏。")
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
            "ENABLE_UPSTREAM_WRITES",
            "ENABLE_SINK_WRITES",
            "ENABLE_BLOCK_WRITES",
            "ENABLE_MUTE_WRITES",
            "ENABLE_UNMUTE_WRITES",
            "ENABLE_RESTORE_WRITES",
            "ENABLE_AI_AUTOMATED_MODERATION",
            "ENABLE_AI_SINK_WRITES",
            "ENABLE_AI_BLOCK_WRITES",
            "SESSION_REPAIR_ENABLED",
        ):
            values.setdefault(key, "false")
    if request.POST.get("config_scope") == "video":
        values.setdefault("VIDEO_ENABLED", "false")
        values.setdefault("VIDEO_PREFLIGHT_VERIFIED", "false")
        values.setdefault("ASR_ENABLED", "false")
    if request.POST.get("config_scope") == "session_maintenance":
        values.setdefault("SESSION_REPAIR_ENABLED", "false")
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
            request,
            f"配置已保存；请重启 {'、'.join(display_service_names(services))} 以加载新配置。",
        )
    else:
        messages.success(request, "配置已保存。")
    return redirect(_return_to(request))


@staff_member_required
@require_POST
def save_scheduled_producer(request, producer_id: int):
    enabled = request.POST.get("enabled", "false").lower() in {"1", "true", "on", "yes"}
    try:
        producer = update_producer_enabled(producer_id=producer_id, enabled=enabled)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        state = "启用" if producer.enabled else "暂停"
        messages.success(
            request,
            f"{DEFINITION_BY_KEY[producer.producer_key].label}已{state}；下轮自动调度生效。",
        )
    return redirect(_return_to(request))


@staff_member_required
@require_POST
def save_scheduled_producer_rule(request, rule_id: int):
    try:
        rule = update_rule_configuration(rule_id=rule_id, values=request.POST)
    except (ValueError, TypeError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"{DEFINITION_BY_KEY[rule.producer.producer_key].label}·{schedule_description_for_rule(rule)}已保存；下轮自动调度生效。",
        )
    return redirect(_return_to(request))


@staff_member_required
@require_POST
def create_scheduled_producer_rule(request, producer_id: int):
    try:
        rule = create_rule_configuration(producer_id=producer_id, values=request.POST)
    except (ValueError, TypeError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"已新增{DEFINITION_BY_KEY[rule.producer.producer_key].label}·{schedule_description_for_rule(rule)}；下轮自动调度生效。",
        )
    return redirect(_return_to(request))


@staff_member_required
@require_POST
def delete_scheduled_producer_rule(request, rule_id: int):
    try:
        delete_rule_configuration(rule_id=rule_id)
    except ScheduledProducerRuleConfig.DoesNotExist:
        messages.error(request, "该规则不存在或已删除。")
    else:
        messages.success(request, "规则已删除；已入队任务不受影响。")
    return redirect(_return_to(request))
