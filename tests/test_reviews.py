import json

import httpx
import pytest
from django.db import connection

from config.settings import runtime
from jobs.models import BackgroundJob
from posts.models import POST_STATUS_DELETED, CandidatePost, OriginalPost, OriginType
from reviews import reply_faq
from reviews.adapters import (
    AssessmentOutput,
    OpenAiCompatibleReviewClient,
    ReplyOutput,
    ReplyStyleOutput,
    RiskOutput,
    _normalize_reply_suggestion,
    _schema_error_summary,
    normalize_assessment_output,
    review_prompt_version,
    review_system_prompt,
)
from reviews.models import (
    OperationAudit,
    PolicyRule,
    PolicyVersion,
    RecommendedAction,
    ReviewStatus,
)
from reviews.reply_faq import CITY_LORD_APPID, select_reply_faq_rules
from reviews.services import (
    _read_mute_state_with_verification,
    create_confirmed_operation,
    execute_confirmed_operation,
    is_operation_enabled,
    persist_assessment,
)
from reviews.tasks import AI_EVALUATE_TASK, evaluate_post_handler


@pytest.mark.django_db
def test_assessment_always_creates_pending_human_review_and_safe_reply_draft():
    post = OriginalPost.objects.create(
        topic_id=201,
        content_id="review-201",
        content_text="活动建议",
        source_appid=CITY_LORD_APPID,
    )
    output = AssessmentOutput(
        content_category="SUGGESTION",
        recommended_action="KEEP",
        overall_confidence=0.9,
        reply_suggestion=ReplyOutput(
            user_intent_summary="活动建议",
            faq_rule_id="activity-suggestion",
            style_variants=[
                ReplyStyleOutput(style="WARM", draft="感谢城主的活动创意。"),
                ReplyStyleOutput(style="CONCISE", draft="已记录活动建议。"),
                ReplyStyleOutput(style="FORMAL", draft="您的活动建议已登记参考。"),
            ],
        ),
    )
    assessment, task = persist_assessment(post, output)
    assert assessment.needs_human_review is True
    assert task.status == ReviewStatus.PENDING
    assert post.reply_draft.draft == "感谢城主的活动创意。"
    assert post.reply_draft.selected_style == "WARM"
    assert len(post.reply_draft.style_variants) == 3


def test_reply_faq_candidates_require_the_matching_game_appid():
    assert select_reply_faq_rules(appid="", content="活动建议") == ()
    assert select_reply_faq_rules(appid="other-game", content="活动建议") == ()
    assert [rule.rule_id for rule in select_reply_faq_rules(
        appid=CITY_LORD_APPID, content="活动建议"
    )] == ["activity-suggestion"]


def test_reply_faq_catalog_rejects_empty_rule_keywords(monkeypatch, tmp_path):
    payload = json.loads(reply_faq._catalog_path().read_text(encoding="utf-8"))
    payload["rules"][0]["keywords"] = []
    catalog_path = tmp_path / "invalid-faq.json"
    catalog_path.write_text(json.dumps(payload), encoding="utf-8")
    reply_faq.load_city_lord_reply_faq.cache_clear()
    monkeypatch.setattr(reply_faq, "_catalog_path", lambda: catalog_path)

    with pytest.raises(ValueError, match="规则字段无效"):
        reply_faq.load_city_lord_reply_faq()

    reply_faq.load_city_lord_reply_faq.cache_clear()


@pytest.mark.django_db
def test_safe_reassessment_without_a_valid_faq_suppresses_an_older_reply_draft():
    post = OriginalPost.objects.create(
        topic_id=220,
        content_id="reply-draft-stale",
        content_text="活动建议",
        source_appid=CITY_LORD_APPID,
    )
    persist_assessment(
        post,
        AssessmentOutput(
            content_category="SUGGESTION",
            recommended_action="KEEP",
            overall_confidence=0.9,
            reply_suggestion=ReplyOutput(
                user_intent_summary="活动建议",
                faq_rule_id="activity-suggestion",
                style_variants=[
                    ReplyStyleOutput(style="WARM", draft="感谢城主的活动创意。"),
                    ReplyStyleOutput(style="CONCISE", draft="已记录活动建议。"),
                    ReplyStyleOutput(style="FORMAL", draft="您的活动建议已登记参考。"),
                ],
            ),
        ),
    )

    persist_assessment(
        post,
        AssessmentOutput(
            content_category="SUGGESTION",
            recommended_action="KEEP",
            overall_confidence=0.9,
            reply_suggestion=ReplyOutput(
                user_intent_summary="活动建议",
                faq_rule_id="unknown-rule",
                style_variants=[
                    ReplyStyleOutput(style="WARM", draft="感谢城主的活动创意。"),
                    ReplyStyleOutput(style="CONCISE", draft="已记录活动建议。"),
                    ReplyStyleOutput(style="FORMAL", draft="您的活动建议已登记参考。"),
                ],
            ),
        ),
    )

    post.reply_draft.refresh_from_db()
    assert post.reply_draft.suppressed_reason == "当前评估未命中有效受控 FAQ"


