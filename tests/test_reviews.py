import pytest
from django.db import connection

from config.settings import runtime
from jobs.models import BackgroundJob
from posts.models import POST_STATUS_DELETED, CandidatePost, OriginalPost, OriginType
from reviews.adapters import AssessmentOutput, ReplyOutput, RiskOutput, normalize_assessment_output
from reviews.models import (
    OperationAudit,
    PolicyRule,
    PolicyVersion,
    RecommendedAction,
    ReviewStatus,
)
from reviews.services import (
    create_confirmed_operation,
    execute_confirmed_operation,
    is_operation_enabled,
    persist_assessment,
)
from reviews.tasks import AI_EVALUATE_TASK, evaluate_post_handler


@pytest.mark.django_db
def test_assessment_always_creates_pending_human_review_and_safe_reply_draft():
    post = OriginalPost.objects.create(
        topic_id=201, content_id="review-201", content_text="建议优化活动"
    )
    output = AssessmentOutput(
        content_category="SUGGESTION",
        recommended_action="KEEP",
        overall_confidence=0.9,
        reply_suggestion=ReplyOutput(user_intent_summary="活动建议", reply_draft="感谢反馈"),
    )
    assessment, task = persist_assessment(post, output)
    assert assessment.needs_human_review is True
    assert task.status == ReviewStatus.PENDING
    assert post.reply_draft.draft == "感谢反馈"


def test_normalize_assessment_output_handles_non_structured_nested_values():
    output = AssessmentOutput.model_validate(
        normalize_assessment_output(
            {
                "content_category": "OTHER",
                "secondary_categories": [],
                "risk_labels": ["TRAFFIC_DIVERSION"],
                "sentiment": "NEGATIVE",
                "feedback_value": "LOW",
                "recommended_action": "HUMAN_REVIEW",
                "overall_confidence": 0.9,
                "needs_human_review": True,
                "reply_suggestion": "请人工确认。",
            }
        )
    )
    assert output.risk_labels[0].confidence == 0.5
    assert output.risk_labels[0].evidence == []
    assert output.reply_suggestion.reply_draft == "请人工确认。"


@pytest.mark.django_db
def test_policy_risk_draft_is_suppressed_and_write_capability_is_disabled(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", False)
    monkeypatch.setattr(runtime, "enable_block_writes", False)
    post = OriginalPost.objects.create(topic_id=202, content_id="review-202", content_text="内容")
    policy = PolicyVersion.objects.create(name="default", version="1", is_active=True)
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="CHEAT",
        min_confidence=0.9,
        recommended_action=RecommendedAction.BLOCK_AND_MUTE,
        priority=100,
    )
    output = AssessmentOutput(
        content_category="FEEDBACK",
        recommended_action="KEEP",
        overall_confidence=0.99,
        risk_labels=[RiskOutput(label="CHEAT", confidence=0.99, evidence=["证据"])],
    )
    _, task = persist_assessment(post, output, policy)
    assert task.suggested_action == RecommendedAction.BLOCK_AND_MUTE
    assert not hasattr(post, "reply_draft")
    with pytest.raises(PermissionError):
        create_confirmed_operation(task, user=None, action=RecommendedAction.BLOCK, reason="违规")


@pytest.mark.django_db
def test_disabled_operation_creates_audit_without_calling_source(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", False)
    monkeypatch.setattr(runtime, "enable_block_writes", False)
    post = OriginalPost.objects.create(topic_id=203, content_id="review-203")
    CandidatePost.objects.create(
        topic_id=203, content_id="review-203", origin_type=OriginType.ORIGINAL
    )
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER", recommended_action="KEEP", overall_confidence=0.5
        ),
    )

    class Source:
        def get_post_metrics(self, topic_id):
            raise AssertionError("disabled path must not call source")

    audit = execute_confirmed_operation(
        task, user=None, action=RecommendedAction.BLOCK, reason="人工确认", source=Source()
    )
    assert audit.result_code == "CAPABILITY_DISABLED"


@pytest.mark.django_db
def test_valuable_feedback_is_not_sunk():
    post = OriginalPost.objects.create(topic_id=204, content_id="review-204")
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="FEEDBACK",
            feedback_value="VALUABLE",
            recommended_action="SINK",
            overall_confidence=0.99,
        ),
    )
    assert task.suggested_action == RecommendedAction.KEEP


