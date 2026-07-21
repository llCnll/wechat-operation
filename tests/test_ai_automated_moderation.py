from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from jobs.models import BackgroundJob
from posts.models import CandidatePost, MediaType, OriginalPost, OriginType, PostMedia
from reviews.adapters import REVIEW_PROMPT_VERSION, AssessmentOutput, RiskOutput
from reviews.automation_tasks import automated_moderation_handler
from reviews.models import (
    AiOperationReview,
    AiOperationReviewStatus,
    FocusReason,
    OcrExtraction,
    OperationAudit,
    OperationExecutionSource,
    PolicyRule,
    PolicyVersion,
    RecommendedAction,
    ReviewStatus,
    VideoEvidence,
    VideoEvidenceKind,
    VideoExtraction,
    VideoExtractionStatus,
)
from reviews.services import (
    AI_AUTOMATED_MODERATION_TASK,
    ai_automation_eligibility,
    escalate_ai_operation_review,
    execute_ai_automated_moderation,
    persist_assessment,
    reserve_ai_automated_moderation,
)
from reviews.video import media_fingerprint
from reviews.video_tasks import VIDEO_EXTRACTOR_VERSION


def _enable_automation(monkeypatch, runtime):
    for name in (
        "enable_ai_automated_moderation",
        "enable_ai_sink_writes",
        "enable_ai_block_writes",
        "enable_upstream_writes",
        "enable_sink_writes",
        "enable_block_writes",
    ):
        monkeypatch.setattr(runtime, name, True)


class _Source:
    def __init__(self, topic_id):
        self.topic_id = topic_id
        self.status = 2

    def get_post_metrics(self, _topic_id):
        return {
            "data": {"topic": {"topic_id": self.topic_id, "status": self.status, "creater": {}}}
        }

    def perform_confirmed_operation(self, *, topic_id, action, payload):
        self.status = 8 if action == RecommendedAction.SINK else 4
        return {"errcode": 0}


class _FailingSource:
    def get_post_metrics(self, _topic_id):
        raise TimeoutError("upstream timeout")


@pytest.mark.django_db
def test_authorized_ai_conclusion_creates_outbox_then_pending_human_review(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(
        topic_id=9001, content_id="auto-9001", content_text="加群领取"
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    post.status = 2
    post.save(update_fields=["status"])
    policy = PolicyVersion.objects.create(name="auto", version="1", is_active=True)
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="TRAFFIC_DIVERSION",
        min_confidence=0.99,
        recommended_action=RecommendedAction.BLOCK,
        priority=100,
        auto_execution_enabled=True,
    )
    assessment, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action=RecommendedAction.BLOCK,
            overall_confidence=0.995,
            risk_labels=[
                RiskOutput(label="TRAFFIC_DIVERSION", confidence=0.995, evidence=["扫码加群"])
            ],
        ),
        policy,
    )
    job = BackgroundJob.objects.get(task_type=AI_AUTOMATED_MODERATION_TASK)
    assert job.parameter_summary["assessment_id"] == assessment.pk
    audit = execute_ai_automated_moderation(
        task.pk, assessment.pk, RecommendedAction.BLOCK, _Source(post.topic_id)
    )
    task.refresh_from_db()
    assert audit.execution_source == "AI_AUTOMATED"
    assert audit.automation_snapshot["authorization"]["enable_ai_automated_moderation"] is True
    assert assessment.prompt_version == REVIEW_PROMPT_VERSION
    assert audit.automation_snapshot["prompt_version"] == REVIEW_PROMPT_VERSION
    assert task.status == ReviewStatus.AUTO_EXECUTED_PENDING_REVIEW
    assert audit.ai_operation_review.status == AiOperationReviewStatus.PENDING
    assert audit.reason.endswith("执行屏蔽。")


