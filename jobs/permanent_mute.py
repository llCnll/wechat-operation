"""Credential-free renewal for locally managed permanent-mute intent."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.db.models import Max, OuterRef, Q, Subquery
from django.utils import timezone

from config.game_circle_ipc import GameCircleIpcError, GameCirclePostClient
from jobs.adapters import SourceContractError
from jobs.models import BackgroundJob, JobStatus
from jobs.services import EnqueueResult, JobDeferred, JobTerminalError, enqueue_active_coalesced_job
from jobs.task_types import (
    AUTHOR_MUTE_STATUS_REFRESH_TASK,
    BLOCKED_USER_LIST_SYNC_TASK,
    PERMANENT_MUTE_RENEWAL_TASK,
    POST_AUTHOR_MUTE_STATUS_SYNC_TASK,
)
from posts.game_catalog import (
    ExecutionScope,
    checkpoint_with_execution_scope,
    current_execution_scope,
    resolve_execution_scope,
)
from posts.models import AuthorMuteStatus, OriginalPost
from posts.services import (
    mute_management_status_queryset,
    set_permanent_mute_intent,
    sync_author_mute_status_from_blocked_user_list,
    sync_author_mute_status_from_user_detail,
)
from posts.source_client import BLOCKED_USER_LIST_PAGE_SIZE_REJECTED
from reviews.models import (
    MuteOperationTrigger,
    OperationAudit,
    OperationExecutionSource,
    RecommendedAction,
)
from reviews.services import execute_author_operation

PERMANENT_MUTE_DAYS = 30
RENEWAL_THRESHOLD = timedelta(days=5)
SHANGHAI = ZoneInfo("Asia/Shanghai")
MAINTENANCE_READ_TASKS = frozenset(
    (
        AUTHOR_MUTE_STATUS_REFRESH_TASK,
        POST_AUTHOR_MUTE_STATUS_SYNC_TASK,
        BLOCKED_USER_LIST_SYNC_TASK,
    )
)
BLOCKED_USER_LIST_PAGE_SIZE_CANDIDATES = (500, 200, 100, 50, 20, 10)
BLOCKED_USER_LIST_FIRST_PAGE = 0


def _renewal_key(scope: ExecutionScope, slot: str) -> str:
    return f"permanent-mute:renewal:{scope.target_appid}:v{scope.scope_version}:{slot}"


def enqueue_permanent_mute_renewal(
    *, source: str, now: datetime | None = None
) -> EnqueueResult:
    """Create/reuse the current game's 12-hour renewal work without source I/O."""
    scope = current_execution_scope()
    local_now = (now or timezone.now()).astimezone(SHANGHAI)
    if source == "scheduler":
        slot = local_now.strftime("%Y%m%d") + ("00" if local_now.hour < 12 else "12")
    else:
        slot = f"manual-{uuid4().hex}"
    key = _renewal_key(scope, slot)
    if source == "manual":
        return enqueue_active_coalesced_job(
            task_type=PERMANENT_MUTE_RENEWAL_TASK,
            idempotency_key=key,
            active_coalescing_key=(
                f"permanent-mute:renewal:manual:{scope.target_appid}:v{scope.scope_version}"
            ),
            parameter_summary={"source": source},
            checkpoint={"execution_scope": scope.as_checkpoint(), "last_status_id": 0},
            max_retries=3,
        )
    job, created = BackgroundJob.objects.get_or_create(
        idempotency_key=key,
        defaults={
            "task_type": PERMANENT_MUTE_RENEWAL_TASK,
            "parameter_summary": {"source": source},
            "checkpoint": {"execution_scope": scope.as_checkpoint(), "last_status_id": 0},
            "max_retries": 3,
        },
    )
    return EnqueueResult(job=job, created=created)


def enqueue_scheduled_permanent_mute_renewal(now: datetime) -> EnqueueResult | None:
    """Admit exactly one renewal slot at 00:00 and 12:00 Shanghai time."""
    if now.tzinfo is None:
        raise ValueError("调度时间必须包含时区")
    local = now.astimezone(SHANGHAI)
    if local.hour not in {0, 12} or local.minute != 0:
        return None
    return enqueue_permanent_mute_renewal(source="scheduler", now=now)


