"""Read-only operations views for managing the original-post queue."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import OuterRef, Prefetch, Q, Subquery
from django.http import QueryDict, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from config.game_circle_ipc import GameCircleIpcClient, GameCircleIpcError, GameCirclePostClient
from jobs.adapters import SourceContractError, SourceTransportError
from jobs.models import BackgroundJob, JobStatus
from jobs.permanent_mute import (
    enqueue_author_mute_status_refresh,
    enqueue_blocked_user_list_sync,
    enqueue_post_author_mute_status_sync,
    persist_permanent_mute_intent,
)
from jobs.task_types import (
    AUTHOR_MUTE_STATUS_REFRESH_TASK,
    BLOCKED_USER_LIST_SYNC_TASK,
    PERMANENT_MUTE_RENEWAL_TASK,
    POST_AUTHOR_MUTE_STATUS_SYNC_TASK,
)
from reviews.models import (
    RISK_LABEL_DISPLAY,
    AiOperationResolution,
    AiOperationReview,
    AiOperationReviewStatus,
    ContentCategory,
    MuteOperationTrigger,
    OperationAudit,
    RecommendedAction,
    ReplyDraft,
    ReviewStatus,
    ReviewTask,
    risk_label_display,
)
from reviews.reply_faq import community_reply_faq_rule
from reviews.services import (
    BLOCK_REASON_OPTIONS,
    OperationExecutionConflict,
    complete_review,
    current_video_extractions,
    enqueue_evaluation_pipeline,
    escalate_ai_operation_review,
    execute_author_operation_for_identity,
    execute_confirmed_operation,
    is_operation_enabled,
    mark_ai_review_restored,
    operation_result_reaches_target,
    save_reply_draft,
    submit_ai_operation_resolution,
    video_evidence_summaries,
    video_extraction_error_detail,
    video_sampling_notice,
)

from .comment_exports import (
    create_or_reuse_run,
    issue_confirmation_token,
    normalize_keyword,
    read_confirmation_snapshot,
    verify_confirmation_token,
)
from .dashboard_filters import filter_original_posts, parse_original_post_filter
from .game_catalog import (
    ExecutionScope,
    active_game_appid,
    current_execution_scope,
    managed_game_options,
)
from .ingestion_jobs import enqueue_filtered_post_refresh
from .models import (
    POST_STATUS_NORMAL,
    ActiveGameScope,
    AuthorMuteStatus,
    CandidatePost,
    CommentExportRun,
    InteractionSnapshot,
    ManagedGame,
    OfficialAuthor,
    OriginalPost,
    OriginType,
    is_post_blocked,
    is_post_deleted,
    is_post_normal,
    is_post_self_visible,
    is_post_sunk,
)
from .official_authors import (
    apply_identity_presentation,
    disable_official_author,
    enable_official_author,
)
from .services import (
    mute_management_status_queryset,
    refresh_post_status_and_metrics,
    set_permanent_mute_intent,
)

DASHBOARD_SORT_OPTIONS = (
    ("priority_desc", "优先级：高到低"),
    ("priority_asc", "优先级：低到高"),
    ("publish_time_desc", "帖子创建时间：最新在前"),
    ("publish_time_asc", "帖子创建时间：最早在前"),
)

BATCH_AI_ACTIONS = frozenset((RecommendedAction.SINK, RecommendedAction.BLOCK))
BATCH_AI_LIMIT = 20
POST_BOUND_UPSTREAM_ACTIONS = frozenset(
    (
        RecommendedAction.SINK,
        RecommendedAction.BLOCK,
        RecommendedAction.RESTORE,
        RecommendedAction.BLOCK_AND_MUTE,
    )
)

POST_STATUS_FILTER_OPTIONS = (
    ("NORMAL", "正常"),
    ("BLOCKED", "已屏蔽"),
    ("SUNK", "已沉底"),
    ("SELF_VISIBLE", "仅自己可见"),
    ("DELETED", "原平台已删除"),
    ("UNSYNCED", "未同步"),
)


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
    """Activate the service-owned page used for login and session observation."""
    try:
        ipc = GameCircleIpcClient()
        result = ipc.call("OPEN_CONTROLLED_LOGIN")
    except GameCircleIpcError as exc:
        messages.error(request, str(exc))
    else:
        status = result.get("status") if isinstance(result, dict) else ""
        if status == "CONTROLLED_SESSION_LOGIN_PENDING":
            messages.info(
                request,
                "已打开受控会话页，正在等待扫码登录；完成扫码后系统会继续恢复当前游戏 Frame。",
            )
        elif status == "CONTROLLED_SESSION_LOGIN_REQUIRED":
            messages.info(
                request, "已打开受控会话页；当前会话需要重新登录，请在该页面完成扫码后刷新状态。"
            )
        elif status == "CONTROLLED_SESSION_PAGE_OPENED":
            messages.info(request, "已打开受控会话页；请在该页面查看登录状态或完成扫码。")
        else:
            messages.info(request, "已打开受控会话页；请在该页面确认登录状态。")
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


def _corrected_post_transition(review: AiOperationReview) -> str:
    """Return a verified AI-to-human status transition for a corrected review."""
    if review.status != AiOperationReviewStatus.CORRECTED:
        return ""
    successful_resolution = next(
        (
            resolution
            for resolution in getattr(review, "prefetched_resolutions", ())
            if resolution.result_code == "SUCCEEDED"
        ),
        None,
    )
    if successful_resolution is None:
        return ""
    final_post_audit = next(
        (
            audit
            for audit in successful_resolution.operation_audits.all()
            if audit.task_id == review.audit.task_id
            and operation_result_reaches_target(audit.result_code)
        ),
        None,
    )
    if final_post_audit is None:
        return ""
    original_label = _post_status_label(review.audit.after_state.get("status"))
    final_label = _post_status_label(final_post_audit.after_state.get("status"))
    return f"{original_label} → {final_label}"


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
    # An unconfigured game context must not turn into an implicit all-games view.
    return Q(pk__in=[]) if not appid else Q(source_appid=appid)


def _dashboard_active_game(requested_appid: str) -> str:
    """Use only the explicitly selected operation scope; do not fall back to history."""
    try:
        return active_game_appid(requested_appid)
    except ValueError:
        if ActiveGameScope.objects.filter(pk=1).exists():
            return "__invalid_scope__"
        return ""


def _require_active_post_scope(post: OriginalPost) -> None:
    """Reject direct post URLs that would otherwise bypass the selected operation scope."""
    if not post.source_appid:
        raise ValueError("帖子缺少游戏范围，不能调用上游")
    active_game_appid(post.source_appid)


@contextmanager
def _source_for_active_game(appid: str):
    """Create a fixed IPC facade; this Django process never owns CDP state."""
    scope = current_execution_scope(appid)
    source = GameCirclePostClient(scope=scope)
    try:
        yield source
    finally:
        source.close()


@contextmanager
def _source_for_post(post: OriginalPost):
    _require_active_post_scope(post)
    with _source_for_active_game(post.source_appid) as source:
        yield source


def _author_mute_presentation(state: AuthorMuteStatus | None, *, now=None) -> dict[str, object]:
    """Return an explicit display state; missing observation is never treated as unmuted."""
    now = now or timezone.now()
    if state is None:
        return {
            "label": "状态未知（尚未同步）",
            "detail": "尚未获得可核验的作者状态",
            "scope": "",
            "until": None,
            "active": False,
            "tone": "unknown",
        }
    local_active = state.local_muted_until and state.local_muted_until > now
    global_active = state.global_muted_until and state.global_muted_until > now
    if local_active and global_active:
        effective_until = max(state.local_muted_until, state.global_muted_until)
        return {
            "label": "当前游戏及全局禁言",
            "scope": "当前游戏及全局",
            "until": effective_until,
            "remaining": _remaining_time_label(effective_until, now),
            "active": True,
            "tone": "muted",
        }
    if local_active:
        return {
            "label": "当前游戏禁言",
            "scope": "当前游戏",
            "until": state.local_muted_until,
            "remaining": _remaining_time_label(state.local_muted_until, now),
            "active": True,
            "tone": "muted",
        }
    if global_active:
        return {
            "label": "全局禁言",
            "scope": "全局",
            "until": state.global_muted_until,
            "remaining": _remaining_time_label(state.global_muted_until, now),
            "active": True,
            "tone": "muted",
        }
    return {
        "label": "已确认未禁言",
        "detail": "最近一次作者状态同步未发现有效禁言",
        "scope": "",
        "until": None,
        "active": False,
        "tone": "clear",
    }


def _remaining_time_label(until, now) -> str:
    remaining_seconds = max(0, int((until - now).total_seconds()))
    days, remainder = divmod(remaining_seconds, 86_400)
    hours = remainder // 3_600
    if days:
        return f"剩余 {days} 天" if not hours else f"剩余 {days} 天 {hours} 小时"
    if hours:
        return f"剩余 {hours} 小时"
    return "即将到期"


def _author_mute_status_for_post(post: OriginalPost, appid: str) -> AuthorMuteStatus | None:
    if not post.author_openid:
        return None
    return AuthorMuteStatus.objects.filter(
        openid=post.author_openid,
        source_appid=appid or post.source_appid,
    ).first()


def _configuration_scope(appid: str) -> ExecutionScope:
    """Build a read-only target scope without mutating the active game selection."""
    if not ManagedGame.objects.filter(appid=appid).exists():
        raise ValueError("只能选择已同步的可管理游戏")
    scope = ActiveGameScope.objects.filter(pk=1).first()
    if scope is None:
        raise ValueError("尚未配置当前操作游戏")
    return ExecutionScope(target_appid=appid, scope_version=scope.scope_version)


def _official_preview_key(appid: str) -> str:
    return f"official-author-preview:{appid}"


@login_required
def official_author_management(request):
    try:
        current_appid = active_game_appid()
    except ValueError:
        messages.error(request, "请先同步可管理游戏并选择当前操作游戏。")
        return redirect("/admin/")
    appid = request.GET.get("appid", current_appid).strip() or current_appid
    game = ManagedGame.objects.filter(appid=appid).first()
    if game is None:
        messages.error(request, "请选择本地可管理游戏。")
        return redirect(reverse("official-author-management"))
    managed_games = managed_game_options()
    return render(
        request,
        "posts/official_author_management.html",
        {
            "appid": appid,
            "game": game,
            "managed_games": managed_games,
            "authors": OfficialAuthor.objects.filter(
                source_appid=appid, disabled_at__isnull=True
            ).order_by("nick_name", "openid"),
            "preview": request.session.get(_official_preview_key(appid)),
        },
    )


@login_required
@require_POST
def preview_official_author(request):
    appid = request.POST.get("appid", "").strip()
    openid = request.POST.get("openid", "").strip()
    try:
        payload = GameCirclePostClient(scope=_configuration_scope(appid)).get_author_detail(openid)
        user = payload["data"]["user"]
        if not isinstance(user, dict) or user.get("openid") != openid:
            raise ValueError("作者详情响应身份不一致")
        topics = payload.get("data", {}).get("topic_list", [])
        preview_topics = [
            {
                "topic_id": item.get("topic_id"),
                "title": str(item.get("title") or item.get("brief_content") or "")[:120],
                "publish_time": item.get("publish_time"),
            }
            for item in topics[:10]
            if isinstance(item, dict)
        ]
        request.session[_official_preview_key(appid)] = {
            "user_id": request.user.pk,
            "openid": openid,
            "nick_name": str(user.get("nick_name") or "")[:255],
            "verified_at": timezone.now().isoformat(),
            "total": payload.get("data", {}).get("total"),
            "topics": preview_topics,
        }
        messages.success(request, "作者信息已核验，请确认加入官方作者名单。")
    except (GameCircleIpcError, KeyError, TypeError, ValueError) as exc:
        messages.error(request, str(exc) if str(exc) else "作者信息核验失败。")
    return redirect(f"{reverse('official-author-management')}?appid={appid}")


@login_required
@require_POST
def confirm_official_author(request):
    appid = request.POST.get("appid", "").strip()
    preview = request.session.get(_official_preview_key(appid))
    try:
        verified_at = (
            datetime.fromisoformat(str(preview["verified_at"]))
            if isinstance(preview, dict)
            else None
        )
        if (
            not isinstance(preview, dict)
            or preview.get("user_id") != request.user.pk
            or not verified_at
            or timezone.now() - verified_at > timedelta(minutes=10)
        ):
            raise ValueError("作者核验已过期，请重新查询")
        enable_official_author(
            appid=appid,
            openid=str(preview["openid"]),
            nick_name=str(preview.get("nick_name") or ""),
            user=request.user,
        )
        del request.session[_official_preview_key(appid)]
        messages.success(request, "已加入官方作者名单；同游戏的历史与后续帖子会即时按身份展示。")
    except (KeyError, TypeError, ValueError) as exc:
        messages.error(request, str(exc) if str(exc) else "作者核验无效。")
    return redirect(f"{reverse('official-author-management')}?appid={appid}")


@login_required
@require_POST
def disable_official_author_view(request, author_id: int):
    appid = request.POST.get("appid", "").strip()
    author = get_object_or_404(
        OfficialAuthor, pk=author_id, source_appid=appid, disabled_at__isnull=True
    )
    disable_official_author(author=author, user=request.user)
    messages.success(request, "已移除官方作者配置；帖子身份标记已即时取消。")
    return redirect(f"{reverse('official-author-management')}?appid={appid}")


def _comment_export_scope_or_error():
    return current_execution_scope()


def _comment_export_runs(request):
    scope = _comment_export_scope_or_error()
    return (
        CommentExportRun.objects.select_related("job")
        .filter(created_by=request.user, source_appid=scope.target_appid)
        .order_by("-created_at")
    )


@login_required
def comment_export_workspace(request):
    """A short-lived confirmation flow; no reply-page client exists in Web."""
    context = {"active_tab": "new", "step": 1}
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "confirm":
                topic_id = int(request.POST.get("topic_id", ""))
                snapshot = read_confirmation_snapshot(topic_id=topic_id)
                context.update(
                    step=2,
                    snapshot=snapshot.public(),
                    confirmation_token=issue_confirmation_token(
                        snapshot=snapshot, user_id=request.user.pk
                    ),
                )
            elif action == "configure":
                token = request.POST.get("confirmation_token", "")
                token_data = verify_confirmation_token(token=token, user_id=request.user.pk)
                topic_id = token_data.get("topic_id")
                if not isinstance(topic_id, int):
                    raise ValueError("帖子确认无效，请重新确认。")
                snapshot = read_confirmation_snapshot(topic_id=topic_id)
                context.update(
                    step=2,
                    snapshot=snapshot.public(),
                    confirmation_token=token,
                )
                context.update(
                    step=3,
                    confirmation_token=token,
                    keyword=normalize_keyword(request.POST.get("keyword", "")),
                )
            elif action == "create":
                token = request.POST.get("confirmation_token", "")
                keyword = normalize_keyword(request.POST.get("keyword", ""))
                run, created = create_or_reuse_run(
                    user_id=request.user.pk,
                    token=token,
                    keyword_value=keyword,
                )
                context.update(step=4, run=run, created=created)
            else:
                raise ValueError("评论导出操作无效。")
        except (ValueError, SourceContractError, SourceTransportError, GameCircleIpcError) as exc:
            context["form_error"] = str(exc) or "帖子确认失败，请稍后重试。"
            if action == "create":
                context.update(
                    step=3,
                    confirmation_token=request.POST.get("confirmation_token", ""),
                    keyword=request.POST.get("keyword", ""),
                )
    return render(request, "posts/comment_export_workspace.html", context)


@login_required
def comment_export_runs(request):
    return render(
        request,
        "posts/comment_export_runs.html",
        {"active_tab": "runs", "runs": _comment_export_runs(request)[:100]},
    )


def _owned_comment_export_run(request, run_id: int) -> CommentExportRun:
    return get_object_or_404(_comment_export_runs(request), pk=run_id)


@login_required
def comment_export_run_detail(request, run_id: int):
    run = _owned_comment_export_run(request, run_id)
    if run.job.status != JobStatus.SUCCEEDED:
        messages.warning(request, "任务尚未成功完成，暂不能查看命中结果。")
        return redirect("comment-export-runs")
    return render(
        request,
        "posts/comment_export_run_detail.html",
        {"run": run, "results": run.results.all()[:500]},
    )


@login_required
def export_comment_export_csv(request, run_id: int):
    run = _owned_comment_export_run(request, run_id)
    if run.job.status != JobStatus.SUCCEEDED:
        messages.error(request, "仅已成功完成的任务可以导出 CSV。")
        return redirect("comment-export-runs")

    def rows():
        yield "\ufeffopenid,nick_name,create_time,content,game_id\r\n"
        import csv
        from io import StringIO

        for result in run.results.iterator(chunk_size=500):
            buffer = StringIO()
            csv.writer(buffer, lineterminator="\r\n").writerow(
                [
                    result.openid,
                    result.nick_name,
                    result.create_time,
                    result.content,
                    result.game_id,
                ]
            )
            yield buffer.getvalue()

    response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="comment-export-{run.pk}.csv"'
    return response


@login_required
def operations_dashboard(request):
    normalized_query, query_was_normalized = _normalize_dashboard_query(request.GET)
    if query_was_normalized:
        query_string = normalized_query.urlencode()
        target = reverse("operations-dashboard")
        return redirect(f"{target}?{query_string}" if query_string else target)
    appid = _dashboard_active_game(request.GET.get("appid", "").strip())
    game_filter = _dashboard_game_filter(appid)
    try:
        post_filter = parse_original_post_filter(request.GET)
    except ValueError:
        # Query normalization already preserves only supported dashboard
        # fields.  This is defensive for hand-crafted URLs and keeps list and
        # background-refresh predicates identical.
        post_filter = parse_original_post_filter(QueryDict(""))
    filters = {
        "post_id": "" if post_filter.post_id is None else str(post_filter.post_id),
        "topic_label": post_filter.topic_label,
        "has_topics": post_filter.has_topics,
        "category": list(post_filter.category),
        "risk": list(post_filter.risk),
        "suggested_action": list(post_filter.suggested_action),
        "review_status": list(post_filter.review_status),
        "post_status": list(post_filter.post_status),
        "min_confidence": ""
        if post_filter.min_confidence is None
        else str(post_filter.min_confidence),
        "min_views": "" if post_filter.min_views is None else str(post_filter.min_views),
        "reviewed": post_filter.reviewed,
        **{key: request.GET.get(key, "").strip() for key in ("sort", "priority_order")},
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
    posts = filter_original_posts(posts, source_appid=appid, filters=post_filter)
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
    game_name = ManagedGame.objects.filter(appid=appid).values_list("name", flat=True).first() or ""
    apply_identity_presentation(posts, appid=appid, game_name=game_name)
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
        reply_draft = getattr(selected_post, "reply_draft", None)
        if reply_draft and reply_draft.faq_rule_id:
            rule = community_reply_faq_rule(reply_draft.faq_rule_id)
            reply_draft.faq_scene = rule.scene if rule else ""
        selected_post.author_mute_status = _author_mute_status_for_post(selected_post, appid)
        selected_post.author_mute_presentation = _author_mute_presentation(
            selected_post.author_mute_status
        )
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
            extraction.sampling_explanation = video_sampling_notice(extraction)
            extraction.error_detail = video_extraction_error_detail(extraction)
            if (
                extraction.status == "SKIPPED_LIMIT"
                and extraction.error_summary == "video_duration_limit_exceeded"
                and extraction.duration_ms is not None
                and extraction.duration_limit_seconds is not None
            ):
                extraction.limit_explanation = (
                    f"视频时长 {extraction.duration_ms / 1000:g} 秒，超过提取时资源与采样时长上限 "
                    f"{extraction.duration_limit_seconds:g} 秒，未进行提取。"
                )
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
            selected_post.ai_operation_reviews[0] if selected_post.ai_operation_reviews else None
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
        "search": post_filter.search,
        "appid": appid,
        "game_name": game_name,
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


@login_required
def mute_management(request):
    """Manage local permanent intent while keeping upstream observation distinct."""
    appid = _dashboard_active_game(request.GET.get("appid", "").strip())
    search = request.GET.get("q", "").strip()
    scope = request.GET.get("scope", "").strip().upper()
    expires = request.GET.get("expires", "").strip().upper()
    kind = request.GET.get("kind", "").strip().upper()
    if scope not in {"", "LOCAL", "GLOBAL"}:
        scope = ""
    if expires not in {"", "SOON"}:
        expires = ""
    if kind not in {"", "PERMANENT", "TEMP"}:
        kind = ""
    now = timezone.now()
    active_mutes = Q(local_muted_until__gt=now) | Q(global_muted_until__gt=now)
    statuses = (
        mute_management_status_queryset(source_appid=appid, now=now)
        if appid
        else AuthorMuteStatus.objects.filter(Q(permanent_mute_enabled=True) | active_mutes)
    )
    if search:
        statuses = statuses.filter(nick_name__icontains=search)
    if kind == "PERMANENT":
        statuses = statuses.filter(permanent_mute_enabled=True)
    elif kind == "TEMP":
        statuses = statuses.filter(
            permanent_mute_enabled=False,
        ).filter(active_mutes)
    if scope == "LOCAL":
        statuses = statuses.filter(local_muted_until__gt=now)
    elif scope == "GLOBAL":
        statuses = statuses.filter(global_muted_until__gt=now)
    if expires == "SOON":
        soon = now + timedelta(days=7)
        local_active = Q(local_muted_until__gt=now)
        global_active = Q(global_muted_until__gt=now)
        local_soon = Q(local_muted_until__gt=now, local_muted_until__lte=soon)
        global_soon = Q(global_muted_until__gt=now, global_muted_until__lte=soon)
        statuses = statuses.filter(
            # When both scopes are active, the presented effective deadline is
            # their later value.  A row is therefore imminent only when both
            # active deadlines are imminent.
            (local_soon & (global_soon | ~global_active)) | (global_soon & ~local_active)
        )
    statuses = list(
        statuses.select_related("last_source_post", "permanent_mute_set_by").order_by(
            "-last_synced_at"
        )
    )
    for status in statuses:
        status.presentation = _author_mute_presentation(status, now=now)
        status.system_muted_at = (
            OperationAudit.objects.filter(
                author_status=status,
                action=RecommendedAction.MUTE,
                result_code="OK",
            )
            .order_by("-executed_at")
            .values_list("executed_at", flat=True)
            .first()
        )
    statuses.sort(
        key=lambda status: (
            status.presentation["until"] is None,
            status.presentation["until"] or datetime.max.replace(tzinfo=UTC),
            -status.last_synced_at.timestamp(),
        )
    )
    job = None
    requested_job_id = request.GET.get("job", "")
    if requested_job_id.isdigit():
        job = BackgroundJob.objects.filter(
            pk=int(requested_job_id),
            task_type__in=(
                PERMANENT_MUTE_RENEWAL_TASK,
                AUTHOR_MUTE_STATUS_REFRESH_TASK,
                POST_AUTHOR_MUTE_STATUS_SYNC_TASK,
                BLOCKED_USER_LIST_SYNC_TASK,
            ),
        ).first()
    return render(
        request,
        "posts/mute_management.html",
        {
            "appid": appid,
            "active_game": next(
                (game for game in managed_game_options() if game.appid == appid), None
            ),
            "managed_games": managed_game_options(),
            "statuses": statuses,
            "search": search,
            "scope": scope,
            "expires": expires,
            "kind": kind,
            "last_job_feedback": _permanent_mute_job_feedback(job),
            "return_query": request.GET.urlencode(),
        },
    )


def _permanent_mute_job_feedback(job: BackgroundJob | None) -> dict | None:
    """Present durable job state without exposing opaque worker error codes."""
    if job is None:
        return None
    checkpoint = job.checkpoint if isinstance(job.checkpoint, dict) else {}
    processed = int(checkpoint.get("processed", 0))
    succeeded = int(checkpoint.get("succeeded", 0))
    failed = int(checkpoint.get("failed", 0))
    skipped_missing_openid = int(checkpoint.get("skipped_missing_openid", 0))
    labels = {
        PERMANENT_MUTE_RENEWAL_TASK: "永久禁言任务",
        AUTHOR_MUTE_STATUS_REFRESH_TASK: "禁言列表状态刷新任务",
        POST_AUTHOR_MUTE_STATUS_SYNC_TASK: "全量帖子作者禁言扫描任务",
        BLOCKED_USER_LIST_SYNC_TASK: "平台当前禁言名单导入任务",
    }
    label = labels.get(job.task_type, "禁言管理任务")
    if job.task_type == BLOCKED_USER_LIST_SYNC_TASK:
        total = checkpoint.get("total")
        total_label = f"平台共 {total} 位；" if isinstance(total, int) else ""
        detail = (
            f"{total_label}已读取 {processed} 位，导入 {int(checkpoint.get('imported', 0))} 位，"
            f"跳过 {int(checkpoint.get('skipped', 0))} 位。"
        )
    else:
        detail = f"已处理 {processed} 位，成功 {succeeded} 位，失败 {failed} 位。"
    if skipped_missing_openid:
        detail += f"已跳过 {skipped_missing_openid} 条缺少作者 ID 的帖子。"
    if job.status == JobStatus.SUCCEEDED:
        return {"job": job, "state": "success", "title": f"{label}已完成", "detail": detail}
    if job.status == JobStatus.RUNNING:
        return {"job": job, "state": "pending", "title": f"{label}处理中", "detail": detail}
    if job.status == JobStatus.PENDING:
        return {
            "job": job,
            "state": "pending",
            "title": f"{label}等待处理",
            "detail": "禁言维护 Worker 启动后会自动处理。" if not job.started_at else detail,
        }
    if job.task_type == PERMANENT_MUTE_RENEWAL_TASK and "SOURCE_DELETED" in str(
        checkpoint.get("last_error", "")
    ):
        return {
            "job": job,
            "state": "error",
            "title": "任务未完成：来源帖子已从管理队列删除",
            "detail": "该作者没有可回读的来源帖子，无法执行需要上游写入的禁言；已有充足有效期时可直接保存永久意图。",
        }
    return {
        "job": job,
        "state": "error",
        "title": f"{label}未完成",
        "detail": detail,
    }


def _mute_management_return_url(request, *, job_id: int | None = None) -> str:
    """Keep permanent-management redirects in the local mute workspace."""
    params = QueryDict(request.POST.get("return_query", ""), mutable=True)
    params.pop("job", None)
    if job_id is not None:
        params["job"] = str(job_id)
    query = params.urlencode()
    target = reverse("mute-management")
    return f"{target}?{query}" if query else target


@login_required
@require_POST
def enqueue_author_mute_status_refresh_request(request):
    """Queue a bounded read-only refresh; the Web request never contacts the provider."""
    try:
        result = enqueue_author_mute_status_refresh()
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(_mute_management_return_url(request))
    outcome = "已创建" if result.created else "已复用正在处理的"
    messages.success(
        request,
        f"{outcome}禁言列表状态刷新任务；仅刷新自动续期或仍有效禁言的列表作者，不执行禁言或解除。",
    )
    return redirect(_mute_management_return_url(request, job_id=result.job.pk))


@login_required
@require_POST
def enqueue_post_author_mute_status_sync_request(request):
    """Queue a read-only distinct-author scan that only supplements muted users."""
    try:
        result = enqueue_post_author_mute_status_sync()
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(_mute_management_return_url(request))
    outcome = "已创建" if result.created else "已复用正在处理的"
    messages.success(
        request,
        f"{outcome}全量帖子作者禁言扫描任务；仅将当前游戏或全局被禁言作者补充到禁言列表，不设置自动续期。",
    )
    return redirect(_mute_management_return_url(request, job_id=result.job.pk))


@login_required
@require_POST
def enqueue_blocked_user_list_sync_request(request):
    """Queue the platform's current blocked-user list import without source I/O in Web."""
    try:
        result = enqueue_blocked_user_list_sync()
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(_mute_management_return_url(request))
    outcome = "已创建" if result.created else "已复用正在处理的"
    messages.success(
        request,
        f"{outcome}平台当前禁言名单导入任务；仅同步仍在名单中的作者与当前游戏截止时间，不设置自动续期或解除本地状态。",
    )
    return redirect(_mute_management_return_url(request, job_id=result.job.pk))