@pytest.mark.django_db
def test_ai_review_input_includes_ballot_title_and_option_text(monkeypatch):
    post = OriginalPost.objects.create(
        topic_id=209,
        content_id="review-ballot",
        content_text="帖子正文",
        ballot_text="投票 1：穿梭（竖着看第一个字）\n选项 1：卫 护山河岁月长",
    )
    job = BackgroundJob.objects.create(
        task_type=AI_EVALUATE_TASK,
        idempotency_key="ai:209",
        parameter_summary={"topic_id": post.topic_id},
    )

    class ReviewClient:
        content = ""

        def evaluate(self, content, ocr_text):
            self.content = content
            assert ocr_text == ""
            return AssessmentOutput(
                content_category="OTHER", recommended_action="KEEP", overall_confidence=0.9
            )

    review_client = ReviewClient()
    monkeypatch.setattr("reviews.tasks.OpenAiCompatibleReviewClient", lambda: review_client)

    evaluate_post_handler(job)

    assert "帖子正文" in review_client.content
    assert "穿梭（竖着看第一个字）" in review_client.content
    assert "卫 护山河岁月长" in review_client.content


@pytest.mark.django_db(transaction=True)
def test_confirmed_sink_records_minimal_state_after_verified_readback(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_sink_writes", True)
    post = OriginalPost.objects.create(topic_id=205, content_id="review-205")
    CandidatePost.objects.create(
        topic_id=205, content_id="review-205", origin_type=OriginType.ORIGINAL
    )
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER", recommended_action="SINK", overall_confidence=0.9
        ),
    )

    class Source:
        states = iter((2, 8))

        def get_post_metrics(self, topic_id):
            assert not connection.in_atomic_block
            pending_audit = OperationAudit.objects.get(task=task)
            assert pending_audit.result_code == "EXECUTING"
            return {"data": {"topic": {"topic_id": topic_id, "status": next(self.states)}}}

        def perform_confirmed_operation(self, *, topic_id, action, payload):
            assert not connection.in_atomic_block
            assert action == RecommendedAction.SINK
            assert payload == {}
            return {"errcode": 0, "errmsg": "ok"}

    audit = execute_confirmed_operation(
        task, user=None, action=RecommendedAction.SINK, reason="无价值灌水", source=Source()
    )
    assert audit.result_code == "OK"
    assert audit.before_state == {
        "topic_id": 205,
        "status": 2,
        "local_block_end_time": 0,
        "global_block_end_time": 0,
    }
    assert audit.after_state == {
        "topic_id": 205,
        "status": 8,
        "local_block_end_time": 0,
        "global_block_end_time": 0,
    }
    post.refresh_from_db()
    assert post.status == 8


@pytest.mark.django_db
def test_confirmed_operation_rejects_deleted_post_without_creating_an_audit():
    post = OriginalPost.objects.create(
        topic_id=210, content_id="deleted-review-210", status=POST_STATUS_DELETED
    )
    CandidatePost.objects.create(
        topic_id=210, content_id="deleted-review-210", origin_type=OriginType.ORIGINAL
    )
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER", recommended_action="SINK", overall_confidence=0.9
        ),
    )

    class Source:
        def get_post_metrics(self, topic_id):
            raise AssertionError("删除帖子不能读取上游处置状态")

    with pytest.raises(ValueError, match="原平台管理队列删除"):
        execute_confirmed_operation(
            task,
            user=None,
            action=RecommendedAction.SINK,
            reason="违规内容",
            source=Source(),
        )

    assert not task.operation_audits.exists()


@pytest.mark.django_db
def test_confirmed_block_fails_when_readback_status_differs(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_block_writes", True)
    post = OriginalPost.objects.create(topic_id=206, content_id="review-206")
    CandidatePost.objects.create(
        topic_id=206, content_id="review-206", origin_type=OriginType.ORIGINAL
    )
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER", recommended_action="BLOCK", overall_confidence=0.9
        ),
    )

    class Source:
        states = iter((2, 8))

        def get_post_metrics(self, topic_id):
            return {"data": {"topic": {"topic_id": topic_id, "status": next(self.states)}}}

        def perform_confirmed_operation(self, **kwargs):
            return {"errcode": 0, "errmsg": "ok"}

    audit = execute_confirmed_operation(
        task,
        user=None,
        action=RecommendedAction.BLOCK,
        reason="广告欺诈",
        source=Source(),
        upstream_payload={"block_reason_code": 8},
    )
    assert audit.result_code == "ACTION_FAILED"
    assert audit.error_summary == "上游状态回读与预期不一致"