@pytest.mark.django_db
def test_automation_disabled_keeps_conclusion_in_manual_queue(monkeypatch):
    from config.settings import runtime

    monkeypatch.setattr(runtime, "enable_ai_automated_moderation", False)
    post = OriginalPost.objects.create(
        topic_id=9002, content_id="auto-9002", content_text="内容", status=2
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    policy = PolicyVersion.objects.create(name="manual", version="1", is_active=True)
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="CHEAT",
        min_confidence=0.99,
        recommended_action="BLOCK",
        priority=100,
        auto_execution_enabled=True,
    )
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action="BLOCK",
            overall_confidence=0.99,
            risk_labels=[RiskOutput(label="CHEAT", confidence=0.99, evidence=["证据"])],
        ),
        policy,
    )
    assert task.status == ReviewStatus.PENDING
    assert not BackgroundJob.objects.filter(task_type=AI_AUTOMATED_MODERATION_TASK).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("action", "risk_label"),
    [
        (RecommendedAction.SINK, "ADVERTISEMENT_SPAM"),
        (RecommendedAction.BLOCK, "OFF_TOPIC"),
    ],
)
def test_authorized_action_uses_its_deciding_rule_threshold_not_fixed_minimum(
    monkeypatch, action, risk_label
):
    """Both supported actions may use the threshold the operator authorized."""
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(
        topic_id=9100 + (1 if action == RecommendedAction.SINK else 2),
        content_id=f"threshold-{action}",
        content_text="普通正文",
        status=2,
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    policy = PolicyVersion.objects.create(name=f"threshold-{action}", version="1", is_active=True)
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label=risk_label,
        min_confidence=0.85,
        recommended_action=action,
        priority=100,
        auto_execution_enabled=True,
    )

    assessment, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action=RecommendedAction.KEEP,
            overall_confidence=0.86,
            risk_labels=[RiskOutput(label=risk_label, confidence=0.86, evidence=["明确风险依据"])],
        ),
        policy,
    )

    assert task.suggested_action == action
    assert assessment.deciding_risk_evidence.label == risk_label
    assert (
        BackgroundJob.objects.get(task_type=AI_AUTOMATED_MODERATION_TASK).parameter_summary[
            "assessment_id"
        ]
        == assessment.pk
    )


@pytest.mark.django_db
def test_non_deciding_low_confidence_risk_does_not_veto_authorized_recommendation(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(
        topic_id=9103, content_id="non-deciding-risk", content_text="普通正文", status=2
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    policy = PolicyVersion.objects.create(name="non-deciding-risk", version="1", is_active=True)
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="OFF_TOPIC",
        min_confidence=0.85,
        recommended_action=RecommendedAction.BLOCK,
        priority=100,
        auto_execution_enabled=True,
    )
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="ADVERTISEMENT_SPAM",
        min_confidence=0.85,
        recommended_action=RecommendedAction.SINK,
        priority=10,
        auto_execution_enabled=False,
    )

    assessment, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action=RecommendedAction.KEEP,
            overall_confidence=0.86,
            risk_labels=[
                RiskOutput(label="OFF_TOPIC", confidence=0.86, evidence=["百事可乐 PEPSI"]),
                RiskOutput(
                    label="ADVERTISEMENT_SPAM", confidence=0.42, evidence=["百事可乐 PEPSI"]
                ),
            ],
        ),
        policy,
    )

    assert task.suggested_action == RecommendedAction.BLOCK
    assert assessment.deciding_risk_evidence.label == "OFF_TOPIC"
    assert ai_automation_eligibility(task, assessment=assessment)[0] is True


@pytest.mark.django_db
def test_below_deciding_rule_threshold_stays_in_manual_queue(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(
        topic_id=9108, content_id="below-threshold", content_text="普通正文", status=2
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    policy = PolicyVersion.objects.create(name="below-threshold", version="1", is_active=True)
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="OFF_TOPIC",
        min_confidence=0.85,
        recommended_action=RecommendedAction.BLOCK,
        priority=100,
        auto_execution_enabled=True,
    )

    assessment, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action=RecommendedAction.BLOCK,
            overall_confidence=0.84,
            risk_labels=[RiskOutput(label="OFF_TOPIC", confidence=0.86, evidence=["无关内容"])],
        ),
        policy,
    )

    assert task.suggested_action == RecommendedAction.BLOCK
    assert assessment.deciding_policy_rule is not None
    assert assessment.deciding_risk_evidence.confidence == 0.86
    assert ai_automation_eligibility(task, assessment=assessment)[0] is False
    assert not BackgroundJob.objects.filter(task_type=AI_AUTOMATED_MODERATION_TASK).exists()