@login_required
@require_POST
@transaction.atomic
def apply_permanent_mute_intent(request):
    """Apply one or many local permanent-intent choices without an upstream operation."""
    action = request.POST.get("action", "")
    if action not in {"enable", "disable"}:
        messages.error(request, "永久禁言标记操作无效。")
        return redirect(_mute_management_return_url(request))
    requested_ids: list[int] = []
    for value in request.POST.getlist("status_id"):
        try:
            requested_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    requested_ids = list(dict.fromkeys(requested_ids))[:100]
    if not requested_ids:
        messages.warning(request, "请先选择至少一位作者。")
        return redirect(_mute_management_return_url(request))
    try:
        scope = current_execution_scope()
        statuses = list(
            AuthorMuteStatus.objects.filter(
                pk__in=requested_ids, source_appid=scope.target_appid
            ).order_by("pk")
        )
        if not statuses:
            raise ValueError("没有可在当前游戏管理的作者")
        enabled = action == "enable"
        for status in statuses:
            persist_permanent_mute_intent(status=status, enabled=enabled, user=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(_mute_management_return_url(request))
    if enabled:
        messages.success(
            request, f"已标记 {len(statuses)} 位作者为永久禁言；续期由禁言维护 Worker 按计划处理。"
        )
    else:
        messages.success(
            request, f"已解除 {len(statuses)} 位作者的永久禁言标记；当前上游禁言保持不变。"
        )
    return redirect(_mute_management_return_url(request))


@login_required
@require_POST
def confirm_author_mute_operation(request, status_id: int, action: str):
    """Run a high-risk author operation from its durable status target, never a post."""
    action = action.upper()
    if action not in {RecommendedAction.MUTE, RecommendedAction.UNMUTE}:
        messages.error(request, "不支持的作者操作。")
        return redirect(_mute_management_return_url(request))
    appid = _dashboard_active_game(request.POST.get("appid", "").strip())
    status = get_object_or_404(AuthorMuteStatus, pk=status_id, source_appid=appid)
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "必须填写业务原因。")
        return redirect(_mute_management_return_url(request))
    payload = _dashboard_operation_payload(request, action)
    if payload is None:
        return redirect(_mute_management_return_url(request))
    try:
        with _source_for_active_game(status.source_appid) as source:
            audit = execute_author_operation_for_identity(
                openid=status.openid,
                source_appid=status.source_appid,
                user=request.user,
                action=action,
                reason=reason,
                source=source,
                upstream_payload=payload,
                confirmation_key=request.POST.get("confirmation_key"),
                mute_trigger=MuteOperationTrigger.HUMAN_INITIAL,
            )
    except (ValueError, PermissionError) as exc:
        messages.error(request, str(exc))
    except OperationExecutionConflict as exc:
        messages.warning(request, str(exc))
    except Exception:
        messages.error(request, "无法核验作者身份或建立上游操作会话，未执行作者操作。")
    else:
        if operation_result_reaches_target(audit.result_code):
            if action == RecommendedAction.MUTE and "permanent_mute" in request.POST:
                set_permanent_mute_intent(
                    status,
                    enabled=bool(payload.get("permanent_mute")),
                    user=request.user,
                )
            messages.success(request, f"作者操作已完成：{audit.result_code}。")
        else:
            messages.warning(request, f"作者操作未成功：{audit.result_code}。")
    return redirect(_mute_management_return_url(request))


