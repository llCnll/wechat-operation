"""Operations analysis views and its explicit asynchronous metric refresh action."""

from datetime import date, timedelta
from urllib.parse import quote, urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Prefetch
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from posts.game_catalog import active_game_appid, managed_game_options
from posts.ingestion_jobs import enqueue_metrics_refresh_window, latest_metrics_refresh_job

from .metric_workspace import WorkspacePreferences, build_metric_workspace
from .models import (
    PublicOpinionReport,
    PublicOpinionReportRun,
    PublicOpinionReportRunStatus,
    PublicOpinionReportSource,
)
from .public_opinion_analysis import feedback_value_display
from .public_opinion_jobs import (
    create_public_opinion_report as create_report_job,
)
from .public_opinion_jobs import (
    default_report_range,
    validate_report_range,
)
from .public_opinion_jobs import (
    rerun_public_opinion_report as rerun_report_job,
)
from .public_opinion_presentation import present_daily_metrics, summarize_daily_metrics
from .public_opinion_word import render_public_opinion_word
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


def _report_scope(request) -> tuple[str, tuple]:
    appid = active_game_appid(request.GET.get("appid", "").strip(), persist_default=False)
    games = managed_game_options(persist_default=False)
    return appid, games


def _report_for_scope(*, report_id: int, appid: str) -> PublicOpinionReport:
    return get_object_or_404(
        PublicOpinionReport,
        pk=report_id,
        source_appid=appid,
        retention_until__gt=timezone.now(),
    )


def _report_payload_for_display(payload: dict) -> dict:
    """Normalize historical presentation fields without rewriting immutable snapshots."""
    if not payload:
        return {}
    summary = dict(payload.get("summary") or {})
    summary["feedback_values"] = [
        {**item, "label": feedback_value_display(item.get("label"))}
        for item in summary.get("feedback_values") or []
        if isinstance(item, dict)
    ]
    representatives = dict(payload.get("representative_posts") or {})
    for key in ("by_total", "by_rate"):
        representatives[key] = [
            {
                **item,
                "engagement_rate_percent": item.get(
                    "engagement_rate_percent",
                    round(float(item.get("engagement_rate") or 0) * 100, 2),
                ),
            }
            for item in representatives.get(key) or []
            if isinstance(item, dict)
        ]
    daily_metrics = present_daily_metrics(payload.get("daily_metrics"))
    return {
        **payload,
        "summary": summary,
        "representative_posts": representatives,
        "daily_metrics": daily_metrics,
        "daily_metric_summaries": summarize_daily_metrics(daily_metrics),
    }


@login_required
def public_opinion_reports(request):
    appid, games = _report_scope(request)
    default_start, default_end = default_report_range()
    try:
        start_day = date.fromisoformat(request.GET.get("start_date", default_start.isoformat()))
        end_day = date.fromisoformat(request.GET.get("end_date", default_end.isoformat()))
        start_day, end_day = validate_report_range(start_day, end_day)
        range_error = ""
    except ValueError:
        start_day, end_day = default_start, default_end
        range_error = "日期范围无效，已恢复最近 30 天。"
    reports = list(
        PublicOpinionReport.objects.filter(source_appid=appid, retention_until__gt=timezone.now())
        .select_related("current_successful_run")
        .prefetch_related(
            Prefetch(
                "runs",
                queryset=PublicOpinionReportRun.objects.select_related("job").order_by(
                    "-run_number"
                ),
            )
        )
    )
    for report in reports:
        report.latest_run = report.runs.all()[0] if report.runs.all() else None
    return render(
        request,
        "reporting/public_opinion_reports.html",
        {
            "reports": reports,
            "appid": appid,
            "managed_games": games,
            "start_date": start_day,
            "end_date": end_day,
            "range_error": range_error,
            "active_game": next((game for game in games if game.appid == appid), None),
        },
    )


@login_required
@require_POST
def create_public_opinion_report(request):
    appid = active_game_appid(request.POST.get("appid", "").strip(), persist_default=False)
    try:
        start_day = date.fromisoformat(request.POST.get("start_date", ""))
        end_day = date.fromisoformat(request.POST.get("end_date", ""))
        result = create_report_job(
            source_appid=appid, start_day=start_day, end_day=end_day, created_by=request.user
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(reverse("public-opinion-reports") + f"?appid={quote(appid)}")
    if result.created:
        messages.success(
            request, f"已创建舆情报告任务 #{result.run.job_id}，等待报告 Worker 执行。"
        )
    else:
        messages.info(request, f"相同范围的报告任务 #{result.run.job_id} 正在处理。")
    return redirect(
        reverse("public-opinion-report-detail", args=(result.report.pk,)) + f"?appid={quote(appid)}"
    )


@login_required
def public_opinion_report_detail(request, report_id: int):
    appid, games = _report_scope(request)
    report = _report_for_scope(report_id=report_id, appid=appid)
    runs = report.runs.select_related("job").all()
    requested_run = request.GET.get("run", "")
    if requested_run.isdigit():
        run = get_object_or_404(runs, run_number=int(requested_run))
    else:
        run = report.current_successful_run or runs.first()
    if run is None:
        raise Http404("报告运行版本不存在")
    payload = (
        _report_payload_for_display(run.result_payload)
        if run.status == PublicOpinionReportRunStatus.SUCCEEDED
        else {}
    )
    return render(
        request,
        "reporting/public_opinion_report_detail.html",
        {
            "report": report,
            "run": run,
            "runs": runs,
            "payload": payload,
            "appid": appid,
            "managed_games": games,
            "active_game": next((game for game in games if game.appid == appid), None),
        },
    )


@login_required
@require_POST
def rerun_public_opinion_report(request, report_id: int):
    appid = active_game_appid(request.POST.get("appid", "").strip(), persist_default=False)
    report = _report_for_scope(report_id=report_id, appid=appid)
    try:
        result = rerun_report_job(report=report)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        outcome = "已创建" if result.created else "已复用"
        messages.success(request, f"报告重跑任务{outcome}（任务 #{result.run.job_id}）。")
    return redirect(
        reverse("public-opinion-report-detail", args=(report.pk,)) + f"?appid={quote(appid)}"
    )


@login_required
def public_opinion_report_sources(request, report_id: int):
    appid, games = _report_scope(request)
    report = _report_for_scope(report_id=report_id, appid=appid)
    if report.source_locked_at is None:
        raise Http404("报告来源尚未锁定")
    sources = PublicOpinionReportSource.objects.filter(report=report, source_kind="CURRENT")
    page = Paginator(sources, 30).get_page(request.GET.get("page"))
    return render(
        request,
        "reporting/public_opinion_report_sources.html",
        {"report": report, "page": page, "appid": appid, "managed_games": games},
    )


@login_required
def export_public_opinion_report_word(request, report_id: int, run_number: int):
    appid, _games = _report_scope(request)
    report = _report_for_scope(report_id=report_id, appid=appid)
    run = get_object_or_404(
        PublicOpinionReportRun,
        report=report,
        run_number=run_number,
        status=PublicOpinionReportRunStatus.SUCCEEDED,
    )
    game_name = next(
        (game.name for game in managed_game_options(persist_default=False) if game.appid == appid),
        "游戏圈",
    )
    content = render_public_opinion_word(report=report, run=run, game_name=game_name)
    filename = f"舆情报告-{report.pk}-运行{run.run_number}-{report.end_day.isoformat()}.docx"
    response = HttpResponse(
        content,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    response["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
    return response
