from datetime import date, datetime, timedelta
from io import BytesIO
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
    PublicOpinionReportRun,
    PublicOpinionReportRunStatus,
    PublicOpinionReportSource,
    PublicOpinionReportSourceKind,
)
from reporting.public_opinion_jobs import (
    PUBLIC_OPINION_REPORT_HANDLERS,
    create_public_opinion_report,
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