@pytest.mark.django_db
def test_raised_rule_threshold_rejects_the_persisted_deciding_evidence(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(
        topic_id=9112, content_id="raised-rule-threshold", content_text="普通正文", status=2
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    policy = PolicyVersion.objects.create(name="raised-rule-threshold", version="1", is_active=True)
    rule = PolicyRule.objects.create(
        policy_version=policy,
        risk_label="OFF_TOPIC",
        min_confidence=0.85,
        recommended_action=RecommendedAction.BLOCK,
        priority=100,
        auto_execution_enabled=True,
    )
    assessment, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action=RecommendedAction.KEEP,
            overall_confidence=0.9,
            risk_labels=[RiskOutput(label="OFF_TOPIC", confidence=0.86, evidence=["无关内容"])],
        ),
        policy,
    )
    rule.min_confidence = 0.87
    rule.save(update_fields=["min_confidence"])

    eligible, reason, _ = ai_automation_eligibility(task, assessment=assessment)
    assert eligible is False
    assert reason == "置信度未达到自动处置下限"


@pytest.mark.django_db
def test_automation_never_replaces_content_management_recommendation(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(
        topic_id=9104, content_id="manual-recommendation", content_text="普通正文", status=2
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    policy = PolicyVersion.objects.create(name="manual-recommendation", version="1", is_active=True)
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="OFF_TOPIC",
        min_confidence=0.85,
        recommended_action=RecommendedAction.BLOCK,
        priority=100,
        auto_execution_enabled=False,
    )
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="ADVERTISEMENT_SPAM",
        min_confidence=0.85,
        recommended_action=RecommendedAction.SINK,
        priority=10,
        auto_execution_enabled=True,
    )

    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action=RecommendedAction.KEEP,
            overall_confidence=0.95,
            risk_labels=[
                RiskOutput(label="OFF_TOPIC", confidence=0.95, evidence=["无关内容"]),
                RiskOutput(label="ADVERTISEMENT_SPAM", confidence=0.95, evidence=["推广信息"]),
            ],
        ),
        policy,
    )

    assert task.suggested_action == RecommendedAction.BLOCK
    assert not BackgroundJob.objects.filter(task_type=AI_AUTOMATED_MODERATION_TASK).exists()


def _empty_body_ocr_post(topic_id: int) -> OriginalPost:
    post = OriginalPost.objects.create(topic_id=topic_id, content_id=f"empty-{topic_id}", status=2)
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    media = PostMedia.objects.create(post=post, url=f"https://example.invalid/{topic_id}.jpg")
    OcrExtraction.objects.create(
        post=post,
        media=media,
        text="扫码添加客服领取游戏礼包",
        confidence=0.99,
        provider="test",
    )
    return post


def _authorized_traffic_policy(name: str) -> PolicyVersion:
    policy = PolicyVersion.objects.create(name=name, version="1", is_active=True)
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="TRAFFIC_DIVERSION",
        min_confidence=0.85,
        recommended_action=RecommendedAction.BLOCK,
        priority=100,
        auto_execution_enabled=True,
    )
    return policy


def _persist_empty_body_traffic_assessment(post: OriginalPost, policy_name: str):
    return persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action=RecommendedAction.BLOCK,
            overall_confidence=0.9,
            risk_labels=[
                RiskOutput(
                    label="TRAFFIC_DIVERSION", confidence=0.9, evidence=["扫码添加客服领取游戏礼包"]
                )
            ],
        ),
        _authorized_traffic_policy(policy_name),
    )


