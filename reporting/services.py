"""Read-only live post-analysis queries for the operations workspace."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models import Count, F, IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from posts.models import CandidatePost, InteractionSnapshot, OriginalPost, OriginType
from reviews.models import ContentCategory, RecommendedAction, ReviewStatus, ReviewTask, RiskEvidence, risk_label_display

SHANGHAI = ZoneInfo("Asia/Shanghai")
ANALYSIS_MAX_DAYS = 31
ANALYSIS_DEFAULT_DAYS = 7


@dataclass(frozen=True)
class ReportRow:
    key: str
    label: str
    value: int


@dataclass(frozen=True)
class LiveOperationsAnalysis:
    start_date: date
    end_date: date
    source_appid: str
    metrics: dict[str, int]
    categories: tuple[ReportRow, ...]
    risks: tuple[ReportRow, ...]
    final_actions: tuple[ReportRow, ...]


def default_analysis_date_range(today: date) -> tuple[date, date]:
    return today - timedelta(days=ANALYSIS_DEFAULT_DAYS - 1), today


def validate_analysis_date_range(start_day: date, end_day: date) -> tuple[date, date]:
    if start_day > end_day:
        raise ValueError("开始日期不能晚于结束日期")
    if (end_day - start_day).days + 1 > ANALYSIS_MAX_DAYS:
        raise ValueError(f"分析范围最长为 {ANALYSIS_MAX_DAYS} 天")
    return start_day, end_day


def analysis_datetime_range(start_day: date, end_day: date) -> tuple[datetime, datetime]:
    start_day, end_day = validate_analysis_date_range(start_day, end_day)
    start = datetime.combine(start_day, time.min, tzinfo=SHANGHAI)
    return start, datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=SHANGHAI)


def _labelled_rows(rows, labels: dict[str, str], fallback) -> tuple[ReportRow, ...]:
    return tuple(ReportRow(key=row["key"], label=labels.get(row["key"], fallback(row["key"])), value=int(row["value"])) for row in rows)


def _source_appid_filter(appid: str) -> Q:
    return Q(source_appid=appid) | Q(source_appid="") if appid == "wx6d2454a8afcd9706" else Q(source_appid=appid)


def build_live_operations_analysis(start_day: date, end_day: date, appid: str) -> LiveOperationsAnalysis:
    start, end = analysis_datetime_range(start_day, end_day)
    candidates = CandidatePost.objects.filter(created_at__gte=start, created_at__lt=end).filter(_source_appid_filter(appid))
    posts = OriginalPost.objects.filter(publish_time__gte=start, publish_time__lt=end).filter(_source_appid_filter(appid))
    latest = InteractionSnapshot.objects.filter(post_id=OuterRef("pk")).order_by("-captured_at", "-pk")
    annotated = posts.annotate(latest_view_count=Subquery(latest.values("view_count")[:1]), latest_love_count=Subquery(latest.values("love_count")[:1]), latest_reply_count=Subquery(latest.values("reply_count")[:1]), latest_share_count=Subquery(latest.values("share_count")[:1]))
    totals = annotated.aggregate(views=Sum(Coalesce("latest_view_count", Value(0), output_field=IntegerField())), loves=Sum(Coalesce("latest_love_count", Value(0), output_field=IntegerField())), replies=Sum(Coalesce("latest_reply_count", Value(0), output_field=IntegerField())), shares=Sum(Coalesce("latest_share_count", Value(0), output_field=IntegerField())))
    review_tasks = ReviewTask.objects.filter(post_id__in=posts.values_list("pk", flat=True))
    assessed = review_tasks.filter(assessment__isnull=False)
    assessment_ids = assessed.values_list("assessment_id", flat=True)
    return LiveOperationsAnalysis(start_day, end_day, appid, {"candidate_count": candidates.count(), "original_count": posts.count(), "forward_excluded_count": candidates.filter(origin_type=OriginType.FORWARD).count(), "pending_count": candidates.filter(origin_type=OriginType.CLASSIFICATION_PENDING).count(), "views": int(totals["views"] or 0), "loves": int(totals["loves"] or 0), "replies": int(totals["replies"] or 0), "shares": int(totals["shares"] or 0), "assessment_count": assessed.count(), "review_pending": review_tasks.filter(status=ReviewStatus.PENDING).count(), "reviewed_count": review_tasks.filter(status=ReviewStatus.REVIEWED).count(), "reply_draft_count": posts.filter(reply_draft__isnull=False).count(), "reply_adopted_count": posts.filter(reply_draft__adopted=True).count()}, _labelled_rows(assessed.values("assessment__category").annotate(key=F("assessment__category"), value=Count("pk")).order_by("key"), dict(ContentCategory.choices), lambda key: key), _labelled_rows(RiskEvidence.objects.filter(assessment_id__in=assessment_ids).values("label").annotate(key=F("label"), value=Count("pk")).order_by("key"), {}, risk_label_display), _labelled_rows(review_tasks.exclude(final_action="").values("final_action").annotate(key=F("final_action"), value=Count("pk")).order_by("key"), dict(RecommendedAction.choices), lambda key: key))