@pytest.mark.django_db
def test_assessment_suppresses_reply_draft_for_risky_human_review():
    post = OriginalPost.objects.create(
        topic_id=202, content_id="review-202", content_text="加群领取福利"
    )
    output = AssessmentOutput(
        content_category="FEEDBACK",
        recommended_action="HUMAN_REVIEW",
        overall_confidence=0.7,
        risk_labels=[
            RiskOutput(
                label="TRAFFIC_DIVERSION", confidence=0.7, evidence=["加群领取福利"]
            )
        ],
        reply_suggestion=ReplyOutput(user_intent_summary="反馈", reply_draft="感谢反馈"),
    )

    _, task = persist_assessment(post, output)

    assert task.suggested_action == RecommendedAction.HUMAN_REVIEW
    assert not hasattr(post, "reply_draft")


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


def test_normalize_assessment_output_compacts_and_merges_repeated_risk_evidence():
    normalized = normalize_assessment_output(
        {
            "risk_labels": [
                {
                    "label": "TRAFFIC_DIVERSION",
                    "confidence": 0.6,
                    "evidence": ["相同证据", "相 同 证 据！", "第二条", "第三条"],
                },
                {
                    "label": "TRAFFIC_DIVERSION",
                    "confidence": 0.9,
                    "evidence": ["第二条", "第四条"],
                },
            ]
        }
    )

    assert normalized["risk_labels"] == [
        {
            "label": "TRAFFIC_DIVERSION",
            "confidence": 0.9,
            "evidence": ["相同证据", "第二条", "第三条"],
        }
    ]


def test_normalize_assessment_output_accepts_named_reply_style_mapping():
    normalized = normalize_assessment_output(
        {
            "reply_suggestion": {
                "user_intent_summary": "活动建议",
                "faq_rule_id": "activity-suggestion",
                "style_variants": {
                    "亲和互动": "感谢你的建议。",
                    "CONCISE": "已记录建议。",
                    "正式稳妥": "建议已登记参考。",
                },
            }
        }
    )

    assert normalized["reply_suggestion"]["style_variants"] == [
        {"style": "WARM", "draft": "感谢你的建议。"},
        {"style": "CONCISE", "draft": "已记录建议。"},
        {"style": "FORMAL", "draft": "建议已登记参考。"},
    ]


def test_reply_suggestion_normalization_leaves_unknown_style_for_strict_rejection():
    normalized = _normalize_reply_suggestion(
        {"style_variants": {"未知风格": "不应被伪造为有效风格"}}
    )

    assert normalized == {
        "style_variants": [{"style": "未知风格", "draft": "不应被伪造为有效风格"}]
    }


def test_schema_diagnostic_only_reports_field_paths_not_model_content():
    with pytest.raises(Exception) as captured:
        AssessmentOutput.model_validate(
            {
                "content_category": "OTHER",
                "recommended_action": "KEEP",
                "overall_confidence": "not-a-number",
                "reply_suggestion": {"user_intent_summary": "包含不应写入日志的帖子原文"},
            }
        )

    summary = _schema_error_summary(captured.value)

    assert "overall_confidence" in summary
    assert "包含不应写入日志的帖子原文" not in summary


def test_review_prompt_adds_video_rules_only_when_video_evidence_is_present():
    without_video = review_system_prompt(has_video_evidence=False)
    with_video = review_system_prompt(has_video_evidence=True)

    assert "【视频审核规则】" not in without_video
    assert "【视频审核规则】" in with_video
    assert "对同一视频风险必须形成一次聚合结论" in with_video
    assert "强引流组合" in with_video


