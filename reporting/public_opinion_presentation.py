"""Presentation-only projections for immutable public-opinion report evidence."""

from __future__ import annotations

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import MetricSource

DAILY_METRIC_LABELS = {
    "add_user": "新增用户",
    "del_user": "流失用户",
    "total_user": "粉丝总量",
    "daily_active_user": "日活用户",
    "content_consume_cnt": "内容消费",
    "publish_pv": "发帖次数",
    "publish_uv": "发帖用户",
    "share_cnt": "分享次数",
    "share_uv": "分享用户",
    "quanzi_dau": "游戏圈日活",
}


def format_metric_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def metric_items(metrics: object) -> list[dict[str, object]]:
    """Expose a stable, readable metric projection without changing its evidence."""
    if not isinstance(metrics, dict):
        return []
    ordered_keys = [
        *[key for key in DAILY_METRIC_LABELS if key in metrics],
        *sorted(key for key in metrics if key not in DAILY_METRIC_LABELS),
    ]
    return [
        {
            "key": key,
            "label": DAILY_METRIC_LABELS.get(key, key),
            "value": format_metric_value(metrics[key]),
            "numeric_value": (
                metrics[key]
                if isinstance(metrics[key], int | float) and not isinstance(metrics[key], bool)
                else None
            ),
        }
        for key in ordered_keys
    ]


def present_daily_metrics(items: object) -> list[dict[str, object]]:
    """Decorate stored daily-metric evidence for views and exports only."""
    rows: list[dict[str, object]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "")
        try:
            source_label = MetricSource(source).label
        except ValueError:
            source_label = source or "未知来源"
        captured_at = parse_datetime(str(item.get("captured_at") or ""))
        rows.append(
            {
                **item,
                "source_label": source_label,
                "metric_items": metric_items(item.get("metrics")),
                "captured_at_label": (
                    timezone.localtime(captured_at).strftime("%Y-%m-%d %H:%M")
                    if captured_at and timezone.is_aware(captured_at)
                    else str(item.get("captured_at") or "")
                ),
            }
        )
    return rows


def summarize_daily_metrics(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Summarize comparable values strictly within one source and raw metric field."""
    sources: dict[str, dict[str, object]] = {}
    for row in rows:
        source = str(row.get("source") or "")
        summary = sources.setdefault(
            source,
            {"source_label": row["source_label"], "days": set(), "metrics": {}},
        )
        day = str(row.get("day") or "")
        if day:
            summary["days"].add(day)
        for metric in row["metric_items"]:
            if metric["numeric_value"] is not None and day:
                summary["metrics"].setdefault(
                    metric["key"], {"label": metric["label"], "points": []}
                )["points"].append((day, metric["numeric_value"]))

    summaries: list[dict[str, object]] = []
    for source in sources.values():
        metrics: list[dict[str, object]] = []
        for metric in source["metrics"].values():
            points = sorted(metric["points"])
            first_day, first_value = points[0]
            last_day, last_value = points[-1]
            delta = last_value - first_value
            rate = None if first_value == 0 else delta / first_value
            metrics.append(
                {
                    "label": metric["label"],
                    "first_day": first_day,
                    "last_day": last_day,
                    "first_value": format_metric_value(first_value),
                    "last_value": format_metric_value(last_value),
                    "delta": f"{delta:+,}" if isinstance(delta, int) else f"{delta:+.2f}",
                    "rate": f"{rate:+.1%}" if rate is not None else "基值为 0",
                }
            )
        summaries.append(
            {
                "source_label": source["source_label"],
                "coverage_days": len(source["days"]),
                "metrics": metrics,
            }
        )
    return summaries
