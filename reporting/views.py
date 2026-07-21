"""Operations analysis views and its explicit asynchronous metric refresh action."""

from datetime import date, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from posts.game_catalog import active_game_appid, managed_game_options
from posts.ingestion_jobs import enqueue_metrics_refresh_window, latest_metrics_refresh_job

from .metric_workspace import WorkspacePreferences, build_metric_workspace
from .services import (
    analysis_datetime_range,
    build_live_operations_analysis,
    default_analysis_date_range,
    validate_analysis_date_range,
)


def _requested_analysis_range(start_value: str, end_value: str) -> tuple[date, date, str]:
    default_start, default_end = default_analysis_date_range(timezone.localdate())
    if not start_value and not end_value:
        return default_start, default_end, ""
    try:
        if not start_value or not end_value:
            raise ValueError
        start_day = date.fromisoformat(start_value)
        end_day = date.fromisoformat(end_value)
        return *validate_analysis_date_range(start_day, end_day), ""
    except ValueError:
        return default_start, default_end, "日期范围无效，已显示最近 7 天数据。"


def _analysis_query(*, start_day: date, end_day: date, appid: str) -> str:
    return urlencode(
        {"start_date": start_day.isoformat(), "end_date": end_day.isoformat(), "appid": appid}
    )


def _quick_ranges(
    today: date, selected_start: date, selected_end: date
) -> tuple[dict[str, date | str | bool], ...]:
    data_day = today - timedelta(days=1)
    ranges = (
        ("昨天", data_day, data_day),
        ("近 7 天", data_day - timedelta(days=6), data_day),
        ("近 30 天", data_day - timedelta(days=29), data_day),
    )
    return tuple(
        {
            "label": label,
            "start_date": start_day,
            "end_date": end_day,
            "active": (start_day, end_day) == (selected_start, selected_end),
        }
        for label, start_day, end_day in ranges
    )


@login_required
def live_operations_analysis(request):
    start_day, end_day, range_error = _requested_analysis_range(
        request.GET.get("start_date", "").strip(), request.GET.get("end_date", "").strip()
    )
    appid = active_game_appid(request.GET.get("appid", "").strip(), persist_default=False)
    games = managed_game_options(persist_default=False)
    start_at, end_at = analysis_datetime_range(start_day, end_day)
    analysis = build_live_operations_analysis(start_day, end_day, appid)
    workspace_preferences = WorkspacePreferences.from_query(
        fans_view=request.GET.get("fans_view", ""),
        daily_view=request.GET.get("daily_view", ""),
        daily_metrics=",".join(
            request.GET.getlist("daily_metrics") or [request.GET.get("daily_metric", "")]
        ),
        daily_scale=request.GET.get("daily_scale", ""),
    )
    return render(
        request,
        "reporting/live_operations_analysis.html",
        {
            "analysis": analysis,
            "metric_workspace": build_metric_workspace(
                start_day=start_day,
                end_day=end_day,
                appid=appid,
                preferences=workspace_preferences,
            ),
            "start_date": start_day,
            "end_date": end_day,
            "range_error": range_error,
            "appid": appid,
            "managed_games": games,
            "active_game": next((game for game in games if game.appid == appid), None),
            "quick_ranges": _quick_ranges(timezone.localdate(), start_day, end_day),
            "metrics_refresh_job": latest_metrics_refresh_job(
                appid=appid,
                start_time=int(start_at.timestamp()),
                end_time=int(end_at.timestamp()),
            ),
        },
    )


@login_required
@require_POST
def refresh_operations_analysis_metrics(request):
    start_day, end_day, range_error = _requested_analysis_range(
        request.POST.get("start_date", "").strip(), request.POST.get("end_date", "").strip()
    )
    appid = active_game_appid(request.POST.get("appid", "").strip(), persist_default=False)
    query = _analysis_query(start_day=start_day, end_day=end_day, appid=appid)
    if range_error:
        messages.error(request, "互动刷新未创建：请先选择有效的日期范围。")
        return redirect(f"{reverse('live-operations-analysis')}?{query}")
    start_at, end_at = analysis_datetime_range(start_day, end_day)
    result = enqueue_metrics_refresh_window(
        appid=appid,
        start_time=int(start_at.timestamp()),
        end_time=int(end_at.timestamp()),
    )
    if result.created:
        messages.success(request, f"已创建互动刷新任务 #{result.job.pk}，采集 Worker 将异步执行。")
    else:
        messages.info(request, f"该范围已有互动刷新任务 #{result.job.pk} 正在处理。")
    return redirect(f"{reverse('live-operations-analysis')}?{query}")
