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
    AUTO_EXECUTED_PENDING_REVIEW = "AUTO_EXECUTED_PENDING_REVIEW", "AI 已执行待复审"
    AUTO_REVIEW_ESCALATED = "AUTO_REVIEW_ESCALATED", "AI 复审异常待跟进"
    REVIEWED = "REVIEWED", "已审核"
    CANCELED = "CANCELED", "已取消"


class OperationExecutionSource(models.TextChoices):
    HUMAN = "HUMAN", "人工"
    AI_AUTOMATED = "AI_AUTOMATED", "AI 自动"
    SYSTEM_MAINTENANCE = "SYSTEM_MAINTENANCE", "系统维护"


class MuteOperationTrigger(models.TextChoices):
    """Explain why a mute-related audit exists without retaining source payloads."""

    HUMAN_INITIAL = "HUMAN_INITIAL", "人工首次禁言"
    HUMAN_PERMANENT = "HUMAN_PERMANENT", "人工永久管理"
    SYSTEM_RENEWAL = "SYSTEM_RENEWAL", "系统续期"
    SYSTEM_REPAIR = "SYSTEM_REPAIR", "系统续期修复"


class AiOperationReviewStatus(models.TextChoices):
    PENDING = "PENDING", "待复审"
    CONFIRMED = "CONFIRMED", "已确认正确"
    ESCALATED = "ESCALATED", "已升级人工跟进"
    RESTORED = "RESTORED", "已人工恢复"


class FocusReason(models.TextChoices):
    EMPTY_BODY = "EMPTY_BODY", "正文为空"
    OCR_FAILED = "OCR_FAILED", "图片 OCR 失败"
    EVIDENCE_MISSING = "EVIDENCE_MISSING", "风险依据缺失"
    VIDEO_INCOMPLETE = "VIDEO_INCOMPLETE", "视频证据不完整"
    HIGH_REWARD = "HIGH_REWARD", "高额奖励待核验"


class OcrExtraction(models.Model):
    post = models.ForeignKey(OriginalPost, on_delete=models.CASCADE, related_name="ocr_extractions")
    media = models.ForeignKey(PostMedia, on_delete=models.CASCADE, related_name="ocr_extractions")
    text = models.TextField(blank=True)
    confidence = models.FloatField(null=True, blank=True)
    provider = models.CharField(max_length=100)
    error_summary = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "OCR 提取"
        verbose_name_plural = "OCR 提取"


class VideoExtractionStatus(models.TextChoices):
    PENDING = "PENDING", "待提取"
    RUNNING = "RUNNING", "提取中"
    SUCCEEDED = "SUCCEEDED", "已完成"
    PARTIAL = "PARTIAL", "部分完成"
    FAILED = "FAILED", "失败"
    CANCELED = "CANCELED", "已取消"
    SKIPPED_LIMIT = "SKIPPED_LIMIT", "受资源限制"


class VideoEvidenceKind(models.TextChoices):
    FRAME_OCR = "FRAME_OCR", "视频帧文字"
    QR_CODE = "QR_CODE", "二维码"
    AUDIO_TRANSCRIPT = "AUDIO_TRANSCRIPT", "视频语音"


class VideoExtraction(models.Model):
    post = models.ForeignKey(
        OriginalPost, on_delete=models.CASCADE, related_name="video_extractions"
    )
    media = models.ForeignKey(PostMedia, on_delete=models.CASCADE, related_name="video_extractions")
    media_fingerprint = models.CharField(max_length=64)
    extractor_version = models.CharField(max_length=64, default="v1")
    attempt = models.PositiveIntegerField(default=1)
    status = models.CharField(
        max_length=20, choices=VideoExtractionStatus.choices, default=VideoExtractionStatus.PENDING
    )
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    duration_limit_seconds = models.PositiveIntegerField(null=True, blank=True)
    frame_count = models.PositiveIntegerField(default=0)
    error_summary = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("media", "media_fingerprint", "extractor_version", "attempt"),
                name="unique_video_extraction_attempt",
            )
        ]
        verbose_name = "视频提取"
        verbose_name_plural = "视频提取"


