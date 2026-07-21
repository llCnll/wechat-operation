"""Validated projections and persistence for game-circle aggregate metric responses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import isfinite
from typing import Any

from django.db import transaction
from django.utils import timezone

from jobs.adapters import SourceContractError, SourceTransportError

from .models import GameCircleMetricSnapshot, MetricPublicationState, MetricSource
from .weekly_publication import classify_publication


class MetricPermissionError(SourceContractError):
    """The authenticated operator is not entitled to a metric domain."""


class MetricBusinessRetryableError(SourceTransportError):
    """An unclassified upstream business result that may be transient."""


@dataclass(frozen=True)
class MetricRecord:
    period_start: date
    period_end: date
    metrics: dict[str, int | float]


@dataclass(frozen=True)
class ProviderMetricPage:
    records: tuple[dict[str, Any], ...]
    total_count: int | None


CIRCLE_FANS_METRICS = frozenset(("total_user", "add_user", "del_user"))
GROUP_CONSUME_METRICS = frozenset(("daily_active_user", "content_consume_cnt"))
GROUP_PUBLISH_METRICS = frozenset(("publish_pv", "publish_uv"))
GROUP_SHARE_METRICS = frozenset(("share_cnt", "share_uv"))
GROUP_RETAIN_METRICS = frozenset(("quanzi_dau",))
OPERATION_EFFECT_METRICS = frozenset(
    (
        "avg_quanzi_dau_cur_week",
        "avg_quannzi_cross_game_uv_cur_week",
        "res_expose_cover_new_uv_cur_week",
        "res_expose_cover_back_uv_cur_week",
        "res_expose_cover_deposit_uv_cur_week",
        "avg_publish_uv_rate_cur_week",
        "avg_quality_publish_pv_rate_cur_week",
        "avg_content_expose_cnt_cur_week",
        "friendfeeds_expose_cnt_cur_week",
        "friendfeeds_click_cnt_cur_week",
    )
)
METRIC_WHITELISTS: dict[MetricSource, frozenset[str]] = {
    MetricSource.CIRCLE_FANS_DAILY: CIRCLE_FANS_METRICS,
    MetricSource.GROUP_CONSUME_DAILY: GROUP_CONSUME_METRICS,
    MetricSource.GROUP_PUBLISH_DAILY: GROUP_PUBLISH_METRICS,
    MetricSource.GROUP_SHARE_DAILY: GROUP_SHARE_METRICS,
    MetricSource.GROUP_RETAIN_DAILY: GROUP_RETAIN_METRICS,
    MetricSource.OPERATION_EFFECT_WEEKLY: OPERATION_EFFECT_METRICS,
}
REQUIRED_METRICS: dict[MetricSource, frozenset[str]] = {
    MetricSource.CIRCLE_FANS_DAILY: CIRCLE_FANS_METRICS,
    MetricSource.GROUP_CONSUME_DAILY: GROUP_CONSUME_METRICS,
    MetricSource.GROUP_PUBLISH_DAILY: GROUP_PUBLISH_METRICS,
    MetricSource.GROUP_SHARE_DAILY: GROUP_SHARE_METRICS,
    MetricSource.GROUP_RETAIN_DAILY: GROUP_RETAIN_METRICS,
}


def provider_metric_page(payload: object) -> ProviderMetricPage:
    """Validate the shared provider envelope without retaining the raw response."""
    if not isinstance(payload, dict):
        raise SourceContractError("指标接口响应结构无效")
    errcode = payload.get("errcode")
    if errcode != 0:
        if errcode == -10000401:
            raise MetricPermissionError("当前账号无权读取该指标")
        raise MetricBusinessRetryableError("指标接口返回未知业务错误")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SourceContractError("指标接口缺少 data")
    records = data.get("list")
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise SourceContractError("指标接口缺少有效 data.list")
    total_count = data.get("total_count")
    if total_count is not None:
        if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
            raise SourceContractError("指标接口 total_count 无效")
    return ProviderMetricPage(records=tuple(records), total_count=total_count)


def metric_page_has_more(provider_page: ProviderMetricPage, *, page: int, page_size: int) -> bool:
    if page < 0 or page_size <= 0:
        raise SourceContractError("指标分页参数无效")
    count = len(provider_page.records)
    if provider_page.total_count is None or provider_page.total_count < count:
        return count == page_size
    before = page * page_size
    after = before + count
    if after < provider_page.total_count and count != page_size:
        # 内容分析接口实测可能在有记录时仍给 total_count=0，或给出与请求范围
        # 不一致的统计值。此时退回仅依据当前页是否已满的安全分页规则。
        return count == page_size
    return after < provider_page.total_count


def _record_date(value: object) -> date:
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        raise SourceContractError("指标记录缺少业务日期")
    try:
        normalized = value.strip()
        if len(normalized) == 8 and normalized.isdigit():
            return datetime.strptime(normalized, "%Y%m%d").date()
        return date.fromisoformat(normalized)
    except ValueError as exc:
        raise SourceContractError("指标记录业务日期无效") from exc


def _numeric_metrics(record: dict[str, Any], whitelist: frozenset[str]) -> dict[str, int | float]:
    metrics: dict[str, int | float] = {}
    for key in whitelist:
        value = record.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int | float):
            number = value
        elif isinstance(value, str):
            try:
                number = float(value) if "." in value else int(value)
            except ValueError:
                continue
        else:
            continue
        if isinstance(number, float) and not isfinite(number):
            continue
        metrics[key] = number
    return metrics


def parse_source_records(payload: object, *, source: MetricSource) -> list[MetricRecord]:
    provider_page = provider_metric_page(payload)
    try:
        whitelist = METRIC_WHITELISTS[source]
    except KeyError as exc:
        raise SourceContractError("该指标来源当前不可读取") from exc
    required = REQUIRED_METRICS.get(source, frozenset())
    parsed: list[MetricRecord] = []
    for item in provider_page.records:
        metrics = _numeric_metrics(item, whitelist)
        if not required.issubset(metrics):
            raise SourceContractError(f"{source.label}记录缺少必需指标字段")
        business_day = _record_date(item.get("ds"))
        period_end = (
            business_day + timedelta(days=6)
            if source == MetricSource.OPERATION_EFFECT_WEEKLY
            else business_day
        )
        parsed.append(MetricRecord(business_day, period_end, metrics))
    return sorted(parsed, key=lambda record: record.period_start)


def upsert_metric_snapshot(
    *, appid: str, source: MetricSource, record: MetricRecord, job: Any
) -> GameCircleMetricSnapshot:
    """Persist a source-whitelisted DTO through the immutable business key."""
    if appid != "wx6d2454a8afcd9706":
        raise SourceContractError("指标快照仅支持城主别慌张")
    try:
        whitelist = METRIC_WHITELISTS[source]
    except KeyError as exc:
        raise SourceContractError("该指标来源当前不可保存") from exc
    metrics = {key: value for key, value in record.metrics.items() if key in whitelist}
    if not metrics:
        raise SourceContractError("指标快照没有可保存的白名单字段")
    decision = classify_publication(source=source, metrics=metrics)
    identity = {
        "appid": appid,
        "source": source,
        "period_start": record.period_start,
        "period_end": record.period_end,
        "scope": "ALL",
    }
    with transaction.atomic():
        existing = (
            GameCircleMetricSnapshot.objects.select_for_update().filter(**identity).first()
        )
        if (
            not decision.replaces_verified_snapshot
            and existing is not None
            and existing.publication_state == MetricPublicationState.VERIFIED
        ):
            return existing
        if existing is None:
            return GameCircleMetricSnapshot.objects.create(
                **identity,
                metrics=metrics,
                publication_state=decision.state,
                captured_at=timezone.now(),
                latest_job=job,
            )
        existing.metrics = metrics
        existing.publication_state = decision.state
        existing.captured_at = timezone.now()
        existing.latest_job = job
        existing.save(
            update_fields=(
                "metrics",
                "publication_state",
                "captured_at",
                "latest_job",
                "updated_at",
            )
        )
        return existing
