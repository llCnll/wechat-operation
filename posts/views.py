"""Read-only operations views for managing the original-post queue."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import F, OuterRef, Q, Subquery, Value
from django.http import QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from config.cdp import CdpUnavailableError, open_controlled_login_page
from jobs.models import BackgroundJob, JobStatus
from reviews.models import (
    RISK_LABEL_DISPLAY,
    AiOperationReview,
    AiOperationReviewStatus,
    ContentCategory,
    RecommendedAction,
    ReplyDraft,
    ReviewStatus,
    ReviewTask,
    risk_label_display,
)
from reviews.services import (
    BLOCK_REASON_OPTIONS,
    OperationExecutionConflict,
    complete_review,
    confirm_ai_operation_review,
    current_video_extractions,
    enqueue_evaluation_pipeline,
    escalate_ai_operation_review,
    execute_confirmed_operation,
    is_operation_enabled,
    mark_ai_review_restored,
    save_reply_draft,
    video_evidence_summaries,
)

from .game_catalog import active_game_appid, managed_game_options
from .models import (
    POST_STATUS_BLOCKED,
    POST_STATUS_DELETED,
    POST_STATUS_NORMAL,
    POST_STATUS_SELF_VISIBLE,
    POST_STATUS_SUNK,
    CandidatePost,
    InteractionSnapshot,
    OriginalPost,
    OriginType,
    is_post_blocked,
    is_post_deleted,
    is_post_normal,
    is_post_self_visible,
    is_post_sunk,
)
from .services import refresh_post_status_and_metrics
from .source_client import CdpGameCircleClient

DASHBOARD_SORT_OPTIONS = (
    ("priority_desc", "优先级：高到低"),
    ("priority_asc", "优先级：低到高"),
    ("publish_time_desc", "帖子创建时间：最新在前"),
    ("publish_time_asc", "帖子创建时间：最早在前"),
)

BATCH_AI_ACTIONS = frozenset((RecommendedAction.SINK, RecommendedAction.BLOCK))
BATCH_AI_LIMIT = 20

POST_STATUS_FILTER_OPTIONS = (
    ("NORMAL", "正常"),
    ("BLOCKED", "已屏蔽"),
    ("SUNK", "已沉底"),
    ("SELF_VISIBLE", "仅自己可见"),
    ("DELETED", "原平台已删除"),
    ("UNSYNCED", "未同步"),
)

POST_STATUS_FILTER_FLAGS = {
    "BLOCKED": POST_STATUS_BLOCKED,
    "SUNK": POST_STATUS_SUNK,
    "SELF_VISIBLE": POST_STATUS_SELF_VISIBLE,
    "DELETED": POST_STATUS_DELETED,
}


def _safe_dashboard_return_url(request) -> str:
    return_to = request.POST.get("return_to", "")
    if url_has_allowed_host_and_scheme(
        return_to, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return return_to
    return reverse("operations-dashboard")


def _safe_ai_operation_reviews_return_url(request) -> str:
    """Keep review actions in the filtered review workspace, never an external URL."""
    return_to = request.POST.get("return_to", "")
    if url_has_allowed_host_and_scheme(
        return_to, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return return_to
    return reverse("ai-operation-reviews")


def _safe_ai_operation_reviews_dashboard_return_url(request) -> str:
    """Only retain a same-site review workspace return path while viewing a post."""
    return_to = request.GET.get("return_to", "")
    if url_has_allowed_host_and_scheme(
        return_to, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return return_to
    return ""


def _escalated_review_dashboard_url(request, review: AiOperationReview) -> str:
    """Take an escalated review directly to its actionable content-management task."""
    review_return_params = QueryDict(mutable=True)
    review_return_params["appid"] = review.audit.task.post.source_appid
    review_return_params["status"] = AiOperationReviewStatus.ESCALATED
    review_return_url = f"{reverse('ai-operation-reviews')}?{review_return_params.urlencode()}"
    params = QueryDict(mutable=True)
    params["appid"] = review.audit.task.post.source_appid
    params["review_status"] = ReviewStatus.AUTO_REVIEW_ESCALATED
    params["topic"] = str(review.audit.task.post.topic_id)
    params["return_to"] = review_return_url
    return f"{reverse('operations-dashboard')}?{params.urlencode()}"


@login_required
@require_POST
def open_controlled_login(request):
    """Open the fixed login page in the remote-debugging Chrome, never in the user browser."""
    try:
        open_controlled_login_page()
    except CdpUnavailableError as exc:
        messages.error(request, str(exc))
    else:
        messages.info(request, "已在受控 Chrome 打开登录页，请使用微信扫码确认。")
    return redirect(_safe_dashboard_return_url(request))


def _with_latest_metrics(queryset):
    latest = InteractionSnapshot.objects.filter(post_id=OuterRef("pk")).order_by("-captured_at")
    return queryset.annotate(
        latest_view_count=Subquery(latest.values("view_count")[:1]),
        latest_love_count=Subquery(latest.values("love_count")[:1]),
        latest_reply_count=Subquery(latest.values("reply_count")[:1]),
    )


def _set_status_context(post: OriginalPost) -> None:
    post.is_deleted = is_post_deleted(post.status)
    post.is_blocked = is_post_blocked(post.status)
    post.is_sunk = is_post_sunk(post.status)
    post.is_self_visible = is_post_self_visible(post.status)
    post.is_normal = is_post_normal(post.status)
    post.status_label = _post_status_label(post.status)


def _post_status_label(status: int | None) -> str:
    if is_post_deleted(status):
        labels = ["原平台已删除"]
        if is_post_blocked(status):
            labels.append("已屏蔽")
        if is_post_sunk(status):
            labels.append("已沉底")
        return "、".join(labels)
    if is_post_blocked(status) and is_post_sunk(status):
        return "已屏蔽、已沉底"
    if is_post_blocked(status):
        return "已屏蔽"
    if is_post_sunk(status):
        return "已沉底"
    if is_post_self_visible(status):
        return "仅自己可见"
    if is_post_normal(status):
        return "正常"
    return "未同步" if status is None else f"未知状态（{status}）"


def _operation_reason_suggestion(post: OriginalPost) -> str:
    """Create a short editable reason from the most relevant AI evidence."""
    review = getattr(post, "review_task", None)
    assessment = getattr(review, "assessment", None) if review else None
    if not assessment:
        return "人工审核依据：结合帖子正文与附件内容。"

    reasons = []
    for evidence in assessment.risk_evidence.all()[:2]:
        snippets = evidence.evidence if isinstance(evidence.evidence, list) else []
        snippet = next(
            (
                " ".join(item.split())[:80]
                for item in snippets
                if isinstance(item, str) and item.strip()
            ),
            "",
        )
        label = risk_label_display(evidence.label)
        reasons.append(f"{label}：{snippet}" if snippet else label)
    if not reasons:
        return "人工审核依据：结合帖子正文与附件内容。"
    return f"AI 风险依据：{'；'.join(reasons)}"


def _batch_ai_candidates(posts: list[OriginalPost]) -> list[OriginalPost]:
    """Keep bulk execution deliberately narrow and visibly bounded in the queue."""
    candidates = []
    for post in posts:
        review = getattr(post, "review_task", None)
        assessment = getattr(review, "assessment", None) if review else None
        if (
            post.status != POST_STATUS_NORMAL
            or not review
            or review.status != ReviewStatus.PENDING
            or review.suggested_action not in BATCH_AI_ACTIONS
            or not assessment
            or not assessment.risk_evidence.exists()
        ):
            continue
        candidates.append(post)
        if len(candidates) == BATCH_AI_LIMIT:
            break
    return candidates


def _dashboard_game_filter(appid: str) -> Q:
    game_filter = Q(source_appid=appid)
    if appid == "wx6d2454a8afcd9706":
        # Historical records predating source_appid attribution belong to the default game.
        game_filter |= Q(source_appid="")
    return game_filter


@login_required
def operations_dashboard(request):
    normalized_query, query_was_normalized = _normalize_dashboard_query(request.GET)
    if query_was_normalized:
        query_string = normalized_query.urlencode()
        target = reverse("operations-dashboard")
        return redirect(f"{target}?{query_string}" if query_string else target)
    search = request.GET.get("q", "").strip()
    appid = active_game_appid(request.GET.get("appid", "").strip())
    game_filter = _dashboard_game_filter(appid)
    filters = {
        "category": [value for value in request.GET.getlist("category") if value],
        "risk": [value for value in request.GET.getlist("risk") if value],
        "suggested_action": [value for value in request.GET.getlist("suggested_action") if value],
        "review_status": [value for value in request.GET.getlist("review_status") if value],
        "post_status": [value for value in request.GET.getlist("post_status") if value],
        **{
            key: request.GET.get(key, "").strip()
            for key in (
                "min_confidence",
                "sort",
                "priority_order",
                "reviewed",
                "min_views",
            )
        },
    }
    valid_sorts = {value for value, _label in DASHBOARD_SORT_OPTIONS}
    if filters["sort"] not in valid_sorts:
        # Keep previously shared URLs working after replacing the old one-purpose control.
        filters["sort"] = "priority_asc" if filters["priority_order"] == "asc" else "priority_desc"
    selected_topic_id = request.GET.get("topic", "").strip()
    posts = _with_latest_metrics(
        OriginalPost.objects.select_related(
            "review_task", "review_task__assessment", "reply_draft"
        ).prefetch_related(
            "review_task__assessment__risk_evidence",
            "ocr_extractions",
            "review_task__operation_audits",
            "video_extractions__evidence",
        )
    )
    if search:
        posts = posts.filter(
            Q(title__icontains=search)
            | Q(content_text__icontains=search)
            | Q(ballot_text__icontains=search)
        )
    posts = posts.filter(game_filter)
    if filters["category"]:
        posts = posts.filter(review_task__assessment__category__in=filters["category"])
    if filters["risk"]:
        posts = posts.filter(review_task__assessment__risk_evidence__label__in=filters["risk"])
    if filters["suggested_action"]:
        posts = posts.filter(review_task__suggested_action__in=filters["suggested_action"])
    if filters["review_status"]:
        posts = posts.filter(review_task__status__in=filters["review_status"])
    else:
        posts = posts.exclude(review_task__status=ReviewStatus.AUTO_EXECUTED_PENDING_REVIEW)
    if filters["post_status"]:
        selected_statuses = set(filters["post_status"])
        status_query = Q()
        if "NORMAL" in selected_statuses:
            status_query |= Q(status=POST_STATUS_NORMAL)
        if "UNSYNCED" in selected_statuses:
            status_query |= Q(status__isnull=True)
        flags = sum(
            flag for name, flag in POST_STATUS_FILTER_FLAGS.items() if name in selected_statuses
        )
        if flags:
            posts = posts.annotate(_selected_status_flags=F("status").bitand(Value(flags)))
            status_query |= Q(_selected_status_flags__gt=0)
        posts = posts.filter(status_query)
    if filters["reviewed"] == "yes":
        posts = posts.filter(review_task__reviewed_at__isnull=False)
    elif filters["reviewed"] == "no":
        posts = posts.filter(review_task__reviewed_at__isnull=True)
    for name, field in (
        ("min_confidence", "review_task__assessment__overall_confidence__gte"),
        ("min_views", "latest_view_count__gte"),
    ):
        if filters[name]:
            try:
                posts = posts.filter(**{field: float(filters[name])})
            except ValueError:
                pass
    ordering = {
        "priority_desc": ("-review_task__priority", "-publish_time", "-updated_at"),
        "priority_asc": ("review_task__priority", "-publish_time", "-updated_at"),
        "publish_time_desc": ("-publish_time", "-updated_at"),
        "publish_time_asc": ("publish_time", "updated_at"),
    }[filters["sort"]]
    ordered_posts = posts.distinct().order_by(*ordering)
    posts = list(ordered_posts[:100])
    if selected_topic_id:
        try:
            selected_topic = int(selected_topic_id)
        except ValueError:
            selected_topic = None
        if selected_topic is not None and all(post.topic_id != selected_topic for post in posts):
            selected_post = ordered_posts.filter(topic_id=selected_topic).first()
            if selected_post is not None:
                posts = [selected_post, *posts][:100]
    for post in posts:
        _set_status_context(post)
    batch_candidates = _batch_ai_candidates(posts)
    batch_sink_count = sum(
        post.review_task.suggested_action == RecommendedAction.SINK for post in batch_candidates
    )
    batch_block_count = sum(
        post.review_task.suggested_action == RecommendedAction.BLOCK for post in batch_candidates
    )
    for post in batch_candidates:
        post.batch_action_label = post.review_task.get_suggested_action_display()
        post.batch_risk_summary = _operation_reason_suggestion(post)
    quick_filter = QueryDict("", mutable=True)
    quick_filter["appid"] = appid
    quick_filter["sort"] = filters["sort"]
    for action in (
        RecommendedAction.SINK,
        RecommendedAction.BLOCK,
        RecommendedAction.MUTE,
        RecommendedAction.HUMAN_REVIEW,
    ):
        quick_filter.appendlist("suggested_action", action)
    quick_filter.appendlist("review_status", ReviewStatus.PENDING)
    quick_filter.appendlist("post_status", "NORMAL")
    selected_index = 0
    if selected_topic_id:
        try:
            selected_index = next(
                index for index, post in enumerate(posts) if post.topic_id == int(selected_topic_id)
            )
        except (StopIteration, ValueError):
            selected_index = 0
    selected_post = posts[selected_index] if posts else None
    evaluation = {"state": "IDLE", "label": "尚未评估", "error": ""}
    if selected_post:
        selected_post.review = getattr(selected_post, "review_task", None)
        selected_post.recommended_operation = ""
        selected_post.recommended_operation_label = ""
        if selected_post.review and selected_post.review.assessment:
            selected_post.recommended_operation = {
                RecommendedAction.KEEP: "SKIP",
                RecommendedAction.SINK: RecommendedAction.SINK,
                RecommendedAction.BLOCK: RecommendedAction.BLOCK,
                RecommendedAction.MUTE: RecommendedAction.MUTE,
                RecommendedAction.UNMUTE: RecommendedAction.UNMUTE,
                RecommendedAction.RESTORE: RecommendedAction.RESTORE,
                RecommendedAction.BLOCK_AND_MUTE: RecommendedAction.BLOCK_AND_MUTE,
            }.get(selected_post.review.suggested_action, "")
            selected_post.recommended_operation_label = (
                selected_post.review.get_suggested_action_display()
            )
            selected_post.risk_evidence = list(selected_post.review.assessment.risk_evidence.all())
            for evidence in selected_post.risk_evidence:
                evidence.display_label = risk_label_display(evidence.label)
        media_indexes = {
            media_id: index
            for index, media_id in enumerate(
                selected_post.media.order_by("pk").values_list("pk", flat=True), start=1
            )
        }
        current_extractions = current_video_extractions(selected_post)
        current_extraction_ids = {extraction.pk for extraction in current_extractions}
        for extraction in current_extractions:
            extraction.media_index = media_indexes.get(extraction.media_id, "已移除媒体")
            extraction.evidence_summaries = video_evidence_summaries(extraction)
        selected_post.current_video_extractions = sorted(
            current_extractions,
            key=lambda item: (media_indexes.get(item.media_id, float("inf")), item.pk),
        )
        selected_post.historical_video_extraction_count = selected_post.video_extractions.exclude(
            pk__in=current_extraction_ids
        ).count()
        selected_post.operation_reason_suggestion = _operation_reason_suggestion(selected_post)
        selected_post.ai_operation_reviews = (
            list(
                AiOperationReview.objects.filter(audit__task=selected_post.review)
                .select_related("audit")
                .order_by("-created_at")
            )
            if selected_post.review
            else []
        )
        # A task may have historical review records. The dashboard should link
        # to the latest record's actual review state, not the default queue.
        selected_post.latest_ai_operation_review = (
            selected_post.ai_operation_reviews[0]
            if selected_post.ai_operation_reviews
            else None
        )
        latest_jobs = list(
            BackgroundJob.objects.filter(parameter_summary__topic_id=selected_post.topic_id)
            .filter(task_type__in=("OCR_EXTRACT", "AI_EVALUATE"))
            .order_by("-created_at")
        )
        ai_job = next((job for job in latest_jobs if job.task_type == "AI_EVALUATE"), None)
        ocr_job = next((job for job in latest_jobs if job.task_type == "OCR_EXTRACT"), None)
        if any(
            job and job.status in {JobStatus.PENDING, JobStatus.RUNNING}
            for job in (ocr_job, ai_job)
        ):
            evaluation = {"state": "RUNNING", "label": "正在评估", "error": ""}
        elif ai_job and ai_job.status == JobStatus.FAILED:
            evaluation = {
                "state": "FAILED",
                "label": "评估失败，可重试",
                "error": ai_job.error_summary,
            }
        elif selected_post.review and selected_post.review.assessment:
            evaluation = {"state": "SUCCEEDED", "label": "评估完成", "error": ""}
    context = {
        "posts": posts,
        "selected_post": selected_post,
        "evaluation": evaluation,
        "selected_position": selected_index + 1 if selected_post else 0,
        "previous_post": posts[selected_index - 1] if selected_index > 0 else None,
        "next_post": posts[selected_index + 1] if selected_index + 1 < len(posts) else None,
        "search": search,
        "appid": appid,
        "managed_games": managed_game_options(),
        "active_game": next((game for game in managed_game_options() if game.appid == appid), None),
        "game_context_query": _game_context_query(request),
        "navigation_query": _navigation_query(request),
        "return_query": _navigation_query(request),
        "ai_operation_reviews_return_url": _safe_ai_operation_reviews_dashboard_return_url(request),
        "quick_filter_query": quick_filter.urlencode(),
        "filters": filters,
        "categories": ContentCategory.choices,
        "review_statuses": ReviewStatus.choices,
        "recommended_actions": RecommendedAction.choices,
        "risk_labels": RISK_LABEL_DISPLAY.items(),
        "post_statuses": POST_STATUS_FILTER_OPTIONS,
        "sort_options": DASHBOARD_SORT_OPTIONS,
        "stats": {
            "original": OriginalPost.objects.filter(game_filter).count(),
            "forward": CandidatePost.objects.filter(
                game_filter, origin_type=OriginType.FORWARD
            ).count(),
            "pending": CandidatePost.objects.filter(
                game_filter, origin_type=OriginType.CLASSIFICATION_PENDING
            ).count(),
        },
        "operation_capabilities": {
            "SINK": is_operation_enabled(RecommendedAction.SINK),
            "BLOCK": is_operation_enabled(RecommendedAction.BLOCK),
            "MUTE": is_operation_enabled(RecommendedAction.MUTE),
            "UNMUTE": is_operation_enabled(RecommendedAction.UNMUTE),
            "RESTORE": is_operation_enabled(RecommendedAction.RESTORE),
        },
        "block_reasons": BLOCK_REASON_OPTIONS,
        "mute_durations": ((1, "1 天"), (3, "3 天"), (7, "7 天"), (30, "1 个月（30 天）")),
        "batch_candidates": batch_candidates,
        "batch_sink_count": batch_sink_count,
        "batch_block_count": batch_block_count,
        "batch_candidate_count": len(batch_candidates),
        "batch_actions_enabled": any(is_operation_enabled(action) for action in BATCH_AI_ACTIONS),
    }
    return render(request, "posts/operations_dashboard.html", context)


def _navigation_query(request) -> str:
    """Preserve every active filter while replacing the selected topic."""
    params = request.GET.copy()
    params.pop("topic", None)
    return params.urlencode()


def _normalize_dashboard_query(query: QueryDict) -> tuple[QueryDict, bool]:
    """Repair legacy keyboard-navigation URLs that encoded separators as parameter names."""
    normalized = QueryDict("", mutable=True)
    changed = False
    for name, values in query.lists():
        normalized_name = name
        while normalized_name.startswith("amp;"):
            normalized_name = normalized_name[4:]
            changed = True
        for value in values:
            normalized.appendlist(normalized_name, value)
    return normalized, changed


def _game_context_query(request) -> str:
    """Preserve active filters when switching the current game."""
    params = request.GET.copy()
    params.pop("appid", None)
    params.pop("topic", None)
    return params.urlencode()


def _dashboard_return_url(request, topic_id: int) -> str:
    """Return to the active dashboard context without accepting an external redirect URL."""
    params = QueryDict(request.POST.get("return_query", ""), mutable=True)
    params["topic"] = str(topic_id)
    return f"{reverse('operations-dashboard')}?{params.urlencode()}"


def _dashboard_return_url_from_query(return_query: str, selected_topic_id: str) -> str:
    """Keep bulk actions inside the dashboard context without accepting external URLs."""
    params = QueryDict(return_query, mutable=True)
    if selected_topic_id.isdigit():
        params["topic"] = selected_topic_id
    return f"{reverse('operations-dashboard')}?{params.urlencode()}"


@login_required
def post_detail(request, topic_id: int):
    post = get_object_or_404(_with_latest_metrics(OriginalPost.objects.all()), topic_id=topic_id)
    return render(
        request,
        "posts/post_detail.html",
        {"post": post, "metrics": post.interaction_snapshots.order_by("-captured_at")[:10]},
    )


@login_required
@require_POST
def refresh_dashboard_post(request, topic_id: int):
    """Refresh the selected post's operational status and interaction counters."""
    post = get_object_or_404(OriginalPost, topic_id=topic_id)
    redirect_url = _dashboard_return_url(request, post.topic_id)
    if not post.source_appid:
        messages.error(request, "帖子缺少游戏 appid，无法刷新。")
        return redirect(redirect_url)
    client = None
    try:
        client = CdpGameCircleClient(appid=post.source_appid)
        refreshed = refresh_post_status_and_metrics(client, post)
    except Exception:
        messages.error(request, "刷新失败，请检查浏览器登录状态或稍后重试。")
    else:
        if is_post_deleted(refreshed.status):
            messages.warning(request, "该帖子已从原平台管理队列删除，已标记为不可处置状态。")
        else:
            messages.success(request, "帖子状态和互动指标已同步。")
    finally:
        if client is not None:
            client.close()
    return redirect(redirect_url)