@login_required
def author_mute_history(request, status_id: int):
    """Show saved original posts for one status row without exposing OpenID in the URL."""
    appid = _dashboard_active_game(request.GET.get("appid", "").strip())
    status_query = {"pk": status_id}
    if appid:
        status_query["source_appid"] = appid
    status = get_object_or_404(AuthorMuteStatus, **status_query)
    posts = _with_latest_metrics(
        OriginalPost.objects.filter(_dashboard_game_filter(appid), author_openid=status.openid)
    ).order_by("-publish_time", "-topic_id")
    return render(
        request,
        "posts/author_mute_history.html",
        {
            "appid": appid,
            "active_game": next(
                (game for game in managed_game_options() if game.appid == appid), None
            ),
            "status": status,
            "presentation": _author_mute_presentation(status),
            "posts": posts,
        },
    )


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


def _has_session_recovering_result(audits: list[OperationAudit]) -> bool:
    return any(audit.result_code == "SESSION_RECOVERING" for audit in audits)


def _persist_human_mute_intent(
    *, post: OriginalPost, payload: dict, audit: OperationAudit, user
) -> None:
    """Persist the local long-term choice only after a verified upstream mute."""
    if not operation_result_reaches_target(audit.result_code):
        return
    state = AuthorMuteStatus.objects.filter(
        openid=post.author_openid, source_appid=post.source_appid
    ).first()
    if state is None:
        # A successful MUTE always performs a bounded readback.  Refuse to infer a
        # local permanent state when that observation was not persisted.
        raise ValueError("禁言回读未形成作者状态，未保存永久禁言意图")
    set_permanent_mute_intent(state, enabled=bool(payload.get("permanent_mute")), user=user)


