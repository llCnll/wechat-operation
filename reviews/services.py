"""AI assessment persistence, policy recommendation, and human-only review workflow."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from difflib import SequenceMatcher
from hashlib import sha256
from time import monotonic, sleep
from uuid import uuid4

from django.db import IntegrityError, transaction
from django.utils import timezone

from config.game_circle_ipc import GameCircleIpcError
from config.settings import runtime
from jobs.adapters import WriteResultUnknownError
from jobs.models import BackgroundJob, JobStatus
from jobs.services import enqueue_job
from jobs.task_types import AI_AUTOMATED_MODERATION_TASK, AI_EVALUATE_TASK, OCR_EXTRACT_TASK
from posts.models import (
    POST_STATUS_MODERATION_MASK,
    POST_STATUS_NORMAL,
    POST_STATUS_SELF_VISIBLE,
    AuthorMuteStatus,
    CandidatePost,
    MediaType,
    OriginalPost,
    OriginType,
    is_post_deleted,
    is_post_self_visible,
)
from posts.services import (
    set_permanent_mute_intent,
    sync_author_mute_status,
    sync_author_mute_status_from_user_detail,
)

from .adapters import REVIEW_PROMPT_VERSION, AssessmentOutput
from .models import (
    AiAssessment,
    AiOperationResolution,
    AiOperationReview,
    AiOperationReviewStatus,
    ContentCategory,
    FocusReason,
    MuteOperationTrigger,
    OcrExtraction,
    OperationAudit,
    OperationExecutionSource,
    PolicyRule,
    PolicyVersion,
    RecommendedAction,
    ReplyDraft,
    ReviewStatus,
    ReviewTask,
    RiskEvidence,
    VideoEvidence,
    VideoEvidenceKind,
    VideoExtraction,
    VideoExtractionStatus,
)
from .reply_faq import (
    STYLE_ORDER,
    ReplyFaqRule,
    available_reply_faq_rules,
    available_reply_faq_version,
    reply_faq_guidance_is_available,
)
from .video import media_fingerprint
from .video_tasks import VIDEO_EXTRACT_TASK, VIDEO_EXTRACTOR_VERSION, normalized_ocr_text

EXPECTED_OPERATION_STATUS = {
    RecommendedAction.RESTORE: 2,
    RecommendedAction.SINK: 8,
    RecommendedAction.BLOCK: 4,
}
POST_BOUND_OPERATION_ACTIONS = frozenset(
    (
        RecommendedAction.RESTORE,
        RecommendedAction.SINK,
        RecommendedAction.BLOCK,
        RecommendedAction.BLOCK_AND_MUTE,
    )
)
_TARGET_REACHED_OPERATION_RESULT_CODES = frozenset(("OK", "ALREADY_APPLIED"))
_READBACK_STATE_OPERATION_RESULT_CODES = _TARGET_REACHED_OPERATION_RESULT_CODES | frozenset(
    ("SKIPPED_UPSTREAM_NOT_NORMAL", "SKIPPED_UPSTREAM_NOT_ACTIONABLE")
)
MUTE_DURATIONS_DAYS = frozenset((1, 3, 7, 30))
AI_FINAL_POST_DECISIONS = frozenset(("KEEP_CURRENT", RecommendedAction.RESTORE, RecommendedAction.SINK, RecommendedAction.BLOCK))
AI_FINAL_AUTHOR_DECISIONS = frozenset(("UNCHANGED", RecommendedAction.MUTE))
BLOCK_REASON_OPTIONS = (
    (1, "其他"),
    (2, "色情低俗"),
    (4, "政治敏感"),
    (8, "广告欺诈"),
    (16, "欺诈钱财"),
)
EVALUATION_PIPELINE_VERSION = "v4"
MAX_REPLY_DRAFT_LENGTH = 4000
VIDEO_OCR_SUMMARY_GAP_MS = 1_500
VIDEO_OCR_SUMMARY_MAX_SPAN_MS = 5_000
VIDEO_OCR_SUMMARY_MAX_TEXTS = 3
VIDEO_OCR_SUMMARY_SIMILARITY_THRESHOLD = 0.85
MAX_EVALUATION_CONTENT_CHARS = 12_000
MAX_IMAGE_OCR_CHARS = 8_000
MAX_VIDEO_EVIDENCE_CHARS = 12_000
MAX_EVALUATION_OCR_CHARS = 20_000
AI_AUTOMATED_DAILY_LIMIT = 100
OCR_GROUNDED_EVIDENCE_MIN_LENGTH = 6
DEFAULT_REVIEW_PROMPT_VERSION = REVIEW_PROMPT_VERSION
# The platform may need a few seconds to project a completed mute write into
# getuserdetail.  These are absolute points after that write, not cumulative
# sleep durations: do not spend a request on an immediate read that is known
# to observe the old state in that window.
MUTE_VERIFY_READBACK_AT_SECONDS = (2, 5, 8, 10)
MUTE_VERIFY_MAX_WAIT_SECONDS = 20
MUTE_VERIFY_REQUEST_OVERHEAD_SECONDS = 0.5
PUBLIC_REPLY_SUPPRESSION_RISK_LABELS = frozenset(
    {
        "TRAFFIC_DIVERSION",
        "PRIVATE_SERVER",
        "CHEAT",
        "FRAUD_SCAM",
        "PORNOGRAPHY",
        "PERSONAL_ATTACK",
    }
)
GENERIC_REPLY_CATEGORIES = frozenset(
    {
        ContentCategory.FEEDBACK,
        ContentCategory.SUGGESTION,
        ContentCategory.QUESTION,
        ContentCategory.ACTIVITY,
        ContentCategory.GUIDE,
        ContentCategory.SOCIAL,
    }
)


def operation_result_reaches_target(result_code: str) -> bool:
    """Whether a confirmed operation has its requested upstream state without a replay."""
    return result_code in _TARGET_REACHED_OPERATION_RESULT_CODES


def operation_result_has_readback_state(result_code: str) -> bool:
    """Whether the audit's state is a verified source readback safe to mirror locally."""
    return result_code in _READBACK_STATE_OPERATION_RESULT_CODES


def _preflight_operation_result(
    action: str,
    before_state: dict[str, object],
    *,
    allow_moderation_overwrite: bool,
) -> tuple[str, str] | None:
    """Classify a SINK/BLOCK preflight without blocking valid overwrite operations."""
    if action not in {RecommendedAction.SINK, RecommendedAction.BLOCK}:
        return None
    status = before_state.get("status")
    if not isinstance(status, int):
        return "SKIPPED_UPSTREAM_NOT_ACTIONABLE", "上游帖子状态无效，无法执行覆盖处置"
    if is_post_deleted(status):
        return "SKIPPED_UPSTREAM_NOT_ACTIONABLE", "上游帖子已从管理队列删除，无法执行覆盖处置"
    if status & POST_STATUS_SELF_VISIBLE:
        return "SKIPPED_UPSTREAM_NOT_ACTIONABLE", "上游帖子仅自己可见，无法执行覆盖处置"
    expected_status = EXPECTED_OPERATION_STATUS[action]
    if status == expected_status:
        return "ALREADY_APPLIED", "上游帖子已处于目标状态，跳过重复处置"
    if status & POST_STATUS_MODERATION_MASK:
        if allow_moderation_overwrite:
            return None
        return "SKIPPED_UPSTREAM_NOT_ACTIONABLE", "上游帖子状态已变化，自动处置不覆盖人工状态"
    if status != POST_STATUS_NORMAL:
        return "SKIPPED_UPSTREAM_NOT_ACTIONABLE", "上游帖子状态不支持覆盖处置"
    return None


@dataclass(frozen=True)
class ReplyDraftPlan:
    kind: str
    rule: ReplyFaqRule | None = None
    variants: tuple[dict[str, str], ...] = ()


class OperationExecutionConflict(ValueError):
    """Another human or AI operation has already reserved this review task."""


def is_operation_enabled(action: str) -> bool:
    """Both the global and the per-operation switch must be explicitly enabled."""
    return runtime.enable_upstream_writes and {
        RecommendedAction.SINK: runtime.enable_sink_writes,
        RecommendedAction.BLOCK: runtime.enable_block_writes,
        RecommendedAction.MUTE: runtime.enable_mute_writes,
        RecommendedAction.UNMUTE: runtime.enable_unmute_writes,
        RecommendedAction.RESTORE: runtime.enable_restore_writes,
    }.get(action, False)


