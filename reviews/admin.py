from uuid import uuid4

from django.contrib import admin, messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from posts.models import is_post_deleted
from posts.source_client import CdpGameCircleClient

from .models import (
    AiAssessment,
    OperationAudit,
    PolicyRule,
    PolicyVersion,
    ReplyDraft,
    ReviewTask,
    RiskEvidence,
)
from .services import (
    BLOCK_REASON_OPTIONS,
    MUTE_DURATIONS_DAYS,
    execute_confirmed_operation,
    is_operation_enabled,
)

MUTE_DURATION_OPTIONS = tuple(
    (days, f"{days} 天" if days != 30 else "1 个月（30 天）")
    for days in sorted(MUTE_DURATIONS_DAYS)
)


class RiskEvidenceInline(admin.TabularInline):
    model = RiskEvidence
    extra = 0
    readonly_fields = ("label", "confidence", "evidence")


@admin.register(ReviewTask)
class ReviewTaskAdmin(admin.ModelAdmin):
    list_display = ("post", "suggested_action", "priority", "status", "final_action", "updated_at")
    list_filter = ("status", "suggested_action", "final_action", "needs_focus")
    readonly_fields = (
        "post",
        "post_content",
        "post_metrics",
        "ocr_summary",
        "protected_action",
        "assessment",
        "suggested_action",
        "priority",
        "created_at",
        "updated_at",
    )
    def get_urls(self):
        urls = super().get_urls()
        protected = [
            path(
                "<int:task_id>/confirm-block/",
                self.admin_site.admin_view(self.confirm_block_view),
                name="reviews_reviewtask_confirm_block",
            ),
            path(
                "<int:task_id>/confirm-sink/",
                self.admin_site.admin_view(self.confirm_sink_view),
                name="reviews_reviewtask_confirm_sink",
            ),
            path(
                "<int:task_id>/confirm-mute/",
                self.admin_site.admin_view(self.confirm_mute_view),
                name="reviews_reviewtask_confirm_mute",
            ),
            path(
                "<int:task_id>/confirm-restore/",
                self.admin_site.admin_view(self.confirm_restore_view),
                name="reviews_reviewtask_confirm_restore",
            ),
        ]
        return protected + urls

    def confirm_block_view(self, request, task_id):
        return self.confirm_operation_view(request, task_id, action="BLOCK")

    def confirm_sink_view(self, request, task_id):
        return self.confirm_operation_view(request, task_id, action="SINK")

    def confirm_mute_view(self, request, task_id):
        return self.confirm_operation_view(request, task_id, action="MUTE")

    def confirm_restore_view(self, request, task_id):
        return self.confirm_operation_view(request, task_id, action="RESTORE")

    def confirm_operation_view(self, request, task_id, *, action):
        task = get_object_or_404(ReviewTask, pk=task_id)
        if is_post_deleted(task.post.status):
            messages.error(request, "帖子已从原平台管理队列删除，不能执行处置。")
            return redirect("admin:reviews_reviewtask_change", task.pk)
        if request.method == "POST":
            reason = request.POST.get("reason", "").strip()
            if not reason or request.POST.get("confirmed") != "yes":
                messages.error(request, "必须填写业务原因并完成二次确认。")
            else:
                upstream_payload = {}
                if action in {"BLOCK", "MUTE"}:
                    selected = {
                        int(value)
                        for value in request.POST.getlist("block_reason")
                        if value.isdigit()
                    }
                    allowed = {code for code, _ in BLOCK_REASON_OPTIONS}
                    if not selected or not selected <= allowed:
                        messages.error(request, "必须选择至少一个已验证的屏蔽原因。")
                        return render(
                            request,
                            "admin/reviews/confirm_operation.html",
                            self.operation_context(task, action),
                        )
                    upstream_payload["block_reason_code"] = sum(selected)
                if action == "MUTE":
                    mute_days = request.POST.get("mute_days", "")
                    if not mute_days.isdigit() or int(mute_days) not in MUTE_DURATIONS_DAYS:
                        messages.error(request, "必须选择已验证的禁言期限。")
                        return render(
                            request,
                            "admin/reviews/confirm_operation.html",
                            self.operation_context(task, action),
                        )
                    upstream_payload["mute_days"] = int(mute_days)
                source = None
                if is_operation_enabled(action) and task.post.source_appid:
                    source = CdpGameCircleClient(appid=task.post.source_appid)
                try:
                    audit = execute_confirmed_operation(
                        task,
                        user=request.user,
                        action=action,
                        reason=reason,
                        source=source,
                        upstream_payload=upstream_payload,
                        confirmation_key=request.POST.get("confirmation_key"),
                    )
                finally:
                    if source is not None:
                        source.close()
                messages.success(request, f"已创建处置审计：{audit.result_code}。")
                return redirect("admin:reviews_reviewtask_change", task.pk)
        return render(
            request,
            "admin/reviews/confirm_operation.html",
            self.operation_context(task, action),
        )

    @staticmethod
    def operation_context(task, action):
        return {
            "task": task,
            "action": action,
            "block_reasons": BLOCK_REASON_OPTIONS,
            "mute_durations": MUTE_DURATION_OPTIONS,
            "confirmation_key": str(uuid4()),
        }

    @admin.display(description="帖子正文")
    def post_content(self, obj):
        return obj.post.content_text

    @admin.display(description="最新互动")
    def post_metrics(self, obj):
        snapshot = obj.post.interaction_snapshots.order_by("-captured_at").first()
        if snapshot is None:
            return "无互动快照"
        return (
            f"浏览 {snapshot.view_count or 0} / 点赞 {snapshot.love_count} "
            f"/ 评论 {snapshot.reply_count}"
        )

    @admin.display(description="OCR 摘要")
    def ocr_summary(self, obj):
        return "\n".join(obj.post.ocr_extractions.values_list("text", flat=True)) or "无 OCR 文本"

    @admin.display(description="受保护操作")
    def protected_action(self, obj):
        if is_post_deleted(obj.post.status):
            return "原平台已删除，不能执行处置"
        block_url = reverse("admin:reviews_reviewtask_confirm_block", args=[obj.pk])
        sink_url = reverse("admin:reviews_reviewtask_confirm_sink", args=[obj.pk])
        mute_url = reverse("admin:reviews_reviewtask_confirm_mute", args=[obj.pk])
        restore_url = reverse("admin:reviews_reviewtask_confirm_restore", args=[obj.pk])
        return format_html(
            '<a class="button" href="{}">确认沉底</a> '
            '<a class="button" href="{}">确认屏蔽</a> '
            '<a class="button" href="{}">确认禁言</a> '
            '<a class="button" href="{}">恢复正常</a>',
            sink_url,
            block_url,
            mute_url,
            restore_url,
        )


@admin.register(AiAssessment)
class AiAssessmentAdmin(admin.ModelAdmin):
    list_display = ("post", "category", "recommended_action", "overall_confidence", "created_at")
    list_filter = ("category", "recommended_action", "needs_human_review")
    readonly_fields = ("created_at",)
    inlines = (RiskEvidenceInline,)


admin.site.register((PolicyVersion, PolicyRule, ReplyDraft, OperationAudit))
