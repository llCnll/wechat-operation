"""Shared, server-side whitelist filters for the original-post dashboard and jobs."""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import F, Q, Value

from reviews.models import ReviewStatus

from .content import normalize_topic_label
from .models import (
    POST_STATUS_BLOCKED,
    POST_STATUS_DELETED,
    POST_STATUS_NORMAL,
    POST_STATUS_SELF_VISIBLE,
    POST_STATUS_SUNK,
)
from .topic_filters import filter_posts_with_topic


POST_STATUS_FILTER_FLAGS = {
    "BLOCKED": POST_STATUS_BLOCKED,
    "SUNK": POST_STATUS_SUNK,
    "SELF_VISIBLE": POST_STATUS_SELF_VISIBLE,
    "DELETED": POST_STATUS_DELETED,
}


@dataclass(frozen=True)
class OriginalPostFilter:
    search: str = ""
    post_id: int | None = None
    topic_label: str = ""
    has_topics: bool = False
    category: tuple[str, ...] = ()
    risk: tuple[str, ...] = ()
    suggested_action: tuple[str, ...] = ()
    review_status: tuple[str, ...] = ()
    post_status: tuple[str, ...] = ()
    reviewed: str = ""
    min_confidence: float | None = None
    min_views: float | None = None

    def as_parameters(self) -> dict[str, object]:
        return {
            "search": self.search,
            "post_id": self.post_id,
            "topic_label": self.topic_label,
            "has_topics": self.has_topics,
            "category": list(self.category),
            "risk": list(self.risk),
            "suggested_action": list(self.suggested_action),
            "review_status": list(self.review_status),
            "post_status": list(self.post_status),
            "reviewed": self.reviewed,
            "min_confidence": self.min_confidence,
            "min_views": self.min_views,
        }


def parse_original_post_filter(values) -> OriginalPostFilter:
    """Accept only the list's filter fields; ordering and selection never enter jobs."""
    def items(name: str) -> tuple[str, ...]:
        return tuple(sorted({value for value in values.getlist(name) if value}))

    def number(name: str) -> float | None:
        raw = values.get(name, "").strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"筛选条件 {name} 无效") from exc

    def positive_integer(name: str) -> int | None:
        raw = values.get(name, "").strip()
        if not raw:
            return None
        if not raw.isdecimal():
            raise ValueError(f"筛选条件 {name} 无效")
        value = int(raw)
        if value <= 0:
            raise ValueError(f"筛选条件 {name} 无效")
        return value

    reviewed = values.get("reviewed", "").strip()
    if reviewed not in {"", "yes", "no"}:
        raise ValueError("筛选条件 reviewed 无效")
    has_topics = values.get("has_topics", "").strip()
    if has_topics not in {"", "yes"}:
        raise ValueError("筛选条件 has_topics 无效")
    return OriginalPostFilter(
        search=values.get("q", "").strip()[:200],
        post_id=positive_integer("post_id"),
        topic_label=normalize_topic_label(values.get("topic_label", "")),
        has_topics=has_topics == "yes",
        category=items("category"),
        risk=items("risk"),
        suggested_action=items("suggested_action"),
        review_status=items("review_status"),
        post_status=items("post_status"),
        reviewed=reviewed,
        min_confidence=number("min_confidence"),
        min_views=number("min_views"),
    )


def original_post_filter_from_parameters(parameters: object) -> OriginalPostFilter:
    """Rebuild a stored normalized predicate without accepting arbitrary fields."""
    if not isinstance(parameters, dict):
        raise ValueError("筛选刷新任务参数无效")

    def sequence(name: str) -> tuple[str, ...]:
        value = parameters.get(name, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("筛选刷新任务参数无效")
        return tuple(sorted(set(value)))

    def numeric(name: str) -> float | None:
        value = parameters.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("筛选刷新任务参数无效")
        return float(value)

    search = parameters.get("search", "")
    post_id = parameters.get("post_id")
    topic_label = parameters.get("topic_label", "")
    has_topics = parameters.get("has_topics", False)
    reviewed = parameters.get("reviewed", "")
    if (
        not isinstance(search, str)
        or (post_id is not None and (isinstance(post_id, bool) or not isinstance(post_id, int) or post_id <= 0))
        or not isinstance(topic_label, str)
        or not isinstance(has_topics, bool)
        or not isinstance(reviewed, str)
        or reviewed not in {"", "yes", "no"}
    ):
        raise ValueError("筛选刷新任务参数无效")
    return OriginalPostFilter(
        search=search,
        post_id=post_id,
        topic_label=normalize_topic_label(topic_label),
        has_topics=has_topics,
        category=sequence("category"),
        risk=sequence("risk"),
        suggested_action=sequence("suggested_action"),
        review_status=sequence("review_status"),
        post_status=sequence("post_status"),
        reviewed=reviewed,
        min_confidence=numeric("min_confidence"),
        min_views=numeric("min_views"),
    )


def filter_original_posts(queryset, *, source_appid: str | None, filters: OriginalPostFilter):
    """Apply the same semantic predicate to a dashboard queryset or a Worker queryset."""
    # The queue stays readable before an operator selects the first game.  All
    # source-touching jobs pass a concrete AppID, so they always remain single
    # game even though this read-only fallback exists.
    posts = queryset.filter(source_appid=source_appid) if source_appid else queryset
    if filters.search:
        posts = posts.filter(
            Q(title__icontains=filters.search)
            | Q(content_text__icontains=filters.search)
            | Q(ballot_text__icontains=filters.search)
        )
    if filters.post_id is not None:
        posts = posts.filter(topic_id=filters.post_id)
    if filters.has_topics:
        posts = posts.exclude(topics=[])
    if filters.topic_label:
        posts = filter_posts_with_topic(posts, filters.topic_label)
    if filters.category:
        posts = posts.filter(review_task__assessment__category__in=filters.category)
    if filters.risk:
        posts = posts.filter(review_task__assessment__risk_evidence__label__in=filters.risk)
    if filters.suggested_action:
        posts = posts.filter(review_task__suggested_action__in=filters.suggested_action)
    if filters.review_status:
        posts = posts.filter(review_task__status__in=filters.review_status)
    else:
        posts = posts.exclude(review_task__status=ReviewStatus.AUTO_EXECUTED_PENDING_REVIEW)
    if filters.post_status:
        selected = set(filters.post_status)
        status_query = Q()
        if "NORMAL" in selected:
            status_query |= Q(status=POST_STATUS_NORMAL)
        if "UNSYNCED" in selected:
            status_query |= Q(status__isnull=True)
        flags = sum(flag for name, flag in POST_STATUS_FILTER_FLAGS.items() if name in selected)
        if flags:
            posts = posts.annotate(_selected_status_flags=F("status").bitand(Value(flags)))
            status_query |= Q(_selected_status_flags__gt=0)
        posts = posts.filter(status_query)
    if filters.reviewed == "yes":
        posts = posts.filter(review_task__reviewed_at__isnull=False)
    elif filters.reviewed == "no":
        posts = posts.filter(review_task__reviewed_at__isnull=True)
    if filters.min_confidence is not None:
        posts = posts.filter(review_task__assessment__overall_confidence__gte=filters.min_confidence)
    if filters.min_views is not None:
        posts = posts.filter(latest_view_count__gte=filters.min_views)
    return posts.distinct()