def ai_automation_enabled(action: str) -> tuple[bool, str]:
    """Return the complete, explicit authorization decision for an AI write."""
    if action not in {RecommendedAction.SINK, RecommendedAction.BLOCK}:
        return False, "AI 自动处置仅支持沉底或屏蔽"
    if not runtime.enable_ai_automated_moderation:
        return False, "AI 自动处置总开关未启用"
    ai_action_enabled = {
        RecommendedAction.SINK: runtime.enable_ai_sink_writes,
        RecommendedAction.BLOCK: runtime.enable_ai_block_writes,
    }[action]
    if not ai_action_enabled:
        return False, "AI 对应动作开关未启用"
    if not is_operation_enabled(action):
        return False, "对应人工上游写入授权未启用"
    return True, ""


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


def _operation_author_openid(payload: dict) -> str:
    try:
        openid = payload["data"]["topic"]["creater"]["openid"]
    except (KeyError, TypeError) as exc:
        raise ValueError("操作前无法读取作者用户 ID") from exc
    if not isinstance(openid, str) or not openid:
        raise ValueError("操作前作者用户 ID无效")
    return openid


def _author_mute_state(payload: dict, *, source_appid: str) -> dict:
    """Project only mute fields from a response already bound to one author."""
    try:
        user = payload["data"]["user"]
    except (KeyError, TypeError) as exc:
        raise ValueError("作者状态回读结构无效") from exc
    if not isinstance(user, dict):
        raise ValueError("作者状态回读结构无效")
    block_info = user.get("block_info")
    local_block_end_time = 0
    if isinstance(block_info, dict) and block_info.get("appid") == source_appid:
        try:
            local_block_end_time = int(block_info.get("block_end_time", 0))
        except (TypeError, ValueError):
            local_block_end_time = 0
    try:
        global_block_end_time = int(user.get("global_block_end_time", 0))
    except (TypeError, ValueError):
        global_block_end_time = 0
    return {
        "local_block_end_time": local_block_end_time,
        "global_block_end_time": global_block_end_time,
    }


def _author_source_appid(post: OriginalPost, source) -> str:
    """Resolve the frozen game context without asking the source for post details."""
    appid = post.source_appid or getattr(source, "appid", "")
    if not isinstance(appid, str) or not appid:
        raise ValueError("作者状态读取缺少当前游戏范围")
    return appid


def _read_mute_state_with_verification(
    source,
    post: OriginalPost,
    *,
    action: str,
    expected_end_time: int | None,
) -> dict:
    """Read at most four times after a mute write; never repeats the write itself."""
    latest_state: dict = {}
    started_at = monotonic()
    for readback_at in MUTE_VERIFY_READBACK_AT_SECONDS:
        elapsed = monotonic() - started_at
        delay = max(0, readback_at - elapsed)
        remaining = MUTE_VERIFY_MAX_WAIT_SECONDS - elapsed
        if remaining <= 0 or delay > remaining:
            break
        if delay:
            sleep(delay)
        remaining = MUTE_VERIFY_MAX_WAIT_SECONDS - (monotonic() - started_at)
        if remaining <= MUTE_VERIFY_REQUEST_OVERHEAD_SECONDS:
            break
        requested_at = timezone.now()
        expected_openid = post.author_openid
        if not expected_openid:
            raise ValueError("禁言状态回读缺少已验证作者身份")
        source_appid = _author_source_appid(post, source)
        bounded_read = getattr(source, "get_author_detail_with_timeout", None)
        if callable(bounded_read):
            payload = bounded_read(
                expected_openid,
                timeout_seconds=remaining - MUTE_VERIFY_REQUEST_OVERHEAD_SECONDS,
            )
        else:
            payload = source.get_author_detail(expected_openid)
        sync_author_mute_status_from_user_detail(
            payload,
            expected_openid=expected_openid,
            source_appid=source_appid,
            source_requested_at=requested_at,
            post=post,
        )
        latest_state = _author_mute_state(payload, source_appid=source_appid)
        if action == RecommendedAction.MUTE:
            if latest_state["local_block_end_time"] >= (expected_end_time or 0):
                break
        elif latest_state["local_block_end_time"] <= int(timezone.now().timestamp()):
            break
    return latest_state


def _author_operation_idempotency_key(
    status: AuthorMuteStatus, action: str, confirmation_key: str | None
) -> str:
    """Deduplicate one explicit confirmation against one author target."""
    token = confirmation_key or str(uuid4())
    if not isinstance(token, str) or not token or len(token) > 64:
        raise ValueError("确认令牌无效")
    return f"author:{status.pk}:{action}:{token}"


def _read_author_mute_state_with_verification(
    source,
    status: AuthorMuteStatus,
    *,
    action: str,
    expected_end_time: int | None,
) -> dict:
    """Bounded, identity-checked author readback without a representative post."""
    latest_state: dict = {}
    started_at = monotonic()
    for readback_at in MUTE_VERIFY_READBACK_AT_SECONDS:
        elapsed = monotonic() - started_at
        delay = max(0, readback_at - elapsed)
        remaining = MUTE_VERIFY_MAX_WAIT_SECONDS - elapsed
        if remaining <= 0 or delay > remaining:
            break
        if delay:
            sleep(delay)
        remaining = MUTE_VERIFY_MAX_WAIT_SECONDS - (monotonic() - started_at)
        if remaining <= MUTE_VERIFY_REQUEST_OVERHEAD_SECONDS:
            break
        requested_at = timezone.now()
        bounded_read = getattr(source, "get_author_detail_with_timeout", None)
        if callable(bounded_read):
            payload = bounded_read(
                status.openid,
                timeout_seconds=remaining - MUTE_VERIFY_REQUEST_OVERHEAD_SECONDS,
            )
        else:
            payload = source.get_author_detail(status.openid)
        sync_author_mute_status_from_user_detail(
            payload,
            expected_openid=status.openid,
            source_appid=status.source_appid,
            source_requested_at=requested_at,
            create_if_inactive=True,
        )
        latest_state = _author_mute_state(payload, source_appid=status.source_appid)
        if action == RecommendedAction.MUTE:
            if latest_state["local_block_end_time"] >= (expected_end_time or 0):
                break
        elif latest_state["local_block_end_time"] <= int(timezone.now().timestamp()):
            break
    return latest_state


def execute_author_operation(
    status: AuthorMuteStatus,
    *,
    user,
    action: str,
    reason: str,
    source,
    upstream_payload: dict | None = None,
    confirmation_key: str | None = None,
    resolution=None,
    mute_trigger: str = MuteOperationTrigger.HUMAN_INITIAL,
    verified_before_payload: dict | None = None,
    execution_source: str = OperationExecutionSource.HUMAN,
) -> OperationAudit:
    """Perform a confirmed MUTE/UNMUTE against one verified author target.

    This deliberately has no `ReviewTask` or `OriginalPost` dependency.  A caller
    may use a post solely to find an already persisted OpenID, then resolve this
    author target before entering this operation boundary.
    """
    if action not in {RecommendedAction.MUTE, RecommendedAction.UNMUTE}:
        raise ValueError("作者目标仅支持禁言或解除禁言")
    payload = _prepare_upstream_payload(action, reason, upstream_payload)
    if not is_operation_enabled(action):
        raise PermissionError("上游写操作能力未启用")

    # The fixed author-detail read is both the identity proof and the only source
    # used to build a normal author's explicit operation target.
    requested_at = timezone.now()
    before_payload = verified_before_payload or source.get_author_detail(status.openid)
    verified_status = sync_author_mute_status_from_user_detail(
        before_payload,
        expected_openid=status.openid,
        source_appid=status.source_appid,
        source_requested_at=requested_at,
        create_if_inactive=True,
    )
    if verified_status is None:
        raise ValueError("作者详情缺少可核验身份")
    status = verified_status
    before_state = _author_mute_state(before_payload, source_appid=status.source_appid)
    idempotency_key = _author_operation_idempotency_key(status, action, confirmation_key)

    with transaction.atomic():
        try:
            audit, created = OperationAudit.objects.get_or_create(
                idempotency_key=idempotency_key,
                defaults={
                    "author_status": status,
                    "resolution": resolution,
                    "operation_id": str(uuid4()),
                    "action": action,
                    "reason": reason,
                    "executor": user,
                    "execution_source": execution_source,
                    "mute_trigger": mute_trigger,
                    "result_code": "EXECUTING",
                },
            )
        except IntegrityError as exc:
            active = OperationAudit.objects.filter(
                author_status=status, result_code="EXECUTING"
            ).first()
            if active is not None:
                raise OperationExecutionConflict("该作者已有禁言操作正在执行，请等待结果") from exc
            raise
        if not created:
            return audit

    result_code = "ACTION_FAILED"
    error_summary = "上游操作失败或契约未验证"
    after_state: dict = {}
    try:
        now_timestamp = int(timezone.now().timestamp())
        if action == RecommendedAction.MUTE and before_state["local_block_end_time"] > now_timestamp:
            result_code, error_summary, after_state = "ALREADY_APPLIED", "当前游戏已处于禁言状态", before_state
        elif action == RecommendedAction.UNMUTE and before_state["local_block_end_time"] <= now_timestamp:
            result_code, error_summary, after_state = "ALREADY_APPLIED", "当前游戏未处于禁言状态", before_state
        else:
            write_error: Exception | None = None
            result = None
            try:
                operation_kwargs = {
                    "topic_id": None,
                    "action": action,
                    "payload": {**payload, "author_openid": status.openid},
                }
                if getattr(source, "requires_operation_id", False):
                    operation_kwargs["operation_id"] = audit.operation_id
                result = source.perform_confirmed_operation(**operation_kwargs)
            except Exception as exc:
                write_error = exc
            after_state = _read_author_mute_state_with_verification(
                source,
                status,
                action=action,
                expected_end_time=payload.get("mute_end_time"),
            )
            if write_error is not None:
                error_summary = f"上游调用失败，回读状态已同步：{type(write_error).__name__}"[:500]
            elif action == RecommendedAction.MUTE:
                if after_state["local_block_end_time"] >= payload["mute_end_time"]:
                    result_code, error_summary = "OK", ""
                else:
                    error_summary = "禁言状态回读与预期不一致"
            elif after_state["local_block_end_time"] <= int(timezone.now().timestamp()):
                result_code, error_summary = "OK", ""
            else:
                error_summary = "当前游戏禁言状态回读与预期不一致"
            if result is not None and (not isinstance(result, dict) or result.get("errcode") != 0):
                result_code, error_summary = "ACTION_FAILED", "上游操作响应结构无效"
    except GameCircleIpcError as exc:
        if exc.category == "SESSION_RECOVERING":
            result_code, error_summary = "SESSION_RECOVERING", "游戏圈会话正在恢复，请稍后重新确认"
        else:
            error_summary = f"上游调用或状态验证失败：{type(exc).__name__}"[:500]
    except WriteResultUnknownError:
        result_code, error_summary = "PENDING_READBACK", "上游写入结果待回读确认；系统不会自动重放"
    except Exception as exc:
        error_summary = f"上游调用或状态验证失败：{type(exc).__name__}"[:500]

    with transaction.atomic():
        audit = OperationAudit.objects.select_for_update().get(pk=audit.pk)
        audit.before_state = before_state
        audit.after_state = after_state
        audit.result_code = result_code
        audit.error_summary = error_summary
        audit.executed_at = timezone.now()
        audit.save(update_fields=("before_state", "after_state", "result_code", "error_summary", "executed_at"))
    return audit


