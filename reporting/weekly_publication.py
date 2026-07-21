"""Publication policy for upstream weekly-operation observations.

The upstream endpoint can acknowledge a request before its weekly aggregate has
settled.  This module is the single owner of that distinction; synchronisation,
scheduling and presentation consume its explicit policy instead of inferring it
from one another.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .models import MetricPublicationState, MetricSource

WEEKLY_OPERATION_CORE_METRICS = frozenset(
    (
        "avg_quanzi_dau_cur_week",
        "avg_quannzi_cross_game_uv_cur_week",
        "res_expose_cover_new_uv_cur_week",
        "res_expose_cover_back_uv_cur_week",
        "res_expose_cover_deposit_uv_cur_week",
        "avg_publish_uv_rate_cur_week",
        "avg_quality_publish_pv_rate_cur_week",
        "avg_content_expose_cnt_cur_week",
    )
)


@dataclass(frozen=True)
class PublicationDecision:
    state: MetricPublicationState
    replaces_verified_snapshot: bool


def classify_publication(
    *, source: MetricSource, metrics: Mapping[str, int | float]
) -> PublicationDecision:
    """Classify a source observation without retaining its raw response."""
    if source != MetricSource.OPERATION_EFFECT_WEEKLY:
        return PublicationDecision(MetricPublicationState.VERIFIED, True)
    values = tuple(metrics.get(key) for key in WEEKLY_OPERATION_CORE_METRICS)
    is_all_zero = len(values) == len(WEEKLY_OPERATION_CORE_METRICS) and all(
        isinstance(value, int | float) and not isinstance(value, bool) and value == 0
        for value in values
    )
    if is_all_zero:
        return PublicationDecision(MetricPublicationState.PENDING_RECHECK, False)
    return PublicationDecision(MetricPublicationState.VERIFIED, True)


DISPLAYABLE_STATES = (MetricPublicationState.VERIFIED,)