def _resolve_escalated_ai_operation(
    request, *, task: ReviewTask, post: OriginalPost, action: str, reason: str, redirect_url: str
):
    """Correct an escalated AI write and apply the human's final decision in one flow."""
    supported_actions = {
        RecommendedAction.RESTORE,
        RecommendedAction.SINK,
        RecommendedAction.BLOCK,
        RecommendedAction.MUTE,
        RecommendedAction.BLOCK_AND_MUTE,
    }
    if action not in supported_actions:
        messages.error(request, "AI 异常跟进仅支持恢复正常或恢复后执行沉底、屏蔽、禁言。")
        return redirect(redirect_url)

    target_payload = {}
    if action == RecommendedAction.BLOCK_AND_MUTE:
        target_payload = _dashboard_operation_payload(request, RecommendedAction.MUTE)
    elif action not in {RecommendedAction.RESTORE, RecommendedAction.SINK}:
        target_payload = _dashboard_operation_payload(request, action)
    if target_payload is None:
        return redirect(redirect_url)

    operation_plan = [(RecommendedAction.RESTORE, {})]
    if action == RecommendedAction.BLOCK_AND_MUTE:
        operation_plan.extend(
            (
                (
                    RecommendedAction.BLOCK,
                    {"block_reason_code": target_payload["block_reason_code"]},
                ),
                (RecommendedAction.MUTE, target_payload),
            )
        )
    elif action != RecommendedAction.RESTORE:
        operation_plan.append((action, target_payload))

    unavailable = [
        operation for operation, _ in operation_plan if not is_operation_enabled(operation)
    ]
    if unavailable:
        labels = "、".join(dict(RecommendedAction.choices)[operation] for operation in unavailable)
        messages.error(request, f"请先在系统配置中启用上游写操作：{labels}。")
        return redirect(redirect_url)

    source = None
    try:
        if post.source_appid:
            source = CdpGameCircleClient(appid=post.source_appid)
        confirmation_key = request.POST.get("confirmation_key")
        audits = [
            execute_confirmed_operation(
                task,
                user=request.user,
                action=operation,
                reason=reason,
                source=source,
                upstream_payload=payload,
                confirmation_key=f"{confirmation_key}:ai-correction:{index}:{operation.lower()}",
            )
            for index, (operation, payload) in enumerate(operation_plan, start=1)
        ]
    except OperationExecutionConflict as exc:
        messages.warning(request, str(exc))
        return redirect(redirect_url)
    except Exception:
        messages.error(request, "无法建立上游操作会话，未执行纠正处置。")
        return redirect(redirect_url)
    finally:
        if source is not None:
            source.close()

    result_summary = "，".join(f"{audit.action}={audit.result_code}" for audit in audits)
    if not all(audit.result_code == "OK" for audit in audits):
        messages.warning(request, f"纠正处置未全部成功，AI 异常仍待跟进：{result_summary}")
        return redirect(redirect_url)

    complete_review(task, user=request.user, action=action, reason=reason)
    mark_ai_review_restored(task, audit=audits[0])
    messages.success(request, f"已纠正 AI 操作并完成最终处置：{result_summary}")
    return redirect(redirect_url)