def execute_author_operation_for_identity(
    *,
    openid: str,
    source_appid: str,
    user,
    action: str,
    reason: str,
    source,
    upstream_payload: dict | None = None,
    confirmation_key: str | None = None,
    resolution=None,
    mute_trigger: str = MuteOperationTrigger.HUMAN_INITIAL,
    execution_source: str = OperationExecutionSource.HUMAN,
) -> OperationAudit:
    """Resolve an explicit author target after confirmation, then execute it.

    The only caller-supplied identity is server-side context (a saved post or an
    existing mute row).  The fixed detail endpoint validates it before a normal
    author is allowed to gain a durable operation target.
    """
    if not isinstance(openid, str) or not openid:
        raise ValueError("缺少已保存的作者身份，无法执行禁言操作")
    if not isinstance(source_appid, str) or not source_appid:
        raise ValueError("作者操作缺少当前游戏范围")
    if not is_operation_enabled(action):
        raise PermissionError("上游写操作能力未启用")
    payload = source.get_author_detail(openid)
    status = sync_author_mute_status_from_user_detail(
        payload,
        expected_openid=openid,
        source_appid=source_appid,
        source_requested_at=timezone.now(),
        create_if_inactive=True,
    )
    if status is None:
        raise ValueError("作者详情缺少可核验身份")
    return execute_author_operation(
        status,
        user=user,
        action=action,
        reason=reason,
        source=source,
        upstream_payload=upstream_payload,
        confirmation_key=confirmation_key,
        resolution=resolution,
        mute_trigger=mute_trigger,
        verified_before_payload=payload,
        execution_source=execution_source,
    )


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


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    priority: int
    rule: PolicyRule | None
    risk_index: int | None


def policy_recommendation(output: AssessmentOutput, policy: PolicyVersion | None) -> PolicyDecision:
    """Preserve the human-review recommendation, while recording its deciding input.

    The automated worker consumes the persisted recommendation.  It must never
    select a different action based on the subset of rules that allow automation.
    """
    action, priority = output.recommended_action, 50
    deciding_rule = None
    deciding_risk_index = None
    if policy:
        for risk_index, risk in enumerate(output.risk_labels):
            for rule in policy.rules.filter(risk_label=risk.label, enabled=True):
                if any(item and item in " ".join(risk.evidence) for item in rule.exceptions):
                    continue
                if risk.confidence >= rule.min_confidence:
                    if rule.priority >= priority:
                        action, priority = rule.recommended_action, rule.priority
                        deciding_rule = rule
                        deciding_risk_index = risk_index

    if output.content_category in {ContentCategory.FEEDBACK, ContentCategory.SUGGESTION}:
        if action == RecommendedAction.SINK and output.feedback_value.upper() in {
            "HIGH",
            "VALUABLE",
        }:
            action = RecommendedAction.KEEP
            deciding_rule = None
            deciding_risk_index = None
    return PolicyDecision(action, priority, deciding_rule, deciding_risk_index)


def _video_timestamp(start_ms: int, end_ms: int | None = None) -> str:
    start = f"{start_ms // 60000:02d}:{(start_ms // 1000) % 60:02d}.{start_ms % 1000:03d}"
    if end_ms is None:
        return start
    end = f"{end_ms // 60000:02d}:{(end_ms // 1000) % 60:02d}.{end_ms % 1000:03d}"
    return f"{start}–{end}"


def _video_sampling_timestamp(timestamp_ms: int) -> str:
    if timestamp_ms % 1000:
        return _video_timestamp(timestamp_ms)
    return f"{timestamp_ms // 60000}:{(timestamp_ms // 1000) % 60:02d}"


def video_sampling_intervals(extraction: VideoExtraction) -> list[tuple[int, int]]:
    """Return the persisted sampling plan only when its stored shape is trustworthy."""
    if extraction.duration_ms is None or extraction.duration_ms <= 0:
        return []
    raw_intervals = extraction.sampled_intervals_ms
    if not isinstance(raw_intervals, list):
        return []
    intervals: list[tuple[int, int]] = []
    previous_end = 0
    for interval in raw_intervals:
        if (
            not isinstance(interval, list)
            or len(interval) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in interval)
        ):
            return []
        start_ms, end_ms = interval
        if start_ms < previous_end or start_ms < 0 or end_ms <= start_ms:
            return []
        if end_ms > extraction.duration_ms:
            return []
        intervals.append((start_ms, end_ms))
        previous_end = end_ms
    return intervals


def video_sampling_notice(extraction: VideoExtraction) -> str:
    """Describe distributed sampling without exposing media locations or tool details."""
    intervals = video_sampling_intervals(extraction)
    if len(intervals) < 2 or extraction.duration_ms is None:
        return ""
    if extraction.duration_limit_seconds is None:
        return ""
    coverage = "、".join(
        f"{_video_sampling_timestamp(start_ms)}–{_video_sampling_timestamp(end_ms)}"
        for start_ms, end_ms in intervals
    )
    return (
        f"视频时长 {extraction.duration_ms / 1000:g} 秒，提取时采样预算 "
        f"{extraction.duration_limit_seconds:g} 秒；已覆盖 {coverage}，未覆盖其他时间，未执行 ASR。"
    )


def video_extraction_error_detail(extraction: VideoExtraction) -> str:
    """Keep additional safe failures visible after the sampling notice explains its code."""
    if not video_sampling_notice(extraction):
        return extraction.error_summary
    errors = [
        error
        for error in extraction.error_summary.split(";")
        if error and error != "video_duration_distributed_sampling"
    ]
    return ";".join(errors)


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


def _normalized_text_fingerprint(value: str) -> str:
    return sha256(normalized_ocr_text(value).encode()).hexdigest()


