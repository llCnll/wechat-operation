import re
from datetime import UTC, datetime
from html import unescape
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from jobs.adapters import SourceBusinessDeletedError, SourceContractError

from .models import (
    AuthorMuteStatus,
    CandidatePost,
    InteractionSnapshot,
    MediaType,
    OriginalPost,
    OriginType,
    PostMedia,
    mark_post_deleted,
    normalize_post_status,
)


def _future_timestamp(value: Any, *, observed_at) -> datetime | None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if timestamp <= int(observed_at.timestamp()):
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


@transaction.atomic
def _sync_author_mute_observation(
    *,
    creator: dict[str, Any],
    source_appid: str,
    source_requested_at=None,
    post: OriginalPost | None = None,
    create_if_inactive: bool = False,
) -> AuthorMuteStatus | None:
    """Persist one normalized author observation from a topic or a user detail."""
    requested_at = source_requested_at or timezone.now()
    observed_at = timezone.now()
    openid = creator.get("openid")
    if not isinstance(openid, str) or not openid:
        return None
    nick_name = creator.get("nick_name") if isinstance(creator.get("nick_name"), str) else ""
    block_info = creator.get("block_info")
    local_until = None
    if isinstance(block_info, dict) and str(block_info.get("appid", "")) == source_appid:
        local_until = _future_timestamp(block_info.get("block_end_time"), observed_at=observed_at)
    global_until = _future_timestamp(creator.get("global_block_end_time"), observed_at=observed_at)

    state = AuthorMuteStatus.objects.select_for_update().filter(
        openid=openid, source_appid=source_appid
    ).first()
    if state is not None and state.source_requested_at > requested_at:
        return state

    if post is not None:
        post_updates = []
        if post.author_openid != openid:
            post.author_openid = openid
            post_updates.append("author_openid")
        if nick_name and post.author_nick_name != nick_name:
            post.author_nick_name = nick_name
            post_updates.append("author_nick_name")
        if post_updates:
            post.save(update_fields=[*post_updates, "updated_at"])
    if nick_name:
        OriginalPost.objects.filter(
            source_appid=source_appid,
            author_openid=openid,
        ).exclude(author_nick_name=nick_name).update(
            author_nick_name=nick_name,
            updated_at=observed_at,
        )
    active = local_until is not None or global_until is not None
    if state is None:
        if not active and not create_if_inactive:
            return None
        return AuthorMuteStatus.objects.create(
            openid=openid,
            source_appid=source_appid,
            nick_name=nick_name,
            local_muted_until=local_until,
            global_muted_until=global_until,
            first_detected_at=observed_at if active else None,
            last_synced_at=observed_at,
            source_requested_at=requested_at,
            last_source_post=post,
        )
    state.nick_name = nick_name
    state.local_muted_until = local_until
    state.global_muted_until = global_until
    if active and state.first_detected_at is None:
        state.first_detected_at = observed_at
    state.last_synced_at = observed_at
    state.source_requested_at = requested_at
    update_fields = [
        "nick_name",
        "local_muted_until",
        "global_muted_until",
        "first_detected_at",
        "last_synced_at",
        "source_requested_at",
        "updated_at",
    ]
    if post is not None:
        state.last_source_post = post
        update_fields.append("last_source_post")
    state.save(update_fields=update_fields)
    return state


def sync_author_mute_status(
    post: OriginalPost,
    payload: dict[str, Any],
    *,
    source_requested_at=None,
    source_appid: str | None = None,
) -> AuthorMuteStatus | None:
    """Persist the author projection carried by an already-fetched topic detail."""
    try:
        topic = payload["data"]["topic"]
        creator = topic.get("creater")
    except (KeyError, TypeError, AttributeError):
        return None
    if not isinstance(creator, dict):
        return None
    resolved_appid = source_appid or post.source_appid or str(topic.get("appid") or "")
    if not resolved_appid:
        return None
    return _sync_author_mute_observation(
        creator=creator,
        source_appid=resolved_appid,
        source_requested_at=source_requested_at,
        post=post,
    )


def sync_author_mute_status_from_user_detail(
    payload: dict[str, Any],
    *,
    source_appid: str,
    expected_openid: str,
    source_requested_at=None,
    post: OriginalPost | None = None,
    create_if_inactive: bool = False,
) -> AuthorMuteStatus | None:
    """Persist the author projection returned by the dedicated user-detail endpoint."""
    try:
        user = payload["data"]["user"]
    except (KeyError, TypeError):
        return None
    if not isinstance(user, dict) or not source_appid:
        return None
    observed_openid = user.get("openid")
    if not isinstance(expected_openid, str) or not expected_openid:
        raise SourceContractError("作者身份校验参数无效")
    if observed_openid != expected_openid:
        raise SourceContractError("作者详情响应身份不一致")
    return _sync_author_mute_observation(
        creator=user,
        source_appid=source_appid,
        source_requested_at=source_requested_at,
        post=post,
        create_if_inactive=create_if_inactive,
    )


