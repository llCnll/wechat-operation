from datetime import date, datetime, timedelta
from io import BytesIO
from unittest.mock import Mock
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from jobs.services import execute_next_job
from jobs.task_types import PUBLIC_OPINION_REPORT_TASK
from posts.game_catalog import set_active_game
from posts.models import (
    POST_STATUS_BLOCKED,
    POST_STATUS_NORMAL,
    InteractionSnapshot,
    ManagedGame,
    OriginalPost,
)
from reporting.models import (
    GameCircleMetricSnapshot,
    MetricPublicationState,
    MetricSource,
    PublicOpinionReportRun,
    PublicOpinionReportRunStatus,
    PublicOpinionReportSource,
    PublicOpinionReportSourceKind,
)
from reporting.public_opinion_analysis import (
    LEGACY_ANALYSIS_VERSION,
    ReportSourceInput,
    build_report_result,
)
from reporting.public_opinion_conclusion import (
    ConclusionError,
    PublicOpinionConclusionClient,
    validate_conclusion,
)
from reporting.public_opinion_jobs import (
    PUBLIC_OPINION_REPORT_HANDLERS,
    create_public_opinion_report,
    execute_public_opinion_report,
    purge_expired_public_opinion_reports,
    rerun_public_opinion_report,
)
from reporting.public_opinion_word import render_public_opinion_word
from reviews.models import (
    AiAssessment,
    ContentCategory,
    RecommendedAction,
    ReviewTask,
    RiskEvidence,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _post(*, topic_id: int, appid: str, day: date, content: str, status: int = POST_STATUS_NORMAL):
    return OriginalPost.objects.create(
        topic_id=topic_id,
        content_id=f"public-opinion-{topic_id}",
        source_appid=appid,
        title=f"标题 {topic_id}",
        content_text=content,
        publish_time=datetime.combine(day, datetime.min.time(), tzinfo=SHANGHAI),
        status=status,
    )


def _assessment(post: OriginalPost, *, sentiment: str = "NEGATIVE") -> AiAssessment:
    assessment = AiAssessment.objects.create(
        post=post,
        category=ContentCategory.FEEDBACK,
        sentiment=sentiment,
        feedback_value="HIGH",
        recommended_action=RecommendedAction.KEEP,
        overall_confidence=0.9,
        needs_human_review=True,
        model_version="test-model",
        prompt_version="test-prompt",
    )
    RiskEvidence.objects.create(
        assessment=assessment,
        label="ADVERTISEMENT_SPAM",
        confidence=0.8,
        evidence=["测试依据"],
    )
    ReviewTask.objects.create(
        post=post,
        assessment=assessment,
        suggested_action=RecommendedAction.KEEP,
    )
    return assessment


def _execute_one():
    job = execute_next_job(PUBLIC_OPINION_REPORT_HANDLERS, task_types=(PUBLIC_OPINION_REPORT_TASK,))
    assert job is not None
    assert job.status == "SUCCEEDED", job.error_summary
    return job


@pytest.mark.django_db
def test_report_locks_sources_and_rerun_only_refreshes_interactions():
    appid = "public-opinion-game"
    current_day = date(2026, 7, 20)
    baseline = _post(topic_id=101, appid=appid, day=current_day - timedelta(days=1), content="旧活动福利")
    first = _post(topic_id=102, appid=appid, day=current_day, content="卡顿问题需要反馈")
    second = _post(topic_id=103, appid=appid, day=current_day, content="抽卡保底体验")
    _post(
        topic_id=104,
        appid=appid,
        day=current_day,
        content="不应被锁定的非正常帖子",
        status=POST_STATUS_BLOCKED,
    )
    _assessment(baseline, sentiment="POSITIVE")
    _assessment(first)
    _assessment(second, sentiment="NEUTRAL")
    InteractionSnapshot.objects.create(
        post=first, source="test", view_count=100, love_count=3, reply_count=4, share_count=1
    )
    result = create_public_opinion_report(
        source_appid=appid,
        start_day=current_day,
        end_day=current_day,
    )
    duplicate = create_public_opinion_report(
        source_appid=appid,
        start_day=current_day,
        end_day=current_day,
    )
    assert duplicate.created is False
    assert duplicate.run.pk == result.run.pk

    _execute_one()
    result.report.refresh_from_db()
    first_run = PublicOpinionReportRun.objects.get(pk=result.run.pk)
    locked = PublicOpinionReportSource.objects.filter(
        report=result.report, source_kind=PublicOpinionReportSourceKind.CURRENT
    )
    assert list(locked.values_list("topic_id", flat=True)) == [102, 103]
    assert first_run.status == PublicOpinionReportRunStatus.SUCCEEDED
    assert first_run.result_payload["summary"]["source_count"] == 2

    first.content_text = "被修改后的当前正文"
    first.status = POST_STATUS_BLOCKED
    first.save(update_fields=["content_text", "status"])
    _post(topic_id=105, appid=appid, day=current_day, content="新加入帖子")
    InteractionSnapshot.objects.create(
        post=first, source="test-next", view_count=200, love_count=8, reply_count=9, share_count=2
    )
    rerun = rerun_public_opinion_report(report=result.report)
    assert rerun.created is True
    _execute_one()
    second_run = PublicOpinionReportRun.objects.get(pk=rerun.run.pk)
    locked_first = locked.get(topic_id=102)
    assert locked_first.content_text == "卡顿问题需要反馈"
    assert list(locked.values_list("topic_id", flat=True)) == [102, 103]
    assert second_run.run_number == 2
    assert second_run.result_payload["summary"]["source_count"] == 2
    assert second_run.metrics_as_of >= first_run.metrics_as_of
    assert result.report.runs.count() == 2


@pytest.mark.django_db
def test_v2_report_keeps_topics_verified_daily_metrics_and_model_failure_as_success():
    appid = "public-opinion-v2-evidence"
    day = date(2026, 7, 20)
    post = _post(topic_id=120, appid=appid, day=day, content="玩法反馈")
    post.topics = ["城主攻略"]
    post.save(update_fields=["topics"])
    _assessment(post)
    GameCircleMetricSnapshot.objects.create(
        appid=appid, source=MetricSource.GROUP_PUBLISH_DAILY, period_start=day, period_end=day,
        scope="ALL", metrics={"publish_pv": 10, "publish_uv": 5},
        publication_state=MetricPublicationState.VERIFIED, captured_at=timezone.now(),
    )
    result = create_public_opinion_report(source_appid=appid, start_day=day, end_day=day)
    _execute_one()
    run = PublicOpinionReportRun.objects.get(pk=result.run.pk)
    assert run.status == PublicOpinionReportRunStatus.SUCCEEDED
    assert run.result_payload["player_topics"][0]["label"] == "城主攻略"
    assert run.result_payload["daily_metrics"][0]["source"] == MetricSource.GROUP_PUBLISH_DAILY
    assert run.result_payload["conclusion"]["status"] == "UNAVAILABLE"
    assert run.deterministic_payload
    document = render_public_opinion_word(report=result.report, run=run, game_name="测试游戏")
    with ZipFile(BytesIO(document)) as archive:
        body = archive.read("word/document.xml").decode()
    assert "内容创作（日）" in body
    assert "发帖次数" in body
    assert "GROUP_PUBLISH_DAILY" not in body
    assert "publish_pv" not in body


def test_conclusion_validation_rejects_unknown_evidence_and_forbidden_language():
    evidence = {"summary": {}, "data_quality": {}, "categories": [], "sentiments": [], "risks": [], "topics": [], "player_topics": [], "hotwords": {}, "daily_trend": [], "daily_metrics": [], "representative_posts": {}}
    assert validate_conclusion({"items": [{"text": "当前仅观察到同期变化。", "evidence_ids": ["summary"]}]}, evidence=evidence)["status"] == "AVAILABLE"
    with pytest.raises(ValueError, match="引用不存在"):
        validate_conclusion({"items": [{"text": "观察", "evidence_ids": ["missing"]}]}, evidence=evidence)
    with pytest.raises(ValueError, match="运营参考限制"):
        validate_conclusion({"items": [{"text": "必须封禁该用户", "evidence_ids": ["summary"]}]}, evidence=evidence)
    with pytest.raises(ValueError, match="运营参考限制"):
        validate_conclusion({"items": [{"text": "建议对相关用户禁言", "evidence_ids": ["summary"]}]}, evidence=evidence)
    with pytest.raises(ValueError, match="运营参考限制"):
        validate_conclusion({"items": [{"text": "负面反馈上升 20%", "evidence_ids": ["sentiments"]}]}, evidence=evidence)
    with pytest.raises(ValueError, match="类型无效"):
        validate_conclusion({"items": [{"kind": "ACTION", "text": "当前仅观察到同期变化。", "evidence_ids": ["summary"]}]}, evidence=evidence)


def test_conclusion_evidence_excludes_representative_official_identity():
    from reporting.public_opinion_conclusion import evidence_for_payload

    evidence = evidence_for_payload(
        {
            "representative_posts": {
                "by_total": [
                    {
                        "topic_id": 1,
                        "title": "安全标题",
                        "interaction_total": 10,
                        "official_identity": "VERIFIED_OFFICIAL",
                    }
                ]
            }
        }
    )

    row = evidence["representative_posts"]["by_total"][0]
    assert row["topic_id"] == 1
    assert "official_identity" not in row


def test_conclusion_snapshot_records_the_generation_contract(monkeypatch):
    from reporting.public_opinion_conclusion import conclusion_snapshot

    monkeypatch.setattr("reporting.public_opinion_conclusion.runtime.ai_model", "test-model")
    snapshot = conclusion_snapshot(
        {"status": "UNAVAILABLE", "items": []},
        input_hash="a" * 64,
        error_code="MODEL_TRANSPORT",
    )

    assert snapshot == {
        "status": "UNAVAILABLE",
        "items": [],
        "model_version": "test-model",
        "prompt_version": "public-opinion-conclusion-v1",
        "evidence_schema_version": "public-opinion-evidence-v1",
        "input_hash": "a" * 64,
        "error_code": "MODEL_TRANSPORT",
    }


def test_conclusion_client_classifies_non_json_and_schema_failure_without_network(monkeypatch):
    evidence = {"summary": {}, "data_quality": {}, "categories": [], "sentiments": [], "risks": [], "topics": [], "player_topics": [], "hotwords": {}, "daily_trend": [], "daily_metrics": [], "representative_posts": {}}
    monkeypatch.setattr("reporting.public_opinion_conclusion.runtime.public_opinion_conclusion_enabled", True)
    monkeypatch.setattr("reporting.public_opinion_conclusion.runtime.ai_base_url", "https://model.invalid")
    monkeypatch.setattr("reporting.public_opinion_conclusion.runtime.ai_api_key", "test")
    monkeypatch.setattr("reporting.public_opinion_conclusion.runtime.ai_model", "test-model")
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"choices": [{"message": {"content": "not-json"}}]}
    monkeypatch.setattr("reporting.public_opinion_conclusion.httpx.post", lambda *args, **kwargs: response)
    with pytest.raises(ConclusionError, match="MODEL_NON_JSON"):
        PublicOpinionConclusionClient().conclude(evidence)
    response.json.return_value = {"choices": [{"message": {"content": '{"items":[{"text":"无证据","evidence_ids":[]}]}'}}]}
    with pytest.raises(ConclusionError, match="MODEL_SCHEMA_INVALID"):
        PublicOpinionConclusionClient().conclude(evidence)


