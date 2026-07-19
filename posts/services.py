import re
from datetime import UTC, datetime
from html import unescape
from typing import Any

from django.db import transaction
from django.utils import timezone

from jobs.adapters import SourceBusinessDeletedError, SourceContractError, SourceTransportError

from .models import (
    CandidatePost,
    InteractionSnapshot,
    MediaType,
    OriginalPost,
    OriginType,
    PostMedia,
    mark_post_deleted,
    normalize_post_status,
)


def html_to_text(value: str) -> str:
    plain = re.sub(r"<(?:br|/p|/div)\s*/?>", "\n", value, flags=re.IGNORECASE)
    plain = re.sub(r"<[^>]+>", "", plain)
    return re.sub(r"\n{3,}", "\n\n", unescape(plain)).strip()


def normalize_ballot_text(value: Any) -> str:
    """Keep only poll titles and option text because both can carry policy-relevant content."""
    if not isinstance(value, list):
        return ""
    ballots: list[str] = []
    for ballot_index, ballot in enumerate(value, start=1):
        if not isinstance(ballot, dict):
            continue
        lines: list[str] = []
        title = ballot.get("title")
        if isinstance(title, str) and title.strip():
            lines.append(f"投票 {ballot_index}：{title.strip()}")
        options = ballot.get("option_item_list")
        if isinstance(options, list):
            for option_index, option in enumerate(options, start=1):
                text = option.get("text") if isinstance(option, dict) else None
                if isinstance(text, str) and text.strip():
                    lines.append(f"选项 {option_index}：{text.strip()}")
        if lines:
            ballots.append("\n".join(lines))
    return "\n\n".join(ballots)


@transaction.atomic
def classify_content_card(candidate: CandidatePost, card: dict[str, Any]) -> OriginalPost | None:
    """The presence of forward_topic_info is the only accepted forward signal."""
    if "forward_topic_info" in card and card["forward_topic_info"] is not None:
        candidate.origin_type = OriginType.FORWARD
        candidate.classified_at = timezone.now()
        candidate.classification_error = ""
        candidate.save(
            update_fields=["origin_type", "classified_at", "classification_error", "updated_at"]
        )
        return None

    # Detail cards use either the legacy top-level layout or base_info for post fields.
    details = card.get("base_info") if isinstance(card.get("base_info"), dict) else card
    base = details.get("base_content")
    if not isinstance(base, dict) or not candidate.content_id:
        candidate.origin_type = OriginType.CLASSIFICATION_PENDING
        candidate.classification_error = "内容卡片结构无效或缺少 content_id"
        candidate.save(update_fields=["origin_type", "classification_error", "updated_at"])
        return None

    content_html = str(base.get("content", ""))
    ballot_info = base.get("ballot_info_list")
    if ballot_info is None:
        ballot_info = details.get("ballot_info_list", card.get("ballot_info_list"))
    publish_time = details.get("publish_time")
    if publish_time:
        try:
            publish_time = datetime.fromtimestamp(int(publish_time), tz=UTC)
        except (TypeError, ValueError, OverflowError):
            publish_time = None
    else:
        publish_time = None
    post, _ = OriginalPost.objects.update_or_create(
        topic_id=candidate.topic_id,
        defaults={
            "content_id": candidate.content_id,
            "title": str(base.get("title", "")),
            "content_html": content_html,
            "content_text": html_to_text(content_html),
            "ballot_text": normalize_ballot_text(ballot_info),
            "publish_time": publish_time,
            "status": normalize_post_status(base.get("status")),
            "recommend_status": base.get("recommend_status"),
            "source_appid": candidate.source_appid,
        },
    )
    PostMedia.objects.filter(post=post).delete()
    for image in base.get("image_list", []):
        if not isinstance(image, dict) or not image.get("url"):
            continue
        PostMedia.objects.create(
            post=post,
            url=str(image["url"]),
            media_type=MediaType.IMAGE,
            width=image.get("width"),
            height=image.get("height"),
            is_gif=bool(image.get("is_gif", False)),
        )
    for video in base.get("video_info", []):
        if not isinstance(video, dict) or not video.get("url"):
            continue
        PostMedia.objects.create(
            post=post,
            url=str(video["url"]),
            media_type=MediaType.VIDEO,
            width=video.get("width"),
            height=video.get("height"),
        )
    reply_info = details.get("reply_info", {})
    love_info = details.get("love_info", {})
    share_info = details.get("share_info", {})
    InteractionSnapshot.objects.create(
        post=post,
        reply_count=int(reply_info.get("total_reply_count", 0)),
        love_count=int(love_info.get("total_love_count", 0)),
        share_count=int(share_info.get("total_share_count", 0)),
        source="content_card",
    )
    candidate.origin_type = OriginType.ORIGINAL
    candidate.classified_at = timezone.now()
    candidate.classification_error = ""
    candidate.save(
        update_fields=["origin_type", "classified_at", "classification_error", "updated_at"]
    )
    return post


@transaction.atomic
def store_post_interaction_metrics(
    post: OriginalPost, payload: dict[str, Any]
) -> InteractionSnapshot:
    """Persist post-level counters without changing any non-interaction post fields."""
    try:
        topic = payload["data"]["topic"]
        if not isinstance(topic, dict):
            raise TypeError("topic is not an object")
        return InteractionSnapshot.objects.create(
            post=post,
            view_count=int(topic["view_count"]),
            reply_count=int(topic.get("reply_count", 0)),
            love_count=int(topic.get("love_count", 0)),
            share_count=int(topic.get("share_count", 0)),
            source="topicsearchgetdetail",
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise SourceContractError("帖子指标响应结构无效") from exc


@transaction.atomic
def store_post_metrics(post: OriginalPost, payload: dict[str, Any]) -> InteractionSnapshot:
    """Persist post-level counters and the upstream status for status-aware refreshes."""
    snapshot = store_post_interaction_metrics(post, payload)
    topic = payload["data"]["topic"]
    if topic.get("status") is not None:
        post.status = normalize_post_status(topic["status"])
        post.save(update_fields=["status", "updated_at"])
    return snapshot


def refresh_original_post(client, post: OriginalPost) -> OriginalPost:
    """Refresh content while keeping management deletion authoritative for operations."""
    deleted = False
    deleted_status = None
    try:
        metrics_payload = client.get_post_metrics(post.topic_id)
    except SourceBusinessDeletedError:
        deleted = True
        metrics_payload = None
        deleted_status = mark_post_deleted(post.status)
        post.status = deleted_status
        post.save(update_fields=["status", "updated_at"])
    candidate, _ = CandidatePost.objects.update_or_create(
        topic_id=post.topic_id,
        defaults={
            "content_id": post.content_id,
            "source_appid": post.source_appid,
            "origin_type": OriginType.ORIGINAL,
        },
    )
    try:
        payload = client.get_content_card(candidate.content_id)
        card = payload["data"]["content_card"]
    except (KeyError, TypeError, SourceContractError, SourceTransportError) as exc:
        if deleted:
            # The deletion state is still useful even when the historical card has expired.
            return post
        raise SourceContractError("内容卡片响应结构无效") from exc
    refreshed = classify_content_card(candidate, card)
    if refreshed is None:
        raise SourceContractError("刷新后帖子不再符合原创条件")
    if deleted:
        refreshed.status = deleted_status
        refreshed.save(update_fields=["status", "updated_at"])
    if metrics_payload is not None:
        store_post_metrics(refreshed, metrics_payload)
    return refreshed
