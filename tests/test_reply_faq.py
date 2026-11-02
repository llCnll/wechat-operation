import json

import pytest

from posts.models import OriginalPost
from reviews import reply_faq
from reviews.adapters import AssessmentOutput, ReplyOutput, ReplyStyleOutput, RiskOutput
from reviews.models import RecommendedAction
from reviews.reply_faq import (
    available_reply_faq_rules,
    available_reply_faq_version,
    community_reply_faq_rule,
    reply_faq_prompt,
    select_reply_faq_rules,
)
from reviews.services import persist_assessment

SAMPLE_APPID = "sample-game"


def _styles(prefix: str = "回复") -> list[ReplyStyleOutput]:
    return [
        ReplyStyleOutput(style="WARM", draft=f"{prefix}：感谢你的分享。"),
        ReplyStyleOutput(style="CONCISE", draft=f"{prefix}：已收到。"),
        ReplyStyleOutput(style="FORMAL", draft=f"{prefix}：已记录并感谢反馈。"),
    ]


def _faq_output(rule_id: str, *, styles: list[ReplyStyleOutput] | None = None) -> AssessmentOutput:
    return AssessmentOutput(
        content_category="QUESTION",
        recommended_action=RecommendedAction.KEEP,
        overall_confidence=0.9,
        reply_suggestion=ReplyOutput(
            user_intent_summary="玩家诉求",
            faq_rule_id=rule_id,
            style_variants=styles or _styles(rule_id),
        ),
    )


def _write_invalid_catalog(monkeypatch, tmp_path, mutate) -> None:
    payload = json.loads(reply_faq._catalog_path().read_text(encoding="utf-8"))
    mutate(payload)
    catalog_path = tmp_path / "invalid-faq.json"
    catalog_path.write_text(json.dumps(payload), encoding="utf-8")
    reply_faq.load_community_reply_faq.cache_clear()
    monkeypatch.setattr(reply_faq, "_catalog_path", lambda: catalog_path)


def test_catalog_loads_generic_rules_and_prompt_keeps_prohibitions():
    catalog = reply_faq.load_community_reply_faq()

    assert len(catalog.rules) == 22
    payment = community_reply_faq_rule("payment-order")
    assert payment is not None
    assert payment.missing_information == ("问题发生时间", "订单状态")
    assert "禁止承诺=到账或退款承诺、处理时限、补偿" in reply_faq_prompt((payment,))


def test_catalog_allows_versioned_rule_additions(monkeypatch, tmp_path):
    def add_rule(payload):
        payload["rules"].append(
            {
                **payload["rules"][0],
                "id": "new-reviewed-rule",
                "keywords": ["新运营场景"],
            }
        )

    _write_invalid_catalog(monkeypatch, tmp_path, add_rule)
    try:
        assert len(reply_faq.load_community_reply_faq().rules) == 23
    finally:
        reply_faq.load_community_reply_faq.cache_clear()


def test_catalog_rejects_invalid_version_and_optional_guidance_fails_closed(monkeypatch, tmp_path):
    _write_invalid_catalog(monkeypatch, tmp_path, lambda payload: payload.update(version=""))
    try:
        with pytest.raises(ValueError, match="有效版本"):
            reply_faq.load_community_reply_faq()
        assert available_reply_faq_rules(content="活动建议") == ()
        assert available_reply_faq_version() == ""
        assert community_reply_faq_rule("event-suggestion") is None
    finally:
        reply_faq.load_community_reply_faq.cache_clear()


@pytest.mark.django_db
def test_generic_faq_version_and_rules_apply_to_multiple_game_scopes():
    version = available_reply_faq_version()
    assert version == "community-reply-faq-v1"
    for index, appid in enumerate(("sample-game-a", "sample-game-b")):
        post = OriginalPost.objects.create(
            topic_id=9390 + index,
            content_id=f"faq-generic-scope-{index}",
            content_text="活动建议",
            source_appid=appid,
        )
        persist_assessment(post, _faq_output("event-suggestion"))
        assert post.reply_draft.faq_rule_id == "event-suggestion"
        assert post.reply_draft.faq_version == version