def _ocr_source_refs(post: OriginalPost, evidence: list[str]) -> list[dict]:
    """Record only server-verified image-OCR sources for a risk evidence row."""
    successful_ocr = list(
        post.ocr_extractions.filter(error_summary="").exclude(text="").order_by("pk")
    )
    refs: list[dict] = []
    for evidence_index, snippet in enumerate(evidence):
        if not isinstance(snippet, str):
            continue
        normalized_snippet = normalized_ocr_text(snippet)
        if len(normalized_snippet) < OCR_GROUNDED_EVIDENCE_MIN_LENGTH:
            continue
        for extraction in successful_ocr:
            normalized_ocr = normalized_ocr_text(extraction.text)
            if normalized_snippet not in normalized_ocr:
                continue
            refs.append(
                {
                    "ocr_extraction_id": extraction.pk,
                    "text_fingerprint": _normalized_text_fingerprint(extraction.text),
                    "evidence_index": evidence_index,
                    "evidence_fingerprint": _normalized_text_fingerprint(snippet),
                }
            )
    return refs


def _evidence_matches_media_text(snippet: str, text: str, *, kind: str) -> bool:
    normalized_snippet = normalized_ocr_text(snippet)
    if kind == VideoEvidenceKind.QR_CODE:
        return "二维码" in normalized_snippet
    return (
        len(normalized_snippet) >= OCR_GROUNDED_EVIDENCE_MIN_LENGTH
        and normalized_snippet in normalized_ocr_text(text)
    )


def _media_source_refs(post: OriginalPost, evidence: list[str]) -> list[dict]:
    """Persist only verified local image/video evidence references for a risk row."""
    refs: list[dict] = []
    successful_ocr = list(
        post.ocr_extractions.filter(error_summary="").exclude(text="").order_by("pk")
    )
    current_video = current_video_extractions(post)
    for evidence_index, snippet in enumerate(evidence):
        if not isinstance(snippet, str):
            continue
        for extraction in successful_ocr:
            if not _evidence_matches_media_text(snippet, extraction.text, kind="IMAGE_OCR"):
                continue
            refs.append(
                {
                    "source_type": "IMAGE_OCR",
                    "ocr_extraction_id": extraction.pk,
                    "media_id": extraction.media_id,
                    "text_fingerprint": _normalized_text_fingerprint(extraction.text),
                    "evidence_index": evidence_index,
                    "evidence_fingerprint": _normalized_text_fingerprint(snippet),
                }
            )
        for video_extraction in current_video:
            if video_extraction.status not in {
                VideoExtractionStatus.SUCCEEDED,
                VideoExtractionStatus.PARTIAL,
            }:
                continue
            for video_evidence in video_extraction.evidence.all():
                if not _evidence_matches_media_text(
                    snippet, video_evidence.text, kind=video_evidence.kind
                ):
                    continue
                refs.append(
                    {
                        "source_type": "VIDEO_EVIDENCE",
                        "video_extraction_id": video_extraction.pk,
                        "video_evidence_id": video_evidence.pk,
                        "media_id": video_extraction.media_id,
                        "start_ms": video_evidence.start_ms,
                        "end_ms": video_evidence.end_ms,
                        "text_fingerprint": _normalized_text_fingerprint(video_evidence.text),
                        "evidence_index": evidence_index,
                        "evidence_fingerprint": _normalized_text_fingerprint(snippet),
                    }
                )
    return refs


def _has_usable_image_ocr(post: OriginalPost, media_id: int) -> bool:
    return post.ocr_extractions.filter(
        media_id=media_id, error_summary=""
    ).exclude(text="").exists()


def _has_usable_video_evidence(extraction: VideoExtraction | None) -> bool:
    if extraction is None or extraction.status not in {
        VideoExtractionStatus.SUCCEEDED,
        VideoExtractionStatus.PARTIAL,
    }:
        return False
    return extraction.evidence.filter(
        kind__in=(
            VideoEvidenceKind.FRAME_OCR,
            VideoEvidenceKind.QR_CODE,
            VideoEvidenceKind.AUDIO_TRANSCRIPT,
        )
    ).exclude(text="").exists()


def _has_textual_input(post: OriginalPost) -> bool:
    return any(value.strip() for value in (post.title, post.content_text, post.ballot_text))


def _focus_reasons(post: OriginalPost) -> list[str]:
    """Only missing media evidence for a textless post requires human focus."""
    if _has_textual_input(post):
        return []
    media = list(post.media.order_by("pk"))
    if not media:
        return [FocusReason.EVIDENCE_MISSING]
    current_video_by_media = {
        extraction.media_id: extraction for extraction in current_video_extractions(post)
    }
    for item in media:
        if item.media_type == MediaType.IMAGE and not _has_usable_image_ocr(post, item.pk):
            return [FocusReason.EVIDENCE_MISSING]
        if item.media_type == MediaType.VIDEO and not _has_usable_video_evidence(
            current_video_by_media.get(item.pk)
        ):
            return [FocusReason.EVIDENCE_MISSING]
    return []


def _video_evidence_input_lines(extraction: VideoExtraction, media_index: int) -> list[str]:
    """Compress repeated dense-frame OCR before it becomes model context."""
    lines: list[str] = []
    if notice := video_sampling_notice(extraction):
        lines.append(f"[视频 {media_index} 采样范围] {notice}")
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
                    duration_limit_seconds=runtime.video_max_duration_seconds,
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
                    duration_limit_seconds=runtime.video_max_duration_seconds,
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


def _reply_draft_plan(
    *,
    suggestion,
    candidates: tuple[ReplyFaqRule, ...],
    risky: bool,
    allow_generic: bool,
) -> ReplyDraftPlan:
    """Resolve exactly one matched, generic, or rejected local-draft outcome."""
    if suggestion is None or risky:
        return ReplyDraftPlan("rejected")
    variants = tuple(
        {"style": item.style, "draft": item.draft.strip()}
        for item in suggestion.style_variants
        if item.draft.strip()
    )
    styles = [item["style"] for item in variants]
    normalized = {re.sub(r"\W+", "", item["draft"]).casefold() for item in variants}
    if set(styles) != set(STYLE_ORDER) or len(variants) != 3 or len(normalized) != 3:
        return ReplyDraftPlan("rejected")
    rule_id = suggestion.faq_rule_id.strip()
    if not rule_id:
        if allow_generic:
            return ReplyDraftPlan("generic", variants=variants)
        return ReplyDraftPlan("rejected")
    rule = next((candidate for candidate in candidates if candidate.rule_id == rule_id), None)
    if rule:
        return ReplyDraftPlan("matched", rule=rule, variants=variants)
    return ReplyDraftPlan("rejected")


