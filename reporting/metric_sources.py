"""One registry maps enabled metric sources to a read-only upstream request and parser."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jobs.adapters import SourceContractError

from .metric_sync import (
    MetricRecord,
    metric_page_has_more,
    parse_source_records,
    provider_metric_page,
)
from .models import MetricSource
from .source_client import CdpGameCircleMetricClient

METRIC_PAGE_SIZE = 366
ENABLED_DAILY_SOURCES = (
    MetricSource.CIRCLE_FANS_DAILY,
    MetricSource.GROUP_CONSUME_DAILY,
    MetricSource.GROUP_PUBLISH_DAILY,
    MetricSource.GROUP_SHARE_DAILY,
    MetricSource.GROUP_RETAIN_DAILY,
)


@dataclass(frozen=True)
class MetricPage:
    records: tuple[MetricRecord, ...]
    has_more: bool


@dataclass(frozen=True)
class MetricSourceDefinition:
    read: Callable[[CdpGameCircleMetricClient, int, int, int, int], dict[str, Any]]


def _circle_fans(
    client: CdpGameCircleMetricClient,
    start_ds: int,
    end_ds: int,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    return client.circle_fans(
        start_ds=start_ds,
        end_ds=end_ds,
        cur_page=page,
        per_page=page_size,
    )


def _group_reader(
    data_flag: str,
) -> Callable[[CdpGameCircleMetricClient, int, int, int, int], dict[str, Any]]:
    def read(
        client: CdpGameCircleMetricClient,
        start_ds: int,
        end_ds: int,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        return client.group_content_analysis(
            start_ds=start_ds,
            end_ds=end_ds,
            data_flag=data_flag,
            cur_page=page,
            per_page=page_size,
        )

    return read


def _operation_effect(
    client: CdpGameCircleMetricClient,
    start_ds: int,
    end_ds: int,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    return client.operation_effect(
        ds_begin=start_ds,
        ds_end=end_ds,
        cur_page=page,
        per_page=page_size,
    )


METRIC_SOURCE_REGISTRY: dict[MetricSource, MetricSourceDefinition] = {
    MetricSource.CIRCLE_FANS_DAILY: MetricSourceDefinition(_circle_fans),
    MetricSource.GROUP_CONSUME_DAILY: MetricSourceDefinition(
        _group_reader("need_content_consume_data")
    ),
    MetricSource.GROUP_PUBLISH_DAILY: MetricSourceDefinition(
        _group_reader("need_content_publish_data")
    ),
    MetricSource.GROUP_SHARE_DAILY: MetricSourceDefinition(
        _group_reader("need_content_share_data")
    ),
    MetricSource.GROUP_RETAIN_DAILY: MetricSourceDefinition(
        _group_reader("need_content_retain_data")
    ),
    MetricSource.OPERATION_EFFECT_WEEKLY: MetricSourceDefinition(_operation_effect),
}


def read_metric_page(
    client: CdpGameCircleMetricClient,
    *,
    source: MetricSource,
    start_ds: int,
    end_ds: int,
    page: int,
    page_size: int = METRIC_PAGE_SIZE,
) -> MetricPage:
    try:
        definition = METRIC_SOURCE_REGISTRY[source]
    except KeyError as exc:
        raise SourceContractError("该指标来源未启用") from exc
    payload = definition.read(client, start_ds, end_ds, page, page_size)
    provider_page = provider_metric_page(payload)
    return MetricPage(
        records=tuple(parse_source_records(payload, source=source)),
        has_more=metric_page_has_more(provider_page, page=page, page_size=page_size),
    )