def test_historical_invalid_feedback_is_a_coverage_gap_not_a_business_bucket():
    source = ReportSourceInput(
        topic_id=1, publish_time=None, title="", content_text="正文", ballot_text="",
        has_ai_assessment=True, ai_category="", ai_sentiment="", ai_feedback_value="",
        ai_feedback_state="INVALID", ai_risk_summary=(),
    )
    payload, quality = build_report_result(
        current=(source,), baseline=(), interactions=(),
        source_locked_at=timezone.now(), metrics_as_of=timezone.now(),
    )
    assert quality["feedback_value_invalid_count"] == 1
    assert quality["feedback_value_missing_count"] == 0
    assert payload["summary"]["feedback_values"] == []


def test_feedback_value_display_keeps_historical_canonical_labels():
    from reporting.public_opinion_analysis import feedback_value_display

    assert feedback_value_display("LOW") == "低"
    assert feedback_value_display("低") == "低"
    assert feedback_value_display("无明确反馈价值") == "无明确反馈价值"
    assert feedback_value_display("自由文本") == ""


def test_report_display_summarizes_daily_metrics_within_each_source_only():
    from reporting.views import _report_payload_for_display

    payload = _report_payload_for_display({
        "summary": {},
        "representative_posts": {},
        "daily_metrics": [
            {"source": "CIRCLE_FANS_DAILY", "day": "2026-07-01", "metrics": {"total_user": 100}, "captured_at": "2026-07-02T00:00:00+00:00"},
            {"source": "CIRCLE_FANS_DAILY", "day": "2026-07-02", "metrics": {"total_user": 120}, "captured_at": "2026-07-03T00:00:00+00:00"},
            {"source": "GROUP_CONSUME_DAILY", "day": "2026-07-01", "metrics": {"content_consume_cnt": 300}, "captured_at": "2026-07-02T00:00:00+00:00"},
        ],
    })

    summaries = payload["daily_metric_summaries"]
    assert [(item["source_label"], item["coverage_days"]) for item in summaries] == [
        ("游戏圈粉丝（日）", 2),
        ("内容消费（日）", 1),
    ]
    assert summaries[0]["metrics"] == [{
        "label": "粉丝总量", "first_day": "2026-07-01", "last_day": "2026-07-02",
        "first_value": "100", "last_value": "120", "delta": "+20", "rate": "+20.0%",
    }]


