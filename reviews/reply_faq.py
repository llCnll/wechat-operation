"""Versioned, reviewable generic FAQ examples for local reply drafts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

STYLE_ORDER = ("WARM", "CONCISE", "FORMAL")
STYLE_LABELS = {"WARM": "亲和互动", "CONCISE": "简洁处理", "FORMAL": "正式稳妥"}


@dataclass(frozen=True)
class ReplyFaqRule:
    rule_id: str
    scene: str
    keywords: tuple[str, ...]
    guidance: str
    missing_information: tuple[str, ...]
    source: str
    prohibited_claims: tuple[str, ...]


@dataclass(frozen=True)
class ReplyFaqCatalog:
    version: str
    rules: tuple[ReplyFaqRule, ...]


def _catalog_path() -> Path:
    return Path(__file__).with_name("data") / "community-reply-faq.v1.json"


@lru_cache(maxsize=1)
def load_community_reply_faq() -> ReplyFaqCatalog:
    payload = json.loads(_catalog_path().read_text(encoding="utf-8"))
    version = payload.get("version")
    if (
        not isinstance(version, str)
        or not version.strip()
        or payload.get("scope") != "all_authorized_games"
    ):
        raise ValueError("通用回复 FAQ 资料缺少有效版本或适用范围")
    rules: list[ReplyFaqRule] = []
    seen: set[str] = set()
    for item in payload.get("rules", []):
        if not isinstance(item, dict):
            raise ValueError("回复 FAQ 规则结构无效")
        rule_id = item.get("id")
        scene = item.get("scene")
        guidance = item.get("guidance")
        source = item.get("source")
        keywords = item.get("keywords", [])
        missing_information = item.get("missing_information", [])
        prohibited_claims = item.get("prohibited_claims", [])
        if (
            not isinstance(rule_id, str)
            or not rule_id.strip()
            or rule_id in seen
            or not isinstance(scene, str)
            or not scene.strip()
            or not isinstance(guidance, str)
            or not guidance.strip()
            or not isinstance(source, str)
            or not source.strip()
            or not isinstance(keywords, list)
            or not keywords
            or not all(isinstance(value, str) and value.strip() for value in keywords)
            or not isinstance(missing_information, list)
            or not all(isinstance(value, str) and value.strip() for value in missing_information)
            or not isinstance(prohibited_claims, list)
            or not all(isinstance(value, str) and value.strip() for value in prohibited_claims)
        ):
            raise ValueError("回复 FAQ 规则字段无效")
        seen.add(rule_id)
        rules.append(
            ReplyFaqRule(
                rule_id=rule_id,
                scene=scene,
                keywords=tuple(keywords),
                guidance=guidance,
                missing_information=tuple(missing_information),
                source=source,
                prohibited_claims=tuple(prohibited_claims),
            )
        )
    if not rules:
        raise ValueError("通用回复 FAQ 至少需要一条规则")
    return ReplyFaqCatalog(version=version, rules=tuple(rules))


def select_reply_faq_rules(*, content: str, limit: int = 4) -> tuple[ReplyFaqRule, ...]:
    """Return a small, deterministic candidate set; no match means no FAQ draft."""
    normalized = re.sub(r"\s+", "", content).casefold()
    if not normalized or limit <= 0:
        return ()
    matches = [
        rule
        for rule in load_community_reply_faq().rules
        if any(keyword.casefold() in normalized for keyword in rule.keywords)
    ]
    return tuple(matches[:limit])


def community_reply_faq_rule(rule_id: str) -> ReplyFaqRule | None:
    """Return a displayable rule only from the catalog used at evaluation."""
    if not rule_id:
        return None
    try:
        catalog = load_community_reply_faq()
    except (OSError, ValueError):
        return None
    return next((rule for rule in catalog.rules if rule.rule_id == rule_id), None)


def available_reply_faq_rules(*, content: str, limit: int = 4) -> tuple[ReplyFaqRule, ...]:
    """Fail closed for optional guidance so a damaged catalog cannot stop content review."""
    try:
        return select_reply_faq_rules(content=content, limit=limit)
    except (OSError, ValueError):
        return ()


def reply_faq_guidance_is_available() -> bool:
    """Distinguish a valid catalog with no match from an unavailable catalog."""
    try:
        load_community_reply_faq()
    except (OSError, ValueError):
        return False
    return True


def available_reply_faq_version() -> str:
    """Return the active guidance version, or no suffix when optional guidance is unavailable."""
    try:
        return load_community_reply_faq().version
    except (OSError, ValueError):
        return ""


def reply_faq_prompt(candidates: tuple[ReplyFaqRule, ...]) -> str:
    if not candidates:
        return (
            "本帖没有命中受控回复 FAQ；若内容安全，可生成通用 reply_suggestion，"
            "但 faq_rule_id 必须为空，且不得填写来源、待补充信息、支持渠道或事实性承诺。"
        )
    rows = []
    for rule in candidates:
        rows.append(
            f"- id={rule.rule_id}；场景={rule.scene}；要点={rule.guidance}；"
            f"待补充={'、'.join(rule.missing_information) or '无'}；"
            f"来源={rule.source}；禁止承诺={'、'.join(rule.prohibited_claims) or '无'}"
        )
    return "【受控回复 FAQ 候选】\n" + "\n".join(rows)