def test_faq_prompt_requires_three_styles_to_preserve_controlled_facts():
    rules = select_reply_faq_rules(appid=CITY_LORD_APPID, content="充值未到账")

    prompt = review_system_prompt(has_video_evidence=False, faq_rules=rules)

    assert "三项必须以同一 faq_rule_id 的受控要点、待补充信息、来源和禁止承诺为准" in prompt
    assert "仅可改变语气、篇幅和互动方式" in prompt
    assert review_prompt_version(
        has_video_evidence=False, faq_version="city-lord-reply-faq-v1"
    ) == "review-v5-city-lord-reply-faq-v1"


def test_review_request_contains_only_textual_assessment_context(monkeypatch):
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "content_category": "OTHER",
                                    "secondary_categories": [],
                                    "risk_labels": [],
                                    "sentiment": "",
                                    "feedback_value": "",
                                    "recommended_action": "KEEP",
                                    "overall_confidence": 0.5,
                                    "needs_human_review": True,
                                    "reply_suggestion": None,
                                }
                            )
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(runtime, "ai_base_url", "https://ai.example")
    monkeypatch.setattr(runtime, "ai_api_key", "test-key")
    monkeypatch.setattr(runtime, "ai_model", "test-model")
    client = OpenAiCompatibleReviewClient(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    client.evaluate("正文", "[视频帧文字 · 视频 1 · 00:01.000] 加群领取", has_video_evidence=True)

    payload = json.dumps(captured, ensure_ascii=False)
    assert "视频帧文字" in payload
    for forbidden in ("https://media.example", "cookie", "task_id", "policy_version", "data:image"):
        assert forbidden not in payload.casefold()


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
def test_explicit_qr_contact_can_block_while_high_reward_stays_in_focus():
    post = OriginalPost.objects.create(topic_id=216, content_id="review-216", content_text="内容")
    policy = PolicyVersion.objects.create(name="traffic", version="1", is_active=True)
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="TRAFFIC_DIVERSION",
        min_confidence=0.85,
        recommended_action=RecommendedAction.BLOCK,
        priority=90,
    )
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="ACTIVITY",
            recommended_action=RecommendedAction.BLOCK,
            overall_confidence=0.92,
            risk_labels=[
                RiskOutput(
                    label="TRAFFIC_DIVERSION",
                    confidence=0.95,
                    evidence=["00:04.250 微信扫码咨询客服", "00:04.250 检测到二维码", "10万奖励"],
                )
            ],
        ),
        policy,
    )

    assert task.suggested_action == RecommendedAction.BLOCK
    assert task.needs_focus is True


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

        def evaluate(self, content, ocr_text, *, has_video_evidence=False, faq_rules=()):
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
                            "openid": "unmute-author",
                        },
                    }
                }
            }

        def perform_confirmed_operation(self, *, topic_id, action, payload):
            assert action == RecommendedAction.UNMUTE
            assert payload == {"author_openid": "unmute-author"}
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
                        "creater": {
                            "block_info": {"appid": "wx-test", "block_end_time": 0},
                            "openid": "unmute-author",
                        },
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


