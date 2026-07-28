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

CONCLUSION_KIND_LABELS = {
    "OVERVIEW": "整体概览",
    "CHANGE": "变化观察",
    "VERIFICATION": "人工核验重点",
    "LIMITATION": "解读限制",
    "OBSERVATION": "运营观察",
}
CONCLUSION_EVIDENCE_LABELS = {
    "summary": "报告概览",
    "data_quality": "数据质量",
    "categories": "内容类别",
    "sentiments": "情绪分布",
    "risks": "风险信号",
    "topics": "预设议题",
    "player_topics": "玩家话题",
    "hotwords": "热词",
    "daily_trend": "帖子日趋势",
    "daily_metrics": "运营指标关联",
    "representative_posts": "玩家高互动代表帖",
}
OBSERVATION_VERIFICATION_NOTES = {
    "AI_COVERAGE": "核对未覆盖帖子是否集中在特定日期、内容类型或采集异常，避免将已覆盖样本直接视为全量玩家反馈。",
    "SENTIMENT_CHANGE": "抽样查看本期与基线中可映射情绪的帖子，确认覆盖范围和内容结构可比，再解读占比变化。",
    "TOPIC_VOLUME": "抽样核对命中帖子与主题词的语义相关性；同一帖子可命中多个主题，主题计数不能直接与帖子总量比较。",
    "PLAYER_HIGH_INTERACTION": "查看该玩家帖的内容、发布时间和互动构成，确认互动是否对应持续玩家关注，而非单次活动、转发或异常流量。",
    "HIGH_INTERACTION": "查看该帖子内容、发布时间和互动构成，确认互动是否对应持续玩家关注，而非单次活动、转发或异常流量。",
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


def _counter_summary(items: object, *, prefix: str) -> str:
    rows = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    labels = [
        f"{item.get('label')} {item.get('count')}"
        for item in rows[:3]
        if item.get("label") not in (None, "") and item.get("count") not in (None, "")
    ]
    return f"{prefix}：{'、'.join(labels)}" if labels else f"{prefix}：暂无固定条目"


def _player_representative_count(payload: dict[str, object]) -> int:
    representatives = payload.get("representative_posts")
    if not isinstance(representatives, dict):
        return 0
    topic_ids = {
        row.get("topic_id")
        for ranking in ("by_total", "by_rate")
        for row in representatives.get(ranking, [])
        if isinstance(row, dict)
        and row.get("official_identity") == "PLAYER"
        and row.get("topic_id") is not None
    }
    return len(topic_ids)


def conclusion_evidence_summary(identifier: object, payload: dict[str, object]) -> str:
    """Summarize one fixed evidence group without querying live state."""
    identifier = str(identifier)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    quality = payload.get("data_quality") if isinstance(payload.get("data_quality"), dict) else {}
    if identifier == "summary":
        return f"锁定来源 {summary.get('source_count', 0)} 帖"
    if identifier == "data_quality":
        return (
            f"AI 覆盖 {quality.get('ai_assessment_count', 0)}/{quality.get('source_count', 0)}，"
            f"互动快照 {quality.get('interaction_snapshot_count', 0)}/{quality.get('source_count', 0)}"
        )
    if identifier in {"categories", "sentiments", "risks", "topics", "player_topics"}:
        prefixes = {
            "categories": "前列类别",
            "sentiments": "情绪分布",
            "risks": "风险信号",
            "topics": "前列议题",
            "player_topics": "前列玩家话题",
        }
        return _counter_summary(payload.get(identifier), prefix=prefixes[identifier])
    if identifier == "hotwords":
        hotwords = payload.get("hotwords")
        rows = hotwords.get("items", []) if isinstance(hotwords, dict) else []
        terms = [str(row.get("term")) for row in rows[:3] if isinstance(row, dict) and row.get("term")]
        return f"前列热词：{'、'.join(terms)}" if terms else "前列热词：暂无固定条目"
    if identifier == "daily_trend":
        rows = payload.get("daily_trend")
        return f"已固定 {len(rows) if isinstance(rows, list) else 0} 个业务日的帖子趋势"
    if identifier == "daily_metrics":
        rows = payload.get("daily_metrics")
        count = len(rows) if isinstance(rows, list) else 0
        sources = {
            row.get("source") for row in rows if isinstance(row, dict) and row.get("source")
        } if isinstance(rows, list) else set()
        return f"已固定 {count} 条日指标快照，覆盖 {len(sources)} 个指标来源"
    if identifier == "representative_posts":
        return f"仅纳入 {_player_representative_count(payload)} 条玩家内容代表帖"
    return "固定报告证据"


def present_conclusion(conclusion: object, payload: dict[str, object]) -> dict[str, object]:
    """Decorate immutable conclusion snapshots with human-readable evidence."""
    result = dict(conclusion) if isinstance(conclusion, dict) else {}
    items: list[dict[str, object]] = []
    for item in result.get("items", []):
        if not isinstance(item, dict):
            continue
        identifiers = [identifier for identifier in item.get("evidence_ids", []) if isinstance(identifier, str)]
        items.append(
            {
                **item,
                "kind_label": CONCLUSION_KIND_LABELS.get(item.get("kind"), "运营观察"),
                "evidence": [
                    {
                        "label": CONCLUSION_EVIDENCE_LABELS.get(identifier, "固定报告证据"),
                        "summary": conclusion_evidence_summary(identifier, payload),
                    }
                    for identifier in identifiers
                ],
            }
        )
    result["items"] = items
    return result


def present_observations(payload: dict[str, object]) -> list[dict[str, object]]:
    """Correct legacy high-interaction wording without mutating a completed run."""
    representatives = payload.get("representative_posts")
    total_ranked = representatives.get("by_total", []) if isinstance(representatives, dict) else []
    identities = {
        row.get("topic_id"): row.get("official_identity")
        for row in total_ranked
        if isinstance(row, dict)
    }
    observations: list[dict[str, object]] = []
    for item in payload.get("observations", []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        message = str(item.get("message") or "")
        if kind == "HIGH_INTERACTION":
            parts = message.replace("帖子 ", "").replace(" 的互动总量为 ", "|").rstrip("。").split("|", 1)
            if (
                len(parts) == 2
                and parts[0].isdigit()
                and identities.get(int(parts[0])) in {"VERIFIED_OFFICIAL", "GAME_NAME_MATCH"}
            ):
                message = f"官方内容帖子 {parts[0]} 的互动总量为 {parts[1]}；已按锁定身份单列，不纳入玩家关注或反馈信号。"
                kind = "OFFICIAL_HIGH_INTERACTION"
        observations.append(
            {
                **item,
                "kind": kind,
                "message": message,
                "verify": False if kind == "OFFICIAL_HIGH_INTERACTION" else item.get("verify", True),
                "verification_note": item.get("verification_note")
                or OBSERVATION_VERIFICATION_NOTES.get(kind, "请结合固定证据和业务背景确认该观察的适用范围。"),
            }
        )
    return [
        *[item for item in observations if item.get("kind") != "OFFICIAL_HIGH_INTERACTION"],
        *[item for item in observations if item.get("kind") == "OFFICIAL_HIGH_INTERACTION"],
    ]