def refresh_one_post_from_source(
    source,
    post: OriginalPost,
    *,
    source_appid: str,
    fallback_author_openids: set[str],
) -> str:
    """Refresh one post through the shared detail/author-projection boundary.

    Only the adapter's explicit deleted/unreadable business error may fall back
    to user detail.  Other source errors propagate so callers keep retry semantics.
    """
    requested_at = timezone.now()
    try:
        payload = source.get_post_metrics(post.topic_id)
    except SourceBusinessDeletedError:
        post.status = mark_post_deleted(post.status)
        post.save(update_fields=("status", "updated_at"))
        openid = post.author_openid
        if not openid or openid in fallback_author_openids:
            return "deleted"
        fallback_author_openids.add(openid)
        payload = source.get_author_detail(openid)
        sync_author_mute_status_from_user_detail(
            payload,
            expected_openid=openid,
            source_appid=source_appid,
            source_requested_at=timezone.now(),
            create_if_inactive=False,
        )
        return "author_fallback"
    store_post_metrics(post, payload, source_requested_at=requested_at)
    return "refreshed"


def mute_management_status_queryset(*, source_appid: str, now=None):
    """Return the unfiltered current-game author set displayed by mute management."""
    observed_at = now or timezone.now()
    active_mutes = Q(local_muted_until__gt=observed_at) | Q(global_muted_until__gt=observed_at)
    return AuthorMuteStatus.objects.filter(source_appid=source_appid).filter(
        Q(permanent_mute_enabled=True) | active_mutes
    )


@transaction.atomic
def set_permanent_mute_intent(
    state: AuthorMuteStatus, *, enabled: bool, user=None
) -> AuthorMuteStatus:
    """Maintain the local long-term intent without changing upstream mute state."""
    state = AuthorMuteStatus.objects.select_for_update().get(pk=state.pk)
    state.permanent_mute_enabled = enabled
    state.permanent_mute_set_at = timezone.now() if enabled else None
    state.permanent_mute_set_by = user if enabled else None
    state.save(
        update_fields=(
            "permanent_mute_enabled",
            "permanent_mute_set_at",
            "permanent_mute_set_by",
            "updated_at",
        )
    )
    return state


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


def _sync_media(post: OriginalPost, base: dict[str, Any]) -> None:
    """Keep stable media rows so unchanged video extraction history survives a refresh."""
    desired: dict[tuple[str, str], dict[str, Any]] = {}
    for image in base.get("image_list", []):
        if isinstance(image, dict) and image.get("url"):
            desired[(str(image["url"]), MediaType.IMAGE)] = {
                "width": image.get("width"),
                "height": image.get("height"),
                "is_gif": bool(image.get("is_gif", False)),
            }
    for video in base.get("video_info", []):
        if isinstance(video, dict) and video.get("url"):
            desired[(str(video["url"]), MediaType.VIDEO)] = {
                "width": video.get("width"),
                "height": video.get("height"),
                "is_gif": False,
            }
    current = {(media.url, media.media_type): media for media in post.media.all()}
    stale_ids = [media.pk for key, media in current.items() if key not in desired]
    if stale_ids:
        PostMedia.objects.filter(pk__in=stale_ids).delete()
    for key, values in desired.items():
        media = current.get(key)
        if media is None:
            PostMedia.objects.create(post=post, url=key[0], media_type=key[1], **values)
            continue
        changed = [field for field, value in values.items() if getattr(media, field) != value]
        if changed:
            for field in changed:
                setattr(media, field, values[field])
            media.save(update_fields=changed)


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
    _sync_media(post, base)
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
def store_post_metrics(
    post: OriginalPost, payload: dict[str, Any], *, source_requested_at=None
) -> InteractionSnapshot:
    """Persist post-level counters and the upstream status for status-aware refreshes."""
    snapshot = store_post_interaction_metrics(post, payload)
    topic = payload["data"]["topic"]
    if topic.get("status") is not None:
        post.status = normalize_post_status(topic["status"])
        post.save(update_fields=["status", "updated_at"])
    sync_author_mute_status(post, payload, source_requested_at=source_requested_at)
    return snapshot


def refresh_post_status_and_metrics(client, post: OriginalPost) -> OriginalPost:
    """Refresh one dashboard post through the shared source refresh boundary.

    This compatibility entry point intentionally delegates to the same detail-first
    flow used by status sync and filtered refresh.  In particular, an explicit
    deleted/unreadable result may read the saved author identity once; all other
    source failures continue to propagate to the caller.
    """
    refresh_one_post_from_source(
        client,
        post,
        source_appid=post.source_appid,
        fallback_author_openids=set(),
    )
    return post