class VideoEvidence(models.Model):
    extraction = models.ForeignKey(
        VideoExtraction, on_delete=models.CASCADE, related_name="evidence"
    )
    kind = models.CharField(max_length=32, choices=VideoEvidenceKind.choices)
    start_ms = models.PositiveIntegerField()
    end_ms = models.PositiveIntegerField(null=True, blank=True)
    sequence = models.PositiveIntegerField(default=0)
    text = models.TextField(blank=True)
    confidence = models.FloatField(null=True, blank=True)
    provider = models.CharField(max_length=100, blank=True)
    error_summary = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(kind="QR_CODE", text="检测到二维码") | ~models.Q(kind="QR_CODE"),
                name="qr_evidence_has_fixed_text",
            ),
            models.UniqueConstraint(
                fields=("extraction", "kind", "sequence"), name="unique_video_evidence_sequence"
            ),
        ]
        ordering = ("extraction_id", "start_ms", "sequence")
        verbose_name = "视频证据"
        verbose_name_plural = "视频证据"


class PolicyVersion(models.Model):
    name = models.CharField(max_length=100, unique=True)
    version = models.CharField(max_length=100)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "审核策略版本"
        verbose_name_plural = "审核策略版本"


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
    auto_execution_enabled = models.BooleanField(default=False)

    class Meta:
        verbose_name = "审核策略规则"
        verbose_name_plural = "审核策略规则"


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
    deciding_policy_rule = models.ForeignKey(
        PolicyRule,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="deciding_assessments",
    )
    deciding_risk_evidence = models.ForeignKey(
        "RiskEvidence",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="deciding_assessments",
    )
    raw_error_summary = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "AI 评估"
        verbose_name_plural = "AI 评估"


class RiskEvidence(models.Model):
    assessment = models.ForeignKey(
        AiAssessment, on_delete=models.CASCADE, related_name="risk_evidence"
    )
    label = models.CharField(max_length=100)
    confidence = models.FloatField()
    evidence = models.JSONField(default=list)
    ocr_source_refs = models.JSONField(default=list, blank=True)
    media_source_refs = models.JSONField(default=list, blank=True)

    class Meta:
        verbose_name = "风险证据"
        verbose_name_plural = "风险证据"


class ReviewTask(models.Model):
    post = models.OneToOneField(OriginalPost, on_delete=models.CASCADE, related_name="review_task")
    assessment = models.ForeignKey(AiAssessment, null=True, blank=True, on_delete=models.SET_NULL)
    suggested_action = models.CharField(max_length=32, choices=RecommendedAction.choices)
    priority = models.PositiveSmallIntegerField(default=50)
    status = models.CharField(
        max_length=32, choices=ReviewStatus.choices, default=ReviewStatus.PENDING
    )
    needs_focus = models.BooleanField(default=False)
    focus_reasons = models.JSONField(default=list, blank=True)
    final_action = models.CharField(max_length=32, choices=RecommendedAction.choices, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "人工审核任务"
        verbose_name_plural = "人工审核任务"


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
    faq_rule_id = models.CharField(max_length=100, blank=True)
    faq_version = models.CharField(max_length=100, blank=True)
    style_variants = models.JSONField(default=list, blank=True)
    selected_style = models.CharField(max_length=16, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "回复草案"
        verbose_name_plural = "回复草案"


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
    execution_source = models.CharField(
        max_length=24,
        choices=OperationExecutionSource.choices,
        default=OperationExecutionSource.HUMAN,
    )
    mute_trigger = models.CharField(
        max_length=24,
        choices=MuteOperationTrigger.choices,
        blank=True,
        default="",
        help_text="仅用于禁言相关操作的来源投影，不保存认证材料或上游响应。",
    )
    automation_snapshot = models.JSONField(default=dict, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("task",),
                condition=models.Q(result_code="EXECUTING"),
                name="one_executing_operation_per_task",
            )
        ]
        verbose_name = "上游操作审计"
        verbose_name_plural = "上游操作审计"


class AiOperationReview(models.Model):
    audit = models.OneToOneField(
        OperationAudit, on_delete=models.PROTECT, related_name="ai_operation_review"
    )
    status = models.CharField(
        max_length=16,
        choices=AiOperationReviewStatus.choices,
        default=AiOperationReviewStatus.PENDING,
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=("status", "created_at"), name="reviews_aio_status_43c5da_idx")
        ]
        verbose_name = "AI 操作复审"
        verbose_name_plural = "AI 操作复审"