@pytest.mark.django_db
def test_empty_body_with_verified_deciding_ocr_can_be_automated(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = _empty_body_ocr_post(9105)
    assessment, task = _persist_empty_body_traffic_assessment(post, "empty-body-ocr")

    assert task.needs_focus is False
    assert task.focus_reasons == []
    assert assessment.deciding_risk_evidence.media_source_refs
    assert ai_automation_eligibility(task, assessment=assessment)[0] is True
    assert BackgroundJob.objects.filter(task_type=AI_AUTOMATED_MODERATION_TASK).exists()


@pytest.mark.django_db
def test_empty_body_rejects_changed_ocr_source_before_write(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = _empty_body_ocr_post(9106)
    assessment, task = _persist_empty_body_traffic_assessment(post, "empty-body-changed-ocr")
    extraction = post.ocr_extractions.get()
    extraction.text = "内容已改变"
    extraction.save(update_fields=["text"])

    eligible, reason, _ = ai_automation_eligibility(task, assessment=assessment)
    assert eligible is False
    assert reason == "空文字缺少可验证的本地媒体风险依据"


@pytest.mark.django_db
def test_empty_body_rejects_missing_ocr_source(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(topic_id=9109, content_id="empty-no-ocr", status=2)
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    assessment, task = _persist_empty_body_traffic_assessment(post, "empty-body-no-ocr")

    assert task.focus_reasons == [FocusReason.EVIDENCE_MISSING]
    assert assessment.deciding_risk_evidence.media_source_refs == []
    eligible, reason, _ = ai_automation_eligibility(task, assessment=assessment)
    assert eligible is False
    assert reason == "输入证据不足，需重点人工核验"
    assert not BackgroundJob.objects.filter(task_type=AI_AUTOMATED_MODERATION_TASK).exists()


@pytest.mark.django_db
def test_empty_body_rejects_failed_ocr_source_before_write(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = _empty_body_ocr_post(9110)
    assessment, task = _persist_empty_body_traffic_assessment(post, "empty-body-failed-ocr")
    extraction = post.ocr_extractions.get()
    extraction.error_summary = "OCR 服务失败"
    extraction.save(update_fields=["error_summary"])

    eligible, reason, _ = ai_automation_eligibility(task, assessment=assessment)
    assert eligible is False
    assert reason == "空文字缺少可验证的本地媒体风险依据"


@pytest.mark.django_db
def test_label_mismatch_in_deciding_evidence_remains_manual(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = _empty_body_ocr_post(9111)
    assessment, task = _persist_empty_body_traffic_assessment(post, "deciding-label-mismatch")
    assessment.deciding_risk_evidence.label = "ADVERTISEMENT_SPAM"
    assessment.deciding_risk_evidence.save(update_fields=["label"])

    eligible, reason, _ = ai_automation_eligibility(task, assessment=assessment)
    assert eligible is False
    assert reason == "决定风险依据不完整"


@pytest.mark.django_db
def test_empty_body_with_ocr_failure_remains_manual(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = _empty_body_ocr_post(9107)
    post.ocr_extractions.update(error_summary="OCR 服务失败")
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action=RecommendedAction.BLOCK,
            overall_confidence=0.9,
            risk_labels=[
                RiskOutput(label="TRAFFIC_DIVERSION", confidence=0.9, evidence=["扫码添加客服"])
            ],
        ),
        _authorized_traffic_policy("empty-body-ocr-failure"),
    )

    assert task.focus_reasons == [FocusReason.EVIDENCE_MISSING]
    assert not BackgroundJob.objects.filter(task_type=AI_AUTOMATED_MODERATION_TASK).exists()


@pytest.mark.django_db
def test_high_reward_with_authorized_traffic_evidence_creates_automation_job(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(
        topic_id=9112, content_id="high-reward-authorized", content_text="扫码领取", status=2
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )

    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action=RecommendedAction.BLOCK,
            overall_confidence=0.9,
            risk_labels=[
                RiskOutput(
                    label="TRAFFIC_DIVERSION",
                    confidence=0.9,
                    evidence=["扫码加群领取10万奖励"],
                )
            ],
        ),
        _authorized_traffic_policy("high-reward-authorized"),
    )

    assert task.focus_reasons == []
    assert BackgroundJob.objects.filter(task_type=AI_AUTOMATED_MODERATION_TASK).exists()


@pytest.mark.django_db
def test_empty_body_with_partial_video_evidence_can_be_automated(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(topic_id=9113, content_id="partial-video", status=2)
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    media = PostMedia.objects.create(
        post=post, url="https://example.invalid/partial-video.mp4", media_type=MediaType.VIDEO
    )
    extraction = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
        status=VideoExtractionStatus.PARTIAL,
    )
    VideoEvidence.objects.create(
        extraction=extraction,
        kind=VideoEvidenceKind.FRAME_OCR,
        start_ms=1_000,
        text="扫码添加客服领取游戏礼包",
    )

    assessment, task = _persist_empty_body_traffic_assessment(
        post, "partial-video-evidence"
    )

    assert task.focus_reasons == []
    assert any(
        ref["source_type"] == "VIDEO_EVIDENCE"
        for ref in assessment.deciding_risk_evidence.media_source_refs
    )
    assert ai_automation_eligibility(task, assessment=assessment)[0] is True


@pytest.mark.django_db
def test_empty_body_failed_video_evidence_is_not_usable_for_automation(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(topic_id=91131, content_id="failed-video", status=2)
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    media = PostMedia.objects.create(
        post=post, url="https://example.invalid/failed-video.mp4", media_type=MediaType.VIDEO
    )
    extraction = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
        status=VideoExtractionStatus.FAILED,
    )
    VideoEvidence.objects.create(
        extraction=extraction,
        kind=VideoEvidenceKind.FRAME_OCR,
        start_ms=1_000,
        text="扫码添加客服领取游戏礼包",
    )

    assessment, task = _persist_empty_body_traffic_assessment(post, "failed-video-evidence")

    assert task.focus_reasons == [FocusReason.EVIDENCE_MISSING]
    assert assessment.deciding_risk_evidence.media_source_refs == []
    assert ai_automation_eligibility(task, assessment=assessment)[0] is False


@pytest.mark.django_db
def test_empty_body_mixed_media_requires_evidence_for_every_attachment(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(topic_id=9114, content_id="mixed-media", status=2)
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    image = PostMedia.objects.create(post=post, url="https://example.invalid/evidence.jpg")
    OcrExtraction.objects.create(
        post=post, media=image, text="扫码加群领取游戏礼包", provider="test"
    )
    PostMedia.objects.create(
        post=post, url="https://example.invalid/missing-video.mp4", media_type=MediaType.VIDEO
    )

    _, task = _persist_empty_body_traffic_assessment(post, "mixed-media-missing-video")

    assert task.focus_reasons == [FocusReason.EVIDENCE_MISSING]
    assert not BackgroundJob.objects.filter(task_type=AI_AUTOMATED_MODERATION_TASK).exists()


@pytest.mark.django_db
def test_empty_body_source_link_failure_rejects_without_rewriting_focus(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = _empty_body_ocr_post(9115)
    assessment, task = _persist_empty_body_traffic_assessment(post, "source-link-rejection")
    post.ocr_extractions.update(text="已被替换的文字")

    assert task.focus_reasons == []
    assert reserve_ai_automated_moderation(
        task.pk, assessment.pk, RecommendedAction.BLOCK
    ) is None
    audit = OperationAudit.objects.get(task=task)
    assert audit.result_code == "AUTO_ELIGIBILITY_REJECTED"
    assert audit.error_summary == "空文字缺少可验证的本地媒体风险依据"
    task.refresh_from_db()
    assert task.focus_reasons == []


@pytest.mark.django_db
def test_historical_focus_reason_rejects_pending_automation_without_write(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(
        topic_id=9116, content_id="historical-focus", content_text="扫码加群", status=2
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    assessment, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action=RecommendedAction.BLOCK,
            overall_confidence=0.9,
            risk_labels=[
                RiskOutput(label="TRAFFIC_DIVERSION", confidence=0.9, evidence=["扫码加群"])
            ],
        ),
        _authorized_traffic_policy("historical-focus"),
    )
    task.focus_reasons = [FocusReason.HIGH_REWARD]
    task.needs_focus = True
    task.save(update_fields=("focus_reasons", "needs_focus", "updated_at"))

    assert reserve_ai_automated_moderation(
        task.pk, assessment.pk, RecommendedAction.BLOCK
    ) is None
    audit = OperationAudit.objects.get(task=task)
    assert audit.result_code == "AUTO_ELIGIBILITY_REJECTED"
    assert audit.error_summary == "历史重点人工核验任务不得自动处置"
    task.refresh_from_db()
    assert task.status == ReviewStatus.PENDING


@pytest.mark.django_db
def test_explicit_reassessment_replaces_current_legacy_focus_reason():
    post = OriginalPost.objects.create(
        topic_id=9117, content_id="reassessed-legacy-focus", content_text="扫码加群", status=2
    )
    first, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action=RecommendedAction.BLOCK,
            overall_confidence=0.9,
            risk_labels=[
                RiskOutput(label="TRAFFIC_DIVERSION", confidence=0.9, evidence=["扫码加群"])
            ],
        ),
    )
    task.focus_reasons = [FocusReason.HIGH_REWARD]
    task.needs_focus = True
    task.save(update_fields=("focus_reasons", "needs_focus", "updated_at"))

    second, current_task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action=RecommendedAction.KEEP,
            overall_confidence=0.5,
        ),
    )

    assert second.pk != first.pk
    assert post.ai_assessments.filter(pk=first.pk).exists()
    assert current_task.pk == task.pk
    assert current_task.assessment_id == second.pk
    assert current_task.focus_reasons == []
    assert current_task.needs_focus is False


@pytest.mark.django_db
def test_reassessment_does_not_overwrite_pending_ai_review(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(
        topic_id=9003, content_id="auto-9003", content_text="内容", status=2
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    policy = PolicyVersion.objects.create(name="freeze", version="1", is_active=True)
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="CHEAT",
        min_confidence=0.99,
        recommended_action="BLOCK",
        priority=100,
        auto_execution_enabled=True,
    )
    first, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action="BLOCK",
            overall_confidence=0.99,
            risk_labels=[RiskOutput(label="CHEAT", confidence=0.99, evidence=["证据"])],
        ),
        policy,
    )
    execute_ai_automated_moderation(task.pk, first.pk, "BLOCK", _Source(post.topic_id))
    second, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER", recommended_action="KEEP", overall_confidence=0.5
        ),
        policy,
    )
    assert second.pk != first.pk
    assert task.assessment_id == first.pk
    assert task.status == ReviewStatus.AUTO_EXECUTED_PENDING_REVIEW