@pytest.mark.django_db
def test_old_version_rerun_stays_without_new_evidence_or_model(monkeypatch):
    appid = "public-opinion-v1"
    day = date(2026, 7, 20)
    post = _post(topic_id=130, appid=appid, day=day, content="旧版报告")
    _assessment(post)
    result = create_public_opinion_report(source_appid=appid, start_day=day, end_day=day)
    type(result.report).objects.filter(pk=result.report.pk).update(analysis_version=LEGACY_ANALYSIS_VERSION)
    monkeypatch.setattr("reporting.public_opinion_jobs.PublicOpinionConclusionClient.conclude", lambda *_: pytest.fail("旧版本不得调用模型"))
    _execute_one()
    run = PublicOpinionReportRun.objects.get(pk=result.run.pk)
    assert run.result_payload["schema_version"] == LEGACY_ANALYSIS_VERSION
    assert "daily_metrics" not in run.result_payload
    assert run.conclusion_status == "NOT_APPLICABLE"


@pytest.mark.django_db
def test_unknown_version_refuses_without_overwriting_existing_run():
    appid = "public-opinion-unknown"
    day = date(2026, 7, 20)
    post = _post(topic_id=140, appid=appid, day=day, content="未知版本")
    _assessment(post)
    result = create_public_opinion_report(source_appid=appid, start_day=day, end_day=day)
    type(result.report).objects.filter(pk=result.report.pk).update(analysis_version="removed-v9")
    job = execute_next_job(PUBLIC_OPINION_REPORT_HANDLERS, task_types=(PUBLIC_OPINION_REPORT_TASK,))
    assert job.status == "FAILED"
    run = PublicOpinionReportRun.objects.get(pk=result.run.pk)
    assert run.result_payload == {}