@pytest.mark.parametrize(
    ("content", "rule_id", "missing_information"),
    [
        (
            "功能异常，操作时出现错误提示",
            "issue-report",
            ("发生时间", "操作步骤", "提示信息或脱敏截图"),
        ),
        ("支付后订单状态仍未更新", "payment-order", ("问题发生时间", "订单状态")),
        (
            "登录失败，提示授权异常",
            "access-authorization",
            ("发生时间", "操作步骤", "提示信息"),
        ),
        ("这次使用体验很差，确实不满意", "complaint", ("具体情形", "发生时间")),
        ("这个功能该怎么使用，求助", "help-question", ("相关场景", "已尝试的步骤")),
        ("活动规则和截止时间是什么", "event-question", ("活动名称", "具体疑问")),
        ("分享一下操作技巧", "guide-share", ()),
    ],
)
def test_candidate_selection_covers_controlled_faq_scenarios(content, rule_id, missing_information):
    candidates = select_reply_faq_rules(content=content)
    rule = next(rule for rule in candidates if rule.rule_id == rule_id)

    assert rule.missing_information == missing_information
    assert rule.source.startswith("项目内通用 FAQ 示例")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("topic_id", "content", "rule_id"),
    [
        (9401, "功能异常，出现错误提示", "issue-report"),
        (9402, "支付后订单状态未更新", "payment-order"),
        (9403, "登录失败，提示授权异常", "access-authorization"),
        (9404, "这次使用体验很差，确实不满意", "complaint"),
        (9405, "这个功能怎么使用，求助", "help-question"),
        (9406, "活动规则怎么参加", "event-question"),
        (9407, "分享一下操作技巧", "guide-share"),
    ],
)
def test_persisted_faq_draft_uses_controlled_sources_and_missing_information(
    topic_id, content, rule_id
):
    post = OriginalPost.objects.create(
        topic_id=topic_id,
        content_id=f"faq-{topic_id}",
        content_text=content,
        source_appid=SAMPLE_APPID,
    )
    persist_assessment(post, _faq_output(rule_id))
    rule = community_reply_faq_rule(rule_id)

    assert rule is not None
    assert post.reply_draft.faq_rule_id == rule_id
    assert post.reply_draft.knowledge_sources == [rule.source]
    assert post.reply_draft.missing_information == list(rule.missing_information)
    assert post.reply_draft.faq_version == reply_faq.load_community_reply_faq().version


@pytest.mark.django_db
def test_safe_reevaluation_replaces_old_draft_and_resets_local_adoption():
    post = OriginalPost.objects.create(
        topic_id=9450,
        content_id="faq-safe-reevaluation",
        content_text="活动建议",
        source_appid=SAMPLE_APPID,
    )
    persist_assessment(post, _faq_output("event-suggestion"))
    post.reply_draft.adopted = True
    post.reply_draft.adopted_with_edits = True
    post.reply_draft.save(update_fields=("adopted", "adopted_with_edits"))

    persist_assessment(post, _faq_output("event-suggestion", styles=_styles("重新评估")))

    post.reply_draft.refresh_from_db()
    assert post.reply_draft.draft == "重新评估：感谢你的分享。"
    assert post.reply_draft.adopted is False
    assert post.reply_draft.adopted_with_edits is False


@pytest.mark.django_db
def test_safe_unmatched_faq_generates_a_labeled_generic_local_draft():
    post = OriginalPost.objects.create(
        topic_id=9460,
        content_id="faq-unmatched-generic",
        content_text="就问你还有多少和我一样的",
        source_appid=SAMPLE_APPID,
    )

    persist_assessment(post, _faq_output("", styles=_styles("通用互动")))

    draft = post.reply_draft
    assert draft.faq_rule_id == ""
    assert draft.faq_version == ""
    assert draft.knowledge_sources == []
    assert draft.missing_information == []
    assert draft.suppressed_reason == ""
    assert draft.style_variants[0]["draft"] == "通用互动：感谢你的分享。"


@pytest.mark.django_db
def test_unknown_nonempty_faq_rule_still_suppresses_an_existing_draft():
    post = OriginalPost.objects.create(
        topic_id=9461,
        content_id="faq-unknown-rule",
        content_text="活动建议",
        source_appid=SAMPLE_APPID,
    )
    persist_assessment(post, _faq_output("event-suggestion"))

    persist_assessment(post, _faq_output("unknown-rule"))

    post.reply_draft.refresh_from_db()
    assert post.reply_draft.suppressed_reason == "当前评估未命中有效受控 FAQ"


@pytest.mark.django_db
def test_risky_unmatched_faq_never_creates_a_generic_draft():
    post = OriginalPost.objects.create(
        topic_id=9462,
        content_id="faq-unmatched-risk",
        content_text="加群领取福利",
        source_appid=SAMPLE_APPID,
    )
    output = _faq_output("")
    output.recommended_action = RecommendedAction.HUMAN_REVIEW
    output.risk_labels = [
        RiskOutput(label="TRAFFIC_DIVERSION", confidence=0.9, evidence=["加群领取福利"])
    ]

    persist_assessment(post, output)

    assert not hasattr(post, "reply_draft")


