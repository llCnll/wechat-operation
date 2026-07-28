"""Creation, source locking and execution for immutable public-opinion reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.db.models import F, Max
from django.utils import timezone

from jobs.models import BackgroundJob, JobStatus
from jobs.services import JobTerminalError
from jobs.task_types import PUBLIC_OPINION_REPORT_TASK
from posts.models import POST_STATUS_NORMAL, InteractionSnapshot, ManagedGame, OriginalPost
from posts.official_authors import active_official_openids, identity_for_values

from .metric_sources import ENABLED_DAILY_SOURCES
from .models import (
    GameCircleMetricSnapshot,
    MetricPublicationState,
    PublicOpinionReport,
    PublicOpinionReportRun,
    PublicOpinionReportRunStatus,
    PublicOpinionReportSource,
    PublicOpinionReportSourceKind,
)
from .public_opinion_analysis import (
    ANALYSIS_VERSION,
    REPORT_STATUS_RULE_VERSION,
    WORD_TEMPLATE_VERSION,
    InteractionInput,
    ReportSourceInput,
    build_report_result,
    report_algorithm,
)
from .public_opinion_conclusion import (
    ConclusionError,
    PublicOpinionConclusionClient,
    conclusion_snapshot,
    evidence_for_payload,
    evidence_hash,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
REPORT_DEFAULT_DAYS = 30
REPORT_MAX_DAYS = 90
REPORT_MAX_POSTS = 10_000
REPORT_RETENTION_DAYS = 180
REPORT_MAX_RETRIES = 2


@dataclass(frozen=True)
class PublicOpinionEnqueueResult:
    report: PublicOpinionReport
    run: PublicOpinionReportRun
    created: bool


def default_report_range(today: date | None = None) -> tuple[date, date]:
    end_day = today or timezone.localdate()
    return end_day - timedelta(days=REPORT_DEFAULT_DAYS - 1), end_day


def validate_report_range(start_day: date, end_day: date) -> tuple[date, date]:
    if start_day > end_day:
        raise ValueError("开始日期不能晚于结束日期")
    if (end_day - start_day).days + 1 > REPORT_MAX_DAYS:
        raise ValueError(f"报告范围最长为 {REPORT_MAX_DAYS} 天")
    return start_day, end_day


def _range_datetimes(start_day: date, end_day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(start_day, time.min, tzinfo=SHANGHAI),
        datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=SHANGHAI),
    )


def _active_key(*, appid: str, start_day: date, end_day: date, version: str) -> str:
    return (
        f"public-opinion:{appid}:{start_day.isoformat()}:{end_day.isoformat()}:{version}"
    )


def _eligible_post_count(*, appid: str, start_day: date, end_day: date) -> int:
    start_at, end_at = _range_datetimes(start_day, end_day)
    return OriginalPost.objects.filter(
        source_appid=appid,
        status=POST_STATUS_NORMAL,
        publish_time__gte=start_at,
        publish_time__lt=end_at,
    ).count()


def _new_job(*, active_key: str, report: PublicOpinionReport, run_number: int) -> BackgroundJob:
    return BackgroundJob.objects.create(
        task_type=PUBLIC_OPINION_REPORT_TASK,
        idempotency_key=f"public-opinion:{report.pk}:run:{run_number}:{uuid4().hex}",
        active_coalescing_key=active_key,
        parameter_summary={"report_id": report.pk, "run_number": run_number},
        max_retries=REPORT_MAX_RETRIES,
    )


def create_public_opinion_report(
    *, source_appid: str, start_day: date, end_day: date, created_by=None
) -> PublicOpinionEnqueueResult:
    if not source_appid:
        raise ValueError("未选择可管理游戏")
    start_day, end_day = validate_report_range(start_day, end_day)
    if _eligible_post_count(appid=source_appid, start_day=start_day, end_day=end_day) > REPORT_MAX_POSTS:
        raise ValueError(f"报告范围帖子数超过 {REPORT_MAX_POSTS} 条限制")
    active_key = _active_key(appid=source_appid, start_day=start_day, end_day=end_day, version=ANALYSIS_VERSION)
    with transaction.atomic():
        existing = (
            BackgroundJob.objects.select_related("public_opinion_run__report")
            .filter(
                active_coalescing_key=active_key, status__in=(JobStatus.PENDING, JobStatus.RUNNING)
            )
            .first()
        )
        if existing is not None:
            return PublicOpinionEnqueueResult(
                report=existing.public_opinion_run.report,
                run=existing.public_opinion_run,
                created=False,
            )
        report = PublicOpinionReport.objects.create(
            source_appid=source_appid,
            start_day=start_day,
            end_day=end_day,
            status_rule_version=REPORT_STATUS_RULE_VERSION,
            analysis_version=ANALYSIS_VERSION,
            template_version=WORD_TEMPLATE_VERSION,
            retention_until=timezone.now() + timedelta(days=REPORT_RETENTION_DAYS),
            created_by=created_by,
        )
        try:
            with transaction.atomic():
                job = _new_job(active_key=active_key, report=report, run_number=1)
        except IntegrityError:
            existing = BackgroundJob.objects.select_related("public_opinion_run__report").get(
                active_coalescing_key=active_key
            )
            return PublicOpinionEnqueueResult(
                report=existing.public_opinion_run.report,
                run=existing.public_opinion_run,
                created=False,
            )
        run = PublicOpinionReportRun.objects.create(report=report, run_number=1, job=job)
    return PublicOpinionEnqueueResult(report=report, run=run, created=True)


def rerun_public_opinion_report(*, report: PublicOpinionReport) -> PublicOpinionEnqueueResult:
    if report.source_locked_at is None:
        raise ValueError("报告来源尚未锁定，不能重跑")
    if report.retention_until <= timezone.now():
        raise ValueError("报告来源已到期，不能重跑")
    active_key = _active_key(
        appid=report.source_appid, start_day=report.start_day, end_day=report.end_day, version=report.analysis_version
    )
    with transaction.atomic():
        report = PublicOpinionReport.objects.select_for_update().get(pk=report.pk)
        active = (
            BackgroundJob.objects.select_related("public_opinion_run")
            .filter(
                active_coalescing_key=active_key, status__in=(JobStatus.PENDING, JobStatus.RUNNING)
            )
            .first()
        )
        if active is not None:
            return PublicOpinionEnqueueResult(
                report=report, run=active.public_opinion_run, created=False
            )
        run_number = (report.runs.aggregate(maximum=Max("run_number"))["maximum"] or 0) + 1
        try:
            with transaction.atomic():
                job = _new_job(active_key=active_key, report=report, run_number=run_number)
        except IntegrityError:
            active = BackgroundJob.objects.select_related("public_opinion_run").get(
                active_coalescing_key=active_key
            )
            return PublicOpinionEnqueueResult(
                report=report, run=active.public_opinion_run, created=False
            )
        run = PublicOpinionReportRun.objects.create(report=report, run_number=run_number, job=job)
    return PublicOpinionEnqueueResult(report=report, run=run, created=True)


def _fingerprint(*, title: str, content_text: str, ballot_text: str) -> str:
    return sha256("\x1f".join((title, content_text, ballot_text)).encode()).hexdigest()


def _source_rows(
    *, report: PublicOpinionReport, posts: list[OriginalPost], source_kind: str
) -> list[PublicOpinionReportSource]:
    rows: list[PublicOpinionReportSource] = []
    game_name = ManagedGame.objects.filter(appid=report.source_appid).values_list("name", flat=True).first() or ""
    official_openids = active_official_openids(appid=report.source_appid)
    for index, post in enumerate(posts, start=1):
        assessment = getattr(getattr(post, "review_task", None), "assessment", None)
        risks = tuple(assessment.risk_evidence.all()) if assessment is not None else ()
        title = post.title or ""
        content_text = post.content_text or ""
        ballot_text = post.ballot_text or ""
        identity = identity_for_values(author_openid=post.author_openid, author_nick_name=post.author_nick_name, game_name=game_name, verified_openids=official_openids)
        feedback = (assessment.feedback_value if assessment is not None else "").strip().upper()
        feedback_state = "VALID" if feedback in {"NONE", "LOW", "MEDIUM", "HIGH"} else ("INVALID" if feedback else "MISSING")
        rows.append(
            PublicOpinionReportSource(
                report=report,
                original_post=post,
                source_kind=source_kind,
                topic_id=post.topic_id,
                sort_index=index,
                publish_time=post.publish_time,
                title=title,
                content_text=content_text,
                topics=list(post.topics or []),
                ballot_text=ballot_text,
                content_fingerprint=_fingerprint(
                    title=title, content_text=content_text, ballot_text=ballot_text
                ),
                has_ai_assessment=assessment is not None,
                ai_category=assessment.category if assessment is not None else "",
                ai_sentiment=assessment.sentiment if assessment is not None else "",
                ai_feedback_value=feedback if feedback_state == "VALID" else "",
                ai_feedback_state=feedback_state,
                official_identity=identity.key,
                ai_risk_summary=[
                    {"label": risk.label, "confidence": risk.confidence} for risk in risks
                ],
                ai_model_version=assessment.model_version if assessment is not None else "",
                ai_prompt_version=assessment.prompt_version if assessment is not None else "",
                ai_assessed_at=assessment.created_at if assessment is not None else None,
            )
        )
    return rows


def _locked_posts(
    *, report: PublicOpinionReport, start_day: date, end_day: date
) -> list[OriginalPost]:
    start_at, end_at = _range_datetimes(start_day, end_day)
    return list(
        OriginalPost.objects.filter(
            source_appid=report.source_appid,
            status=POST_STATUS_NORMAL,
            publish_time__gte=start_at,
            publish_time__lt=end_at,
        )
        .select_related("review_task__assessment")
        .prefetch_related("review_task__assessment__risk_evidence")
        .order_by("publish_time", "pk")[: REPORT_MAX_POSTS + 1]
    )


def lock_report_sources(*, report_id: int) -> PublicOpinionReport:
    """Atomically freeze current and equal-length baseline source inputs once."""
    with transaction.atomic():
        report = PublicOpinionReport.objects.select_for_update().get(pk=report_id)
        if report.source_locked_at is not None:
            return report
        current_posts = _locked_posts(
            report=report, start_day=report.start_day, end_day=report.end_day
        )
        if len(current_posts) > REPORT_MAX_POSTS:
            raise JobTerminalError(f"报告范围帖子数超过 {REPORT_MAX_POSTS} 条限制")
        duration = report.end_day - report.start_day + timedelta(days=1)
        baseline_end = report.start_day - timedelta(days=1)
        baseline_start = baseline_end - duration + timedelta(days=1)
        baseline_posts = _locked_posts(
            report=report, start_day=baseline_start, end_day=baseline_end
        )
        if len(baseline_posts) > REPORT_MAX_POSTS:
            raise JobTerminalError(f"报告对比基线帖子数超过 {REPORT_MAX_POSTS} 条限制")
        PublicOpinionReportSource.objects.bulk_create(
            [
                *_source_rows(
                    report=report,
                    posts=current_posts,
                    source_kind=PublicOpinionReportSourceKind.CURRENT,
                ),
                *_source_rows(
                    report=report,
                    posts=baseline_posts,
                    source_kind=PublicOpinionReportSourceKind.BASELINE,
                ),
            ]
        )
        report.source_locked_at = timezone.now()
        report.save(update_fields=["source_locked_at", "updated_at"])
    return report


def _source_input(source: PublicOpinionReportSource) -> ReportSourceInput:
    risks = tuple(item for item in source.ai_risk_summary if isinstance(item, dict))
    return ReportSourceInput(
        topic_id=source.topic_id,
        publish_time=source.publish_time,
        title=source.title,
        content_text=source.content_text,
        ballot_text=source.ballot_text,
        has_ai_assessment=source.has_ai_assessment,
        ai_category=source.ai_category,
        ai_sentiment=source.ai_sentiment,
        ai_feedback_value=source.ai_feedback_value,
        ai_risk_summary=risks,
        ai_feedback_state=source.ai_feedback_state or "MISSING",
        topics=tuple(item for item in source.topics if isinstance(item, str)),
        official_identity=source.official_identity or "PLAYER",
    )


def load_report_inputs(
    *, report: PublicOpinionReport
) -> tuple[tuple[ReportSourceInput, ...], tuple[ReportSourceInput, ...]]:
    sources = PublicOpinionReportSource.objects.filter(report=report).order_by(
        "source_kind", "sort_index"
    )
    current = tuple(
        _source_input(item)
        for item in sources
        if item.source_kind == PublicOpinionReportSourceKind.CURRENT
    )
    baseline = tuple(
        _source_input(item)
        for item in sources
        if item.source_kind == PublicOpinionReportSourceKind.BASELINE
    )
    return current, baseline


def load_interactions(
    *, current: tuple[ReportSourceInput, ...], metrics_as_of: datetime
) -> tuple[InteractionInput, ...]:
    topic_ids = {item.topic_id for item in current}
    latest: dict[int, InteractionInput] = {}
    snapshots = (
        InteractionSnapshot.objects.filter(
            post__topic_id__in=topic_ids, captured_at__lte=metrics_as_of
        )
        .select_related("post")
        .order_by("post_id", "-captured_at", "-pk")
    )
    for snapshot in snapshots:
        if snapshot.post.topic_id not in latest:
            latest[snapshot.post.topic_id] = InteractionInput(
                topic_id=snapshot.post.topic_id,
                view_count=snapshot.view_count,
                love_count=snapshot.love_count,
                reply_count=snapshot.reply_count,
                share_count=snapshot.share_count,
                captured_at=snapshot.captured_at,
            )
    return tuple(latest.values())


def load_daily_metrics(*, report: PublicOpinionReport, metrics_as_of: datetime) -> tuple[dict[str, object], ...]:
    snapshots = GameCircleMetricSnapshot.objects.filter(
        appid=report.source_appid, scope="ALL", source__in=ENABLED_DAILY_SOURCES,
        period_start__gte=report.start_day, period_end__lte=report.end_day,
        captured_at__lte=metrics_as_of, publication_state=MetricPublicationState.VERIFIED,
    ).filter(period_start=F("period_end")).order_by("source", "period_start")
    return tuple({"source": item.source, "day": item.period_start.isoformat(), "metrics": item.metrics, "captured_at": item.captured_at.isoformat()} for item in snapshots)


def execute_public_opinion_report(job: BackgroundJob) -> None:
    try:
        run = PublicOpinionReportRun.objects.select_related("report").get(job=job)
    except PublicOpinionReportRun.DoesNotExist as exc:
        raise JobTerminalError("舆情报告任务未关联运行版本") from exc
    report = run.report
    try:
        algorithm = report_algorithm(report.analysis_version)
    except ValueError as exc:
        raise JobTerminalError(str(exc)) from exc
    if report.retention_until <= timezone.now():
        raise JobTerminalError("舆情报告来源已到期")
    PublicOpinionReportRun.objects.filter(pk=run.pk).update(
        status=PublicOpinionReportRunStatus.RUNNING,
        started_at=timezone.now(),
        error_summary="",
    )
    try:
        run.refresh_from_db()
        if run.deterministic_payload:
            payload = dict(run.deterministic_payload["payload"])
            quality = dict(run.data_quality)
            metrics_as_of = run.metrics_as_of
            input_summary = dict(run.deterministic_payload.get("input_summary") or {})
        else:
            report = lock_report_sources(report_id=report.pk)
            current, baseline = load_report_inputs(report=report)
            metrics_as_of = timezone.now()
            interactions = load_interactions(current=current, metrics_as_of=metrics_as_of)
            daily_metrics = load_daily_metrics(report=report, metrics_as_of=metrics_as_of) if algorithm.supports_daily_metrics else ()
            payload, quality = build_report_result(current=current, baseline=baseline, interactions=interactions, source_locked_at=report.source_locked_at, metrics_as_of=metrics_as_of, daily_metrics=daily_metrics, version=report.analysis_version)
            input_summary = {
                "current_source_count": len(current),
                "baseline_source_count": len(baseline),
                "interaction_input_count": len(interactions),
            }
            with transaction.atomic():
                locked_run = PublicOpinionReportRun.objects.select_for_update().get(pk=run.pk)
                if not locked_run.deterministic_payload:
                    locked_run.metrics_as_of = metrics_as_of
                    locked_run.data_quality = quality
                    locked_run.deterministic_payload = {
                        "payload": payload,
                        "input_summary": input_summary,
                    }
                    locked_run.save(update_fields=("metrics_as_of", "data_quality", "deterministic_payload", "updated_at"))
                else:
                    payload = dict(locked_run.deterministic_payload["payload"])
                    quality = dict(locked_run.data_quality)
                    metrics_as_of = locked_run.metrics_as_of
                    input_summary = dict(locked_run.deterministic_payload.get("input_summary") or {})
    except JobTerminalError as exc:
        PublicOpinionReportRun.objects.filter(pk=run.pk).update(
            status=PublicOpinionReportRunStatus.FAILED,
            error_summary=str(exc)[:500],
            finished_at=timezone.now(),
            result_payload={},
        )
        raise
    except Exception as exc:
        final_attempt = job.retry_count + 1 > job.max_retries
        PublicOpinionReportRun.objects.filter(pk=run.pk).update(
            status=(
                PublicOpinionReportRunStatus.FAILED
                if final_attempt
                else PublicOpinionReportRunStatus.PENDING
            ),
            error_summary=str(exc)[:500],
            finished_at=timezone.now() if final_attempt else None,
            result_payload={},
        )
        raise
    conclusion = {"status": "NOT_APPLICABLE", "items": []}
    conclusion_status = "NOT_APPLICABLE"
    conclusion_error = ""
    if algorithm.supports_model_conclusion:
        evidence = evidence_for_payload(payload)
        input_hash = evidence_hash(evidence)
        run.refresh_from_db()
        if run.conclusion_attempted_at is not None:
            conclusion = {"status": "UNAVAILABLE", "items": []}
            conclusion_status, conclusion_error = "UNAVAILABLE", "MODEL_INTERRUPTED"
        else:
            with transaction.atomic():
                locked_run = PublicOpinionReportRun.objects.select_for_update().get(pk=run.pk)
                locked_run.conclusion_attempted_at = timezone.now()
                locked_run.conclusion_input_hash = input_hash
                locked_run.save(update_fields=("conclusion_attempted_at", "conclusion_input_hash", "updated_at"))
            try:
                conclusion = PublicOpinionConclusionClient().conclude(evidence)
                conclusion_status = "AVAILABLE"
            except ConclusionError as exc:
                conclusion = {"status": "UNAVAILABLE", "items": []}
                conclusion_status, conclusion_error = "UNAVAILABLE", exc.code
            except Exception:
                conclusion = {"status": "UNAVAILABLE", "items": []}
                conclusion_status, conclusion_error = "UNAVAILABLE", "MODEL_UNAVAILABLE"
        conclusion = conclusion_snapshot(
            conclusion, input_hash=input_hash, error_code=conclusion_error
        )
        payload["conclusion"] = conclusion
    with transaction.atomic():
        run = (
            PublicOpinionReportRun.objects.select_for_update()
            .select_related("report")
            .get(pk=run.pk)
        )
        run.status = PublicOpinionReportRunStatus.SUCCEEDED
        run.metrics_as_of = metrics_as_of
        run.input_summary = input_summary
        run.data_quality = quality
        run.result_payload = payload
        run.conclusion_status = conclusion_status
        run.conclusion_payload = conclusion
        run.conclusion_error_code = conclusion_error
        run.error_summary = ""
        run.finished_at = timezone.now()
        run.save()
        run.report.current_successful_run = run
        run.report.save(update_fields=["current_successful_run", "updated_at"])


def purge_expired_public_opinion_reports(*, now: datetime | None = None) -> int:
    cutoff = now or timezone.now()
    deleted = 0
    reports = PublicOpinionReport.objects.filter(retention_until__lte=cutoff).order_by("pk")
    for report in reports:
        if report.runs.filter(
            status__in=(
                PublicOpinionReportRunStatus.PENDING,
                PublicOpinionReportRunStatus.RUNNING,
            )
        ).exists():
            continue
        count, _ = report.delete()
        deleted += count
    return deleted


PUBLIC_OPINION_REPORT_HANDLERS = {PUBLIC_OPINION_REPORT_TASK: execute_public_opinion_report}