@pytest.mark.django_db
def test_sink_skips_upstream_write_when_preflight_status_is_not_normal(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_sink_writes", True)
    post = OriginalPost.objects.create(topic_id=212, content_id="sink-non-normal", status=8)
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER", recommended_action="SINK", overall_confidence=0.9
        ),
    )

    class Source:
        writes = 0

        def get_post_metrics(self, topic_id):
            return {"data": {"topic": {"topic_id": topic_id, "status": 8}}}

        def perform_confirmed_operation(self, **kwargs):
            self.writes += 1
            raise AssertionError("非正常帖子不得重复发送沉底请求")

    source = Source()
    audit = execute_confirmed_operation(
        task,
        user=None,
        action=RecommendedAction.SINK,
        reason="人工确认",
        source=source,
    )

    assert audit.result_code == "SKIPPED_UPSTREAM_NOT_NORMAL"
    assert audit.before_state["status"] == 8
    assert source.writes == 0


@pytest.mark.django_db
def test_new_confirmation_allows_a_repeated_operation_after_same_confirmation_is_deduplicated(
    monkeypatch,
):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_sink_writes", True)
    post = OriginalPost.objects.create(topic_id=208, content_id="review-208")
    CandidatePost.objects.create(
        topic_id=208, content_id="review-208", origin_type=OriginType.ORIGINAL
    )
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER", recommended_action="SINK", overall_confidence=0.9
        ),
    )

    class Source:
        states = iter((2, 8, 2, 8))
        writes = 0

        def get_post_metrics(self, topic_id):
            return {"data": {"topic": {"topic_id": topic_id, "status": next(self.states)}}}

        def perform_confirmed_operation(self, **kwargs):
            self.writes += 1
            return {"errcode": 0, "errmsg": "ok"}

    source = Source()
    first = execute_confirmed_operation(
        task,
        user=None,
        action=RecommendedAction.SINK,
        reason="无价值灌水",
        source=source,
        confirmation_key="confirmation-one",
    )
    duplicate = execute_confirmed_operation(
        task,
        user=None,
        action=RecommendedAction.SINK,
        reason="无价值灌水",
        source=source,
        confirmation_key="confirmation-one",
    )
    second = execute_confirmed_operation(
        task,
        user=None,
        action=RecommendedAction.SINK,
        reason="人工再次确认",
        source=source,
        confirmation_key="confirmation-two",
    )

    assert first.pk == duplicate.pk
    assert second.pk != first.pk
    assert source.writes == 2


def test_unverified_operation_has_no_enabled_capability(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_sink_writes", True)
    monkeypatch.setattr(runtime, "enable_block_writes", True)
    monkeypatch.setattr(runtime, "enable_mute_writes", True)
    monkeypatch.setattr(runtime, "enable_unmute_writes", True)
    monkeypatch.setattr(runtime, "enable_restore_writes", True)
    assert is_operation_enabled(RecommendedAction.SINK) is True
    assert is_operation_enabled(RecommendedAction.BLOCK) is True
    assert is_operation_enabled(RecommendedAction.MUTE) is True
    assert is_operation_enabled(RecommendedAction.UNMUTE) is True
    assert is_operation_enabled(RecommendedAction.RESTORE) is True


@pytest.mark.django_db
def test_confirmed_mute_reads_back_only_block_deadlines(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_mute_writes", True)
    post = OriginalPost.objects.create(topic_id=207, content_id="review-207")
    CandidatePost.objects.create(
        topic_id=207, content_id="review-207", origin_type=OriginType.ORIGINAL
    )
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER", recommended_action="MUTE", overall_confidence=0.9
        ),
    )

    class Source:
        reads = 0

        def get_post_metrics(self, topic_id):
            self.reads += 1
            block_end_time = 0 if self.reads == 1 else 2_000_000_000
            return {
                "data": {
                    "topic": {
                        "topic_id": topic_id,
                        "status": 1,
                        "appid": "wx-test",
                        "creater": {
                            "block_info": {"appid": "wx-test", "block_end_time": block_end_time},
                            "global_block_end_time": 0,
                            "openid": "must-not-be-audited",
                        },
                    }
                }
            }

        def perform_confirmed_operation(self, *, topic_id, action, payload):
            assert action == RecommendedAction.MUTE
            assert payload["mute_days"] == 7
            assert payload["block_reason_code"] == 8
            assert payload["reason"] == "广告欺诈"
            return {"errcode": 0, "errmsg": "ok"}

    audit = execute_confirmed_operation(
        task,
        user=None,
        action=RecommendedAction.MUTE,
        reason="广告欺诈",
        source=Source(),
        upstream_payload={"mute_days": 7, "block_reason_code": 8},
    )
    assert audit.result_code == "OK"
    assert "openid" not in str(audit.before_state)
    assert audit.after_state["local_block_end_time"] == 2_000_000_000