@transaction.atomic
def persist_assessment(
    post: OriginalPost,
    output: AssessmentOutput,
    policy: PolicyVersion | None = None,
    *,
    prompt_version: str = DEFAULT_REVIEW_PROMPT_VERSION,
    faq_rules=None,
    faq_version: str = "",
):
    decision = policy_recommendation(output, policy)
    assessment = AiAssessment.objects.create(
        post=post,
        category=output.content_category,
        secondary_categories=output.secondary_categories,
        sentiment=output.sentiment,
        feedback_value=output.feedback_value,
        recommended_action=decision.action,
        overall_confidence=output.overall_confidence,
        needs_human_review=True,
        model_version=runtime.ai_model or "unconfigured",
        prompt_version=prompt_version,
        policy_version=policy,
        deciding_policy_rule=decision.rule,
    )
    evidence_rows = []
    for risk in output.risk_labels:
        evidence_rows.append(
            RiskEvidence.objects.create(
                assessment=assessment,
                label=risk.label,
                confidence=risk.confidence,
                evidence=risk.evidence,
                ocr_source_refs=_ocr_source_refs(post, risk.evidence),
                media_source_refs=_media_source_refs(post, risk.evidence),
            )
        )
    deciding_evidence = (
        evidence_rows[decision.risk_index] if decision.risk_index is not None else None
    )
    if deciding_evidence is not None:
        assessment.deciding_risk_evidence = deciding_evidence
        assessment.save(update_fields=["deciding_risk_evidence"])
    focus_reasons = _focus_reasons(post)
    needs_focus = bool(focus_reasons)
    task = ReviewTask.objects.select_for_update().filter(post=post).first()
    if task is None:
        task = ReviewTask.objects.create(
            post=post,
            assessment=assessment,
            suggested_action=decision.action,
            priority=decision.priority,
            status=ReviewStatus.PENDING,
            needs_focus=needs_focus,
            focus_reasons=focus_reasons,
        )
    elif task.status not in {
        ReviewStatus.AUTO_EXECUTED_PENDING_REVIEW,
        ReviewStatus.AUTO_REVIEW_ESCALATED,
    }:
        task.assessment = assessment
        task.suggested_action = decision.action
        task.priority = decision.priority
        task.status = ReviewStatus.PENDING
        task.needs_focus = needs_focus
        task.focus_reasons = focus_reasons
        task.final_action = ""
        task.reviewed_by = None
        task.reviewed_at = None
        task.reason = ""
        task.save()
    risky = decision.action in {
        RecommendedAction.BLOCK,
        RecommendedAction.MUTE,
        RecommendedAction.BLOCK_AND_MUTE,
    } or any(
        risk.label in PUBLIC_REPLY_SUPPRESSION_RISK_LABELS for risk in output.risk_labels
    )
    candidates = tuple(faq_rules) if faq_rules is not None else available_reply_faq_rules(
        appid=post.source_appid,
        content="\n".join((post.title, post.content_text, post.ballot_text)),
    )
    guidance_available = reply_faq_guidance_is_available(appid=post.source_appid)
    plan = _reply_draft_plan(
        suggestion=output.reply_suggestion,
        candidates=candidates,
        risky=risky,
        allow_generic=(
            guidance_available
            and not output.risk_labels
            and output.content_category in GENERIC_REPLY_CATEGORIES
        ),
    )
    if plan.kind != "rejected":
        variant_by_style = {item["style"]: item for item in plan.variants}
        ordered_variants = [variant_by_style[style] for style in STYLE_ORDER]
        ReplyDraft.objects.update_or_create(
            post=post,
            defaults={
                "assessment": assessment,
                "intent_summary": output.reply_suggestion.user_intent_summary,
                "draft": ordered_variants[0]["draft"],
                "missing_information": list(plan.rule.missing_information)
                if plan.rule
                else [],
                "knowledge_sources": [plan.rule.source] if plan.rule else [],
                "risk_notice": output.reply_suggestion.risk_notice,
                "faq_rule_id": plan.rule.rule_id if plan.rule else "",
                "faq_version": (
                    faq_version or available_reply_faq_version(appid=post.source_appid)
                )
                if plan.rule
                else "",
                "style_variants": ordered_variants,
                "selected_style": STYLE_ORDER[0],
                "suppressed_reason": "",
                "adopted": False,
                "adopted_with_edits": False,
            },
        )
    else:
        suppression_reason = (
            "当前评估含公开回复不安全风险"
            if risky
            else "当前评估未命中有效受控 FAQ"
        )
        # A re-evaluation without a safe, validated FAQ draft must not leave an
        # older draft looking like a current recommendation.  Keep it for local
        # history, but remove edit/adopt controls through the existing suppression state.
        ReplyDraft.objects.filter(post=post).update(suppressed_reason=suppression_reason)
    eligible, _, _ = ai_automation_eligibility(task, assessment=assessment)
    if eligible:
        enqueue_job(
            task_type=AI_AUTOMATED_MODERATION_TASK,
            idempotency_key=f"ai-auto:{task.pk}:{assessment.pk}:{decision.action}",
            parameter_summary={
                "task_id": task.pk,
                "assessment_id": assessment.pk,
                "action": decision.action,
            },
            max_retries=0,
        )
    return assessment, task


def _automation_evidence(assessment: AiAssessment, rule: PolicyRule) -> list[dict]:
    """Return the one evidence row that produced the existing AI recommendation."""
    risk = assessment.deciding_risk_evidence
    if risk is None or risk.assessment_id != assessment.pk or risk.label != rule.risk_label:
        return []
    snippets = risk.evidence if isinstance(risk.evidence, list) else []
    normalized = []
    for snippet in snippets:
        if isinstance(snippet, str):
            value = " ".join(snippet.split())
            if value and value not in normalized:
                normalized.append(value[:180])
    if not normalized:
        return []
    return [
        {
            "label": risk.label,
            "confidence": risk.confidence,
            "evidence": normalized[:3],
            "ocr_source_refs": risk.ocr_source_refs,
            "media_source_refs": risk.media_source_refs,
        }
    ]


def _has_verified_deciding_media_source(assessment: AiAssessment) -> bool:
    """Revalidate the persisted local media source before an empty-input write."""
    risk = assessment.deciding_risk_evidence
    if risk is None or risk.assessment_id != assessment.pk:
        return False
    snippets = risk.evidence if isinstance(risk.evidence, list) else []
    refs = risk.media_source_refs if isinstance(risk.media_source_refs, list) else []
    current_video_by_id = {
        extraction.pk: extraction for extraction in current_video_extractions(assessment.post)
    }
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        evidence_index = ref.get("evidence_index")
        text_fingerprint = ref.get("text_fingerprint")
        evidence_fingerprint = ref.get("evidence_fingerprint")
        if not isinstance(evidence_index, int):
            continue
        if not isinstance(text_fingerprint, str) or not isinstance(evidence_fingerprint, str):
            continue
        if evidence_index < 0 or evidence_index >= len(snippets):
            continue
        snippet = snippets[evidence_index]
        if not isinstance(snippet, str):
            continue
        normalized_snippet = normalized_ocr_text(snippet)
        if (
            not normalized_snippet or _normalized_text_fingerprint(snippet) != evidence_fingerprint
        ):
            continue
        if ref.get("source_type") == "IMAGE_OCR":
            extraction_id = ref.get("ocr_extraction_id")
            media_id = ref.get("media_id")
            if not isinstance(extraction_id, int) or not isinstance(media_id, int):
                continue
            extraction = OcrExtraction.objects.filter(
                pk=extraction_id, post=assessment.post, media_id=media_id
            ).first()
            if (
                extraction is not None
                and not extraction.error_summary
                and extraction.text
                and _normalized_text_fingerprint(extraction.text) == text_fingerprint
                and _evidence_matches_media_text(snippet, extraction.text, kind="IMAGE_OCR")
            ):
                return True
            continue
        if ref.get("source_type") == "VIDEO_EVIDENCE":
            extraction_id = ref.get("video_extraction_id")
            video_evidence_id = ref.get("video_evidence_id")
            media_id = ref.get("media_id")
            start_ms = ref.get("start_ms")
            end_ms = ref.get("end_ms")
            extraction = current_video_by_id.get(extraction_id)
            if (
                not isinstance(video_evidence_id, int)
                or not isinstance(media_id, int)
                or not isinstance(start_ms, int)
                or extraction is None
                or extraction.media_id != media_id
                or extraction.status
                not in {VideoExtractionStatus.SUCCEEDED, VideoExtractionStatus.PARTIAL}
            ):
                continue
            video_evidence = VideoEvidence.objects.filter(
                pk=video_evidence_id, extraction=extraction, start_ms=start_ms
            ).first()
            if (
                video_evidence is None
                or video_evidence.end_ms != end_ms
                or not video_evidence.text
                or _normalized_text_fingerprint(video_evidence.text) != text_fingerprint
            ):
                continue
            if _evidence_matches_media_text(snippet, video_evidence.text, kind=video_evidence.kind):
                return True
    return False


