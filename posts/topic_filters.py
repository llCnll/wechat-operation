"""Exact, local topic predicates for the SQLite-backed operations dashboard."""

from __future__ import annotations

from django.db.models import BooleanField, QuerySet
from django.db.models.expressions import RawSQL

from .content import normalize_topic_label
from .models import OriginalPost


def filter_posts_with_topic(queryset: QuerySet[OriginalPost], topic: str) -> QuerySet[OriginalPost]:
    """Return posts containing one normalized topic label exactly.

    Topics are a JSON array because their source order is meaningful for the
    post projection.  SQLite does not support Django's JSON ``contains``
    lookup, so keep its JSON1 membership query in this single local boundary
    instead of falling back to an imprecise text search.
    """
    normalized_topic = normalize_topic_label(topic)
    if not normalized_topic:
        return queryset
    return queryset.filter(
        RawSQL(
            "EXISTS (SELECT 1 FROM json_each(posts_originalpost.topics) "
            "AS post_topic WHERE post_topic.value = %s)",
            [normalized_topic],
            output_field=BooleanField(),
        )
    )