@login_required
@require_POST
def confirm_dashboard_operation(request, topic_id: int, action: str):
    """Execute a human-confirmed dashboard action through the audited service boundary."""
    post = get_object_or_404(OriginalPost, topic_id=topic_id)
    redirect_url = _dashboard_return_url(request, post.topic_id)
    action = action.upper()
    if is_post_deleted(post.status):
        messages.error(request, "帖子已从原平台管理队列删除，不能执行处置。")
        return redirect(redirect_url)
    if is_post_self_visible(post.status) and action != "SKIP":
        messages.error(request, "帖子当前仅自己可见，不能覆盖原平台可见性状态。")
        return redirect(redirect_url)
    task, _ = ReviewTask.objects.get_or_create(
        post=post,
        defaults={"suggested_action": RecommendedAction.HUMAN_REVIEW},
    )
    if task.status == ReviewStatus.AUTO_EXECUTED_PENDING_REVIEW:
        messages.error(request, "该帖 AI 操作仍等待复审，请先在“AI 操作复审”中确认或标记异常。")
        return redirect(redirect_url)
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "必须填写业务原因。")
        return redirect(redirect_url)
    if task.status == ReviewStatus.AUTO_REVIEW_ESCALATED:
        return _resolve_escalated_ai_operation(
            request,
            task=task,
            post=post,
            action=action,
            reason=reason,
            redirect_url=redirect_url,
        )
    if action == "SKIP":
        complete_review(
            task,
            user=request.user,
            action=RecommendedAction.KEEP,
            reason=reason,
        )
        messages.success(request, "已跳过该帖子并记录人工审核。")
        return redirect(redirect_url)
    if action == RecommendedAction.BLOCK_AND_MUTE:
        payload = _dashboard_operation_payload(request, RecommendedAction.MUTE)
        if payload is None:
            return redirect(redirect_url)
        source = None
        try:
            if post.source_appid:
                source = CdpGameCircleClient(appid=post.source_appid)
            token = request.POST.get("confirmation_key")
            audits = [
                execute_confirmed_operation(
                    task,
                    user=request.user,
                    action=operation,
                    reason=reason,
                    source=source,
                    upstream_payload=payload
                    if operation == RecommendedAction.MUTE
                    else {"block_reason_code": payload["block_reason_code"]},
                    confirmation_key=f"{token}:{operation.lower()}",
                )
                for operation in (RecommendedAction.BLOCK, RecommendedAction.MUTE)
            ]
        except OperationExecutionConflict as exc:
            messages.warning(request, str(exc))
            return redirect(redirect_url)
        except Exception:
            messages.error(request, "无法建立上游操作会话，未执行处置。")
            return redirect(redirect_url)
        finally:
            if source is not None:
                source.close()
        result_summary = "，".join(f"{item.action}={item.result_code}" for item in audits)
        if all(item.result_code == "OK" for item in audits):
            complete_review(
                task, user=request.user, action=RecommendedAction.BLOCK_AND_MUTE, reason=reason
            )
            messages.success(request, f"组合处置成功，审核已完成：{result_summary}")
        else:
            messages.warning(request, f"组合处置未全部成功，审核保持待处理：{result_summary}")
        return redirect(redirect_url)
    if action not in {
        RecommendedAction.RESTORE,
        RecommendedAction.SINK,
        RecommendedAction.BLOCK,
        RecommendedAction.MUTE,
        RecommendedAction.UNMUTE,
    }:
        messages.error(request, "不支持的处置动作。")
        return redirect(redirect_url)

    payload = _dashboard_operation_payload(request, action)
    if payload is None:
        return redirect(redirect_url)
    source = None
    try:
        if is_operation_enabled(action) and post.source_appid:
            source = CdpGameCircleClient(appid=post.source_appid)
        audit = execute_confirmed_operation(
            task,
            user=request.user,
            action=action,
            reason=reason,
            source=source,
            upstream_payload=payload,
            confirmation_key=request.POST.get("confirmation_key"),
        )
    except OperationExecutionConflict as exc:
        messages.warning(request, str(exc))
        return redirect(redirect_url)
    except Exception:
        messages.error(request, "无法建立上游操作会话，未执行处置。")
        return redirect(redirect_url)
    finally:
        if source is not None:
            source.close()
    if audit.result_code == "OK":
        complete_review(task, user=request.user, action=action, reason=reason)
        if action == RecommendedAction.RESTORE:
            mark_ai_review_restored(task, audit=audit)
        messages.success(request, f"处置成功，审核已完成：{audit.result_code}。")
    else:
        messages.warning(request, f"处置未成功，审核保持待处理：{audit.result_code}。")
    return redirect(redirect_url)