def ai_automation_eligibility(
    task: ReviewTask, *, assessment: AiAssessment | None = None
) -> tuple[bool, str, dict]:
    """Recheck every automatic-write condition without creating any side effect."""
    assessment = assessment or task.assessment
    snapshot: dict = {"task_id": task.pk, "assessment_id": getattr(assessment, "pk", None)}
    if assessment is None or task.assessment_id != assessment.pk:
        return False, "审核任务不是当前 AI 评估", snapshot
    if task.status != ReviewStatus.PENDING:
        return False, "审核任务当前不允许 AI 自动处置", snapshot
    candidate = CandidatePost.objects.filter(topic_id=task.post.topic_id).first()
    if candidate is None or candidate.origin_type != OriginType.ORIGINAL:
        return False, "操作前原创状态复核失败", snapshot
    if (
        task.post.status != POST_STATUS_NORMAL
        or is_post_deleted(task.post.status)
        or is_post_self_visible(task.post.status)
    ):
        return False, "帖子不是可处置的正常状态", snapshot
    enabled, reason = ai_automation_enabled(task.suggested_action)
    if not enabled:
        return False, reason, snapshot
    # Authorization is operator-controlled mutable state.  Do not reuse a cached
    # related object here: a worker or caller may otherwise apply an obsolete
    # threshold or auto-execution switch.
    rule = PolicyRule.objects.filter(pk=assessment.deciding_policy_rule_id).first()
    if (
        rule is None
        or not rule.enabled
        or not rule.auto_execution_enabled
        or rule.policy_version_id != assessment.policy_version_id
        or rule.recommended_action != task.suggested_action
    ):
        return False, "决定规则未授权 AI 自动执行", snapshot
    minimum = rule.min_confidence
    evidence = _automation_evidence(assessment, rule)
    if not evidence:
        return False, "决定风险依据不完整", snapshot
    if assessment.overall_confidence < minimum or evidence[0]["confidence"] < minimum:
        return False, "置信度未达到自动处置下限", snapshot
    focus_reasons = task.focus_reasons
    if not isinstance(focus_reasons, list):
        return False, "重点核验原因记录无效", snapshot
    if task.needs_focus != bool(focus_reasons):
        return False, "重点核验状态与原因记录不一致", snapshot
    if focus_reasons:
        if set(focus_reasons) == {FocusReason.EVIDENCE_MISSING}:
            return False, "输入证据不足，需重点人工核验", snapshot
        return False, "历史重点人工核验任务不得自动处置", snapshot
    if not _has_textual_input(task.post) and not _has_verified_deciding_media_source(assessment):
        return False, "空文字缺少可验证的本地媒体风险依据", snapshot
    since = timezone.now() - timedelta(hours=24)
    if (
        OperationAudit.objects.filter(
            execution_source=OperationExecutionSource.AI_AUTOMATED,
            result_code="OK",
            created_at__gte=since,
        ).count()
        >= AI_AUTOMATED_DAILY_LIMIT
    ):
        return False, "AI 自动处置 24 小时配额已用尽", snapshot
    snapshot.update(
        {
            "action": task.suggested_action,
            "overall_confidence": assessment.overall_confidence,
            "risk_evidence": evidence,
            "focus_reasons": focus_reasons,
            "model_version": assessment.model_version,
            "prompt_version": assessment.prompt_version,
            "policy_version": assessment.policy_version.version
            if assessment.policy_version
            else "",
            "policy_rule_id": rule.pk,
            "rule_min_confidence": rule.min_confidence,
            "rule_auto_execution_enabled": rule.auto_execution_enabled,
            "authorization": {
                "enable_ai_automated_moderation": runtime.enable_ai_automated_moderation,
                "enable_ai_sink_writes": runtime.enable_ai_sink_writes,
                "enable_ai_block_writes": runtime.enable_ai_block_writes,
                "enable_upstream_writes": runtime.enable_upstream_writes,
                "enable_sink_writes": runtime.enable_sink_writes,
                "enable_block_writes": runtime.enable_block_writes,
            },
            "daily_success_count": OperationAudit.objects.filter(
                execution_source=OperationExecutionSource.AI_AUTOMATED,
                result_code="OK",
                created_at__gte=since,
            ).count(),
        }
    )
    return True, "", snapshot


def ai_automation_reason(snapshot: dict, action: str) -> str:
    snippets = []
    for item in snapshot.get("risk_evidence", []):
        for evidence in item.get("evidence", []):
            if evidence not in snippets:
                snippets.append(evidence)
    summary = "；".join(snippets[:3]) or "已验证的风险依据"
    action_label = "沉底" if action == RecommendedAction.SINK else "屏蔽"
    return f"AI 风险依据：{summary}；执行{action_label}。"[:500]


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
def save_reply_draft(
    post: OriginalPost, *, draft: str, adopted: bool, selected_style: str = ""
) -> ReplyDraft:
    """Persist an existing local draft without creating any upstream side effect."""
    draft = draft.strip()
    if not draft:
        raise ValueError("回复草案不能为空")
    if len(draft) > MAX_REPLY_DRAFT_LENGTH:
        raise ValueError(f"回复草案不能超过 {MAX_REPLY_DRAFT_LENGTH} 个字符")
    reply = ReplyDraft.objects.select_for_update().get(post=post)
    if reply.suppressed_reason:
        raise ValueError("该回复草案已不适用于当前风险评估。")
    variant_styles = {
        item.get("style") for item in reply.style_variants if isinstance(item, dict)
    }
    if selected_style and selected_style not in variant_styles:
        raise ValueError("回复风格无效。")
    edited = draft != reply.draft
    reply.draft = draft
    if selected_style:
        reply.selected_style = selected_style
    reply.adopted = adopted
    reply.adopted_with_edits = adopted and edited
    reply.save(
        update_fields=("draft", "selected_style", "adopted", "adopted_with_edits", "updated_at")
    )
    return reply


def create_confirmed_operation(
    task: ReviewTask, *, user, action: str, reason: str, confirmation_key: str | None = None
) -> OperationAudit:
    if is_post_deleted(task.post.status) and action in POST_BOUND_OPERATION_ACTIONS:
        raise ValueError("帖子已从原平台管理队列删除，不能执行帖子处置")
    if is_post_self_visible(task.post.status) and action in POST_BOUND_OPERATION_ACTIONS:
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


def _reserve_operation_audit(
    task: ReviewTask,
    *,
    user,
    action: str,
    reason: str,
    confirmation_key: str | None,
    execution_source: str,
    automation_snapshot: dict | None,
    mute_trigger: str = "",
    resolution: AiOperationResolution | None = None,
) -> tuple[OperationAudit, bool]:
    """Reserve the only in-flight upstream write for a task before contacting CDP."""
    idempotency_key = _operation_idempotency_key(task, action, confirmation_key)
    try:
        audit, created = OperationAudit.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults={
                "task": task,
                "resolution": resolution,
                "operation_id": str(uuid4()),
                "action": action,
                "reason": reason,
                "executor": user,
                "execution_source": execution_source,
                "mute_trigger": mute_trigger,
                "automation_snapshot": automation_snapshot or {},
                "policy_version": task.assessment.policy_version if task.assessment_id else None,
                "result_code": "EXECUTING",
            },
        )
    except IntegrityError as exc:
        active = OperationAudit.objects.filter(task=task, result_code="EXECUTING").first()
        if active is not None:
            raise OperationExecutionConflict("该帖子已有处置正在执行，请等待结果") from exc
        raise
    return audit, created


