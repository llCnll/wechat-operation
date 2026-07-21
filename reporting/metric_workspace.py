"""Read model for the operator-facing game-circle metric workspace."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from django.db.models import QuerySet

from .metric_sources import ENABLED_DAILY_SOURCES
from .models import GameCircleMetricSnapshot, MetricPublicationState, MetricSource
from .weekly_publication import DISPLAYABLE_STATES


class WorkspaceView(StrEnum):
    CHART = "chart"
    TABLE = "table"


@dataclass(frozen=True)
class WorkspacePreferences:
    fans_view: WorkspaceView = WorkspaceView.CHART
    daily_view: WorkspaceView = WorkspaceView.CHART
    daily_metrics: tuple[str, ...] = ("consume",)
    daily_scale: str = "actual"

    @classmethod
    def from_query(
        cls, *, fans_view: str, daily_view: str, daily_metrics: str, daily_scale: str
    ) -> WorkspacePreferences:
        return cls(
            fans_view=_workspace_view(fans_view),
            daily_view=_workspace_view(daily_view),
            daily_metrics=_daily_metric_keys(daily_metrics),
            daily_scale=daily_scale if daily_scale in {"actual", "index"} else "actual",
        )


@dataclass(frozen=True)
class DailyMetricDefinition:
    key: str
    label: str
    source: MetricSource
    metric: str
    color: str


DAILY_METRICS = {
    "fans": DailyMetricDefinition(
        "fans", "游戏圈粉丝", MetricSource.CIRCLE_FANS_DAILY, "total_user", "#16756b"
    ),
    "publish": DailyMetricDefinition(
        "publish", "内容创作", MetricSource.GROUP_PUBLISH_DAILY, "publish_pv", "#8061b4"
    ),
    "consume": DailyMetricDefinition(
        "consume", "内容消费", MetricSource.GROUP_CONSUME_DAILY, "content_consume_cnt", "#ca7b32"
    ),
    "share": DailyMetricDefinition(
        "share", "内容分享", MetricSource.GROUP_SHARE_DAILY, "share_cnt", "#447aa8"
    ),
    "active": DailyMetricDefinition(
        "active", "游戏圈活跃用户", MetricSource.GROUP_RETAIN_DAILY, "quanzi_dau", "#bf5965"
    ),
}
SIGNIFICANT_DECLINE_RATE = 0.05


@dataclass(frozen=True)
class MetricCard:
    label: str
    value: str
    detail: str
    comparison: str | None
    business_day: date | None
    tone: str = ""


@dataclass(frozen=True)
class ChartPoint:
    day: date
    x: int
    y: int
    value: float
    value_label: str
    tooltip: str


@dataclass(frozen=True)
class MetricChart:
    label: str
    color: str
    points: tuple[ChartPoint, ...]
    paths: tuple[str, ...]
    minimum: str
    maximum: str


@dataclass(frozen=True)
class DailyChartSeries:
    definition: DailyMetricDefinition
    points: tuple[ChartPoint, ...]
    paths: tuple[str, ...]


@dataclass(frozen=True)
class DailyComparisonChart:
    series: tuple[DailyChartSeries, ...]
    minimum: str
    maximum: str
    scale_label: str


@dataclass(frozen=True)
class MetricWorkspace:
    preferences: WorkspacePreferences
    complete_business_day: date | None
    daily_status: str
    p0_cards: tuple[MetricCard, ...]
    primary_cards: tuple[MetricCard, ...]
    secondary_cards: tuple[MetricCard, ...]
    fan_chart: MetricChart
    daily_chart: DailyComparisonChart
    daily_metric_options: tuple[DailyMetricDefinition, ...]
    daily_rows: tuple[dict[str, object], ...]
    chart_ticks: tuple[date, ...]
    daily_business_days: int
    weekly_cards: tuple[MetricCard, ...]
    weekly_rows: tuple[dict[str, object], ...]
    weekly_status: str
    alerts: tuple[str, ...]
    primary_alert: str | None
    remaining_alert_count: int
    latest_captured_at: datetime | None


def _workspace_view(value: str) -> WorkspaceView:
    try:
        return WorkspaceView(value)
    except ValueError:
        return WorkspaceView.CHART


def _daily_metric_keys(value: str) -> tuple[str, ...]:
    keys = tuple(key for key in value.split(",") if key in DAILY_METRICS)
    return keys or ("consume",)


def _number(value: object, *, percent: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "—"
    return f"{value * 100:.2f}%" if percent else f"{value:,.0f}"


def _metric_value(snapshot: GameCircleMetricSnapshot | None, key: str) -> object:
    return snapshot.metrics.get(key) if snapshot is not None else None


def _source_snapshot_maps(
    *, appid: str, start_day: date, end_day: date
) -> dict[MetricSource, dict[date, GameCircleMetricSnapshot]]:
    snapshots = GameCircleMetricSnapshot.objects.filter(
        appid=appid,
        source__in=ENABLED_DAILY_SOURCES,
        period_start__gte=start_day,
        period_start__lte=end_day,
    ).order_by("source", "period_start", "captured_at", "pk")
    result = {source: {} for source in ENABLED_DAILY_SOURCES}
    for snapshot in snapshots:
        result[MetricSource(snapshot.source)][snapshot.period_start] = snapshot
    return result


def _latest_snapshot(
    snapshots: dict[date, GameCircleMetricSnapshot],
) -> GameCircleMetricSnapshot | None:
    return max(snapshots.values(), key=lambda item: item.period_start, default=None)


def _comparison(current: object, previous: object, *, percent: bool = False) -> str | None:
    if not isinstance(current, int | float) or not isinstance(previous, int | float):
        return None
    delta = current - previous
    if percent:
        return f"较上一完整日 {delta * 100:+.2f}%"
    return f"较上一完整日 {delta:+,.0f}"


def _daily_cards(
    *,
    selected: dict[MetricSource, GameCircleMetricSnapshot | None],
    previous: dict[MetricSource, GameCircleMetricSnapshot | None],
) -> tuple[MetricCard, ...]:
    fans = selected[MetricSource.CIRCLE_FANS_DAILY]
    previous_fans = previous[MetricSource.CIRCLE_FANS_DAILY]
    published = selected[MetricSource.GROUP_PUBLISH_DAILY]
    consumed = selected[MetricSource.GROUP_CONSUME_DAILY]
    shared = selected[MetricSource.GROUP_SHARE_DAILY]
    active = selected[MetricSource.GROUP_RETAIN_DAILY]
    add, removed = _metric_value(fans, "add_user"), _metric_value(fans, "del_user")
    net = add - removed if isinstance(add, int | float) and isinstance(removed, int | float) else None
    fan_total = _metric_value(fans, "total_user")
    return (
        MetricCard(
            "游戏圈粉丝",
            _number(fan_total),
            f"新增 {_number(add)} · 退圈 {_number(removed)} · 净增 {_number(net)}",
            _comparison(fan_total, _metric_value(previous_fans, "total_user")),
            fans.period_start if fans else None,
            "positive" if isinstance(net, int | float) and net >= 0 else "negative",
        ),
        _card_for_source(
            "内容创作", published, previous[MetricSource.GROUP_PUBLISH_DAILY], "publish_pv", "创作人数", "publish_uv"
        ),
        _card_for_source(
            "内容消费", consumed, previous[MetricSource.GROUP_CONSUME_DAILY], "daily_active_user", "浏览量", "content_consume_cnt"
        ),
        _card_for_source(
            "内容分享", shared, previous[MetricSource.GROUP_SHARE_DAILY], "share_cnt", "分享人数", "share_uv"
        ),
        _card_for_source(
            "游戏圈活跃用户", active, previous[MetricSource.GROUP_RETAIN_DAILY], "quanzi_dau", "活跃用户值，非留存率", None
        ),
    )


def _period_total(
    snapshots: dict[date, GameCircleMetricSnapshot], metric: str
) -> float:
    return sum(
        float(value)
        for snapshot in snapshots.values()
        if isinstance((value := _metric_value(snapshot, metric)), int | float)
    )


def _period_cards(
    by_source: dict[MetricSource, dict[date, GameCircleMetricSnapshot]],
) -> tuple[MetricCard, ...]:
    fans = by_source[MetricSource.CIRCLE_FANS_DAILY]
    latest_fans = _latest_snapshot(fans)
    added = _period_total(fans, "add_user")
    removed = _period_total(fans, "del_user")
    published = by_source[MetricSource.GROUP_PUBLISH_DAILY]
    consumed = by_source[MetricSource.GROUP_CONSUME_DAILY]
    shared = by_source[MetricSource.GROUP_SHARE_DAILY]
    active = by_source[MetricSource.GROUP_RETAIN_DAILY]
    active_values = [
        value
        for snapshot in active.values()
        if isinstance((value := _metric_value(snapshot, "quanzi_dau")), int | float)
    ]
    return (
        MetricCard(
            "期末游戏圈粉丝",
            _number(_metric_value(latest_fans, "total_user")),
            f"期间新增 {_number(added)} · 退圈 {_number(removed)} · 净增 {_number(added - removed)}",
            None,
            latest_fans.period_start if latest_fans else None,
        ),
        MetricCard(
            "累计内容创作",
            _number(_period_total(published, "publish_pv")),
            f"累计创作人数 {_number(_period_total(published, 'publish_uv'))}",
            None,
            None,
        ),
        MetricCard(
            "累计内容消费",
            _number(_period_total(consumed, "content_consume_cnt")),
            f"累计访问用户 {_number(_period_total(consumed, 'daily_active_user'))}",
            None,
            None,
        ),
        MetricCard(
            "累计内容分享",
            _number(_period_total(shared, "share_cnt")),
            f"累计分享人数 {_number(_period_total(shared, 'share_uv'))}",
            None,
            None,
        ),
        MetricCard(
            "日均游戏圈活跃用户",
            _number(sum(active_values) / len(active_values) if active_values else None),
            f"基于 {len(active_values)} 个已落库日 · 非留存率",
            None,
            None,
        ),
    )


def _card_for_source(
    label: str,
    snapshot: GameCircleMetricSnapshot | None,
    previous: GameCircleMetricSnapshot | None,
    primary_metric: str,
    detail_label: str,
    detail_metric: str | None,
) -> MetricCard:
    value = _metric_value(snapshot, primary_metric)
    detail = (
        f"{detail_label} {_number(_metric_value(snapshot, detail_metric))}"
        if detail_metric is not None
        else detail_label
    )
    return MetricCard(
        label,
        _number(value),
        detail,
        _comparison(value, _metric_value(previous, primary_metric)),
        snapshot.period_start if snapshot else None,
    )


def _chart(
    *,
    label: str,
    color: str,
    start_day: date,
    end_day: date,
    values: tuple[tuple[date, float, str, str], ...],
) -> MetricChart:
    if not values:
        return MetricChart(label, color, (), (), "—", "—")
    minimum = min(value for _, value, _, _ in values)
    maximum = max(value for _, value, _, _ in values)
    x_range = max((end_day - start_day).days, 1)
    points: list[ChartPoint] = []
    paths: list[list[str]] = [[]]
    last_day: date | None = None
    for day, value, value_label, tooltip in values:
        x = round(42 + (day - start_day).days / x_range * 638)
        y = 110 if maximum == minimum else round(28 + (maximum - value) / (maximum - minimum) * 164)
        point = ChartPoint(day, x, y, value, value_label, tooltip)
        points.append(point)
        if last_day is not None and (day - last_day).days > 1:
            paths.append([])
        paths[-1].append(f"{x},{y}")
        last_day = day
    return MetricChart(
        label,
        color,
        tuple(points),
        tuple(" ".join(path) for path in paths if path),
        _number(minimum),
        _number(maximum),
    )


def _fan_chart(
    snapshots: dict[date, GameCircleMetricSnapshot], *, start_day: date, end_day: date
) -> MetricChart:
    values: list[tuple[date, float, str, str]] = []
    for day, snapshot in sorted(snapshots.items()):
        total = _metric_value(snapshot, "total_user")
        if not isinstance(total, int | float):
            continue
        add, removed = _metric_value(snapshot, "add_user"), _metric_value(snapshot, "del_user")
        net = add - removed if isinstance(add, int | float) and isinstance(removed, int | float) else None
        values.append(
            (
                day,
                float(total),
                _number(total),
                f"{day:%m/%d} · 粉丝 {_number(total)} · 新增 {_number(add)} · 退圈 {_number(removed)} · 净增 {_number(net)}",
            )
        )
    return _chart(
        label="粉丝总量",
        color="#16756b",
        start_day=start_day,
        end_day=end_day,
        values=tuple(values),
    )


def _daily_chart(
    by_source: dict[MetricSource, dict[date, GameCircleMetricSnapshot]],
    definitions: tuple[DailyMetricDefinition, ...],
    scale: str,
    *,
    start_day: date,
    end_day: date,
) -> DailyComparisonChart:
    raw_series = []
    for definition in definitions:
        values = []
        for day, snapshot in sorted(by_source[definition.source].items()):
            value = _metric_value(snapshot, definition.metric)
            if isinstance(value, int | float):
                values.append((day, float(value), _number(value)))
        if values:
            raw_series.append((definition, tuple(values)))
    display_values = []
    for definition, values in raw_series:
        baseline = values[0][1]
        display_values.extend(value if scale == "actual" or baseline == 0 else value / baseline * 100 for _, value, _ in values)
    minimum = 0 if scale == "actual" else min(display_values, default=0)
    maximum = max(display_values, default=0)
    series = []
    for definition, values in raw_series:
        baseline = values[0][1]
        points, paths, last_day = [], [[]], None
        for day, raw_value, label in values:
            value = raw_value if scale == "actual" or baseline == 0 else raw_value / baseline * 100
            x = round(42 + (day - start_day).days / max((end_day - start_day).days, 1) * 638)
            y = 110 if maximum == minimum else round(28 + (maximum - value) / (maximum - minimum) * 164)
            points.append(ChartPoint(day, x, y, raw_value, label, f"{day:%m/%d} · {definition.label} {label}"))
            if last_day is not None and (day - last_day).days > 1:
                paths.append([])
            paths[-1].append(f"{x},{y}")
            last_day = day
        series.append(DailyChartSeries(definition, tuple(points), tuple(" ".join(path) for path in paths if path)))
    return DailyComparisonChart(
        tuple(series),
        _number(minimum) if scale == "actual" else f"{minimum:.0f}",
        _number(maximum) if scale == "actual" else f"{maximum:.0f}",
        "实际值" if scale == "actual" else "趋势指数（首日=100）",
    )


def _daily_rows(
    by_source: dict[MetricSource, dict[date, GameCircleMetricSnapshot]],
) -> tuple[dict[str, object], ...]:
    all_days = sorted(set().union(*(snapshots.keys() for snapshots in by_source.values())))
    return tuple(
        {
            "day": day,
            "fans": _number(_metric_value(by_source[MetricSource.CIRCLE_FANS_DAILY].get(day), "total_user")),
            "publish": _number(_metric_value(by_source[MetricSource.GROUP_PUBLISH_DAILY].get(day), "publish_pv")),
            "consume": _number(_metric_value(by_source[MetricSource.GROUP_CONSUME_DAILY].get(day), "content_consume_cnt")),
            "share": _number(_metric_value(by_source[MetricSource.GROUP_SHARE_DAILY].get(day), "share_cnt")),
            "active": _number(_metric_value(by_source[MetricSource.GROUP_RETAIN_DAILY].get(day), "quanzi_dau")),
        }
        for day in all_days
    )


def _weekly_snapshots(*, appid: str) -> tuple[QuerySet[GameCircleMetricSnapshot], GameCircleMetricSnapshot | None]:
    verified = GameCircleMetricSnapshot.objects.filter(
        appid=appid,
        source=MetricSource.OPERATION_EFFECT_WEEKLY,
        publication_state__in=DISPLAYABLE_STATES,
    ).order_by("-period_start", "-captured_at", "-pk")
    pending = (
        GameCircleMetricSnapshot.objects.filter(
            appid=appid,
            source=MetricSource.OPERATION_EFFECT_WEEKLY,
            publication_state=MetricPublicationState.PENDING_RECHECK,
        )
        .order_by("-period_start", "-captured_at", "-pk")
        .first()
    )
    return verified, pending


def build_metric_workspace(
    *,
    start_day: date,
    end_day: date,
    appid: str,
    preferences: WorkspacePreferences | None = None,
) -> MetricWorkspace:
    preferences = preferences or WorkspacePreferences()
    by_source = _source_snapshot_maps(appid=appid, start_day=start_day, end_day=end_day)
    complete_days = set.intersection(*(set(snapshots) for snapshots in by_source.values()))
    complete_day = max(complete_days, default=None)
    previous_complete_day = max((day for day in complete_days if day < complete_day), default=None) if complete_day else None
    selected = {source: snapshots.get(complete_day) if complete_day else _latest_snapshot(snapshots) for source, snapshots in by_source.items()}
    previous = {source: snapshots.get(previous_complete_day) if previous_complete_day else None for source, snapshots in by_source.items()}
    verified, pending = _weekly_snapshots(appid=appid)
    weekly_by_period: dict[date, GameCircleMetricSnapshot] = {}
    for snapshot in verified:
        weekly_by_period.setdefault(snapshot.period_start, snapshot)
    latest_week = next(iter(weekly_by_period.values()), None)
    weekly_metrics = latest_week.metrics if latest_week else {}
    weekly_status = (
        f"{pending.period_start:%Y-%m-%d}–{pending.period_end:%Y-%m-%d} 待上游稳定性复核"
        if pending is not None and (latest_week is None or pending.period_start > latest_week.period_start)
        else "已验证"
    )
    definitions = tuple(DAILY_METRICS[key] for key in preferences.daily_metrics)
    alerts: list[str] = []
    if complete_day is None:
        missing = [source.label for source, snapshot in selected.items() if snapshot is None]
        alerts.append(f"缺少每日快照：{'、'.join(missing)}。" if missing else "五个来源尚无共同业务日，禁止跨域比较。")
    for source, metric, label, action in (
        (MetricSource.GROUP_CONSUME_DAILY, "content_consume_cnt", "内容消费", "检查内容发布与投放节奏"),
        (MetricSource.GROUP_PUBLISH_DAILY, "publish_pv", "内容创作", "检查创作者供给与活动安排"),
        (MetricSource.GROUP_SHARE_DAILY, "share_cnt", "内容分享", "检查内容互动引导与分享链路"),
        (MetricSource.GROUP_RETAIN_DAILY, "quanzi_dau", "游戏圈活跃用户", "检查活跃用户变化"),
    ):
        history = sorted(by_source[source].values(), key=lambda item: item.period_start)
        if len(history) >= 2:
            current, prior = _metric_value(history[-1], metric), _metric_value(history[-2], metric)
            if (
                isinstance(current, int | float)
                and isinstance(prior, int | float)
                and prior > 0
                and current < prior
                and (prior - current) / prior >= SIGNIFICANT_DECLINE_RATE
            ):
                alerts.append(
                    f"{label}：{history[-2].period_start:%m/%d} {_number(prior)} → "
                    f"{history[-1].period_start:%m/%d} {_number(current)}，"
                    f"下降 {_number(prior - current)}（{(prior - current) / prior:.1%}）。"
                    f"建议：{action}。"
                )
    captures = [snapshot.captured_at for snapshot in selected.values() if snapshot is not None]
    if latest_week is not None:
        captures.append(latest_week.captured_at)
    cards = _period_cards(by_source)
    return MetricWorkspace(
        preferences=preferences,
        complete_business_day=complete_day,
        daily_status=f"完整业务日 {complete_day:%Y-%m-%d}" if complete_day else "不完整日数据：卡片按各自最新业务日显示",
        p0_cards=cards,
        primary_cards=(cards[0], cards[2], cards[4]),
        secondary_cards=(cards[1], cards[3]),
        fan_chart=_fan_chart(by_source[MetricSource.CIRCLE_FANS_DAILY], start_day=start_day, end_day=end_day),
        daily_chart=_daily_chart(
            by_source, definitions, preferences.daily_scale, start_day=start_day, end_day=end_day
        ),
        daily_metric_options=tuple(DAILY_METRICS.values()),
        daily_rows=_daily_rows(by_source),
        chart_ticks=tuple(
            dict.fromkeys(
                (start_day, start_day + (end_day - start_day) // 2, end_day)
            )
        ),
        daily_business_days=max((len(by_source[option.source]) for option in definitions), default=0),
        weekly_cards=(
            MetricCard("日均访问", _number(weekly_metrics.get("avg_quanzi_dau_cur_week")), "周度运营效果", None, latest_week.period_start if latest_week else None),
            MetricCard("游戏圈渗透", _number(weekly_metrics.get("avg_quannzi_cross_game_uv_cur_week"), percent=True), "周度运营效果", None, latest_week.period_start if latest_week else None),
            MetricCard("内容曝光", _number(weekly_metrics.get("avg_content_expose_cnt_cur_week")), "周度运营效果", None, latest_week.period_start if latest_week else None),
            MetricCard("优质内容率", _number(weekly_metrics.get("avg_quality_publish_pv_rate_cur_week"), percent=True), "周度运营效果", None, latest_week.period_start if latest_week else None),
        ),
        weekly_rows=tuple(
            {
                "period": f"{item.period_start:%m/%d}–{item.period_end:%m/%d}",
                "dau": _number(item.metrics.get("avg_quanzi_dau_cur_week")),
                "exposure": _number(item.metrics.get("avg_content_expose_cnt_cur_week")),
                "publish_rate": _number(item.metrics.get("avg_publish_uv_rate_cur_week"), percent=True),
                "quality_rate": _number(item.metrics.get("avg_quality_publish_pv_rate_cur_week"), percent=True),
            }
            for item in list(weekly_by_period.values())[:6]
        ),
        weekly_status=weekly_status,
        alerts=tuple(alerts),
        primary_alert=alerts[0] if alerts else None,
        remaining_alert_count=max(len(alerts) - 1, 0),
        latest_captured_at=max(captures, default=None),
    )