@pytest.mark.django_db
def test_ai_review_escalation_needs_no_reason(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(
        topic_id=9004, content_id="auto-9004", content_text="内容", status=2
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    policy = PolicyVersion.objects.create(name="review", version="1", is_active=True)
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="CHEAT",
        min_confidence=0.99,
        recommended_action="SINK",
        priority=100,
        auto_execution_enabled=True,
    )
    assessment, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action="SINK",
            overall_confidence=0.99,
            risk_labels=[RiskOutput(label="CHEAT", confidence=0.99, evidence=["证据"])],
        ),
        policy,
    )
    execute_ai_automated_moderation(task.pk, assessment.pk, "SINK", _Source(post.topic_id))
    user = get_user_model().objects.create_user("reviewer")
    review = task.operation_audits.get(execution_source="AI_AUTOMATED").ai_operation_review
    escalate_ai_operation_review(review, user=user, reason="")
    task.refresh_from_db()
    review.refresh_from_db()
    assert review.status == AiOperationReviewStatus.ESCALATED
    assert review.reason == ""
    assert task.status == ReviewStatus.AUTO_REVIEW_ESCALATED


@pytest.mark.django_db
def test_ai_operation_review_tab_and_post_actions(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(
        topic_id=9005, content_id="auto-9005", content_text="内容", status=2
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    policy = PolicyVersion.objects.create(name="tab", version="1", is_active=True)
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="CHEAT",
        min_confidence=0.99,
        recommended_action="SINK",
        priority=100,
        auto_execution_enabled=True,
    )
    assessment, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action="SINK",
            overall_confidence=0.99,
            risk_labels=[RiskOutput(label="CHEAT", confidence=0.99, evidence=["证据"])],
        ),
        policy,
    )
    audit = execute_ai_automated_moderation(task.pk, assessment.pk, "SINK", _Source(post.topic_id))
    user = get_user_model().objects.create_user("tab-reviewer")
    client = Client()
    client.force_login(user)
    page = client.get("/ai-operation-reviews/")
    assert page.status_code == 200
    assert "AI 操作复审" in page.content.decode()
    response = client.post(f"/ai-operation-reviews/{audit.ai_operation_review.pk}/confirm/", {})
    assert response.status_code == 302
    audit.ai_operation_review.refresh_from_db()
    assert audit.ai_operation_review.status == AiOperationReviewStatus.CONFIRMED