@login_required
def ai_operation_reviews(request):
    appid = active_game_appid(request.GET.get("appid", "").strip())
    reviews = AiOperationReview.objects.select_related(
        "audit__task__post", "audit__task__assessment", "audit__policy_version", "reviewer"
    ).prefetch_related("audit__task__assessment__risk_evidence")
    reviews = reviews.filter(
        audit__task__post__in=OriginalPost.objects.filter(_dashboard_game_filter(appid))
    )
    status = request.GET.get("status", AiOperationReviewStatus.PENDING)
    action = request.GET.get("action", "")
    risk = request.GET.get("risk", "")
    valid_statuses = {value for value, _ in AiOperationReviewStatus.choices}
    valid_actions = {RecommendedAction.SINK, RecommendedAction.BLOCK}
    if status not in valid_statuses and status:
        status = AiOperationReviewStatus.PENDING
    if action not in valid_actions:
        action = ""
    if risk not in RISK_LABEL_DISPLAY:
        risk = ""
    start_at = parse_datetime(request.GET.get("start_at", ""))
    end_at = parse_datetime(request.GET.get("end_at", ""))
    if status:
        reviews = reviews.filter(status=status)
    if action:
        reviews = reviews.filter(audit__action=action)
    if risk:
        reviews = reviews.filter(audit__task__assessment__risk_evidence__label=risk).distinct()
    if start_at:
        reviews = reviews.filter(
            created_at__gte=timezone.make_aware(start_at)
            if timezone.is_naive(start_at)
            else start_at
        )
    if end_at:
        reviews = reviews.filter(
            created_at__lte=timezone.make_aware(end_at) if timezone.is_naive(end_at) else end_at
        )
    reviews = list(reviews.order_by("-created_at")[:100])
    for review in reviews:
        review.before_status_label = _post_status_label(review.audit.before_state.get("status"))
        review.after_status_label = _post_status_label(review.audit.after_state.get("status"))
    return render(
        request,
        "posts/ai_operation_reviews.html",
        {
            "reviews": reviews,
            "appid": appid,
            "managed_games": managed_game_options(),
            "statuses": AiOperationReviewStatus.choices,
            "actions": ((RecommendedAction.SINK, "沉底"), (RecommendedAction.BLOCK, "屏蔽")),
            "risk_labels": RISK_LABEL_DISPLAY.items(),
            "selected_status": status,
            "selected_action": action,
            "selected_risk": risk,
            "start_at": request.GET.get("start_at", ""),
            "end_at": request.GET.get("end_at", ""),
            "return_to": request.get_full_path(),
            "operation_capabilities": {
                "SINK": is_operation_enabled(RecommendedAction.SINK),
                "BLOCK": is_operation_enabled(RecommendedAction.BLOCK),
                "MUTE": is_operation_enabled(RecommendedAction.MUTE),
                "RESTORE": is_operation_enabled(RecommendedAction.RESTORE),
            },
            "block_reasons": BLOCK_REASON_OPTIONS,
            "mute_durations": ((1, "1 天"), (3, "3 天"), (7, "7 天"), (30, "1 个月（30 天）")),
        },
    )