@login_required
def post_detail(request, topic_id: int):
    post = get_object_or_404(_with_latest_metrics(OriginalPost.objects.all()), topic_id=topic_id)
    game_name = (
        ManagedGame.objects.filter(appid=post.source_appid).values_list("name", flat=True).first()
        or ""
    )
    apply_identity_presentation([post], appid=post.source_appid, game_name=game_name)
    return render(
        request,
        "posts/post_detail.html",
        {
            "post": post,
            "game_name": game_name,
            "metrics": post.interaction_snapshots.order_by("-captured_at")[:10],
        },
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
    try:
        with _source_for_post(post) as client:
            refreshed = refresh_post_status_and_metrics(client, post)
    except Exception:
        messages.error(request, "刷新失败，请检查浏览器登录状态或稍后重试。")
    else:
        if is_post_deleted(refreshed.status):
            messages.warning(request, "该帖子已从原平台管理队列删除，已标记为不可处置状态。")
        else:
            messages.success(request, "帖子状态和互动指标已同步。")
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

    try:
        source_context = _source_for_post(post) if post.source_appid else nullcontext(None)
        with source_context as source:
            confirmation_key = request.POST.get("confirmation_key")
            audits = []
            for index, (operation, payload) in enumerate(operation_plan, start=1):
                key = f"{confirmation_key}:ai-correction:{index}:{operation.lower()}"
                if operation == RecommendedAction.MUTE:
                    if not post.author_openid or not post.source_appid:
                        raise ValueError("帖子未保存可核验的作者身份，不能执行作者禁言")
                    audit = execute_author_operation_for_identity(
                        openid=post.author_openid,
                        source_appid=post.source_appid,
                        user=request.user,
                        action=operation,
                        reason=reason,
                        source=source,
                        upstream_payload=payload,
                        confirmation_key=key,
                        mute_trigger=MuteOperationTrigger.HUMAN_INITIAL,
                    )
                else:
                    audit = execute_confirmed_operation(
                        task,
                        user=request.user,
                        action=operation,
                        reason=reason,
                        source=source,
                        upstream_payload=payload,
                        confirmation_key=key,
                    )
                audits.append(audit)
                if not operation_result_reaches_target(audit.result_code):
                    break
    except OperationExecutionConflict as exc:
        messages.warning(request, str(exc))
        return redirect(redirect_url)
    except Exception:
        messages.error(request, "无法建立上游操作会话，未执行纠正处置。")
        return redirect(redirect_url)

    result_summary = "，".join(f"{audit.action}={audit.result_code}" for audit in audits)
    if _has_session_recovering_result(audits):
        messages.warning(request, "游戏圈会话正在恢复，请稍后重新确认。")
        return redirect(redirect_url)
    if not all(audit.result_code == "OK" for audit in audits):
        messages.warning(request, f"纠正处置未全部成功，AI 异常仍待跟进：{result_summary}")
        return redirect(redirect_url)

    try:
        for operation, payload in operation_plan:
            if operation != RecommendedAction.MUTE:
                continue
            audit = next(item for item in audits if item.action == RecommendedAction.MUTE)
            _persist_human_mute_intent(post=post, payload=payload, audit=audit, user=request.user)
    except ValueError as exc:
        messages.warning(request, f"纠正处置已执行，但{exc}。")
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
    if is_post_deleted(post.status) and action in POST_BOUND_UPSTREAM_ACTIONS:
        messages.error(request, "帖子已从原平台管理队列删除，不能执行帖子处置。")
        return redirect(redirect_url)
    if is_post_self_visible(post.status) and action in POST_BOUND_UPSTREAM_ACTIONS:
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
    if action in {RecommendedAction.MUTE, RecommendedAction.UNMUTE}:
        if not post.author_openid or not post.source_appid:
            messages.error(request, "帖子未保存可核验的作者身份，不能执行禁言操作。")
            return redirect(redirect_url)
        payload = _dashboard_operation_payload(request, action)
        if payload is None:
            return redirect(redirect_url)
        try:
            with _source_for_post(post) as source:
                audit = execute_author_operation_for_identity(
                    openid=post.author_openid,
                    source_appid=post.source_appid,
                    user=request.user,
                    action=action,
                    reason=reason,
                    source=source,
                    upstream_payload=payload,
                    confirmation_key=request.POST.get("confirmation_key"),
                    mute_trigger=MuteOperationTrigger.HUMAN_INITIAL,
                )
        except OperationExecutionConflict as exc:
            messages.warning(request, str(exc))
        except PermissionError as exc:
            messages.error(request, str(exc))
        except Exception:
            messages.error(request, "无法核验作者身份或建立上游操作会话，未执行禁言。")
        else:
            if operation_result_reaches_target(audit.result_code):
                if action == RecommendedAction.MUTE:
                    try:
                        _persist_human_mute_intent(
                            post=post, payload=payload, audit=audit, user=request.user
                        )
                    except ValueError as exc:
                        messages.warning(request, f"禁言已执行，但{exc}。")
                        return redirect(redirect_url)
                messages.success(request, f"作者操作已完成：{audit.result_code}。")
            elif audit.result_code == "SESSION_RECOVERING":
                messages.warning(request, "游戏圈会话正在恢复，请稍后重新确认。")
            else:
                messages.warning(request, f"作者操作未成功：{audit.result_code}。")
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
        try:
            source_context = _source_for_post(post) if post.source_appid else nullcontext(None)
            with source_context as source:
                token = request.POST.get("confirmation_key")
                block_audit = execute_confirmed_operation(
                    task,
                    user=request.user,
                    action=RecommendedAction.BLOCK,
                    reason=reason,
                    source=source,
                    upstream_payload={"block_reason_code": payload["block_reason_code"]},
                    confirmation_key=f"{token}:block",
                )
                audits = [block_audit]
                if operation_result_reaches_target(block_audit.result_code):
                    if not post.author_openid or not post.source_appid:
                        raise ValueError("帖子未保存可核验的作者身份，不能执行作者禁言")
                    audits.append(
                        execute_author_operation_for_identity(
                            openid=post.author_openid,
                            source_appid=post.source_appid,
                            user=request.user,
                            action=RecommendedAction.MUTE,
                            reason=reason,
                            source=source,
                            upstream_payload=payload,
                            confirmation_key=f"{token}:mute",
                            mute_trigger=MuteOperationTrigger.HUMAN_INITIAL,
                        )
                    )
        except OperationExecutionConflict as exc:
            messages.warning(request, str(exc))
            return redirect(redirect_url)
        except Exception:
            messages.error(request, "无法建立上游操作会话，未执行处置。")
            return redirect(redirect_url)
        result_summary = "，".join(f"{item.action}={item.result_code}" for item in audits)
        if _has_session_recovering_result(audits):
            messages.warning(request, "游戏圈会话正在恢复，请稍后重新确认。")
            return redirect(redirect_url)
        if all(operation_result_reaches_target(item.result_code) for item in audits):
            mute_audit = next(item for item in audits if item.action == RecommendedAction.MUTE)
            try:
                _persist_human_mute_intent(
                    post=post, payload=payload, audit=mute_audit, user=request.user
                )
            except ValueError as exc:
                messages.warning(request, f"组合处置已执行，但{exc}。")
                return redirect(redirect_url)
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
    try:
        source_context = (
            _source_for_post(post)
            if is_operation_enabled(action) and post.source_appid
            else nullcontext(None)
        )
        with source_context as source:
            audit = execute_confirmed_operation(
                task,
                user=request.user,
                action=action,
                reason=reason,
                source=source,
                upstream_payload=payload,
                confirmation_key=request.POST.get("confirmation_key"),
                mute_trigger=(
                    MuteOperationTrigger.HUMAN_INITIAL if action == RecommendedAction.MUTE else ""
                ),
            )
    except OperationExecutionConflict as exc:
        messages.warning(request, str(exc))
        return redirect(redirect_url)
    except Exception:
        messages.error(request, "无法建立上游操作会话，未执行处置。")
        return redirect(redirect_url)
    if operation_result_reaches_target(audit.result_code):
        if action == RecommendedAction.MUTE:
            try:
                _persist_human_mute_intent(
                    post=post, payload=payload, audit=audit, user=request.user
                )
            except ValueError as exc:
                messages.warning(request, f"禁言已执行，但{exc}。")
                return redirect(redirect_url)
        complete_review(task, user=request.user, action=action, reason=reason)
        if action == RecommendedAction.RESTORE:
            mark_ai_review_restored(task, audit=audit)
        messages.success(request, f"处置成功，审核已完成：{audit.result_code}。")
    elif audit.result_code == "SESSION_RECOVERING":
        messages.warning(request, "游戏圈会话正在恢复，请稍后重新确认。")
    else:
        messages.warning(request, f"处置未成功，审核保持待处理：{audit.result_code}。")
    return redirect(redirect_url)


@login_required
def ai_operation_reviews(request):
    appid = _dashboard_active_game(request.GET.get("appid", "").strip())
    reviews = AiOperationReview.objects.select_related(
        "audit__task__post", "audit__task__assessment", "audit__policy_version", "reviewer"
    ).prefetch_related(
        "audit__task__assessment__risk_evidence",
        Prefetch(
            "resolutions",
            queryset=AiOperationResolution.objects.prefetch_related("operation_audits"),
            to_attr="prefetched_resolutions",
        ),
    )
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
        review.corrected_post_transition = _corrected_post_transition(review)
        post = review.audit.task.post
        _set_status_context(post)
        review.post_actions_available = not (post.is_deleted or post.is_self_visible)
        review.post_unavailable_reason = (
            "帖子已删除，不能再改变帖子状态；仍可在已保存作者身份可核验时单独禁言作者。"
            if post.is_deleted
            else "帖子仅自己可见，不能覆盖帖子状态；仍可在已保存作者身份可核验时单独禁言作者。"
            if post.is_self_visible
            else ""
        )
        author_status = _author_mute_status_for_post(post, appid)
        review.author_status_id = author_status.pk if author_status else None
        review.author_nick_name = (
            author_status.nick_name if author_status else post.author_nick_name
        ) or ""
        review.author_mute_presentation = _author_mute_presentation(author_status)
        review.author_actions_available = bool(post.author_openid and post.source_appid)
        review.author_unavailable_reason = (
            "该帖子没有已保存的作者身份，不能安全执行作者禁言。"
            if not review.author_actions_available
            else ""
        )
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
    author_decision = request.POST.get("author_decision", "UNCHANGED").upper()
    try:
        if decision == "escalate":
            # Historical links keep their explicit escalation transition.  New
            # records use the in-place final-decision dialog instead.
            escalate_ai_operation_review(review, user=request.user, reason=reason)
            messages.info(request, "已升级为人工跟进，请在内容管理中继续处理历史记录。")
            return redirect(_escalated_review_dashboard_url(request, review))
        if decision not in {"confirm", "resolve"}:
            messages.error(request, "不支持的复审操作。")
            return redirect(redirect_url)
        if final_action == RecommendedAction.MUTE:
            final_action, author_decision = "KEEP_CURRENT", RecommendedAction.MUTE
        elif final_action == RecommendedAction.BLOCK_AND_MUTE:
            final_action, author_decision = RecommendedAction.BLOCK, RecommendedAction.MUTE
        post_decision = "KEEP_CURRENT" if decision == "confirm" else final_action
        if not post_decision:
            raise ValueError("请选择最终帖子处置")
        post_payload = None
        if post_decision == RecommendedAction.BLOCK:
            post_payload = _dashboard_operation_payload(request, RecommendedAction.BLOCK)
        author_payload = None
        if author_decision == RecommendedAction.MUTE:
            author_payload = _dashboard_operation_payload(request, RecommendedAction.MUTE)
        if post_payload is None and post_decision == RecommendedAction.BLOCK:
            return redirect(redirect_url)
        if author_payload is None and author_decision == RecommendedAction.MUTE:
            return redirect(redirect_url)
        requires_write = (
            post_decision != "KEEP_CURRENT" or author_decision == RecommendedAction.MUTE
        )
        source_context = (
            _source_for_post(review.audit.task.post) if requires_write else nullcontext(None)
        )
        with source_context as source:
            resolution, audits = submit_ai_operation_resolution(
                review,
                user=request.user,
                post_decision=post_decision,
                author_decision=author_decision,
                reason=reason,
                confirmation_key=request.POST.get("confirmation_key", ""),
                source=source,
                post_payload=post_payload,
                author_payload=author_payload,
            )
        if resolution.result_code == "SUCCEEDED":
            messages.success(request, "AI 复审最终处置已完成。")
        else:
            summary = "，".join(f"{audit.action}={audit.result_code}" for audit in audits)
            messages.warning(
                request, f"最终处置未全部成功，复审保持待处理：{summary or '请重新确认'}。"
            )
    except ValueError as exc:
        messages.error(request, str(exc))
    except PermissionError as exc:
        messages.error(request, str(exc))
    except OperationExecutionConflict as exc:
        messages.warning(request, str(exc))
    except Exception:
        messages.error(request, "无法建立上游操作会话或完成最终处置。")
    return redirect(redirect_url)


@login_required
@require_POST
def enqueue_filtered_post_refresh_request(request):
    """Freeze the current list predicate and delegate all source work to backfill ingestion."""
    try:
        filters = parse_original_post_filter(QueryDict(request.POST.get("filters", "")))
        result = enqueue_filtered_post_refresh(filters=filters)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        outcome = "已创建" if result.created else "已复用正在处理的"
        messages.success(request, f"{outcome}筛选原创帖刷新任务；将在后台同步全部匹配帖。")
    query = request.POST.get("return_query", "")
    target = reverse("operations-dashboard")
    return redirect(f"{target}?{query}" if query else target)


@login_required
@require_POST
def execute_batch_ai_recommendations(request):
    """Apply only explicit, high-signal AI sink/block recommendations after a human click."""
    appid = _dashboard_active_game(request.POST.get("appid", "").strip())
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

    succeeded = failed = recovering = 0
    try:
        source_context = (
            _source_for_active_game(appid)
            if any(is_operation_enabled(post.review_task.suggested_action) for post in posts)
            else nullcontext(None)
        )
        with source_context as source:
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
                    if operation_result_reaches_target(audit.result_code):
                        complete_review(
                            post.review_task,
                            user=request.user,
                            action=action,
                            reason=reason,
                        )
                        succeeded += 1
                    elif audit.result_code == "SESSION_RECOVERING":
                        recovering += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
                    continue
    except Exception:
        messages.error(request, "无法建立上游操作会话，未执行批量处置。")
        return redirect(redirect_url)
    if succeeded:
        messages.success(request, f"已按 AI 建议完成 {succeeded} 条处置。")
    if recovering:
        messages.warning(request, f"{recovering} 条游戏圈会话正在恢复，请稍后重新确认。")
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
        save_reply_draft(
            post,
            draft=request.POST.get("draft", ""),
            adopted=adopted,
            selected_style=request.POST.get("selected_style", ""),
        )
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
    """Validate operator choices and apply the documented mute defaults."""
    if action in {RecommendedAction.SINK, RecommendedAction.RESTORE, RecommendedAction.UNMUTE}:
        return {}
    selected = {int(value) for value in request.POST.getlist("block_reason") if value.isdigit()}
    if not selected and action == RecommendedAction.MUTE:
        selected = {1}
    allowed = {code for code, _ in BLOCK_REASON_OPTIONS}
    if not selected or not selected <= allowed:
        messages.error(request, "必须选择至少一个已验证的违规原因。")
        return None
    payload = {"block_reason_code": sum(selected)}
    if action == RecommendedAction.MUTE:
        mute_days = request.POST.get("mute_days", "30")
        if mute_days not in {"1", "3", "7", "30"}:
            messages.error(request, "必须选择已验证的禁言期限。")
            return None
        payload["mute_days"] = int(mute_days)
        payload["permanent_mute"] = request.POST.get("permanent_mute", "1") == "1"
    return payload