def execute_confirmed_operation(
    task: ReviewTask,
    *,
    user,
    action: str,
    reason: str,
    source,
    upstream_payload: dict | None = None,
    confirmation_key: str | None = None,
    execution_source: str = OperationExecutionSource.HUMAN,
    automation_snapshot: dict | None = None,
    reserved_audit: OperationAudit | None = None,
    mute_trigger: str = "",
    resolution: AiOperationResolution | None = None,
) -> OperationAudit:
    """Execute one verified upstream operation with an explicit actor source."""
    payload = _prepare_upstream_payload(action, reason, upstream_payload)
    if is_post_deleted(task.post.status) and action in POST_BOUND_OPERATION_ACTIONS:
        raise ValueError("帖子已从原平台管理队列删除，不能执行帖子处置")
    if is_post_self_visible(task.post.status) and action in POST_BOUND_OPERATION_ACTIONS:
        raise ValueError("帖子当前仅自己可见，不能覆盖原平台可见性状态")
    if action in POST_BOUND_OPERATION_ACTIONS:
        candidate = CandidatePost.objects.filter(topic_id=task.post.topic_id).first()
        if candidate is None or candidate.origin_type != OriginType.ORIGINAL:
            raise ValueError("操作前原创状态复核失败")

    # Commit the confirmation before contacting the upstream service. If the process
    # stops after an upstream write, a repeated confirmation returns this audit rather
    # than sending the same moderation request again.
    with transaction.atomic():
        if reserved_audit is None:
            audit, created = _reserve_operation_audit(
                task,
                user=user,
                action=action,
                reason=reason,
                confirmation_key=confirmation_key,
                execution_source=execution_source,
                automation_snapshot=automation_snapshot,
                mute_trigger=mute_trigger,
                resolution=resolution,
            )
        else:
            audit = OperationAudit.objects.select_for_update().get(pk=reserved_audit.pk)
            created = audit.result_code == "EXECUTING"
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
        stored_author_openid = task.post.author_openid
        current_author_openid = ""
        if action in {RecommendedAction.MUTE, RecommendedAction.UNMUTE} and stored_author_openid:
            source_appid = _author_source_appid(task.post, source)
            before_payload = source.get_author_detail(stored_author_openid)
            sync_author_mute_status_from_user_detail(
                before_payload,
                expected_openid=stored_author_openid,
                source_appid=source_appid,
                source_requested_at=timezone.now(),
                post=task.post,
            )
            before_state = _author_mute_state(before_payload, source_appid=source_appid)
            payload["author_openid"] = stored_author_openid
        else:
            before_requested_at = timezone.now()
            before_payload = source.get_post_metrics(task.post.topic_id)
            before_state = _operation_state(before_payload)
            sync_author_mute_status(task.post, before_payload, source_requested_at=before_requested_at)
            if action in {RecommendedAction.MUTE, RecommendedAction.UNMUTE}:
                current_author_openid = _operation_author_openid(before_payload)
                payload["author_openid"] = current_author_openid
        expected_status = EXPECTED_OPERATION_STATUS.get(action)
        preflight_result = _preflight_operation_result(
            action,
            before_state,
            allow_moderation_overwrite=execution_source == OperationExecutionSource.HUMAN,
        )
        if preflight_result is not None:
            result_code, error_summary = preflight_result
            after_state = before_state
        elif action == RecommendedAction.UNMUTE and before_state["local_block_end_time"] <= int(
            timezone.now().timestamp()
        ):
            error_summary = "当前游戏未处于禁言状态"
        elif action == RecommendedAction.MUTE and before_state["local_block_end_time"] > int(
            timezone.now().timestamp()
        ):
            error_summary = "当前游戏已处于禁言状态"
        else:
            write_error: Exception | None = None
            result = None
            try:
                operation_kwargs = {
                    "topic_id": task.post.topic_id,
                    "action": action,
                    "payload": payload,
                }
                if getattr(source, "requires_operation_id", False):
                    operation_kwargs["operation_id"] = audit.operation_id
                result = source.perform_confirmed_operation(**operation_kwargs)
            except Exception as exc:
                write_error = exc
                if action not in {RecommendedAction.MUTE, RecommendedAction.UNMUTE}:
                    raise
            if action in {RecommendedAction.MUTE, RecommendedAction.UNMUTE}:
                after_state = _read_mute_state_with_verification(
                    source,
                    task.post,
                    action=action,
                    expected_end_time=payload.get("mute_end_time"),
                )
                if write_error is not None:
                    error_summary = (
                        f"上游调用失败，回读状态已同步：{type(write_error).__name__}"[:500]
                    )
                elif action == RecommendedAction.MUTE:
                    expected_end_time = payload["mute_end_time"]
                    if after_state["local_block_end_time"] < expected_end_time:
                        error_summary = "禁言状态回读与预期不一致"
                    else:
                        result_code = "OK"
                        error_summary = ""
                elif after_state["local_block_end_time"] > int(timezone.now().timestamp()):
                    error_summary = "当前游戏禁言状态回读与预期不一致"
                else:
                    result_code = "OK"
                    error_summary = ""
                # MUTE and UNMUTE are fully handled by their bounded verification branch.
                result = None
            else:
                after_requested_at = timezone.now()
                after_payload = source.get_post_metrics(task.post.topic_id)
                sync_author_mute_status(
                    task.post,
                    after_payload,
                    source_requested_at=after_requested_at,
                )
                after_state = _operation_state(after_payload)
            if action in {RecommendedAction.MUTE, RecommendedAction.UNMUTE}:
                pass
            elif expected_status is None or after_state["status"] != expected_status:
                error_summary = "上游状态回读与预期不一致"
            elif not isinstance(result, dict) or result.get("errcode") != 0:
                error_summary = "上游操作响应结构无效"
            else:
                result_code = "OK"
                error_summary = ""
    except GameCircleIpcError as exc:
        if exc.category == "SESSION_RECOVERING":
            result_code = "SESSION_RECOVERING"
            error_summary = "游戏圈会话正在恢复，请稍后重新确认"
        elif exc.category == "SOURCE_CONTRACT":
            error_summary = f"上游调用或状态验证失败：{exc}"[:500]
        else:
            error_summary = f"上游调用或状态验证失败：{type(exc).__name__}"[:500]
    except WriteResultUnknownError:
        result_code = "PENDING_READBACK"
        error_summary = "上游写入结果待回读确认；系统不会自动重放"
    except Exception as exc:
        error_summary = f"上游调用或状态验证失败：{type(exc).__name__}"[:500]

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
        if operation_result_has_readback_state(result_code) and action in EXPECTED_OPERATION_STATUS:
            OriginalPost.objects.filter(pk=task.post.pk).update(
                status=after_state["status"], updated_at=timezone.now()
            )
    return audit


def record_ai_automation_rejection(
    task: ReviewTask, assessment: AiAssessment, reason: str, snapshot: dict
) -> OperationAudit:
    """Keep a bounded, non-writing trace when an already enqueued AI event becomes ineligible."""
    key = _operation_idempotency_key(
        task, task.suggested_action, f"ai:{task.pk}:{assessment.pk}:{task.suggested_action}"
    )
    audit, _ = OperationAudit.objects.get_or_create(
        idempotency_key=key,
        defaults={
            "task": task,
            "operation_id": str(uuid4()),
            "action": task.suggested_action,
            "reason": "AI 自动处置资格复核未通过。",
            "execution_source": OperationExecutionSource.AI_AUTOMATED,
            "automation_snapshot": {**snapshot, "eligibility_reason": reason},
            "policy_version": assessment.policy_version,
            "result_code": "AUTO_ELIGIBILITY_REJECTED",
            "error_summary": reason[:500],
            "executed_at": timezone.now(),
        },
    )
    return audit


@transaction.atomic
def reserve_ai_automated_moderation(
    task_id: int, assessment_id: int, action: str
) -> OperationAudit | None:
    """Atomically claim a qualified AI write before opening its controlled CDP session."""
    task = (
        ReviewTask.objects.select_for_update()
        .select_related("post", "assessment", "assessment__deciding_policy_rule")
        .get(pk=task_id)
    )
    assessment = AiAssessment.objects.get(pk=assessment_id)
    if action != task.suggested_action:
        record_ai_automation_rejection(task, assessment, "自动处置动作与当前建议不一致", {})
        return None
    eligible, rejection, snapshot = ai_automation_eligibility(task, assessment=assessment)
    if not eligible:
        record_ai_automation_rejection(task, assessment, rejection, snapshot)
        return None
    reason = ai_automation_reason(snapshot, action)
    snapshot["reason"] = reason
    try:
        audit, created = _reserve_operation_audit(
            task,
            user=None,
            action=action,
            reason=reason,
            confirmation_key=f"ai:{task.pk}:{assessment.pk}:{action}",
            execution_source=OperationExecutionSource.AI_AUTOMATED,
            automation_snapshot=snapshot,
        )
    except OperationExecutionConflict:
        record_ai_automation_rejection(task, assessment, "已有操作正在执行", snapshot)
        return None
    return audit if created and audit.result_code == "EXECUTING" else None


def complete_ai_automation_review(audit: OperationAudit) -> AiOperationReview:
    """Atomically expose a successful AI write to the human post-action review queue."""
    with transaction.atomic():
        audit = OperationAudit.objects.select_for_update().select_related("task").get(pk=audit.pk)
        task = ReviewTask.objects.select_for_update().get(pk=audit.task_id)
        review, _ = AiOperationReview.objects.get_or_create(audit=audit)
        if audit.result_code == "OK":
            task.status = ReviewStatus.AUTO_EXECUTED_PENDING_REVIEW
            task.final_action = ""
            task.reviewed_by = None
            task.reviewed_at = None
            task.reason = ""
            task.save()
        return review


def ai_final_operation_plan(post_decision: str, author_decision: str) -> tuple[str, ...]:
    """Return the minimal ordered writes implied by one final AI-review decision."""
    if post_decision not in AI_FINAL_POST_DECISIONS:
        raise ValueError("帖子最终决定无效")
    if author_decision not in AI_FINAL_AUTHOR_DECISIONS:
        raise ValueError("作者最终决定无效")
    steps: list[str] = []
    if post_decision != "KEEP_CURRENT":
        steps.append(post_decision)
    if author_decision == RecommendedAction.MUTE:
        steps.append(RecommendedAction.MUTE)
    return tuple(steps)


def _ai_review_completion_status(review: AiOperationReview, post_decision: str, author_decision: str) -> str:
    """Keep the historical confirmed/corrected projection without losing dimensions."""
    original = review.audit.action
    if post_decision == "KEEP_CURRENT" and author_decision == "UNCHANGED":
        return AiOperationReviewStatus.CONFIRMED
    if post_decision == original and author_decision == "UNCHANGED":
        return AiOperationReviewStatus.CONFIRMED
    return AiOperationReviewStatus.CORRECTED


def _fail_ai_operation_resolution(resolution_id: int) -> None:
    """Terminalize a committed resolution when no complete plan can be produced."""
    with transaction.atomic():
        resolution = AiOperationResolution.objects.select_for_update().get(pk=resolution_id)
        if resolution.result_code == "EXECUTING":
            resolution.result_code = "FAILED"
            resolution.completed_at = timezone.now()
            resolution.save(update_fields=("result_code", "completed_at"))


