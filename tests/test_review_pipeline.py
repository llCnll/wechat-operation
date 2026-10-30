import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from jobs.models import BackgroundJob
from posts.models import POST_STATUS_DELETED, CandidatePost, OriginalPost, OriginType, PostMedia
from reviews.adapters import AssessmentOutput, ReplyOutput, ReplyStyleOutput
from reviews.models import OcrExtraction, RecommendedAction
from reviews.services import (
    AI_EVALUATE_TASK,
    OCR_EXTRACT_TASK,
    build_evaluation_input,
    enqueue_evaluation_pipeline,
    persist_assessment,
)

SAMPLE_APPID = "sample-game"


@pytest.mark.django_db
def test_pipeline_is_idempotent_and_includes_poll_copy():
    post = OriginalPost.objects.create(
        topic_id=801,
        content_id="pipeline-801",
        title="标题",
        content_text="正文",
        ballot_text="投票：竖着读\n选项 1：联 系 我",
    )
    CandidatePost.objects.create(
        topic_id=801, content_id="pipeline-801", origin_type=OriginType.ORIGINAL
    )
    media = PostMedia.objects.create(post=post, url="https://example.test/image.jpg")
    OcrExtraction.objects.create(
        post=post, media=media, provider="test", text="图片文字", confidence=0.9
    )
    first = enqueue_evaluation_pipeline(post)
    second = enqueue_evaluation_pipeline(post)
    assert first[0].pk == second[0].pk
    assert first[1].pk == second[1].pk
    assert BackgroundJob.objects.filter(task_type=OCR_EXTRACT_TASK).count() == 1
    assert BackgroundJob.objects.filter(task_type=AI_EVALUATE_TASK).count() == 1
    content, ocr_text, _ = build_evaluation_input(post)
    assert "联 系 我" in content
    assert ocr_text == "图片文字"


@pytest.mark.django_db
def test_pipeline_rejects_forward_but_allows_deleted_post_history_evaluation():
    post = OriginalPost.objects.create(topic_id=802, content_id="pipeline-802")
    CandidatePost.objects.create(
        topic_id=802, content_id="pipeline-802", origin_type=OriginType.FORWARD
    )
    with pytest.raises(ValueError, match="原创"):
        enqueue_evaluation_pipeline(post)
    CandidatePost.objects.filter(topic_id=802).update(origin_type=OriginType.ORIGINAL)
    post.status = POST_STATUS_DELETED
    post.save()
    ocr_job, ai_job = enqueue_evaluation_pipeline(post)
    assert ocr_job.task_type == OCR_EXTRACT_TASK
    assert ai_job.task_type == AI_EVALUATE_TASK


@pytest.mark.django_db
def test_workspace_saves_reply_without_publishing():
    user = get_user_model().objects.create_user(username="reviewer", password="test")
    post = OriginalPost.objects.create(
        topic_id=803,
        content_id="pipeline-803",
        content_text="活动建议",
        source_appid=SAMPLE_APPID,
    )
    CandidatePost.objects.create(
        topic_id=803, content_id="pipeline-803", origin_type=OriginType.ORIGINAL
    )
    persist_assessment(
        post,
        AssessmentOutput(
            content_category="FEEDBACK",
            recommended_action=RecommendedAction.KEEP,
            overall_confidence=0.8,
            reply_suggestion=ReplyOutput(
                user_intent_summary="诉求",
                faq_rule_id="event-suggestion",
                style_variants=[
                    ReplyStyleOutput(style="WARM", draft="感谢你的活动建议。"),
                    ReplyStyleOutput(style="CONCISE", draft="已记录活动建议。"),
                    ReplyStyleOutput(style="FORMAL", draft="您的活动建议已登记参考。"),
                ],
            ),
        ),
    )
    client = Client()
    client.force_login(user)
    response = client.post(
        f"/posts/{post.topic_id}/reply-draft/", {"draft": "编辑后草案", "action": "adopt"}
    )
    assert response.status_code == 302
    post.reply_draft.refresh_from_db()
    assert post.reply_draft.draft == "编辑后草案"
    assert post.reply_draft.adopted is True
    assert post.reply_draft.adopted_with_edits is True


@pytest.mark.django_db
def test_workspace_rejects_invalid_or_missing_local_reply_drafts_without_creating_jobs():
    user = get_user_model().objects.create_user(username="reply-draft-validation", password="test")
    post = OriginalPost.objects.create(
        topic_id=804,
        content_id="pipeline-804",
        content_text="活动建议",
        source_appid=SAMPLE_APPID,
    )
    persist_assessment(
        post,
        AssessmentOutput(
            content_category="SUGGESTION",
            recommended_action=RecommendedAction.KEEP,
            overall_confidence=0.8,
                reply_suggestion=ReplyOutput(
                    user_intent_summary="诉求",
                    faq_rule_id="event-suggestion",
                    style_variants=[
                        ReplyStyleOutput(style="WARM", draft="感谢你的活动建议。"),
                        ReplyStyleOutput(style="CONCISE", draft="已记录活动建议。"),
                        ReplyStyleOutput(style="FORMAL", draft="您的活动建议已登记参考。"),
                    ],
                ),
        ),
    )
    without_draft = OriginalPost.objects.create(topic_id=805, content_id="pipeline-805")
    client = Client()
    client.force_login(user)
    job_count = BackgroundJob.objects.count()

    blank = client.post(f"/posts/{post.topic_id}/reply-draft/", {"draft": "   ", "action": "save"})
    too_long = client.post(
        f"/posts/{post.topic_id}/reply-draft/", {"draft": "a" * 4001, "action": "save"}
    )
    missing = client.post(
        f"/posts/{without_draft.topic_id}/reply-draft/", {"draft": "不应创建", "action": "save"}
    )

    assert blank.status_code == too_long.status_code == missing.status_code == 302
    post.reply_draft.refresh_from_db()
    assert post.reply_draft.draft == "感谢你的活动建议。"
    assert BackgroundJob.objects.count() == job_count
    assert not hasattr(without_draft, "reply_draft")