@pytest.mark.django_db
def test_confirmed_unmute_requires_active_local_mute_and_reads_back_release(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_unmute_writes", True)
    post = OriginalPost.objects.create(topic_id=210, content_id="review-unmute")
    CandidatePost.objects.create(
        topic_id=210, content_id="review-unmute", origin_type=OriginType.ORIGINAL
    )
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER", recommended_action="KEEP", overall_confidence=0.9
        ),
    )

    class Source:
        reads = 0
        writes = 0

        def get_post_metrics(self, topic_id):
            self.reads += 1
            block_end_time = 2_000_000_000 if self.reads == 1 else 0
            return {
                "data": {
                    "topic": {
                        "topic_id": topic_id,
                        "status": 2,
                        "appid": "wx-test",
                        "creater": {
                            "block_info": {"appid": "wx-test", "block_end_time": block_end_time},
                            "global_block_end_time": 2_000_000_000,
                        },
                    }
                }
            }

        def perform_confirmed_operation(self, *, topic_id, action, payload):
            assert action == RecommendedAction.UNMUTE
            assert payload == {}
            self.writes += 1
            return {"errcode": 0, "errmsg": "ok"}

    source = Source()
    audit = execute_confirmed_operation(
        task,
        user=None,
        action=RecommendedAction.UNMUTE,
        reason="人工确认解除禁言",
        source=source,
        confirmation_key="unmute-confirmation",
    )
    duplicate = execute_confirmed_operation(
        task,
        user=None,
        action=RecommendedAction.UNMUTE,
        reason="人工确认解除禁言",
        source=source,
        confirmation_key="unmute-confirmation",
    )

    assert audit.result_code == "OK"
    assert duplicate.pk == audit.pk
    assert source.writes == 1
    assert audit.after_state["local_block_end_time"] == 0
    assert audit.after_state["global_block_end_time"] == 2_000_000_000


@pytest.mark.django_db
def test_unmute_does_not_call_upstream_when_local_mute_is_inactive(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_unmute_writes", True)
    post = OriginalPost.objects.create(topic_id=211, content_id="unmute-inactive")
    CandidatePost.objects.create(
        topic_id=211, content_id="unmute-inactive", origin_type=OriginType.ORIGINAL
    )
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER", recommended_action="KEEP", overall_confidence=0.9
        ),
    )

    class Source:
        writes = 0

        def get_post_metrics(self, topic_id):
            return {
                "data": {
                    "topic": {
                        "topic_id": topic_id,
                        "status": 2,
                        "appid": "wx-test",
                        "creater": {"block_info": {"appid": "wx-test", "block_end_time": 0}},
                    }
                }
            }

        def perform_confirmed_operation(self, **kwargs):
            self.writes += 1
            raise AssertionError("未禁言时不得发起解除禁言请求")

    source = Source()
    audit = execute_confirmed_operation(
        task,
        user=None,
        action=RecommendedAction.UNMUTE,
        reason="人工确认解除禁言",
        source=source,
    )

    assert audit.result_code == "ACTION_FAILED"
    assert audit.error_summary == "当前游戏未处于禁言状态"
    assert source.writes == 0