@pytest.mark.django_db
def test_escalating_an_ai_review_opens_its_content_management_task(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(
        topic_id=9011,
        content_id="escalate-dashboard-9011",
        content_text="内容",
        status=2,
        source_appid="wx6d2454a8afcd9706",
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    policy = PolicyVersion.objects.create(name="escalate-dashboard", version="1", is_active=True)
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="CHEAT",
        min_confidence=0.99,
        recommended_action=RecommendedAction.SINK,
        priority=100,
        auto_execution_enabled=True,
    )
    assessment, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action=RecommendedAction.SINK,
            overall_confidence=0.99,
            risk_labels=[RiskOutput(label="CHEAT", confidence=0.99, evidence=["证据"])],
        ),
        policy,
    )
    audit = execute_ai_automated_moderation(task.pk, assessment.pk, "SINK", _Source(post.topic_id))
    user = get_user_model().objects.create_user("escalate-dashboard-reviewer")
    client = Client()
    client.force_login(user)

    response = client.post(
        f"/ai-operation-reviews/{audit.ai_operation_review.pk}/escalate/",
        {"return_to": "/ai-operation-reviews/?status=PENDING"},
    )

    assert response.status_code == 302
    assert response.url.startswith(
        f"/?appid={post.source_appid}&review_status=AUTO_REVIEW_ESCALATED&topic={post.topic_id}"
    )
    assert (
        "return_to=%2Fai-operation-reviews%2F%3Fappid%3Dwx6d2454a8afcd9706%26status%3DESCALATED"
        in response.url
    )

    dashboard = client.get(response.url)
    body = dashboard.content.decode()
    assert dashboard.status_code == 200
    assert "AI 已执行" in body
    assert "复审状态" in body
    assert "已升级人工跟进" in body
    assert "查看复审记录" in body
    assert 'href="/ai-operation-reviews/?appid=' in body
    assert "status=ESCALATED" in body


