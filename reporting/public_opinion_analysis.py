"""Pure, versioned analysis for locked public-opinion report inputs."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import jieba

from reviews.models import ContentCategory, risk_label_display

ANALYSIS_VERSION = "public-opinion-v1"
WORD_TEMPLATE_VERSION = "public-opinion-word-v1"
REPORT_STATUS_RULE_VERSION = "original-post-normal-v1"
MIN_ENGAGEMENT_VIEWS = 20
MAX_REPRESENTATIVE_POSTS = 10
MAX_OBSERVATIONS = 5
SHANGHAI = ZoneInfo("Asia/Shanghai")

SENTIMENT_LABELS = {
    "POSITIVE": "正面",
    "积极": "正面",
    "正面": "正面",
    "NEUTRAL": "中性",
    "平稳": "中性",
    "中性": "中性",
    "NEGATIVE": "负面",
    "消极": "负面",
    "负面": "负面",
}
FEEDBACK_VALUE_LABELS = {
    "NONE": "无明确反馈价值",
    "LOW": "低",
    "MEDIUM": "中",
    "HIGH": "高",
}
TOPIC_TERMS = {
    "GAMEPLAY": ("玩法", "攻略", "配队", "阵容", "流派", "通关", "boss"),
    "CHARACTER": ("角色", "英雄", "射手", "战士", "法师", "强度"),
    "GACHA": ("抽卡", "欧气", "非酋", "保底", "氪金", "出货"),
    "BUG": ("bug", "卡顿", "闪退", "崩溃", "问题", "反馈"),
    "ACTIVITY": ("活动", "福利", "礼包", "兑换码", "补偿"),
    "SOCIAL": ("公会", "好友", "组队", "大佬", "萌新"),
    "BALANCE": ("太难", "平衡", "削弱", "增强", "版本答案", "op"),
    "EVALUATION": ("好玩", "无聊", "垃圾", "推荐", "避雷", "劝退"),
}
TOPIC_LABELS = {
    "GAMEPLAY": "玩法/策略",
    "CHARACTER": "角色/强度",
    "GACHA": "抽卡/付费",
    "BUG": "BUG/性能",
    "ACTIVITY": "活动/福利",
    "SOCIAL": "社交/公会",
    "BALANCE": "难度/平衡",
    "EVALUATION": "情感/评价",
}
STOP_WORDS = frozenset(
    {
        "的",
        "了",
        "是",
        "我",
        "就",
        "都",
        "和",
        "与",
        "在",
        "也",
        "有",
        "这",
        "那",
        "一个",
        "这个",
        "可以",
        "没有",
        "怎么",
        "什么",
        "真的",
        "感觉",
        "游戏",
        "帖子",
        "回复",
    }
)


@dataclass(frozen=True)
class ReportSourceInput:
    topic_id: int
    publish_time: datetime | None
    title: str
    content_text: str
    ballot_text: str
    has_ai_assessment: bool
    ai_category: str
    ai_sentiment: str
    ai_feedback_value: str
    ai_risk_summary: tuple[dict[str, object], ...]

    @property
    def text(self) -> str:
        return "\n".join((self.title, self.content_text, self.ballot_text)).strip()


@dataclass(frozen=True)
class InteractionInput:
    topic_id: int
    view_count: int | None
    love_count: int | None
    reply_count: int | None
    share_count: int | None
    captured_at: datetime


def normalize_sentiment(value: str) -> str | None:
    return SENTIMENT_LABELS.get(value.strip().upper()) or SENTIMENT_LABELS.get(value.strip())


def normalize_feedback_value(value: str) -> str | None:
    if not value.strip():
        return None
    return FEEDBACK_VALUE_LABELS.get(value.strip().upper(), "未归类")


def feedback_value_display(value: object) -> str:
    return normalize_feedback_value(value) if isinstance(value, str) else "未归类"


def _topic_keys(text: str) -> tuple[str, ...]:
    normalized = text.casefold()
    return tuple(
        key
        for key, terms in TOPIC_TERMS.items()
        if any(term.casefold() in normalized for term in terms)
    )


def _terms(sources: tuple[ReportSourceInput, ...]) -> Counter[str]:
    values: Counter[str] = Counter()
    for source in sources:
        for term in jieba.lcut(source.text):
            normalized = term.strip().casefold()
            if (
                len(normalized) < 2
                or normalized in STOP_WORDS
                or normalized.isdigit()
                or not any(character.isalnum() for character in normalized)
            ):
                continue
            values[normalized] += 1
    return values


def _rows(counter: Counter[str], labels: dict[str, str] | None = None) -> list[dict[str, object]]:
    return [
        {"key": key, "label": (labels or {}).get(key, key), "count": count}
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _safe_count(value: int | None) -> int:
    return max(int(value or 0), 0)


def _representative_rows(
    interactions: tuple[InteractionInput, ...],
) -> tuple[list[dict], list[dict]]:
    rows = []
    for item in interactions:
        views = _safe_count(item.view_count)
        loves = _safe_count(item.love_count)
        replies = _safe_count(item.reply_count)
        shares = _safe_count(item.share_count)
        total = loves + replies + shares
        rows.append(
            {
                "topic_id": item.topic_id,
                "views": views,
                "loves": loves,
                "replies": replies,
                "shares": shares,
                "interaction_total": total,
                "engagement_rate": round(total / max(views, 1), 6),
                "engagement_rate_percent": round(total / max(views, 1) * 100, 2),
                "captured_at": item.captured_at.isoformat(),
            }
        )
    total_ranked = sorted(
        rows, key=lambda row: (-row["interaction_total"], -row["views"], row["topic_id"])
    )
    rate_ranked = sorted(
        (row for row in rows if row["views"] >= MIN_ENGAGEMENT_VIEWS),
        key=lambda row: (-row["engagement_rate"], -row["views"], row["topic_id"]),
    )
    return total_ranked[:MAX_REPRESENTATIVE_POSTS], rate_ranked[:MAX_REPRESENTATIVE_POSTS]


def _hotwords(
    current: tuple[ReportSourceInput, ...], baseline: tuple[ReportSourceInput, ...]
) -> tuple[list[dict[str, object]], bool]:
    current_terms = _terms(current)
    baseline_terms = _terms(baseline)
    if not baseline:
        return [
            {"term": term, "current_count": count, "baseline_count": None, "difference": None}
            for term, count in current_terms.most_common(25)
        ], False
    scale = len(current) / max(len(baseline), 1)
    rows = [
        {
            "term": term,
            "current_count": count,
            "baseline_count": baseline_terms.get(term, 0),
            "difference": round(count - baseline_terms.get(term, 0) * scale, 3),
        }
        for term, count in current_terms.items()
    ]
    rows.sort(key=lambda row: (-row["difference"], -row["current_count"], row["term"]))
    return rows[:25], True


def _observations(
    *,
    total: int,
    ai_count: int,
    sentiment_counts: Counter[str],
    baseline_sentiments: Counter[str],
    topic_counts: Counter[str],
    total_ranked: list[dict],
) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    if total and ai_count < total:
        observations.append(
            {"kind": "AI_COVERAGE", "message": f"AI 评估覆盖 {ai_count}/{total}。", "verify": True}
        )
    current_mapped = sum(sentiment_counts.values())
    baseline_mapped = sum(baseline_sentiments.values())
    if current_mapped >= 10 and baseline_mapped >= 10:
        delta = (
            sentiment_counts["负面"] / current_mapped
            - baseline_sentiments["负面"] / baseline_mapped
        )
        if abs(delta) >= 0.1:
            direction = "上升" if delta > 0 else "下降"
            observations.append(
                {
                    "kind": "SENTIMENT_CHANGE",
                    "message": f"负面情绪占比较上期{direction} {abs(delta):.0%}。",
                    "verify": True,
                }
            )
    for key, count in sorted(topic_counts.items(), key=lambda item: (-item[1], item[0]))[:2]:
        if count:
            observations.append(
                {
                    "kind": "TOPIC_VOLUME",
                    "message": f"{TOPIC_LABELS[key]}主题命中 {count} 帖。",
                    "verify": True,
                }
            )
    if total_ranked and total_ranked[0]["interaction_total"]:
        observations.append(
            {
                "kind": "HIGH_INTERACTION",
                "message": f"帖子 {total_ranked[0]['topic_id']} 的互动总量为 {total_ranked[0]['interaction_total']}。",
                "verify": True,
            }
        )
    return observations[:MAX_OBSERVATIONS]


def validate_result_payload(payload: dict[str, object]) -> None:
    required = {
        "schema_version",
        "summary",
        "data_quality",
        "categories",
        "sentiments",
        "risks",
        "topics",
        "hotwords",
        "daily_trend",
        "hour_distribution",
        "representative_posts",
        "observations",
        "limitations",
    }
    if set(payload) != required:
        raise ValueError("舆情报告结果结构无效")
    if payload["schema_version"] != ANALYSIS_VERSION:
        raise ValueError("舆情报告结果版本无效")


def build_report_result(
    *,
    current: tuple[ReportSourceInput, ...],
    baseline: tuple[ReportSourceInput, ...],
    interactions: tuple[InteractionInput, ...],
    source_locked_at: datetime,
    metrics_as_of: datetime,
) -> tuple[dict[str, object], dict[str, object]]:
    categories: Counter[str] = Counter()
    sentiments: Counter[str] = Counter()
    baseline_sentiments: Counter[str] = Counter()
    feedback: Counter[str] = Counter()
    risks: Counter[str] = Counter()
    topics: Counter[str] = Counter()
    daily: dict[str, dict[str, object]] = defaultdict(
        lambda: {"post_count": 0, "sentiment_covered": 0, "topic_hits": 0}
    )
    hours: Counter[str] = Counter()
    text_missing = 0
    ai_count = 0
    sentiment_covered = 0
    for source in current:
        if not source.text:
            text_missing += 1
        if source.has_ai_assessment:
            ai_count += 1
        if source.ai_category:
            categories[source.ai_category] += 1
        sentiment = normalize_sentiment(source.ai_sentiment)
        if sentiment:
            sentiments[sentiment] += 1
            sentiment_covered += 1
        feedback_value = normalize_feedback_value(source.ai_feedback_value)
        if feedback_value:
            feedback[feedback_value] += 1
        for risk in source.ai_risk_summary:
            label = risk.get("label") if isinstance(risk, dict) else ""
            if isinstance(label, str) and label:
                risks[label] += 1
        source_topics = _topic_keys(source.text)
        topics.update(source_topics)
        if source.publish_time:
            day_key = source.publish_time.astimezone(SHANGHAI).date().isoformat()
            daily[day_key]["post_count"] += 1
            daily[day_key]["sentiment_covered"] += int(bool(sentiment))
            daily[day_key]["topic_hits"] += len(source_topics)
            hours[f"{source.publish_time.astimezone(SHANGHAI).hour:02d}"] += 1
    for source in baseline:
        sentiment = normalize_sentiment(source.ai_sentiment)
        if sentiment:
            baseline_sentiments[sentiment] += 1
    total_ranked, rate_ranked = _representative_rows(interactions)
    titles = {source.topic_id: (source.title[:120] or "（无标题）") for source in current}
    for row in (*total_ranked, *rate_ranked):
        row["title"] = titles.get(row["topic_id"], "")
    hotwords, baseline_available = _hotwords(current, baseline)
    total = len(current)
    interaction_count = len(interactions)
    snapshot_times = [item.captured_at for item in interactions]
    limitations: list[str] = []
    if ai_count != total:
        limitations.append("部分帖子没有锁定的 AI 评估，相关分布只代表已覆盖帖子。")
    if sentiment_covered != total:
        limitations.append("情绪占比只基于可映射的 AI 情绪值。")
    if interaction_count != total:
        limitations.append("部分帖子缺少互动快照，互动榜单仅使用可用快照。")
    if not baseline_available:
        limitations.append("没有可用的上一等长周期基线，热词不展示变化值。")
    quality = {
        "source_count": total,
        "baseline_count": len(baseline),
        "text_missing_count": text_missing,
        "ai_assessment_count": ai_count,
        "sentiment_covered_count": sentiment_covered,
        "interaction_snapshot_count": interaction_count,
        "interaction_first_captured_at": min(snapshot_times).isoformat()
        if snapshot_times
        else None,
        "interaction_last_captured_at": max(snapshot_times).isoformat() if snapshot_times else None,
        "baseline_available": baseline_available,
        "source_locked_at": source_locked_at.isoformat(),
        "metrics_as_of": metrics_as_of.isoformat(),
    }
    payload = {
        "schema_version": ANALYSIS_VERSION,
        "summary": {"source_count": total, "feedback_values": _rows(feedback)},
        "data_quality": quality,
        "categories": _rows(categories, dict(ContentCategory.choices)),
        "sentiments": _rows(sentiments),
        "risks": _rows(risks, {key: risk_label_display(key) for key in risks}),
        "topics": _rows(topics, TOPIC_LABELS),
        "hotwords": {"baseline_available": baseline_available, "items": hotwords},
        "daily_trend": [{"day": day, **values} for day, values in sorted(daily.items())],
        "hour_distribution": _rows(hours),
        "representative_posts": {"by_total": total_ranked, "by_rate": rate_ranked},
        "observations": _observations(
            total=total,
            ai_count=ai_count,
            sentiment_counts=sentiments,
            baseline_sentiments=baseline_sentiments,
            topic_counts=topics,
            total_ranked=total_ranked,
        ),
        "limitations": limitations,
    }
    validate_result_payload(payload)
    return payload, quality
