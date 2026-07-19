from django.conf import settings
from django.db import models

from posts.models import OriginalPost, PostMedia


class ContentCategory(models.TextChoices):
    GUIDE = "GUIDE", "攻略"
    FEEDBACK = "FEEDBACK", "反馈"
    SUGGESTION = "SUGGESTION", "建议"
    QUESTION = "QUESTION", "求助问答"
    ACTIVITY = "ACTIVITY", "活动资讯"
    SOCIAL = "SOCIAL", "晒图社交"
    OFF_TOPIC = "OFF_TOPIC", "无关内容"
    OTHER = "OTHER", "其他"


class RecommendedAction(models.TextChoices):
    KEEP = "KEEP", "保留"
    SINK = "SINK", "沉底"
    BLOCK = "BLOCK", "屏蔽"
    MUTE = "MUTE", "禁言"
    UNMUTE = "UNMUTE", "解除禁言"
    RESTORE = "RESTORE", "恢复正常"
    BLOCK_AND_MUTE = "BLOCK_AND_MUTE", "屏蔽并禁言"
    HUMAN_REVIEW = "HUMAN_REVIEW", "人工判断"


RISK_LABEL_DISPLAY = {
    "TRAFFIC_DIVERSION": "引流/外部联系方式",
    "PRIVATE_SERVER": "私服/盗版服务",
    "CHEAT": "外挂/作弊",
    "PERSONAL_ATTACK": "人身攻击/辱骂",
    "FRAUD_SCAM": "欺诈/诈骗",
    "PORNOGRAPHY": "色情低俗",
    "POLITICAL_SENSITIVE": "政治敏感",
    "ADVERTISEMENT_SPAM": "广告/垃圾内容",
    "TRANSACTION_RISK": "交易/资金风险",
    "OFF_TOPIC": "无关游戏圈内容",
    "NEGATIVE_SPAM": "纯负面灌水",
    "CONTENT_ANOMALY": "内容异常，需人工核验",
    # Compatibility for historical model output. New evaluations use the generic labels above.
    "SUSPICIOUS_TRANSACTION_SCREENSHOT": "交易/资金风险",
    "FINANCIAL_TRANSACTION": "交易/资金风险",
    "LEGAL_SERVICE_REFERENCE": "引流/外部联系方式",
    "HIGH_VALUE_PAYMENT": "交易/资金风险",
    "CONTEXT_MISMATCH_BETWEEN_TEXT_AND_OCR": "内容异常，需人工核验",
}


def risk_label_display(label: str) -> str:
    """Keep unknown model labels understandable without losing their raw code in storage."""
    return RISK_LABEL_DISPLAY.get(label, f"待归类风险（{label}）")


class ReviewStatus(models.TextChoices):
    PENDING = "PENDING", "待审核"
    REVIEWED = "REVIEWED", "已审核"
    CANCELED = "CANCELED", "已取消"


class OcrExtraction(models.Model):
    post = models.ForeignKey(OriginalPost, on_delete=models.CASCADE, related_name="ocr_extractions")
    media = models.ForeignKey(PostMedia, on_delete=models.CASCADE, related_name="ocr_extractions")
    text = models.TextField(blank=True)
    confidence = models.FloatField(null=True, blank=True)
    provider = models.CharField(max_length=100)
    error_summary = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class PolicyVersion(models.Model):
    name = models.CharField(max_length=100, unique=True)
    version = models.CharField(max_length=100)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class PolicyRule(models.Model):
    policy_version = models.ForeignKey(
        PolicyVersion, on_delete=models.CASCADE, related_name="rules"
    )
    risk_label = models.CharField(max_length=100)
    min_confidence = models.FloatField(default=0)
    recommended_action = models.CharField(max_length=32, choices=RecommendedAction.choices)
    priority = models.PositiveSmallIntegerField(default=50)
    exceptions = models.JSONField(default=list, blank=True)
    enabled = models.BooleanField(default=True)


class AiAssessment(models.Model):
    post = models.ForeignKey(OriginalPost, on_delete=models.CASCADE, related_name="ai_assessments")
    category = models.CharField(max_length=32, choices=ContentCategory.choices)
    secondary_categories = models.JSONField(default=list, blank=True)
    sentiment = models.CharField(max_length=32, blank=True)
    feedback_value = models.CharField(max_length=32, blank=True)
    recommended_action = models.CharField(max_length=32, choices=RecommendedAction.choices)
    overall_confidence = models.FloatField()
    needs_human_review = models.BooleanField(default=True)
    model_version = models.CharField(max_length=200)
    prompt_version = models.CharField(max_length=100, blank=True)
    policy_version = models.ForeignKey(
        PolicyVersion, null=True, blank=True, on_delete=models.SET_NULL
    )
    raw_error_summary = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class RiskEvidence(models.Model):
    assessment = models.ForeignKey(
        AiAssessment, on_delete=models.CASCADE, related_name="risk_evidence"
    )
    label = models.CharField(max_length=100)
    confidence = models.FloatField()
    evidence = models.JSONField(default=list)


class ReviewTask(models.Model):
    post = models.OneToOneField(OriginalPost, on_delete=models.CASCADE, related_name="review_task")
    assessment = models.ForeignKey(AiAssessment, null=True, blank=True, on_delete=models.SET_NULL)
    suggested_action = models.CharField(max_length=32, choices=RecommendedAction.choices)
    priority = models.PositiveSmallIntegerField(default=50)
    status = models.CharField(
        max_length=16, choices=ReviewStatus.choices, default=ReviewStatus.PENDING
    )
    needs_focus = models.BooleanField(default=False)
    final_action = models.CharField(max_length=32, choices=RecommendedAction.choices, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class ReplyDraft(models.Model):
    post = models.OneToOneField(OriginalPost, on_delete=models.CASCADE, related_name="reply_draft")
    assessment = models.ForeignKey(AiAssessment, on_delete=models.CASCADE)
    intent_summary = models.TextField()
    draft = models.TextField()
    missing_information = models.JSONField(default=list)
    knowledge_sources = models.JSONField(default=list)
    risk_notice = models.TextField(blank=True)
    adopted = models.BooleanField(default=False)
    adopted_with_edits = models.BooleanField(default=False)
    suppressed_reason = models.CharField(max_length=500, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class OperationAudit(models.Model):
    task = models.ForeignKey(ReviewTask, on_delete=models.PROTECT, related_name="operation_audits")
    operation_id = models.CharField(max_length=64, unique=True)
    idempotency_key = models.CharField(max_length=255, unique=True)
    action = models.CharField(max_length=32, choices=RecommendedAction.choices)
    reason = models.CharField(max_length=500)
    policy_version = models.ForeignKey(
        PolicyVersion, null=True, blank=True, on_delete=models.SET_NULL
    )
    before_state = models.JSONField(default=dict)
    after_state = models.JSONField(default=dict)
    result_code = models.CharField(max_length=100, blank=True)
    error_summary = models.CharField(max_length=500, blank=True)
    executor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    executed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
