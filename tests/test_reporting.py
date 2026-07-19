from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from jobs.models import BackgroundJob
from posts.models import CandidatePost, OriginalPost, OriginType
from reporting.services import (
    build_live_operations_analysis,
    default_analysis_date_range,
    validate_analysis_date_range,
)
from reviews.adapters import AssessmentOutput, RiskOutput
from reviews.models import RecommendedAction, ReviewStatus
from reviews.services import persist_assessment

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _at(hour: int) -> datetime:
    return datetime(2026, 7, 18, hour, tzinfo=SHANGHAI)


def _candidate(topic_id: int, appid: str, origin_type: str) -> CandidatePost:
    candidate = CandidatePost.objects.create(
        topic_id=topic_id,
        content_id=f"candidate-{topic_id}",
        source_appid=appid,
        origin_type=origin_type,
    )
    CandidatePost.objects.filter(pk=candidate.pk).update(created_at=_at(9))
    return candidate


@pytest.mark.django_db
def test_live_analysis_uses_latest_snapshot_and_excludes_forward_posts():
    appid = "game-a"
    post = OriginalPost.objects.create(
        topic_id=301,
        content_id="report-301",
        source_appid=appid,
        publish_time=_at(12),
    )
    post.interaction_snapshots.create(
        source="first", view_count=9, love_count=2, reply_count=1, share_count=1
    )
    post.interaction_snapshots.create(
        source="latest", view_count=15, love_count=4, reply_count=3, share_count=2
    )
    _candidate(301, appid, OriginType.ORIGINAL)
    _candidate(302, appid, OriginType.FORWARD)
    _candidate(303, appid, OriginType.CLASSIFICATION_PENDING)

    report = build_live_operations_analysis(date(2026, 7, 18), date(2026, 7, 18), appid)

    assert report.metrics["candidate_count"] == 3
    assert report.metrics["original_count"] == 1
    assert report.metrics["forward_excluded_count"] == 1
    assert report.metrics["pending_count"] == 1
    assert report.metrics["views"] == 15
    assert report.metrics["loves"] == 4
    assert report.metrics["replies"] == 3
    assert report.metrics["shares"] == 2


@pytest.mark.django_db
def test_live_analysis_is_game_scoped_and_uses_current_assessment_and_review_task():
    post = OriginalPost.objects.create(
        topic_id=401,
        content_id="report-401",
        source_appid="game-a",
        publish_time=_at(11),
    )
    other_game = OriginalPost.objects.create(
        topic_id=402,
        content_id="report-402",
        source_appid="game-b",
        publish_time=_at(11),
    )
    _candidate(401, "game-a", OriginType.ORIGINAL)
    _candidate(402, "game-b", OriginType.ORIGINAL)
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="FEEDBACK",
            recommended_action=RecommendedAction.KEEP,
            overall_confidence=0.9,
            risk_labels=[RiskOutput(label="CHEAT", confidence=0.9, evidence=["外挂"])],
        ),
    )
    task.status = ReviewStatus.REVIEWED
    task.final_action = RecommendedAction.SINK
    task.save(update_fields=["status", "final_action"])
    persist_assessment(
        other_game,
        AssessmentOutput(
            content_category="GUIDE",
            recommended_action=RecommendedAction.KEEP,
            overall_confidence=0.9,
        ),
    )

    report = build_live_operations_analysis(
        date(2026, 7, 18), date(2026, 7, 18), "game-a"
    )

    assert report.metrics["original_count"] == 1
    assert report.metrics["assessment_count"] == 1
    assert report.metrics["review_pending"] == 0
    assert report.metrics["reviewed_count"] == 1
    assert [(row.label, row.value) for row in report.categories] == [("反馈", 1)]
    assert [(row.label, row.value) for row in report.risks] == [("外挂/作弊", 1)]
    assert [(row.label, row.value) for row in report.final_actions] == [("沉底", 1)]


@pytest.mark.django_db
def test_live_analysis_is_read_only_and_returns_empty_metrics_for_an_empty_range():
    jobs_before = BackgroundJob.objects.count()

    report = build_live_operations_analysis(
        date(2026, 7, 18), date(2026, 7, 18), "game-a"
    )

    assert report.metrics["candidate_count"] == 0
    assert report.metrics["original_count"] == 0
    assert report.metrics["views"] == 0
    assert report.categories == ()
    assert report.risks == ()
    assert report.final_actions == ()
    assert BackgroundJob.objects.count() == jobs_before


@pytest.mark.django_db
def test_live_analysis_includes_both_beijing_range_boundaries():
    first = OriginalPost.objects.create(
        topic_id=501,
        content_id="range-first",
        source_appid="game-a",
        publish_time=datetime(2026, 7, 17, 0, tzinfo=SHANGHAI),
    )
    last = OriginalPost.objects.create(
        topic_id=502,
        content_id="range-last",
        source_appid="game-a",
        publish_time=datetime(2026, 7, 18, 23, 59, tzinfo=SHANGHAI),
    )
    first.interaction_snapshots.create(source="first", view_count=2)
    last.interaction_snapshots.create(source="last", view_count=3)

    analysis = build_live_operations_analysis(
        date(2026, 7, 17), date(2026, 7, 18), "game-a"
    )

    assert analysis.metrics["original_count"] == 2
    assert analysis.metrics["views"] == 5


def test_analysis_range_defaults_to_seven_days_and_rejects_more_than_maximum():
    assert default_analysis_date_range(date(2026, 7, 19)) == (date(2026, 7, 13), date(2026, 7, 19))
    with pytest.raises(ValueError, match="最长"):
        validate_analysis_date_range(date(2026, 7, 1), date(2026, 8, 1))
