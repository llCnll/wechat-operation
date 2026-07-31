"""Bounded, auditable comment-export workflow services.

Web requests only confirm a post and enqueue work.  The reply reader is used
exclusively by the manual backfill worker, which is the only process given the
fixed reply-page IPC operation.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.core import signing
from django.db import transaction

from config.game_circle_ipc import (
    GameCircleIpcError,
    GameCirclePostClient,
    is_session_recovering_error,
)
from jobs.adapters import SourceBusinessDeletedError, SourceContractError
from jobs.models import BackgroundJob
from jobs.services import (
    EnqueueResult,
    JobDeferred,
    JobTerminalError,
    enqueue_active_coalesced_job,
)
from jobs.task_types import COMMENT_EXPORT_V1_TASK

from .content import split_trailing_topics
from .game_catalog import ExecutionScope, current_execution_scope, resolve_fixed_execution_scope
from .models import CommentExportResult, CommentExportRun, OriginalPost
from .services import html_to_text

RULE_VERSION = "keyword-first-digit-v1"
PAGE_SIZE = 100
MAX_PAGES = 10_000
TOKEN_SALT = "comment-export-confirmation-v1"
TOKEN_MAX_AGE_SECONDS = 10 * 60
SHANGHAI = ZoneInfo("Asia/Shanghai")
ASCII_NUMBER = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class CommentExportSnapshot:
    topic_id: int
    source_appid: str
    scope_version: int
    author_openid: str
    author_nick_name: str
    title: str
    content_text: str
    view_count: int
    love_count: int
    reply_count: int
    fingerprint: str

    def public(self) -> dict[str, object]:
        return {
            "topic_id": self.topic_id,
            "title": self.title,
            "content_text": self.content_text,
            "author_nick_name": self.author_nick_name,
            "view_count": self.view_count,
            "love_count": self.love_count,
            "reply_count": self.reply_count,
        }


def _canonical_digest(title: str, content_text: str) -> str:
    raw = json.dumps([title, content_text], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _int(value: object, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise SourceContractError(f"帖子{name}响应无效") from exc
    if result < 0:
        raise SourceContractError(f"帖子{name}响应无效")
    return result


def read_confirmation_snapshot(
    *, topic_id: int, scope: ExecutionScope | None = None
) -> CommentExportSnapshot:
    """Read and validate a post without modifying any local business record."""
    if not isinstance(topic_id, int) or topic_id <= 0:
        raise ValueError("帖子 ID 必须是正整数。")
    scope = scope or current_execution_scope(persist_default=False)
    post = OriginalPost.objects.filter(topic_id=topic_id, source_appid=scope.target_appid).first()
    if post is None or not post.content_id or not post.author_openid:
        raise ValueError("当前游戏未找到可确认的已采集帖子。")
    client = GameCirclePostClient(scope=scope)
    try:
        metrics = client.get_post_metrics(topic_id)
        topic = metrics.get("data", {}).get("topic")
        if not isinstance(topic, dict):
            raise SourceContractError("帖子指标响应结构无效")
        creator = topic.get("creater", topic.get("creator"))
        if not isinstance(creator, dict) or creator.get("openid") != post.author_openid:
            raise SourceContractError("帖子作者身份校验失败")
        if topic.get("appid") != scope.target_appid:
            raise SourceContractError("帖子游戏范围校验失败")
        source_topic_id = topic.get("topic_id", topic.get("id", topic_id))
        if int(source_topic_id) != topic_id:
            raise SourceContractError("帖子 ID 校验失败")
        card_payload = client.get_content_card(post.content_id)
    except SourceBusinessDeletedError as exc:
        raise ValueError("原平台帖子已删除或不可读取。") from exc
    finally:
        client.close()
    try:
        card = card_payload["data"]["content_card"]
        if not isinstance(card, dict) or card.get("forward_topic_info") is not None:
            raise TypeError
        details = card.get("base_info") if isinstance(card.get("base_info"), dict) else card
        base = details["base_content"]
        if (
            not isinstance(base, dict)
            or not isinstance(base.get("title", ""), str)
            or not isinstance(base.get("content", ""), str)
        ):
            raise TypeError
        content_text, _ = split_trailing_topics(html_to_text(base["content"]))
    except (KeyError, TypeError) as exc:
        raise SourceContractError("帖子内容卡片响应结构无效") from exc
    return CommentExportSnapshot(
        topic_id=topic_id,
        source_appid=scope.target_appid,
        scope_version=scope.scope_version,
        author_openid=post.author_openid,
        author_nick_name=str(creator.get("nick_name") or post.author_nick_name or ""),
        title=base["title"].strip(),
        content_text=content_text,
        view_count=_int(topic.get("view_count", 0), "浏览量"),
        love_count=_int(topic.get("love_count", 0), "点赞量"),
        reply_count=_int(topic.get("reply_count", 0), "评论量"),
        fingerprint=_canonical_digest(base["title"].strip(), content_text),
    )


def normalize_keyword(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("关键词无效。")
    keyword = unicodedata.normalize("NFC", value.strip())
    if not 1 <= len(keyword) <= 100:
        raise ValueError("关键词长度须为 1 至 100 个字符。")
    return keyword


def match_comment(content: object, keyword: str) -> str | None:
    if not isinstance(content, str):
        return None
    match = ASCII_NUMBER.search(unicodedata.normalize("NFC", content))
    return match.group(0) if keyword in unicodedata.normalize("NFC", content) and match else None


def format_source_time(value: object) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        raise SourceContractError("评论创建时间无效") from None


def issue_confirmation_token(*, snapshot: CommentExportSnapshot, user_id: int) -> str:
    return signing.dumps(
        {
            "topic_id": snapshot.topic_id,
            "appid": snapshot.source_appid,
            "scope_version": snapshot.scope_version,
            "fingerprint": snapshot.fingerprint,
            "user_id": user_id,
        },
        salt=TOKEN_SALT,
        compress=True,
    )


def verify_confirmation_token(*, token: str, user_id: int) -> dict[str, object]:
    try:
        data = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE_SECONDS)
    except signing.BadSignature as exc:
        raise ValueError("帖子确认已失效，请重新确认。") from exc
    if not isinstance(data, dict) or data.get("user_id") != user_id:
        raise ValueError("帖子确认不属于当前用户，请重新确认。")
    return data


def _coalescing_key(*, user_id: int, snapshot: CommentExportSnapshot, keyword: str) -> str:
    payload = [
        user_id,
        snapshot.source_appid,
        snapshot.scope_version,
        snapshot.topic_id,
        snapshot.author_openid,
        RULE_VERSION,
        keyword,
    ]
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return f"comment-export:v1:{digest}"


def create_or_reuse_run(
    *, user_id: int, token: str, keyword_value: object
) -> tuple[CommentExportRun, bool]:
    token_data = verify_confirmation_token(token=token, user_id=user_id)
    topic_id = token_data.get("topic_id")
    if not isinstance(topic_id, int):
        raise ValueError("帖子确认无效，请重新确认。")
    snapshot = read_confirmation_snapshot(topic_id=topic_id)
    for field, value in (
        ("appid", snapshot.source_appid),
        ("scope_version", snapshot.scope_version),
        ("fingerprint", snapshot.fingerprint),
    ):
        if token_data.get(field) != value:
            raise ValueError("帖子内容或游戏范围已变化，请重新确认。")
    keyword = normalize_keyword(keyword_value)
    with transaction.atomic():
        result: EnqueueResult = enqueue_active_coalesced_job(
            task_type=COMMENT_EXPORT_V1_TASK,
            active_coalescing_key=_coalescing_key(
                user_id=user_id, snapshot=snapshot, keyword=keyword
            ),
            idempotency_key=f"comment-export:{uuid4().hex}",
            parameter_summary={},
            checkpoint={
                "execution_scope": {
                    "target_appid": snapshot.source_appid,
                    "scope_version": snapshot.scope_version,
                }
            },
        )
        if result.created:
            run = CommentExportRun.objects.create(
                job=result.job,
                created_by_id=user_id,
                source_appid=snapshot.source_appid,
                scope_version=snapshot.scope_version,
                topic_id=snapshot.topic_id,
                title_snapshot=snapshot.title,
                author_openid=snapshot.author_openid,
                author_nick_name=snapshot.author_nick_name,
                rule_version=RULE_VERSION,
                keyword=keyword,
                page_size=PAGE_SIZE,
            )
            result.job.parameter_summary = {
                "comment_export_run_id": run.pk,
                "scope_version": snapshot.scope_version,
            }
            result.job.save(update_fields=["parameter_summary", "updated_at"])
        else:
            run = CommentExportRun.objects.select_related("job").get(job=result.job)
    return run, result.created


def _run_for_job(job: BackgroundJob) -> CommentExportRun:
    try:
        run = CommentExportRun.objects.get(job=job)
    except CommentExportRun.DoesNotExist as exc:
        raise SourceContractError("评论导出任务缺少执行记录") from exc
    if run.rule_version != RULE_VERSION or run.page_size != PAGE_SIZE:
        raise SourceContractError("评论导出任务规则无效")
    return run


def _reset_attempt(run: CommentExportRun, job: BackgroundJob) -> None:
    with transaction.atomic():
        CommentExportResult.objects.filter(run=run).delete()
        CommentExportRun.objects.filter(pk=run.pk).update(
            processed_count=0, matched_count=0, page_count=0, total_count=None
        )
        job.checkpoint = {
            "execution_scope": {
                "target_appid": run.source_appid,
                "scope_version": run.scope_version,
            }
        }
        job.save(update_fields=["checkpoint", "updated_at"])


def comment_export_handler(job: BackgroundJob) -> None:
    """Use the shared retry policy while making source contracts terminal."""
    try:
        _comment_export_handler(job)
    except GameCircleIpcError as exc:
        if is_session_recovering_error(exc):
            raise JobDeferred("等待游戏圈会话恢复", delay_seconds=5) from exc
        if exc.category in {"SOURCE_DELETED", "SOURCE_CONTRACT"}:
            raise JobTerminalError(str(exc)) from exc
        raise
    except (SourceBusinessDeletedError, SourceContractError) as exc:
        raise JobTerminalError(str(exc)) from exc


def _comment_export_handler(job: BackgroundJob) -> None:
    run = _run_for_job(job)
    scope = resolve_fixed_execution_scope(job)
    if scope.target_appid != run.source_appid or scope.scope_version != run.scope_version:
        raise SourceContractError("评论导出执行范围无效")
    _reset_attempt(run, job)
    succeeded = False
    seen_reply_ids: set[str] = set()
    processed = matched = 0
    try:
        client = GameCirclePostClient(scope=scope)
        try:
            for page in range(MAX_PAGES):
                payload = client.list_topic_replies(
                    topic_id=run.topic_id,
                    topic_openid=run.author_openid,
                    page=page,
                    limit=PAGE_SIZE,
                )
                items = payload.get("items")
                has_next = payload.get("has_next")
                total = payload.get("total_count")
                if (
                    not isinstance(items, list)
                    or not isinstance(has_next, bool)
                    or (total is not None and (not isinstance(total, int) or total < 0))
                ):
                    raise SourceContractError("评论分页响应结构无效")
                new_reply_ids: set[str] = set()
                rows = []
                for item in items:
                    if not isinstance(item, dict):
                        raise SourceContractError("评论分页响应结构无效")
                    reply_id = item.get("reply_id")
                    if not isinstance(reply_id, str) or not reply_id:
                        raise SourceContractError("评论回复 ID 无效")
                    if reply_id in seen_reply_ids:
                        continue
                    seen_reply_ids.add(reply_id)
                    new_reply_ids.add(reply_id)
                    processed += 1
                    game_id = match_comment(item.get("content"), run.keyword)
                    if game_id is not None:
                        rows.append(
                            CommentExportResult(
                                run=run,
                                reply_id=reply_id,
                                openid=str(item.get("openid", "")),
                                nick_name=str(item.get("nick_name", "")),
                                create_time=format_source_time(item.get("create_time")),
                                content=str(item.get("content", "")),
                                game_id=game_id,
                            )
                        )
                if has_next and (not items or not new_reply_ids):
                    raise SourceContractError("评论分页没有新增回复")
                with transaction.atomic():
                    CommentExportResult.objects.bulk_create(rows, ignore_conflicts=True)
                    matched += len(rows)
                    CommentExportRun.objects.filter(pk=run.pk).update(
                        processed_count=processed,
                        matched_count=matched,
                        page_count=page + 1,
                        total_count=total,
                    )
                    job.checkpoint = {
                        "execution_scope": scope.as_checkpoint(),
                        "page": page + 1,
                        "processed": processed,
                        "matched": matched,
                    }
                    job.save(update_fields=["checkpoint", "updated_at"])
                if not has_next:
                    succeeded = True
                    return
            raise SourceContractError("评论分页超过最大页数")
        finally:
            client.close()
    finally:
        if not succeeded:
            with transaction.atomic():
                CommentExportResult.objects.filter(run=run).delete()
                CommentExportRun.objects.filter(pk=run.pk).update(
                    processed_count=0, matched_count=0, page_count=0, total_count=None
                )