@pytest.mark.django_db
def test_transaction_risk_unmatched_faq_never_creates_a_generic_draft():
    post = OriginalPost.objects.create(
        topic_id=9465,
        content_id="faq-unmatched-transaction-risk",
        content_text="求问交易相关问题",
        source_appid=SAMPLE_APPID,
    )
    output = _faq_output("")
    output.content_category = "QUESTION"
    output.recommended_action = RecommendedAction.HUMAN_REVIEW
    output.risk_labels = [
        RiskOutput(label="TRANSACTION_RISK", confidence=0.7, evidence=["交易"])
    ]

    persist_assessment(post, output)

    assert not hasattr(post, "reply_draft")


@pytest.mark.django_db
def test_unmatched_off_topic_content_never_creates_a_generic_draft():
    post = OriginalPost.objects.create(
        topic_id=9463,
        content_id="faq-unmatched-off-topic",
        content_text="与游戏无关的内容",
        source_appid=SAMPLE_APPID,
    )
    output = _faq_output("")
    output.content_category = "OFF_TOPIC"

    persist_assessment(post, output)

    assert not hasattr(post, "reply_draft")


@pytest.mark.django_db
def test_unavailable_faq_catalog_never_generates_a_generic_draft(monkeypatch):
    post = OriginalPost.objects.create(
        topic_id=9464,
        content_id="faq-unavailable-generic",
        content_text="安全社区互动",
        source_appid=SAMPLE_APPID,
    )
    monkeypatch.setattr("reviews.services.reply_faq_guidance_is_available", lambda **_kwargs: False)

    persist_assessment(post, _faq_output(""))

    assert not hasattr(post, "reply_draft")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "styles",
    [
        _styles()[:2],
        [
            ReplyStyleOutput(style="UNKNOWN", draft="感谢你的活动建议。"),
            ReplyStyleOutput(style="CONCISE", draft="已收到活动建议。"),
            ReplyStyleOutput(style="FORMAL", draft="活动建议已登记。"),
        ],
        [
            ReplyStyleOutput(style="WARM", draft="感谢你的活动建议。"),
            ReplyStyleOutput(style="CONCISE", draft="感谢你的活动建议！"),
            ReplyStyleOutput(style="FORMAL", draft="活动建议已登记。"),
        ],
    ],
)
def test_invalid_style_output_never_creates_a_faq_draft(styles):
    post = OriginalPost.objects.create(
        topic_id=9500 + len(styles),
        content_id=f"faq-invalid-style-{len(styles)}-{styles[0].style}",
        content_text="活动建议",
        source_appid=SAMPLE_APPID,
    )
    persist_assessment(post, _faq_output("event-suggestion", styles=styles))

    assert not hasattr(post, "reply_draft")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "risk_label",
    [
        "TRAFFIC_DIVERSION",
        "PRIVATE_SERVER",
        "CHEAT",
        "FRAUD_SCAM",
        "PORNOGRAPHY",
        "PERSONAL_ATTACK",
    ],
)
def test_all_public_reply_suppression_labels_block_human_review_drafts(risk_label):
    post = OriginalPost.objects.create(
        topic_id=9600 + len(risk_label),
        content_id=f"faq-risk-{risk_label}",
        content_text="活动建议",
        source_appid=SAMPLE_APPID,
    )
    output = _faq_output("event-suggestion")
    output.recommended_action = RecommendedAction.HUMAN_REVIEW
    output.risk_labels = [RiskOutput(label=risk_label, confidence=0.9, evidence=["风险原文"])]

    persist_assessment(post, output)

    assert not hasattr(post, "reply_draft")


@pytest.mark.django_db
def test_suspected_player_draft_stays_neutral_and_requests_evidence():
    post = OriginalPost.objects.create(
        topic_id=9701,
        content_id="faq-suspected-misconduct",
        content_text="疑似违规行为，请核查",
        source_appid=SAMPLE_APPID,
    )
    output = _faq_output(
        "suspected-misconduct",
        styles=[
            ReplyStyleOutput(
                style="WARM", draft="感谢反馈，辛苦补充截图或录屏并联系官方客服核查。"
            ),
            ReplyStyleOutput(style="CONCISE", draft="请补充截图或录屏，并通过官方客服反馈。"),
            ReplyStyleOutput(
                style="FORMAL", draft="请提供相关截图或录屏，方便通过官方客服进一步核查。"
            ),
        ],
    )

    persist_assessment(post, output)

    assert post.reply_draft.faq_rule_id == "suspected-misconduct"
    assert post.reply_draft.missing_information == ["可核实的材料"]
    assert all(
        forbidden not in variant["draft"]
        for variant in post.reply_draft.style_variants
        for forbidden in ("作弊", "处罚", "处理时限")
    )