@login_required
@require_POST
def update_ai_operation_review(request, review_id: int, decision: str):
    review = get_object_or_404(
        AiOperationReview.objects.select_related("audit__task__post"), pk=review_id
    )
    redirect_url = _safe_ai_operation_reviews_return_url(request)
    reason = request.POST.get("reason", "")
    final_action = request.POST.get("final_action", "").upper()
    try:
        if decision == "confirm":
            confirm_ai_operation_review(review, user=request.user, reason=reason)
            messages.success(request, "已确认 AI 操作正确。")
        elif decision == "escalate":
            escalate_ai_operation_review(review, user=request.user, reason=reason)
            if final_action:
                return _resolve_escalated_ai_operation(
                    request,
                    task=review.audit.task,
                    post=review.audit.task.post,
                    action=final_action,
                    reason=reason or "人工复审核验：AI 原处置不正确。",
                    redirect_url=redirect_url,
                )
            messages.warning(request, "已标记异常，请在内容管理中人工跟进。")
            redirect_url = _escalated_review_dashboard_url(request, review)
        elif decision == "correct":
            if review.status != AiOperationReviewStatus.ESCALATED:
                raise ValueError("该 AI 操作当前不需要异常纠正")
            if not final_action:
                raise ValueError("请选择异常纠正后的最终处置")
            return _resolve_escalated_ai_operation(
                request,
                task=review.audit.task,
                post=review.audit.task.post,
                action=final_action,
                reason=reason or "人工复审核验：继续纠正 AI 原处置。",
                redirect_url=redirect_url,
            )
        else:
            messages.error(request, "不支持的复审操作。")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect(redirect_url)


