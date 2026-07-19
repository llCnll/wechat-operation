from django.contrib import admin
from django.http import HttpResponseGone
from django.urls import path

from config.admin_support import ReadOnlyAdmin, ReadOnlyTabularInline, operations_dashboard_link

from .models import (
    AiAssessment,
    OcrExtraction,
    OperationAudit,
    PolicyRule,
    PolicyVersion,
    ReplyDraft,
    ReviewTask,
    RiskEvidence,
    risk_label_display,
)


class RiskEvidenceInline(ReadOnlyTabularInline):
    model = RiskEvidence


@admin.register(PolicyVersion)
class PolicyVersionAdmin(admin.ModelAdmin):
    list_display = ("name", "version", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "version")
    ordering = ("-is_active", "name")


@admin.register(PolicyRule)
class PolicyRuleAdmin(admin.ModelAdmin):
    list_display = (
        "risk_label",
        "risk_label_chinese",
        "policy_version",
        "recommended_action",
        "min_confidence",
        "priority",
        "enabled",
    )
    list_filter = ("enabled", "recommended_action", "policy_version")
    search_fields = ("risk_label", "policy_version__name", "policy_version__version")
    ordering = ("policy_version", "-priority", "risk_label")

    @admin.display(description="风险类型（中文）", ordering="risk_label")
    def risk_label_chinese(self, obj):
        return risk_label_display(obj.risk_label)


@admin.register(OcrExtraction)
class OcrExtractionAdmin(ReadOnlyAdmin):
    list_display = ("post", "media", "confidence", "provider", "created_at")
    list_filter = ("provider",)
    search_fields = ("post__topic_id", "text", "error_summary")
    date_hierarchy = "created_at"


@admin.register(AiAssessment)
class AiAssessmentAdmin(ReadOnlyAdmin):
    list_display = (
        "post",
        "category",
        "recommended_action",
        "overall_confidence",
        "needs_human_review",
        "created_at",
    )
    list_filter = ("category", "recommended_action", "needs_human_review")
    search_fields = ("post__topic_id", "model_version", "prompt_version")
    date_hierarchy = "created_at"
    inlines = (RiskEvidenceInline,)


@admin.register(RiskEvidence)
class RiskEvidenceAdmin(ReadOnlyAdmin):
    list_display = ("label", "confidence", "assessment", "assessment_post")
    list_filter = ("label",)
    search_fields = ("assessment__post__topic_id", "evidence")

    @admin.display(description="帖子")
    def assessment_post(self, obj):
        return obj.assessment.post


@admin.register(ReviewTask)
class ReviewTaskAdmin(ReadOnlyAdmin):
    list_display = (
        "post",
        "suggested_action",
        "priority",
        "status",
        "final_action",
        "reviewed_at",
        "dashboard_link",
    )
    list_filter = ("status", "suggested_action", "final_action", "needs_focus")
    search_fields = ("post__topic_id", "reason")
    date_hierarchy = "updated_at"

    def get_readonly_fields(self, request, obj=None):
        return (*super().get_readonly_fields(request, obj), "dashboard_link")

    def get_urls(self):
        """Make removed Admin moderation endpoints fail explicitly, without a write path."""
        legacy_urls = [
            path(
                "<path:object_id>/confirm-<str:action>/",
                self.admin_site.admin_view(self.legacy_operation_view),
                name="reviews_reviewtask_legacy_operation",
            )
        ]
        return legacy_urls + super().get_urls()

    def legacy_operation_view(self, request, object_id, action):
        return HttpResponseGone("审核处置已迁移至内容管理工作台。")

    @admin.display(description="内容管理")
    def dashboard_link(self, obj):
        return operations_dashboard_link(obj.post.topic_id)


@admin.register(ReplyDraft)
class ReplyDraftAdmin(ReadOnlyAdmin):
    list_display = ("post", "assessment", "adopted", "adopted_with_edits", "updated_at")
    list_filter = ("adopted", "adopted_with_edits")
    search_fields = ("post__topic_id", "intent_summary", "draft")
    date_hierarchy = "updated_at"


@admin.register(OperationAudit)
class OperationAuditAdmin(ReadOnlyAdmin):
    list_display = ("operation_id", "task", "action", "result_code", "executor", "executed_at")
    list_filter = ("action", "result_code")
    search_fields = ("operation_id", "task__post__topic_id", "reason", "error_summary")
    date_hierarchy = "created_at"
