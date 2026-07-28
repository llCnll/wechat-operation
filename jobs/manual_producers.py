"""Server-side allow-list for one-off runs of fixed automatic producers.

This module intentionally creates only domain jobs.  It has no Scheduler-slot,
CDP, or upstream-client dependency, so a manual run cannot mutate automation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from jobs.game_circle_keepalive import enqueue_keepalive
from jobs.permanent_mute import enqueue_permanent_mute_renewal
from posts.ingestion_jobs import (
    enqueue_manual_current_ingestion_window,
    enqueue_manual_historical_ingestion_window,
)
from reporting.metric_jobs import (
    enqueue_manual_daily_metric_range,
    enqueue_manual_weekly_metric_range,
)


@dataclass(frozen=True)
class ManualProducerRun:
    producer_key: str
    jobs: tuple[object, ...]
    created: tuple[bool, ...]
    requested_range: tuple[date, date] | None = None
    actual_range: tuple[date, date] | None = None


MANUAL_PRODUCER_KEYS = frozenset(
    {
        "post_ingestion",
        "metric_daily",
        "metric_weekly",
        "session_keepalive",
        "permanent_mute_renewal",
    }
)


def _date(value: object, *, label: str) -> date:
    if not isinstance(value, str) or not value:
        raise ValueError(f"请填写{label}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label}格式无效") from exc


def run_manual_producer(*, producer_key: str, values: dict[str, object]) -> ManualProducerRun:
    """Validate a fixed operator form and enqueue its actual business job(s)."""
    if producer_key not in MANUAL_PRODUCER_KEYS:
        raise ValueError("不支持该自动任务生产者的立即执行")
    if producer_key in {"metric_daily", "metric_weekly"}:
        start_day = _date(values.get("start_date"), label="开始日期")
        end_day = _date(values.get("end_date"), label="结束日期")
        if producer_key == "metric_daily":
            results = enqueue_manual_daily_metric_range(start_day=start_day, end_day=end_day)
            return ManualProducerRun(
                producer_key=producer_key,
                jobs=tuple(item.job for item in results),
                created=tuple(item.created for item in results),
                requested_range=(start_day, end_day),
                actual_range=(start_day, end_day),
            )
        result, actual_start, actual_end = enqueue_manual_weekly_metric_range(
            start_day=start_day, end_day=end_day
        )
        return ManualProducerRun(
            producer_key=producer_key,
            jobs=(result.job,),
            created=(result.created,),
            requested_range=(start_day, end_day),
            actual_range=(actual_start, actual_end),
        )
    if producer_key == "post_ingestion":
        if "appid" in values:
            raise ValueError("历史补采使用当前操作游戏，不接受 AppID 参数")
        mode = values.get("mode", "current")
        if mode == "history":
            result = enqueue_manual_historical_ingestion_window(hours=values.get("hours"))
        elif mode == "current":
            result = enqueue_manual_current_ingestion_window()
        else:
            raise ValueError("帖子采集执行模式无效")
    elif producer_key == "session_keepalive":
        result = enqueue_keepalive(trigger="manual")
    else:
        result = enqueue_permanent_mute_renewal(source="manual")
    return ManualProducerRun(producer_key=producer_key, jobs=(result.job,), created=(result.created,))