def enqueue_author_mute_status_refresh(*, source: str = "manual") -> EnqueueResult:
    """Queue one bounded, read-only refresh for the current mute-management list."""
    scope = current_execution_scope()
    list_as_of = timezone.now()
    status_ids = list(
        mute_management_status_queryset(
            source_appid=scope.target_appid, now=list_as_of
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    return _enqueue_or_reuse_read_maintenance_job(
        task_type=AUTHOR_MUTE_STATUS_REFRESH_TASK,
        scope=scope,
        source=source,
        checkpoint={
            "execution_scope": scope.as_checkpoint(),
            "last_status_id": 0,
            "status_ids": status_ids,
            "list_as_of": list_as_of.isoformat(),
        },
    )


def enqueue_post_author_mute_status_sync(*, source: str = "manual") -> EnqueueResult:
    """Queue one bounded scan of distinct saved post authors without upstream writes."""
    scope = current_execution_scope()
    posts = OriginalPost.objects.filter(source_appid=scope.target_appid)
    upper_post_id = posts.aggregate(max_id=Max("pk"))["max_id"] or 0
    skipped_missing_openid = OriginalPost.objects.filter(
        source_appid=scope.target_appid, author_openid="", pk__lte=upper_post_id
    ).count()
    return _enqueue_or_reuse_read_maintenance_job(
        task_type=POST_AUTHOR_MUTE_STATUS_SYNC_TASK,
        scope=scope,
        source=source,
        checkpoint={
            "execution_scope": scope.as_checkpoint(),
            "last_post_id": 0,
            "upper_post_id": upper_post_id,
            "skipped_missing_openid": skipped_missing_openid,
        },
    )


def enqueue_blocked_user_list_sync(*, source: str = "manual") -> EnqueueResult:
    """Queue one current-game, read-only platform blocked-user list import."""
    scope = current_execution_scope()
    return _enqueue_or_reuse_read_maintenance_job(
        task_type=BLOCKED_USER_LIST_SYNC_TASK,
        scope=scope,
        source=source,
        checkpoint={
            "execution_scope": scope.as_checkpoint(),
            "page": BLOCKED_USER_LIST_FIRST_PAGE,
            "page_size": None,
            "total": None,
            "processed": 0,
            "imported": 0,
            "skipped": 0,
            "completed": False,
        },
    )


@transaction.atomic
def _enqueue_or_reuse_read_maintenance_job(
    *,
    task_type: str,
    scope: ExecutionScope,
    source: str,
    checkpoint: dict,
) -> EnqueueResult:
    """Allow one active read-maintenance pass per task and frozen game scope."""
    if task_type not in MAINTENANCE_READ_TASKS:
        raise ValueError("不支持的禁言管理只读任务")
    active_key = f"mute-maintenance:{task_type}:{scope.target_appid}:v{scope.scope_version}"
    # A conflicting insert can be followed immediately by the other Worker
    # reaching its terminal transition and releasing the key.  Retry from the
    # authoritative state instead of assuming a conflicting row still exists.
    while True:
        active = (
            BackgroundJob.objects.select_for_update()
            .filter(active_coalescing_key=active_key)
            .first()
        )
        if active is not None:
            return EnqueueResult(job=active, created=False)

        # Jobs created before the coalescing-key migration remain active until
        # their normal terminal transition.  Adopt one before creating a new job.
        legacy_active = (
            BackgroundJob.objects.select_for_update()
            .filter(
                task_type=task_type,
                status__in=(JobStatus.PENDING, JobStatus.RUNNING),
                active_coalescing_key__isnull=True,
                checkpoint__execution_scope__target_appid=scope.target_appid,
                checkpoint__execution_scope__scope_version=scope.scope_version,
            )
            .order_by("created_at")
            .first()
        )
        if legacy_active is not None:
            try:
                with transaction.atomic():
                    legacy_active.active_coalescing_key = active_key
                    legacy_active.save(update_fields=["active_coalescing_key", "updated_at"])
            except IntegrityError:
                continue
            return EnqueueResult(job=legacy_active, created=False)

        try:
            with transaction.atomic():
                job = BackgroundJob.objects.create(
                    task_type=task_type,
                    idempotency_key=f"{task_type.lower()}:{scope.target_appid}:v{scope.scope_version}:{uuid4().hex}",
                    active_coalescing_key=active_key,
                    parameter_summary={"source": source},
                    checkpoint=checkpoint,
                    max_retries=3,
                )
        except IntegrityError:
            continue
        return EnqueueResult(job=job, created=True)


def _record_intent_audit(
    *, status: AuthorMuteStatus, result_code: str, user=None, error: str = ""
) -> None:
    """Record an intent-only management outcome without pretending it wrote upstream."""
    OperationAudit.objects.create(
        author_status=status,
        operation_id=str(uuid4()),
        idempotency_key=f"permanent-mute:intent:{status.pk}:{uuid4().hex}",
        action=RecommendedAction.MUTE,
        reason="永久禁言意图管理（不自动解除当前上游禁言）。",
        executor=user,
        execution_source=OperationExecutionSource.HUMAN,
        mute_trigger=MuteOperationTrigger.HUMAN_PERMANENT,
        result_code=result_code,
        error_summary=error[:500],
        executed_at=timezone.now(),
    )


def persist_permanent_mute_intent(
    *, status: AuthorMuteStatus, enabled: bool, user=None
) -> AuthorMuteStatus:
    """Apply one local management choice and retain its audit independently of a Worker."""
    state = set_permanent_mute_intent(status, enabled=enabled, user=user)
    _record_intent_audit(
        status=state,
        result_code="PERMANENT_INTENT_SET" if enabled else "PERMANENT_INTENT_CLEARED",
        user=user,
    )
    return state


def _record_failure_audit(
    *, job: BackgroundJob, status: AuthorMuteStatus, error: Exception
) -> None:
    """Keep one safe per-author failure projection when maintenance cannot finish."""
    OperationAudit.objects.create(
        author_status=status,
        operation_id=str(uuid4()),
        idempotency_key=f"permanent-mute:failure:{job.pk}:{status.pk}:{uuid4().hex}",
        action=RecommendedAction.MUTE,
        reason="永久禁言维护未完成，等待后续任务从当前状态修复。",
        execution_source=OperationExecutionSource.SYSTEM_MAINTENANCE,
        mute_trigger=MuteOperationTrigger.SYSTEM_REPAIR,
        result_code="PERMANENT_REPAIR_FAILED",
        error_summary=f"永久禁言维护失败：{type(error).__name__}",
        executed_at=timezone.now(),
    )


def _refresh_status(
    *, status: AuthorMuteStatus, source: GameCirclePostClient, scope: ExecutionScope
) -> AuthorMuteStatus:
    payload = source.get_author_detail(status.openid)
    observed = sync_author_mute_status_from_user_detail(
        payload,
        expected_openid=status.openid,
        source_requested_at=timezone.now(),
        source_appid=scope.target_appid,
    )
    if observed is None or observed.pk != status.pk:
        raise ValueError("作者状态与最新来源身份不一致，已拒绝执行永久禁言")
    return AuthorMuteStatus.objects.get(pk=status.pk)


def _verified_result(audit: OperationAudit, *, action: str) -> None:
    if audit.result_code == "SESSION_RECOVERING":
        raise JobDeferred("游戏圈会话正在恢复，永久禁言任务稍后继续", delay_seconds=30)
    if audit.result_code != "OK":
        raise ValueError(f"{action}回读未确认：{audit.result_code}")


def _maintain_one(*, job: BackgroundJob, status: AuthorMuteStatus, scope: ExecutionScope) -> None:
    """Renew one marked author by directly replacing the target mute deadline."""
    with GameCirclePostClient(scope=scope) as source:
        status = _refresh_status(status=status, source=source, scope=scope)
        now = timezone.now()
        if not _needs_renewal(status, now=now):
            return
        repair_pending = OperationAudit.objects.filter(
            author_status=status,
            action=RecommendedAction.MUTE,
            execution_source=OperationExecutionSource.SYSTEM_MAINTENANCE,
        ).exclude(result_code="OK").exists()
        trigger = (
            MuteOperationTrigger.SYSTEM_REPAIR
            if repair_pending
            else MuteOperationTrigger.SYSTEM_RENEWAL
        )
        mute = execute_author_operation(
            status,
            user=None,
            action=RecommendedAction.MUTE,
            reason="永久禁言维护：设置当前游戏 30 天禁言期限。",
            source=source,
            upstream_payload={"mute_days": PERMANENT_MUTE_DAYS, "block_reason_code": 1},
            confirmation_key=f"permanent-mute:{job.pk}:{status.pk}:mute",
            execution_source=OperationExecutionSource.SYSTEM_MAINTENANCE,
            mute_trigger=trigger,
        )
        _verified_result(mute, action="禁言")


def _needs_renewal(status: AuthorMuteStatus, *, now) -> bool:
    """Use the freshly synchronized deadline, never the stale candidate projection alone."""
    return status.local_muted_until is None or status.local_muted_until < now + RENEWAL_THRESHOLD


def _next_status(job: BackgroundJob, scope: ExecutionScope) -> AuthorMuteStatus | None:
    checkpoint = job.checkpoint or {}
    cutoff = timezone.now() + RENEWAL_THRESHOLD
    last_status_id = int(checkpoint.get("last_status_id", 0))
    return (
        AuthorMuteStatus.objects.filter(
            source_appid=scope.target_appid,
            permanent_mute_enabled=True,
            pk__gt=last_status_id,
        )
        .filter(Q(local_muted_until__lt=cutoff) | Q(local_muted_until__isnull=True))
        .order_by("pk")
        .first()
    )


def _next_refresh_status(job: BackgroundJob, scope: ExecutionScope) -> AuthorMuteStatus | None:
    checkpoint = job.checkpoint or {}
    last_status_id = int(checkpoint.get("last_status_id", 0))
    try:
        list_as_of = datetime.fromisoformat(str(checkpoint["list_as_of"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("禁言列表刷新任务缺少列表快照时刻") from exc
    if not timezone.is_aware(list_as_of):
        raise ValueError("禁言列表刷新任务快照时刻无时区")
    raw_status_ids = checkpoint.get("status_ids")
    if not isinstance(raw_status_ids, list):
        raise ValueError("禁言列表刷新任务缺少作者集合快照")
    try:
        status_ids = [int(status_id) for status_id in raw_status_ids]
    except (TypeError, ValueError) as exc:
        raise ValueError("禁言列表刷新任务作者集合无效") from exc
    return (
        AuthorMuteStatus.objects.filter(
            source_appid=scope.target_appid,
            pk__in=status_ids,
            pk__gt=last_status_id,
        )
        .order_by("pk")
        .first()
    )


def _next_post_author_sync_post(job: BackgroundJob, scope: ExecutionScope) -> OriginalPost | None:
    checkpoint = job.checkpoint or {}
    last_post_id = int(checkpoint.get("last_post_id", 0))
    upper_post_id = int(checkpoint.get("upper_post_id", 0))
    first_author_post_id = (
        OriginalPost.objects.filter(
            source_appid=scope.target_appid,
            author_openid=OuterRef("author_openid"),
            author_openid__gt="",
            pk__lte=upper_post_id,
        )
        .order_by("pk")
        .values("pk")[:1]
    )
    return (
        OriginalPost.objects.filter(
            source_appid=scope.target_appid,
            author_openid__gt="",
            pk__gt=last_post_id,
            pk__lte=upper_post_id,
            pk=Subquery(first_author_post_id),
        )
        .order_by("pk")
        .first()
    )


def _advance_checkpoint(
    job: BackgroundJob,
    *,
    cursor_field: str,
    item_id: int | None,
    succeeded: bool,
    error: str = "",
) -> None:
    checkpoint = dict(job.checkpoint or {})
    if item_id is not None:
        checkpoint[cursor_field] = item_id
    checkpoint["processed"] = int(checkpoint.get("processed", 0)) + 1
    key = "succeeded" if succeeded else "failed"
    checkpoint[key] = int(checkpoint.get(key, 0)) + 1
    if error:
        checkpoint["last_error"] = error[:160]
    job.checkpoint = checkpoint_with_execution_scope(job, checkpoint)
    job.save(update_fields=["checkpoint", "updated_at"])


def _safe_failure_summary(error: Exception, *, label: str) -> str:
    """Job checkpoints are operator-visible and must never echo a provider payload."""
    if isinstance(error, GameCircleIpcError):
        return f"游戏圈会话调用失败：{error.category}"
    return f"{label}失败：{type(error).__name__}"


def _raise_if_partially_failed(job: BackgroundJob, *, label: str) -> None:
    """Expose per-author failures in the job status without replaying writes."""
    checkpoint = job.checkpoint or {}
    failed = int(checkpoint.get("failed", 0))
    if not failed:
        return
    processed = int(checkpoint.get("processed", 0))
    succeeded = int(checkpoint.get("succeeded", 0))
    raise JobTerminalError(
        f"{label}部分完成：已处理 {processed} 位，成功 {succeeded} 位，失败 {failed} 位"
    )


def _process_checkpointed_source_job(
    job: BackgroundJob,
    *,
    next_item,
    process_one,
    label: str,
    cursor_field: str,
    record_failure=None,
) -> None:
    """Run bounded source work with durable progress and no payload logging."""
    scope = resolve_execution_scope(job)
    while True:
        item = next_item(job, scope)
        if item is None:
            _raise_if_partially_failed(job, label=label)
            return
        try:
            process_one(job=job, item=item, scope=scope)
        except JobDeferred:
            raise
        except GameCircleIpcError as exc:
            if exc.category == "SESSION_RECOVERING":
                raise JobDeferred(
                    f"游戏圈会话正在恢复，{label}稍后继续", delay_seconds=30
                ) from exc
            if record_failure is not None:
                record_failure(job=job, item=item, error=exc)
            _advance_checkpoint(
                job,
                cursor_field=cursor_field,
                item_id=item.pk,
                succeeded=False,
                error=_safe_failure_summary(exc, label=label),
            )
        except Exception as exc:
            if record_failure is not None:
                record_failure(job=job, item=item, error=exc)
            _advance_checkpoint(
                job,
                cursor_field=cursor_field,
                item_id=item.pk,
                succeeded=False,
                error=_safe_failure_summary(exc, label=label),
            )
        else:
            _advance_checkpoint(
                job, cursor_field=cursor_field, item_id=item.pk, succeeded=True
            )


def permanent_mute_handler(job: BackgroundJob) -> None:
    """Process one renewal job serially; each author gets an independent checkpoint outcome."""
    _process_checkpointed_source_job(
        job,
        next_item=_next_status,
        process_one=_maintain_status_item,
        label="永久禁言任务",
        cursor_field="last_status_id",
        record_failure=_record_maintenance_item_failure,
    )


def _maintain_status_item(*, job: BackgroundJob, item: AuthorMuteStatus, scope: ExecutionScope) -> None:
    _maintain_one(job=job, status=item, scope=scope)


def _record_maintenance_item_failure(
    *, job: BackgroundJob, item: AuthorMuteStatus, error: Exception
) -> None:
    _record_failure_audit(job=job, status=item, error=error)


def _refresh_one(*, job: BackgroundJob, status: AuthorMuteStatus, scope: ExecutionScope) -> None:
    """Refresh one saved author projection without performing an upstream write."""
    with GameCirclePostClient(scope=scope) as source:
        observed = _refresh_status(status=status, source=source, scope=scope)
    if observed.pk != status.pk:
        raise ValueError("作者状态与最新来源身份不一致")


def _refresh_status_item(*, job: BackgroundJob, item: AuthorMuteStatus, scope: ExecutionScope) -> None:
    _refresh_one(job=job, status=item, scope=scope)


def author_mute_status_refresh_handler(job: BackgroundJob) -> None:
    """Refresh the bounded saved-author set through one user-detail read each."""
    _process_checkpointed_source_job(
        job,
        next_item=_next_refresh_status,
        process_one=_refresh_status_item,
        label="作者禁言状态刷新任务",
        cursor_field="last_status_id",
    )


def _sync_post_author_item(*, job: BackgroundJob, item: OriginalPost, scope: ExecutionScope) -> None:
    """Project one distinct post author, adding only actively muted new authors."""
    with GameCirclePostClient(scope=scope) as source:
        payload = source.get_author_detail(item.author_openid)
    sync_author_mute_status_from_user_detail(
        payload,
        expected_openid=item.author_openid,
        source_requested_at=timezone.now(),
        source_appid=scope.target_appid,
        post=item,
        create_if_inactive=False,
    )


def post_author_mute_status_sync_handler(job: BackgroundJob) -> None:
    """Scan each saved post author once and supplement only active mute records."""
    _process_checkpointed_source_job(
        job,
        next_item=_next_post_author_sync_post,
        process_one=_sync_post_author_item,
        label="全量帖子作者禁言扫描任务",
        cursor_field="last_post_id",
    )


def _blocked_user_page_size(job: BackgroundJob, source: GameCirclePostClient) -> tuple[int, dict]:
    """Read one page, lowering size only when the fixed source rejects that parameter."""
    checkpoint = job.checkpoint or {}
    page = int(checkpoint.get("page", BLOCKED_USER_LIST_FIRST_PAGE))
    if page < 0:
        raise ValueError("平台禁言名单导入页码无效")
    stored_size = checkpoint.get("page_size")
    if stored_size is not None:
        if stored_size not in BLOCKED_USER_LIST_PAGE_SIZE_CANDIDATES:
            raise ValueError("平台禁言名单导入分页大小无效")
        return int(stored_size), source.list_blocked_users(page=page, page_size=int(stored_size))

    for page_size in BLOCKED_USER_LIST_PAGE_SIZE_CANDIDATES:
        try:
            result = source.list_blocked_users(page=page, page_size=page_size)
        except SourceContractError as exc:
            if exc.safe_reason != BLOCKED_USER_LIST_PAGE_SIZE_REJECTED:
                raise
            continue
        expected_first_page_size = min(page_size, result["total"])
        if page == 0 and len(result["items"]) != expected_first_page_size:
            continue
        return page_size, result
    raise ValueError("平台禁言名单不接受受控分页大小")


def blocked_user_list_sync_handler(job: BackgroundJob) -> None:
    """Import only the current platform block-list projection; never infer unblocks by absence."""
    scope = resolve_execution_scope(job)
    with GameCirclePostClient(scope=scope) as source:
        while True:
            checkpoint = dict(job.checkpoint or {})
            if checkpoint.get("completed") is True:
                return
            frozen_total = checkpoint.get("total")
            processed_before = int(checkpoint.get("processed", 0))
            if isinstance(frozen_total, int) and processed_before >= frozen_total:
                checkpoint["completed"] = True
                job.checkpoint = checkpoint_with_execution_scope(job, checkpoint)
                job.save(update_fields=["checkpoint", "updated_at"])
                return
            page_size, result = _blocked_user_page_size(job, source)
            page = int(checkpoint.get("page", BLOCKED_USER_LIST_FIRST_PAGE))
            items = result.get("items")
            total = result.get("total")
            if not isinstance(items, list) or not isinstance(total, int) or total < 0:
                raise ValueError("平台禁言名单响应结构无效")
            effective_total = frozen_total if isinstance(frozen_total, int) else total
            expected_count = min(page_size, effective_total - processed_before)
            if len(items) != expected_count:
                raise ValueError("平台禁言名单分页结果不完整，未导入不确定数据")

            requested_at = timezone.now()
            imported = 0
            skipped = 0
            for item in items:
                state = sync_author_mute_status_from_blocked_user_list(
                    item,
                    source_appid=scope.target_appid,
                    source_requested_at=requested_at,
                )
                if state is None:
                    skipped += 1
                else:
                    imported += 1

            checkpoint["page"] = page + 1
            checkpoint["page_size"] = page_size
            checkpoint["total"] = total if checkpoint.get("total") is None else checkpoint["total"]
            checkpoint["processed"] = processed_before + len(items)
            checkpoint["imported"] = int(checkpoint.get("imported", 0)) + imported
            checkpoint["skipped"] = int(checkpoint.get("skipped", 0)) + skipped
            checkpoint["completed"] = checkpoint["processed"] >= int(checkpoint["total"])
            job.checkpoint = checkpoint_with_execution_scope(job, checkpoint)
            job.save(update_fields=["checkpoint", "updated_at"])
            if checkpoint["completed"]:
                return


WORKER_HANDLERS = {
    PERMANENT_MUTE_RENEWAL_TASK: permanent_mute_handler,
    AUTHOR_MUTE_STATUS_REFRESH_TASK: author_mute_status_refresh_handler,
    POST_AUTHOR_MUTE_STATUS_SYNC_TASK: post_author_mute_status_sync_handler,
    BLOCKED_USER_LIST_SYNC_TASK: blocked_user_list_sync_handler,
}