@pytest.mark.django_db
def test_mute_rejects_when_preflight_author_differs_from_saved_value(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_mute_writes", True)
    post = OriginalPost.objects.create(
        topic_id=213, content_id="mute-mismatch", source_appid="wx-test", author_openid="old-author"
    )
    CandidatePost.objects.create(topic_id=213, content_id="mute-mismatch", origin_type=OriginType.ORIGINAL)
    _, task = persist_assessment(
        post, AssessmentOutput(content_category="OTHER", recommended_action="MUTE", overall_confidence=0.9)
    )

    class Source:
        writes = 0

        def get_post_metrics(self, topic_id):
            return {"data": {"topic": {"topic_id": topic_id, "status": 2, "appid": "wx-test", "creater": {"openid": "new-author", "block_info": {"appid": "wx-test", "block_end_time": 0}, "global_block_end_time": 0}}}}

        def perform_confirmed_operation(self, **kwargs):
            self.writes += 1

    source = Source()
    audit = execute_confirmed_operation(
        task, user=None, action=RecommendedAction.MUTE, reason="人工确认", source=source,
        upstream_payload={"mute_days": 1, "block_reason_code": 1},
    )
    post.refresh_from_db()
    assert audit.result_code == "ACTION_FAILED"
    assert audit.error_summary.endswith("ValueError")
    assert source.writes == 0
    assert post.author_openid == "new-author"


@pytest.mark.django_db
def test_mute_reads_at_bounded_delays_without_repeating_write(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_mute_writes", True)
    delays = []
    monkeypatch.setattr("reviews.services.sleep", delays.append)
    post = OriginalPost.objects.create(topic_id=214, content_id="mute-delayed", source_appid="wx-test")
    CandidatePost.objects.create(topic_id=214, content_id="mute-delayed", origin_type=OriginType.ORIGINAL)
    _, task = persist_assessment(
        post, AssessmentOutput(content_category="OTHER", recommended_action="MUTE", overall_confidence=0.9)
    )

    class Source:
        reads = 0
        writes = 0

        def get_post_metrics(self, topic_id):
            self.reads += 1
            end = 2_000_000_000 if self.reads == 3 else 0
            return {"data": {"topic": {"topic_id": topic_id, "status": 2, "appid": "wx-test", "creater": {"openid": "delayed-author", "block_info": {"appid": "wx-test", "block_end_time": end}, "global_block_end_time": 0}}}}

        def perform_confirmed_operation(self, **kwargs):
            self.writes += 1
            return {"errcode": 0}

    source = Source()
    audit = execute_confirmed_operation(
        task, user=None, action=RecommendedAction.MUTE, reason="人工确认", source=source,
        upstream_payload={"mute_days": 1, "block_reason_code": 1},
    )
    assert audit.result_code == "OK"
    assert source.writes == 1
    assert source.reads == 3
    assert delays == [2]


@pytest.mark.django_db
def test_mute_readback_never_exceeds_the_twenty_second_budget(monkeypatch):
    clock = [0.0]
    sleeps = []
    monkeypatch.setattr("reviews.services.monotonic", lambda: clock[0])

    def advance_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("reviews.services.sleep", advance_sleep)
    post = OriginalPost.objects.create(topic_id=216, content_id="mute-readback-budget")

    class Source:
        timeout_values = []

        def get_post_metrics_with_timeout(self, topic_id, *, timeout_seconds):
            self.timeout_values.append(timeout_seconds)
            clock[0] += timeout_seconds
            return {
                "data": {
                    "topic": {
                        "topic_id": topic_id,
                        "status": 2,
                        "appid": "wx-test",
                        "creater": {
                            "openid": "budget-author",
                            "block_info": {"appid": "wx-test", "block_end_time": 0},
                            "global_block_end_time": 0,
                        },
                    }
                }
            }

    source = Source()
    _read_mute_state_with_verification(
        source, post, action=RecommendedAction.MUTE, expected_end_time=2_000_000_000
    )

    assert len(source.timeout_values) == 1
    assert source.timeout_values[0] < 20
    assert sleeps == []
    assert clock[0] < 20


@pytest.mark.django_db
def test_mute_keeps_action_failed_when_write_raises_even_if_readback_matches(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_mute_writes", True)
    monkeypatch.setattr("reviews.services.sleep", lambda _seconds: None)
    post = OriginalPost.objects.create(topic_id=215, content_id="mute-write-failed", source_appid="wx-test")
    CandidatePost.objects.create(topic_id=215, content_id="mute-write-failed", origin_type=OriginType.ORIGINAL)
    _, task = persist_assessment(
        post, AssessmentOutput(content_category="OTHER", recommended_action="MUTE", overall_confidence=0.9)
    )

    class Source:
        reads = 0
        writes = 0

        def get_post_metrics(self, topic_id):
            self.reads += 1
            end = 0 if self.reads == 1 else 2_000_000_000
            return {"data": {"topic": {"topic_id": topic_id, "status": 2, "appid": "wx-test", "creater": {"openid": "failed-author", "block_info": {"appid": "wx-test", "block_end_time": end}, "global_block_end_time": 0}}}}

        def perform_confirmed_operation(self, **kwargs):
            self.writes += 1
            raise TimeoutError("uncertain")

    source = Source()
    audit = execute_confirmed_operation(
        task, user=None, action=RecommendedAction.MUTE, reason="人工确认", source=source,
        upstream_payload={"mute_days": 1, "block_reason_code": 1},
    )
    assert audit.result_code == "ACTION_FAILED"
    assert "回读状态已同步" in audit.error_summary
    assert audit.after_state["local_block_end_time"] == 2_000_000_000
    assert source.writes == 1