@pytest.mark.django_db
def test_interrupted_conclusion_finishes_from_checkpoint_without_reloading_or_recalling_model(monkeypatch):
    appid = "public-opinion-interrupted"
    day = date(2026, 7, 20)
    post = _post(topic_id=150, appid=appid, day=day, content="可恢复报告")
    _assessment(post)
    result = create_public_opinion_report(source_appid=appid, start_day=day, end_day=day)
    _execute_one()
    run = PublicOpinionReportRun.objects.get(pk=result.run.pk)
    run.status = PublicOpinionReportRunStatus.PENDING
    run.result_payload = {}
    run.save(update_fields=("status", "result_payload", "updated_at"))
    monkeypatch.setattr("reporting.public_opinion_jobs.load_interactions", lambda **_: pytest.fail("不得重新读取互动"))
    monkeypatch.setattr("reporting.public_opinion_jobs.PublicOpinionConclusionClient.conclude", lambda *_: pytest.fail("不得二次调用模型"))
    execute_public_opinion_report(run.job)
    run.refresh_from_db()
    assert run.status == PublicOpinionReportRunStatus.SUCCEEDED
    assert run.conclusion_status == "UNAVAILABLE"
    assert run.conclusion_error_code == "MODEL_INTERRUPTED"


@pytest.mark.django_db
def test_report_source_page_and_word_do_not_expose_author_or_full_body():
    appid = "public-opinion-view-game"
    game = ManagedGame.objects.create(appid=appid, name="测试游戏")
    set_active_game(appid=game.appid)
    post = _post(
        topic_id=201,
        appid=appid,
        day=date(2026, 7, 20),
        content="不可写入导出的完整正文和作者身份",
    )
    post.author_openid = "private-author-openid"
    post.author_nick_name = "作者昵称"
    post.save(update_fields=["author_openid", "author_nick_name"])
    _assessment(post)
    result = create_public_opinion_report(
        source_appid=appid,
        start_day=date(2026, 7, 20),
        end_day=date(2026, 7, 20),
    )
    _execute_one()
    run = PublicOpinionReportRun.objects.get(pk=result.run.pk)
    source = PublicOpinionReportSource.objects.get(
        report=result.report, source_kind=PublicOpinionReportSourceKind.CURRENT
    )
    document = render_public_opinion_word(report=result.report, run=run, game_name=game.name)
    with ZipFile(BytesIO(document)) as archive:
        body = archive.read("word/document.xml").decode()
    assert source.content_text not in body
    assert post.author_openid not in body
    assert post.author_nick_name not in body

    user = get_user_model().objects.create_user(username="report-user", password="test-password")
    client = Client()
    client.force_login(user)
    response = client.get(f"/public-opinion-reports/{result.report.pk}/sources/?appid={appid}")
    assert response.status_code == 200
    assert source.content_text in response.content.decode()
    response = client.get(
        f"/public-opinion-reports/{result.report.pk}/runs/{run.run_number}/word/?appid={appid}"
    )
    assert response.status_code == 200
    assert response["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    type(result.report).objects.filter(pk=result.report.pk).update(
        retention_until=timezone.now() - timedelta(seconds=1)
    )
    assert client.get(f"/public-opinion-reports/{result.report.pk}/?appid={appid}").status_code == 404