@login_required
@require_POST
def execute_batch_ai_recommendations(request):
    """Apply only explicit, high-signal AI sink/block recommendations after a human click."""
    appid = active_game_appid(request.POST.get("appid", "").strip())
    requested_ids = []
    for value in request.POST.getlist("topic_id"):
        try:
            requested_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    requested_ids = list(dict.fromkeys(requested_ids))[:BATCH_AI_LIMIT]
    return_query = request.POST.get("return_query", "")
    selected_topic_id = request.POST.get("selected_topic_id", "")
    redirect_url = _dashboard_return_url_from_query(return_query, selected_topic_id)
    batch_key = request.POST.get("batch_key", "").strip()
    if not requested_ids:
        messages.warning(request, "没有可执行的 AI 推荐帖子。")
        return redirect(redirect_url)
    if not batch_key or len(batch_key) > 48:
        messages.error(request, "批量执行令牌无效，请重新发起操作。")
        return redirect(redirect_url)

    original_ids = CandidatePost.objects.filter(
        topic_id__in=requested_ids, origin_type=OriginType.ORIGINAL
    ).values("topic_id")
    eligible = (
        OriginalPost.objects.select_related("review_task", "review_task__assessment")
        .prefetch_related("review_task__assessment__risk_evidence")
        .filter(
            _dashboard_game_filter(appid),
            topic_id__in=original_ids,
            status=POST_STATUS_NORMAL,
            review_task__status=ReviewStatus.PENDING,
            review_task__suggested_action__in=BATCH_AI_ACTIONS,
            review_task__assessment__isnull=False,
            review_task__assessment__risk_evidence__isnull=False,
        )
        .distinct()
    )
    eligible_by_id = {post.topic_id: post for post in eligible}
    posts = [eligible_by_id[topic_id] for topic_id in requested_ids if topic_id in eligible_by_id]
    if not posts:
        messages.warning(request, "所选帖子已不满足批量执行条件，请刷新列表后重试。")
        return redirect(redirect_url)

    source = None
    succeeded = failed = 0
    try:
        if any(is_operation_enabled(post.review_task.suggested_action) for post in posts):
            source = CdpGameCircleClient(appid=appid)
        for post in posts:
            action = post.review_task.suggested_action
            reason = (
                f"{_operation_reason_suggestion(post)}；"
                f"执行{'沉底' if action == RecommendedAction.SINK else '屏蔽'}。"
            )
            payload = {} if action == RecommendedAction.SINK else {"block_reason_code": 1}
            try:
                audit = execute_confirmed_operation(
                    post.review_task,
                    user=request.user,
                    action=action,
                    reason=reason,
                    source=source,
                    upstream_payload=payload,
                    confirmation_key=f"{batch_key}:{post.topic_id}",
                )
                if audit.result_code == "OK":
                    complete_review(
                        post.review_task,
                        user=request.user,
                        action=action,
                        reason=reason,
                    )
                    succeeded += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
                continue
    except Exception:
        messages.error(request, "无法建立上游操作会话，未执行批量处置。")
        return redirect(redirect_url)
    finally:
        if source is not None:
            source.close()
    if succeeded:
        messages.success(request, f"已按 AI 建议完成 {succeeded} 条处置。")
    if failed:
        messages.warning(request, f"{failed} 条未执行成功，仍保持待审核状态。")
    return redirect(redirect_url)


