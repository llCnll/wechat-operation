"""AI assessment persistence, policy recommendation, and human-only review workflow."""

from __future__ import annotations

import re
from datetime import timedelta
from difflib import SequenceMatcher
from hashlib import sha256
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from config.settings import runtime
from jobs.models import BackgroundJob, JobStatus
from jobs.services import enqueue_job
from posts.models import (
    POST_STATUS_NORMAL,
    CandidatePost,
    MediaType,
    OriginalPost,
    OriginType,
    is_post_deleted,
    is_post_self_visible,
)

from .adapters import AssessmentOutput
from .models import (
    AiAssessment,
    ContentCategory,
    OperationAudit,
    PolicyVersion,
    RecommendedAction,
    ReplyDraft,
    ReviewStatus,
    ReviewTask,
    RiskEvidence,
    VideoExtraction,
    VideoExtractionStatus,
)
from .video import media_fingerprint
from .video_tasks import VIDEO_EXTRACT_TASK, VIDEO_EXTRACTOR_VERSION, normalized_ocr_text

EXPECTED_OPERATION_STATUS = {
    RecommendedAction.RESTORE: 2,
    RecommendedAction.SINK: 8,
    RecommendedAction.BLOCK: 4,
}
MUTE_DURATIONS_DAYS = frozenset((1, 3, 7, 30))
BLOCK_REASON_OPTIONS = (
    (1, "其他"),
    (2, "色情低俗"),
    (4, "政治敏感"),
    (8, "广告欺诈"),
    (16, "欺诈钱财"),
)
OCR_EXTRACT_TASK = "OCR_EXTRACT"
AI_EVALUATE_TASK = "AI_EVALUATE"
EVALUATION_PIPELINE_VERSION = "v3"
MAX_REPLY_DRAFT_LENGTH = 4000
VIDEO_OCR_SUMMARY_GAP_MS = 1_500
VIDEO_OCR_SUMMARY_MAX_SPAN_MS = 5_000
VIDEO_OCR_SUMMARY_MAX_TEXTS = 3
VIDEO_OCR_SUMMARY_SIMILARITY_THRESHOLD = 0.85
MAX_EVALUATION_CONTENT_CHARS = 12_000
MAX_IMAGE_OCR_CHARS = 8_000
MAX_VIDEO_EVIDENCE_CHARS = 12_000
MAX_EVALUATION_OCR_CHARS = 20_000


def is_operation_enabled(action: str) -> bool:
    """Both the global and the per-operation switch must be explicitly enabled."""
    return runtime.enable_upstream_writes and {
        RecommendedAction.SINK: runtime.enable_sink_writes,
        RecommendedAction.BLOCK: runtime.enable_block_writes,
        RecommendedAction.MUTE: runtime.enable_mute_writes,
        RecommendedAction.UNMUTE: runtime.enable_unmute_writes,
        RecommendedAction.RESTORE: runtime.enable_restore_writes,
    }.get(action, False)