def submit_ai_operation_resolution(
    review: AiOperationReview,
    *,
    user,
    post_decision: str,
    author_decision: str,
    reason: str,
    confirmation_key: str,
    source,
    post_payload: dict | None = None,
    author_payload: dict | None = None,
) -> tuple[AiOperationResolution, list[OperationAudit]]:
    """Execute one AI-review final decision as independent post/author audits.

    The resolution is committed before writes.  A repeated browser submission
    therefore returns the same durable batch instead of replaying platform writes.
    """
    steps = ai_final_operation_plan(post_decision, author_decision)
    reason = reason.strip()
    if not isinstance(confirmation_key, str) or len(confirmation_key) > 64:
        raise ValueError("确认令牌无效")
    if steps and not confirmation_key:
        raise ValueError("确认令牌无效")
    if not confirmation_key:
        confirmation_key = f"local:{review.pk}:{uuid4()}"
    with transaction.atomic():
        review = (
            AiOperationReview.objects.select_for_update()
            .select_related("audit__task__post", "audit__task__assessment")
            .get(pk=review.pk)
        )
        if review.status not in {AiOperationReviewStatus.PENDING, AiOperationReviewStatus.ESCALATED}:
            raise ValueError("该 AI 操作不再等待复审")
        resolution, created = AiOperationResolution.objects.get_or_create(
            confirmation_key=confirmation_key,
            defaults={
                "review": review,
                "post_decision": post_decision,
                "author_decision": author_decision,
                "reason": reason,
                "executor": user,
                "result_code": "EXECUTING",
            },
        )
        if not created:
            if resolution.review_id != review.pk:
                raise ValueError("确认令牌不属于当前复审记录")
            return resolution, list(resolution.operation_audits.order_by("created_at"))
        if not steps:
            reviewed_at = timezone.now()
            review.status = _ai_review_completion_status(review, post_decision, author_decision)
            review.reviewer = user
            review.reviewed_at = reviewed_at
            review.reason = reason
            review.final_post_decision = post_decision
            review.final_author_decision = author_decision
            review.save()
            task = review.audit.task
            task.status = ReviewStatus.REVIEWED
            task.final_action = review.audit.action
            task.final_post_decision = post_decision
            task.final_author_decision = author_decision
            task.reviewed_by = user
            task.reviewed_at = reviewed_at
            task.reason = reason
            task.save()
            resolution.result_code = "SUCCEEDED"
            resolution.completed_at = reviewed_at
            resolution.save(update_fields=("result_code", "completed_at"))
            return resolution, []

    unavailable = [step for step in steps if not is_operation_enabled(step)]
    if unavailable:
        labels = "、".join(dict(RecommendedAction.choices)[step] for step in unavailable)
        _fail_ai_operation_resolution(resolution.pk)
        raise PermissionError(f"请先在系统配置中启用上游写操作：{labels}。")

    task = review.audit.task
    post = task.post
    audits: list[OperationAudit] = []
    try:
        for index, step in enumerate(steps, start=1):
            key = f"{confirmation_key}:step:{index}:{step.lower()}"
            if step == RecommendedAction.MUTE:
                if not post.author_openid or not post.source_appid:
                    raise ValueError("帖子未保存可核验的作者身份，不能执行作者禁言")
                audit = execute_author_operation_for_identity(
                    openid=post.author_openid,
                    source_appid=post.source_appid,
                    user=user,
                    action=step,
                    reason=reason,
                    source=source,
                    upstream_payload=author_payload,
                    confirmation_key=key,
                    resolution=resolution,
                )
            else:
                payload = post_payload if step == RecommendedAction.BLOCK else {}
                audit = execute_confirmed_operation(
                    task,
                    user=user,
                    action=step,
                    reason=reason,
                    source=source,
                    upstream_payload=payload,
                    confirmation_key=key,
                    resolution=resolution,
                )
            audits.append(audit)
            if not operation_result_reaches_target(audit.result_code):
                _fail_ai_operation_resolution(resolution.pk)
                return resolution, audits
            if step == RecommendedAction.MUTE and author_payload is not None:
                # The platform only receives a finite mute.  The optional
                # "permanent" checkbox is an explicit local renewal intention and
                # must be persisted only after the verified author write succeeds.
                if audit.author_status_id is None:
                    raise ValueError("禁言回读未形成作者状态，不能保存自动续期意图")
                set_permanent_mute_intent(
                    audit.author_status,
                    enabled=bool(author_payload.get("permanent_mute")),
                    user=user,
                )
    except Exception:
        _fail_ai_operation_resolution(resolution.pk)
        raise

    with transaction.atomic():
        resolution = AiOperationResolution.objects.select_for_update().get(pk=resolution.pk)
        review = AiOperationReview.objects.select_for_update().select_related("audit__task").get(pk=review.pk)
        reviewed_at = timezone.now()
        review.status = _ai_review_completion_status(review, post_decision, author_decision)
        review.reviewer = user
        review.reviewed_at = reviewed_at
        review.reason = reason
        review.final_post_decision = post_decision
        review.final_author_decision = author_decision
        review.save()
        task = review.audit.task
        task.status = ReviewStatus.REVIEWED
        task.final_action = post_decision if post_decision != "KEEP_CURRENT" else review.audit.action
        task.final_post_decision = post_decision
        task.final_author_decision = author_decision
        task.reviewed_by = user
        task.reviewed_at = reviewed_at
        task.reason = reason
        task.save()
        resolution.result_code = "SUCCEEDED"
        resolution.completed_at = reviewed_at
        resolution.save(update_fields=("result_code", "completed_at"))
    return resolution, audits


@transaction.atomic
def confirm_ai_operation_review(
    review: AiOperationReview, *, user, reason: str
) -> AiOperationReview:
    reason = reason.strip()
    review = (
        AiOperationReview.objects.select_for_update()
        .select_related("audit__task")
        .get(pk=review.pk)
    )
    if review.status != AiOperationReviewStatus.PENDING:
        raise ValueError("该 AI 操作不再等待复审")
    task = review.audit.task
    review.status = AiOperationReviewStatus.CONFIRMED
    review.reviewer = user
    review.reviewed_at = timezone.now()
    review.reason = reason
    review.save()
    task.status = ReviewStatus.REVIEWED
    task.final_action = review.audit.action
    task.reviewed_by = user
    task.reviewed_at = review.reviewed_at
    task.reason = reason
    task.save()
    return review


@transaction.atomic
def escalate_ai_operation_review(
    review: AiOperationReview, *, user, reason: str
) -> AiOperationReview:
    reason = reason.strip()
    review = (
        AiOperationReview.objects.select_for_update()
        .select_related("audit__task")
        .get(pk=review.pk)
    )
    if review.status != AiOperationReviewStatus.PENDING:
        raise ValueError("该 AI 操作不再等待复审")
    task = review.audit.task
    review.status = AiOperationReviewStatus.ESCALATED
    review.reviewer = user
    review.reviewed_at = timezone.now()
    review.reason = reason
    review.save()
    task.status = ReviewStatus.AUTO_REVIEW_ESCALATED
    task.needs_focus = True
    task.save(update_fields=["status", "needs_focus", "updated_at"])
    return review


@transaction.atomic
def mark_ai_review_restored(task: ReviewTask, *, audit: OperationAudit) -> None:
    """Only a successful manual RESTORE can close an escalated AI operation review."""
    review = (
        AiOperationReview.objects.select_for_update()
        .filter(audit__task=task, status=AiOperationReviewStatus.ESCALATED)
        .order_by("-created_at")
        .first()
    )
    if review is None:
        return
    if (
        review.status != AiOperationReviewStatus.ESCALATED
        or audit.action != RecommendedAction.RESTORE
    ):
        return
    review.status = AiOperationReviewStatus.RESTORED
    review.save(update_fields=["status", "updated_at"])


def execute_ai_automated_moderation(
    task_id: int,
    assessment_id: int,
    action: str,
    source,
    *,
    reserved_audit: OperationAudit | None = None,
) -> OperationAudit:
    """Run one outbox event; callers own the controlled CDP client lifecycle."""
    if reserved_audit is None:
        reserved_audit = reserve_ai_automated_moderation(task_id, assessment_id, action)
        if reserved_audit is None:
            audit = (
                OperationAudit.objects.filter(
                    task_id=task_id,
                    execution_source=OperationExecutionSource.AI_AUTOMATED,
                )
                .order_by("-created_at")
                .first()
            )
            if audit is None:
                raise ValueError("自动处置未创建可追溯审计记录")
            if audit.result_code == "OK":
                complete_ai_automation_review(audit)
            return audit
    task = ReviewTask.objects.select_related("post").get(pk=task_id)
    payload = {} if action == RecommendedAction.SINK else {"block_reason_code": 1}
    audit = execute_confirmed_operation(
        task,
        user=None,
        action=action,
        reason=reserved_audit.reason,
        source=source,
        upstream_payload=payload,
        confirmation_key=f"ai:{task.pk}:{assessment_id}:{action}",
        execution_source=OperationExecutionSource.AI_AUTOMATED,
        reserved_audit=reserved_audit,
    )
    if audit.result_code == "OK":
        complete_ai_automation_review(audit)
    return audit