@pytest.mark.django_db
def test_unapproved_automation_worker_never_opens_a_controlled_source(monkeypatch):
    """A queued event can become unapproved after enqueue; it must not create a CDP session."""
    post = OriginalPost.objects.create(
        topic_id=9006, content_id="auto-9006", content_text="内容", status=2, source_appid="wx-test"
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    assessment, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER", recommended_action="BLOCK", overall_confidence=0.99
        ),
    )
    job = BackgroundJob.objects.create(
        task_type=AI_AUTOMATED_MODERATION_TASK,
        idempotency_key="ai-auto:unapproved",
        parameter_summary={"task_id": task.pk, "assessment_id": assessment.pk, "action": "BLOCK"},
    )

    def source_must_not_be_created(**_kwargs):
        raise AssertionError("未授权任务不应建立受控上游会话")

    monkeypatch.setattr("reviews.automation_tasks.CdpGameCircleClient", source_must_not_be_created)
    automated_moderation_handler(job)

    audit = task.operation_audits.get()
    assert audit.result_code == "AUTO_ELIGIBILITY_REJECTED"
    assert not AiOperationReview.objects.filter(audit=audit).exists()


@pytest.mark.django_db
def test_ai_worker_does_not_open_cdp_or_write_while_human_operation_is_executing(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(
        topic_id=9008,
        content_id="auto-9008",
        content_text="内容",
        status=2,
        source_appid="wx6d2454a8afcd9706",
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    policy = PolicyVersion.objects.create(name="execution-conflict", version="1", is_active=True)
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="CHEAT",
        min_confidence=0.99,
        recommended_action=RecommendedAction.BLOCK,
        priority=100,
        auto_execution_enabled=True,
    )
    assessment, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action=RecommendedAction.BLOCK,
            overall_confidence=0.99,
            risk_labels=[RiskOutput(label="CHEAT", confidence=0.99, evidence=["证据"])],
        ),
        policy,
    )
    OperationAudit.objects.create(
        task=task,
        operation_id=str(uuid4()),
        idempotency_key="human-executing-9008",
        action=RecommendedAction.BLOCK,
        reason="人工操作正在执行",
        execution_source=OperationExecutionSource.HUMAN,
        result_code="EXECUTING",
    )
    job = BackgroundJob.objects.get(task_type=AI_AUTOMATED_MODERATION_TASK)

    monkeypatch.setattr(
        "reviews.automation_tasks.CdpGameCircleClient",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("不应建立 CDP 会话")),
    )
    automated_moderation_handler(job)

    assert not task.operation_audits.filter(
        execution_source=OperationExecutionSource.AI_AUTOMATED,
        result_code="EXECUTING",
    ).exists()
    assert task.operation_audits.filter(result_code="AUTO_ELIGIBILITY_REJECTED").exists()