@login_required
@require_POST
def requeue_dashboard_post(request, topic_id: int):
    post = get_object_or_404(OriginalPost, topic_id=topic_id)
    try:
        _, ai_job = enqueue_evaluation_pipeline(post, force=request.POST.get("force") == "yes")
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"已加入 AI 评估队列（任务 {ai_job.pk}）。")
    return redirect(_dashboard_return_url(request, topic_id))


@login_required
@require_POST
def save_dashboard_reply_draft(request, topic_id: int):
    post = get_object_or_404(OriginalPost, topic_id=topic_id)
    reply_draft = getattr(post, "reply_draft", None)
    redirect_url = _dashboard_return_url(request, post.topic_id)
    if reply_draft is None:
        messages.error(request, "该帖子没有可维护的回复草案。")
        return redirect(redirect_url)

    action = request.POST.get("action", "save")
    if action not in {"save", "adopt"}:
        messages.error(request, "回复草案操作无效。")
        return redirect(redirect_url)
    adopted = action == "adopt" or request.POST.get("adopted") == "yes"
    if action == "save" and not adopted:
        adopted = reply_draft.adopted
    try:
        save_reply_draft(post, draft=request.POST.get("draft", ""), adopted=adopted)
    except ValueError as exc:
        messages.error(request, str(exc))
    except ReplyDraft.DoesNotExist:
        messages.error(request, "该帖子没有可维护的回复草案。")
    else:
        message = (
            "已标记为本地采纳，未发布到原平台。"
            if action == "adopt"
            else "回复草案已保存，仅记录在本系统，未发布到原平台。"
        )
        messages.success(request, message)
    return redirect(redirect_url)


def _dashboard_operation_payload(request, action: str) -> dict | None:
    if action in {RecommendedAction.SINK, RecommendedAction.RESTORE, RecommendedAction.UNMUTE}:
        return {}
    selected = {int(value) for value in request.POST.getlist("block_reason") if value.isdigit()}
    allowed = {code for code, _ in BLOCK_REASON_OPTIONS}
    if not selected or not selected <= allowed:
        messages.error(request, "必须选择至少一个已验证的违规原因。")
        return None
    payload = {"block_reason_code": sum(selected)}
    if action == RecommendedAction.MUTE:
        mute_days = request.POST.get("mute_days", "")
        if mute_days not in {"1", "3", "7", "30"}:
            messages.error(request, "必须选择已验证的禁言期限。")
            return None
        payload["mute_days"] = int(mute_days)
    return payload