@pytest.mark.django_db
def test_report_worker_handler_has_no_other_task_type():
    assert tuple(PUBLIC_OPINION_REPORT_HANDLERS) == (PUBLIC_OPINION_REPORT_TASK,)


@pytest.mark.django_db
def test_expired_report_is_deleted_as_a_complete_audit_unit():
    appid = "public-opinion-retention-game"
    post = _post(topic_id=301, appid=appid, day=date(2026, 7, 20), content="活动反馈")
    _assessment(post)
    result = create_public_opinion_report(
        source_appid=appid,
        start_day=date(2026, 7, 20),
        end_day=date(2026, 7, 20),
    )
    _execute_one()
    type(result.report).objects.filter(pk=result.report.pk).update(
        retention_until=timezone.now() - timedelta(seconds=1)
    )
    active = create_public_opinion_report(
        source_appid=appid,
        start_day=date(2026, 7, 20),
        end_day=date(2026, 7, 20),
    )
    type(active.report).objects.filter(pk=active.report.pk).update(
        retention_until=timezone.now() - timedelta(seconds=1)
    )

    assert purge_expired_public_opinion_reports() > 0
    assert not PublicOpinionReportRun.objects.filter(pk=result.run.pk).exists()
    assert not PublicOpinionReportSource.objects.filter(report_id=result.report.pk).exists()
    assert PublicOpinionReportRun.objects.filter(pk=active.run.pk).exists()