def _operation_state(payload: dict) -> dict:
    """Keep only the state fields needed for the operation audit."""
    try:
        topic = payload["data"]["topic"]
        topic_id = int(topic["topic_id"])
        status = int(topic["status"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("操作状态回读结构无效") from exc
    creator = topic.get("creater") if isinstance(topic, dict) else None
    block_info = creator.get("block_info") if isinstance(creator, dict) else None
    local_block_end_time = 0
    if isinstance(block_info, dict) and block_info.get("appid") == topic.get("appid"):
        try:
            local_block_end_time = int(block_info.get("block_end_time", 0))
        except (TypeError, ValueError):
            local_block_end_time = 0
    try:
        global_block_end_time = int(creator.get("global_block_end_time", 0))
    except (AttributeError, TypeError, ValueError):
        global_block_end_time = 0
    return {
        "topic_id": topic_id,
        "status": status,
        "local_block_end_time": local_block_end_time,
        "global_block_end_time": global_block_end_time,
    }


def _prepare_upstream_payload(action: str, reason: str, payload: dict | None) -> dict:
    prepared = dict(payload or {})
    if action != RecommendedAction.MUTE:
        return prepared
    mute_days = prepared.get("mute_days")
    if not isinstance(mute_days, int) or mute_days not in MUTE_DURATIONS_DAYS:
        raise ValueError("禁言期限未验证")
    prepared["mute_end_time"] = int((timezone.now() + timedelta(days=mute_days)).timestamp())
    prepared["reason"] = reason
    return prepared


def _operation_idempotency_key(task: ReviewTask, action: str, confirmation_key: str | None) -> str:
    """Deduplicate a confirmation submission, not every same-day moderation decision."""
    token = confirmation_key or str(uuid4())
    if not isinstance(token, str) or not token or len(token) > 64:
        raise ValueError("确认令牌无效")
    return f"{task.post.topic_id}:{action}:{token}"


def policy_recommendation(
    output: AssessmentOutput, policy: PolicyVersion | None
) -> tuple[str, int]:
    action, priority = output.recommended_action, 50
    if policy:
        for risk in output.risk_labels:
            for rule in policy.rules.filter(risk_label=risk.label, enabled=True):
                if any(item and item in " ".join(risk.evidence) for item in rule.exceptions):
                    continue
                if risk.confidence >= rule.min_confidence and rule.priority >= priority:
                    action, priority = rule.recommended_action, rule.priority
    if output.content_category in {ContentCategory.FEEDBACK, ContentCategory.SUGGESTION}:
        if action == RecommendedAction.SINK and output.feedback_value.upper() in {
            "HIGH",
            "VALUABLE",
        }:
            action = RecommendedAction.KEEP
    return action, priority


def _video_timestamp(start_ms: int, end_ms: int | None = None) -> str:
    start = f"{start_ms // 60000:02d}:{(start_ms // 1000) % 60:02d}.{start_ms % 1000:03d}"
    if end_ms is None:
        return start
    end = f"{end_ms // 60000:02d}:{(end_ms // 1000) % 60:02d}.{end_ms % 1000:03d}"
    return f"{start}–{end}"


def _bounded_text(value: str, limit: int) -> str:
    """Keep model context bounded without splitting a marker or exposing source URLs."""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _group_video_ocr(evidence_rows) -> list[list]:
    """Split dense frame OCR into bounded continuous visual segments."""
    groups: list[list] = []
    for evidence in evidence_rows:
        if evidence.kind != "FRAME_OCR":
            continue
        if (
            groups
            and evidence.start_ms - groups[-1][-1].start_ms <= VIDEO_OCR_SUMMARY_GAP_MS
            and evidence.start_ms - groups[-1][0].start_ms <= VIDEO_OCR_SUMMARY_MAX_SPAN_MS
        ):
            groups[-1].append(evidence)
        else:
            groups.append([evidence])
    return groups


def _representative_video_ocr(group) -> list:
    """Keep the strongest and materially different OCR observations in a segment."""
    ordered = sorted(
        group,
        key=lambda evidence: (
            evidence.confidence if evidence.confidence is not None else 0,
            len(evidence.text),
            evidence.start_ms,
        ),
        reverse=True,
    )
    selected = []
    for evidence in ordered:
        normalized = normalized_ocr_text(evidence.text)
        if not normalized:
            continue
        if any(
            SequenceMatcher(
                a=normalized,
                b=normalized_ocr_text(existing.text),
                autojunk=False,
            ).ratio()
            >= VIDEO_OCR_SUMMARY_SIMILARITY_THRESHOLD
            for existing in selected
        ):
            continue
        selected.append(evidence)
        if len(selected) == VIDEO_OCR_SUMMARY_MAX_TEXTS:
            break
    return sorted(selected, key=lambda evidence: evidence.start_ms)


def video_evidence_summaries(extraction: VideoExtraction) -> list[dict]:
    """Return compact visual evidence with original OCR retained for disclosure."""
    kind_order = {"QR_CODE": 0, "FRAME_OCR": 1, "AUDIO_TRANSCRIPT": 2}
    evidence_rows = sorted(
        extraction.evidence.all(),
        key=lambda evidence: (
            evidence.start_ms,
            kind_order[evidence.kind],
            evidence.sequence,
            evidence.pk,
        ),
    )
    summaries: list[dict] = []
    for group in _group_video_ocr(evidence_rows):
        summaries.append(
            {
                "kind": "FRAME_OCR_SUMMARY",
                "start_ms": group[0].start_ms,
                "end_ms": group[-1].start_ms if len(group) > 1 else None,
                "frame_count": len(group),
                "representatives": _representative_video_ocr(group),
                "raw_evidence": group,
            }
        )
    for evidence in evidence_rows:
        if evidence.kind == "FRAME_OCR":
            continue
        summaries.append(
            {
                "kind": evidence.kind,
                "start_ms": evidence.start_ms,
                "end_ms": evidence.end_ms,
                "evidence": evidence,
            }
        )
    return sorted(
        summaries,
        key=lambda summary: (
            summary["start_ms"],
            kind_order.get(summary["kind"].replace("_SUMMARY", ""), 1),
        ),
    )


def current_video_extractions(post: OriginalPost) -> list[VideoExtraction]:
    """Return the latest active-version attempt for each video media item."""
    latest_by_media: dict[int, VideoExtraction] = {}
    for extraction in (
        post.video_extractions.filter(extractor_version=VIDEO_EXTRACTOR_VERSION)
        .select_related("media")
        .prefetch_related("evidence")
        .order_by("media_id", "-attempt", "-pk")
    ):
        latest_by_media.setdefault(extraction.media_id, extraction)
    return list(latest_by_media.values())


def _video_evidence_input_lines(extraction: VideoExtraction, media_index: int) -> list[str]:
    """Compress repeated dense-frame OCR before it becomes model context."""
    lines: list[str] = []
    for summary in video_evidence_summaries(extraction):
        if summary["kind"] == "FRAME_OCR_SUMMARY":
            label = "视频帧文字" if summary["frame_count"] == 1 else "视频帧文字汇总"
            timestamp = _video_timestamp(summary["start_ms"], summary["end_ms"])
            representative_text = "；".join(
                f"{_video_timestamp(evidence.start_ms)}：{evidence.text}"
                for evidence in summary["representatives"]
            )
            frame_detail = (
                "" if summary["frame_count"] == 1 else f" · 连续 {summary['frame_count']} 帧"
            )
            lines.append(
                f"[{label} · 视频 {media_index} · {timestamp}{frame_detail}] "
                f"代表文字：{representative_text}"
            )
            continue
        evidence = summary["evidence"]
        label = {"QR_CODE": "二维码", "AUDIO_TRANSCRIPT": "视频语音"}[summary["kind"]]
        lines.append(
            f"[{label} · 视频 {media_index} · "
            f"{_video_timestamp(evidence.start_ms, evidence.end_ms)}] {evidence.text}"
        )
    return lines


def build_evaluation_input(post: OriginalPost) -> tuple[str, str, str]:
    """Return prompt input and a stable fingerprint without provider payloads."""
    content = _bounded_text(
        "\n\n".join(
            part
            for part in (post.title.strip(), post.content_text.strip(), post.ballot_text.strip())
            if part
        ),
        MAX_EVALUATION_CONTENT_CHARS,
    )
    image_ocr = _bounded_text(
        "\n".join(
            text.strip()
            for text in post.ocr_extractions.order_by("media_id", "created_at").values_list(
                "text", flat=True
            )
            if text.strip()
        ),
        MAX_IMAGE_OCR_CHARS,
    )
    video_lines: list[str] = []
    media_ids = list(post.media.order_by("pk").values_list("pk", flat=True))
    for extraction in current_video_extractions(post):
        media_index = media_ids.index(extraction.media_id) + 1
        video_lines.extend(_video_evidence_input_lines(extraction, media_index))
        if extraction.status != VideoExtractionStatus.SUCCEEDED:
            video_lines.append(
                f"[视频 {media_index} 提取状态] {extraction.status}："
                f"{extraction.error_summary or '需人工核验'}"
            )
    ocr_text = image_ocr
    if video_lines:
        video_text = _bounded_text("\n".join(video_lines), MAX_VIDEO_EVIDENCE_CHARS)
        ocr_text = _bounded_text(
            "\n".join(part for part in (image_ocr, video_text) if part),
            MAX_EVALUATION_OCR_CHARS,
        )
    urls = ",".join(post.media.order_by("pk").values_list("url", flat=True))
    # OCR is a pipeline output. The source snapshot is post copy plus media references.
    fingerprint = sha256(f"{content}\n--MEDIA--\n{urls}".encode()).hexdigest()
    return content, ocr_text, fingerprint


@transaction.atomic
def enqueue_evaluation_pipeline(
    post: OriginalPost, *, force: bool = False
) -> tuple[BackgroundJob, BackgroundJob]:
    """Queue OCR first and AI second; repeated requests reuse the same jobs."""
    candidate = CandidatePost.objects.filter(topic_id=post.topic_id).first()
    if candidate is None or candidate.origin_type != OriginType.ORIGINAL:
        raise ValueError("仅原创帖可进入 AI 审核流水线")
    # Deleted posts remain valuable historical moderation samples. They can be assessed,
    # but the existing operation boundary still rejects every upstream write for them.
    _, _, fingerprint = build_evaluation_input(post)
    suffix = f"{post.topic_id}:{fingerprint}:{EVALUATION_PIPELINE_VERSION}"
    if force:
        # An explicit operator re-evaluation intentionally creates a new assessment history entry.
        suffix = f"{suffix}:rerun:{uuid4().hex}"
    summary = {
        "topic_id": post.topic_id,
        "input_fingerprint": fingerprint,
        "pipeline_version": EVALUATION_PIPELINE_VERSION,
    }
    ocr_job = enqueue_job(
        task_type=OCR_EXTRACT_TASK, idempotency_key=f"ocr:{suffix}", parameter_summary=summary
    )
    video_job_ids: list[int] = []
    if runtime.video_enabled and runtime.video_preflight_verified:
        for media in post.media.filter(media_type=MediaType.VIDEO).order_by("pk")[
            : runtime.video_max_per_post
        ]:
            fingerprint_value = media_fingerprint(media.url)
            extraction = (
                VideoExtraction.objects.filter(
                    media=media,
                    media_fingerprint=fingerprint_value,
                    extractor_version=VIDEO_EXTRACTOR_VERSION,
                )
                .order_by("-attempt", "-pk")
                .first()
            )
            if extraction is None:
                extraction = VideoExtraction.objects.create(
                    post=post,
                    media=media,
                    media_fingerprint=fingerprint_value,
                    extractor_version=VIDEO_EXTRACTOR_VERSION,
                    attempt=1,
                )
            video_summary = {
                "topic_id": post.topic_id,
                "video_extraction_id": extraction.pk,
                "extractor_version": VIDEO_EXTRACTOR_VERSION,
                "attempt": extraction.attempt,
            }
            video_key = (
                f"video:{post.topic_id}:{fingerprint_value}:{VIDEO_EXTRACTOR_VERSION}:"
                f"attempt:{extraction.attempt}"
            )
            retryable_video_statuses = {
                VideoExtractionStatus.FAILED,
                VideoExtractionStatus.PARTIAL,
                VideoExtractionStatus.CANCELED,
                VideoExtractionStatus.SKIPPED_LIMIT,
            }
            active_video_job = (
                BackgroundJob.objects.filter(
                    task_type=VIDEO_EXTRACT_TASK,
                    status__in=(JobStatus.PENDING, JobStatus.RUNNING),
                    parameter_summary__video_extraction_id=extraction.pk,
                )
                .order_by("created_at")
                .first()
            )
            if force and extraction.status in retryable_video_statuses and not active_video_job:
                extraction = VideoExtraction.objects.create(
                    post=post,
                    media=media,
                    media_fingerprint=fingerprint_value,
                    extractor_version=VIDEO_EXTRACTOR_VERSION,
                    attempt=extraction.attempt + 1,
                )
                video_summary = {
                    "topic_id": post.topic_id,
                    "video_extraction_id": extraction.pk,
                    "extractor_version": VIDEO_EXTRACTOR_VERSION,
                    "attempt": extraction.attempt,
                }
                video_key = (
                    f"video:{post.topic_id}:{fingerprint_value}:{VIDEO_EXTRACTOR_VERSION}:"
                    f"attempt:{extraction.attempt}"
                )
            video_job = active_video_job or enqueue_job(
                task_type=VIDEO_EXTRACT_TASK,
                idempotency_key=video_key,
                parameter_summary=video_summary,
            )
            video_job_ids.append(video_job.pk)
    ai_job = enqueue_job(
        task_type=AI_EVALUATE_TASK,
        idempotency_key=f"ai:{suffix}",
        parameter_summary={**summary, "ocr_job_id": ocr_job.pk, "video_job_ids": video_job_ids},
    )
    return ocr_job, ai_job


@transaction.atomic
def persist_assessment(
    post: OriginalPost, output: AssessmentOutput, policy: PolicyVersion | None = None
):
    action, priority = policy_recommendation(output, policy)
    assessment = AiAssessment.objects.create(
        post=post,
        category=output.content_category,
        secondary_categories=output.secondary_categories,
        sentiment=output.sentiment,
        feedback_value=output.feedback_value,
        recommended_action=action,
        overall_confidence=output.overall_confidence,
        needs_human_review=True,
        model_version=runtime.ai_model or "unconfigured",
        policy_version=policy,
    )
    for risk in output.risk_labels:
        RiskEvidence.objects.create(
            assessment=assessment,
            label=risk.label,
            confidence=risk.confidence,
            evidence=risk.evidence,
        )
    high_reward = any(
        re.search(r"(?:上千|上万|无限|巨额|\d+\s*[万千])", evidence)
        for risk in output.risk_labels
        for evidence in risk.evidence
    )
    video_incomplete = any(
        extraction.status != VideoExtractionStatus.SUCCEEDED
        for extraction in current_video_extractions(post)
    )
    task, _ = ReviewTask.objects.update_or_create(
        post=post,
        defaults={
            "assessment": assessment,
            "suggested_action": action,
            "priority": priority,
            "status": ReviewStatus.PENDING,
            "needs_focus": (
                not output.needs_human_review
                or not post.content_text
                or post.ocr_extractions.filter(error_summary__gt="").exists()
                or any(not risk.evidence for risk in output.risk_labels)
                or video_incomplete
                or high_reward
            ),
        },
    )
    risky = action in {
        RecommendedAction.BLOCK,
        RecommendedAction.MUTE,
        RecommendedAction.BLOCK_AND_MUTE,
    }
    if (
        output.reply_suggestion
        and output.content_category in {ContentCategory.FEEDBACK, ContentCategory.SUGGESTION}
        and not risky
    ):
        ReplyDraft.objects.update_or_create(
            post=post,
            defaults={
                "assessment": assessment,
                "intent_summary": output.reply_suggestion.user_intent_summary,
                "draft": output.reply_suggestion.reply_draft,
                "missing_information": output.reply_suggestion.missing_information,
                "knowledge_sources": output.reply_suggestion.knowledge_sources,
                "risk_notice": output.reply_suggestion.risk_notice,
            },
        )
    return assessment, task


@transaction.atomic
def complete_review(task: ReviewTask, *, user, action: str, reason: str):
    task.status = ReviewStatus.REVIEWED
    task.final_action = action
    task.reviewed_by = user
    task.reviewed_at = timezone.now()
    task.reason = reason
    task.save()
    return task


@transaction.atomic
def save_reply_draft(post: OriginalPost, *, draft: str, adopted: bool) -> ReplyDraft:
    """Persist an existing local draft without creating any upstream side effect."""
    draft = draft.strip()
    if not draft:
        raise ValueError("回复草案不能为空")
    if len(draft) > MAX_REPLY_DRAFT_LENGTH:
        raise ValueError(f"回复草案不能超过 {MAX_REPLY_DRAFT_LENGTH} 个字符")
    reply = ReplyDraft.objects.select_for_update().get(post=post)
    edited = draft != reply.draft
    reply.draft = draft
    reply.adopted = adopted
    reply.adopted_with_edits = adopted and edited
    reply.save()
    return reply


def create_confirmed_operation(
    task: ReviewTask, *, user, action: str, reason: str, confirmation_key: str | None = None
) -> OperationAudit:
    if is_post_deleted(task.post.status):
        raise ValueError("帖子已从原平台管理队列删除，不能执行处置")
    if is_post_self_visible(task.post.status):
        raise ValueError("帖子当前仅自己可见，不能覆盖原平台可见性状态")
    if not is_operation_enabled(action):
        raise PermissionError("上游写操作能力未启用")
    return OperationAudit.objects.create(
        task=task,
        operation_id=str(uuid4()),
        idempotency_key=_operation_idempotency_key(task, action, confirmation_key),
        action=action,
        reason=reason,
        executor=user,
    )


def execute_confirmed_operation(
    task: ReviewTask,
    *,
    user,
    action: str,
    reason: str,
    source,
    upstream_payload: dict | None = None,
    confirmation_key: str | None = None,
) -> OperationAudit:
    """Human-only operation boundary; disabled capabilities never reach the source adapter."""
    payload = _prepare_upstream_payload(action, reason, upstream_payload)
    if is_post_deleted(task.post.status):
        raise ValueError("帖子已从原平台管理队列删除，不能执行处置")
    if is_post_self_visible(task.post.status):
        raise ValueError("帖子当前仅自己可见，不能覆盖原平台可见性状态")
    candidate = CandidatePost.objects.filter(topic_id=task.post.topic_id).first()
    if candidate is None or candidate.origin_type != OriginType.ORIGINAL:
        raise ValueError("操作前原创状态复核失败")

    # Commit the confirmation before contacting the upstream service. If the process
    # stops after an upstream write, a repeated confirmation returns this audit rather
    # than sending the same moderation request again.
    with transaction.atomic():
        audit, created = OperationAudit.objects.get_or_create(
            idempotency_key=_operation_idempotency_key(task, action, confirmation_key),
            defaults={
                "task": task,
                "operation_id": str(uuid4()),
                "action": action,
                "reason": reason,
                "executor": user,
                "result_code": "EXECUTING",
            },
        )
        if not created:
            return audit
        if not is_operation_enabled(action):
            audit.result_code = "CAPABILITY_DISABLED"
            audit.error_summary = "上游写操作未验证或能力未启用"
            audit.save(update_fields=["result_code", "error_summary"])
            return audit

    before_state: dict = {}
    after_state: dict = {}
    result_code = "ACTION_FAILED"
    error_summary = "上游操作失败或契约未验证"
    try:
        before_state = _operation_state(source.get_post_metrics(task.post.topic_id))
        if (
            action in {RecommendedAction.SINK, RecommendedAction.BLOCK}
            and before_state["status"] != POST_STATUS_NORMAL
        ):
            result_code = "SKIPPED_UPSTREAM_NOT_NORMAL"
            error_summary = "上游帖子已不是正常状态，跳过重复处置"
        elif action == RecommendedAction.UNMUTE and before_state["local_block_end_time"] <= int(
            timezone.now().timestamp()
        ):
            error_summary = "当前游戏未处于禁言状态"
        else:
            result = source.perform_confirmed_operation(
                topic_id=task.post.topic_id, action=action, payload=payload
            )
            after_state = _operation_state(source.get_post_metrics(task.post.topic_id))
            expected_status = EXPECTED_OPERATION_STATUS.get(action)
            if action == RecommendedAction.MUTE:
                expected_end_time = payload["mute_end_time"]
                actual_end_time = max(
                    after_state["local_block_end_time"],
                    after_state["global_block_end_time"],
                )
                if actual_end_time < expected_end_time:
                    error_summary = "禁言状态回读与预期不一致"
                else:
                    result_code = "OK"
                    error_summary = ""
            elif action == RecommendedAction.UNMUTE:
                if after_state["local_block_end_time"] > int(timezone.now().timestamp()):
                    error_summary = "当前游戏禁言状态回读与预期不一致"
                else:
                    result_code = "OK"
                    error_summary = ""
            elif expected_status is None or after_state["status"] != expected_status:
                error_summary = "上游状态回读与预期不一致"
            elif not isinstance(result, dict) or result.get("errcode") != 0:
                error_summary = "上游操作响应结构无效"
            else:
                result_code = "OK"
                error_summary = ""
    except Exception:
        pass

    with transaction.atomic():
        audit = OperationAudit.objects.select_for_update().get(pk=audit.pk)
        audit.before_state = before_state
        audit.after_state = after_state
        audit.result_code = result_code
        audit.error_summary = error_summary
        audit.executed_at = timezone.now()
        audit.save(
            update_fields=[
                "before_state",
                "after_state",
                "result_code",
                "error_summary",
                "executed_at",
            ]
        )
        if result_code == "OK" and action in EXPECTED_OPERATION_STATUS:
            OriginalPost.objects.filter(pk=task.post.pk).update(
                status=after_state["status"], updated_at=timezone.now()
            )
    return audit