@pytest.mark.django_db
def test_human_operation_reports_conflict_while_ai_operation_is_executing(monkeypatch):
    from config.settings import runtime

    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_sink_writes", True)
    post = OriginalPost.objects.create(
        topic_id=9010,
        content_id="human-conflict-9010",
        content_text="内容",
        status=2,
        source_appid="wx6d2454a8afcd9706",
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    assessment, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER", recommended_action="SINK", overall_confidence=0.99
        ),
    )
    OperationAudit.objects.create(
        task=task,
        operation_id=str(uuid4()),
        idempotency_key="ai-executing-9010",
        action=RecommendedAction.SINK,
        reason="AI 操作正在执行",
        execution_source=OperationExecutionSource.AI_AUTOMATED,
        result_code="EXECUTING",
    )

    class Source:
        def close(self):
            pass

    monkeypatch.setattr("posts.views.CdpGameCircleClient", lambda **_kwargs: Source())
    user = get_user_model().objects.create_user("human-conflict-reviewer")
    client = Client()
    client.force_login(user)

    response = client.post(
        f"/posts/{post.topic_id}/operate/SINK/",
        {"reason": "人工确认", "confirmation_key": "human-conflict-9010"},
        follow=True,
    )

    assert response.status_code == 200
    assert "该帖子已有处置正在执行，请等待结果" in response.content.decode()
    assert task.operation_audits.count() == 1
    assert not OperationAudit.objects.filter(
        task=task,
        execution_source=OperationExecutionSource.HUMAN,
    ).exists()


@pytest.mark.django_db
def test_ai_upstream_failure_records_a_safe_actionable_error(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(
        topic_id=9009, content_id="auto-9009", content_text="内容", status=2
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    policy = PolicyVersion.objects.create(name="safe-error", version="1", is_active=True)
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="CHEAT",
        min_confidence=0.99,
        recommended_action=RecommendedAction.SINK,
        priority=100,
        auto_execution_enabled=True,
    )
    assessment, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action=RecommendedAction.SINK,
            overall_confidence=0.99,
            risk_labels=[RiskOutput(label="CHEAT", confidence=0.99, evidence=["证据"])],
        ),
        policy,
    )

    audit = execute_ai_automated_moderation(task.pk, assessment.pk, "SINK", _FailingSource())

    assert audit.result_code == "ACTION_FAILED"
    assert audit.error_summary == "上游调用或状态验证失败：TimeoutError"


@pytest.mark.django_db
def test_review_workspace_filters_and_blocks_direct_action_while_pending(monkeypatch):
    from config.settings import runtime

    _enable_automation(monkeypatch, runtime)
    post = OriginalPost.objects.create(
        topic_id=9007,
        content_id="auto-9007",
        content_text="内容",
        status=2,
        source_appid="wx6d2454a8afcd9706",
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    policy = PolicyVersion.objects.create(name="workspace", version="1", is_active=True)
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="CHEAT",
        min_confidence=0.99,
        recommended_action="SINK",
        priority=100,
        auto_execution_enabled=True,
    )
    assessment, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action="SINK",
            overall_confidence=0.99,
            risk_labels=[RiskOutput(label="CHEAT", confidence=0.99, evidence=["风险证据"])],
        ),
        policy,
    )
    audit = execute_ai_automated_moderation(task.pk, assessment.pk, "SINK", _Source(post.topic_id))
    user = get_user_model().objects.create_user("workspace-reviewer")
    client = Client()
    client.force_login(user)

    page = client.get("/ai-operation-reviews/?status=PENDING&action=SINK&risk=CHEAT")
    body = page.content.decode()
    assert page.status_code == 200
    assert "风险证据" in body
    assert "正常 → 已沉底" in body
    assert "执行与追溯" in body
    assert str(post.topic_id) in body
    assert "review_status=AUTO_EXECUTED_PENDING_REVIEW" in body
    assert (
        "return_to=/ai-operation-reviews/%3Fstatus%3DPENDING%26action%3DSINK%26risk%3DCHEAT" in body
    )
    assert str(post.topic_id) not in client.get("/").content.decode()

    response = client.post(
        f"/posts/{post.topic_id}/operate/RESTORE/", {"reason": "不应执行", "confirmation_key": "x"}
    )
    assert response.status_code == 302
    audit.refresh_from_db()
    assert audit.result_code == "OK"
