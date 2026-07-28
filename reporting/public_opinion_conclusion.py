"""Bounded model synthesis for already-computed public-opinion evidence."""

from __future__ import annotations

import json
import re
from hashlib import sha256

import httpx

from config.settings import runtime

CONCLUSION_PROMPT_VERSION = "public-opinion-conclusion-v1"
CONCLUSION_SCHEMA_VERSION = "public-opinion-evidence-v1"
CONCLUSION_KINDS = frozenset({"OVERVIEW", "CHANGE", "VERIFICATION", "LIMITATION", "OBSERVATION"})
FORBIDDEN_TERMS = (
    "自动处置",
    "禁言",
    "封禁",
    "屏蔽",
    "沉底",
    "恢复",
    "下架",
    "处罚",
    "封号",
    "责任在",
    "归责",
    "导致",
    "造成",
    "因果",
)
NUMERIC_CLAIM_PATTERN = re.compile(r"\d")


class ConclusionError(RuntimeError):
    """A fixed, non-sensitive reason why the optional conclusion was unavailable."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def evidence_for_payload(payload: dict[str, object]) -> dict[str, object]:
    """Whitelist only aggregate evidence safe to leave the local report worker."""
    representatives: dict[str, list[dict[str, object]]] = {}
    source_representatives = payload.get("representative_posts")
    if isinstance(source_representatives, dict):
        for ranking in ("by_total", "by_rate"):
            rows = source_representatives.get(ranking)
            if not isinstance(rows, list):
                continue
            representatives[ranking] = [
                {
                    key: row[key]
                    for key in (
                        "topic_id",
                        "title",
                        "views",
                        "loves",
                        "replies",
                        "shares",
                        "interaction_total",
                        "engagement_rate",
                        "engagement_rate_percent",
                    )
                    if key in row
                }
                for row in rows
                if isinstance(row, dict)
            ]
    evidence: dict[str, object] = {
        "schema_version": CONCLUSION_SCHEMA_VERSION,
        "summary": payload.get("summary", {}), "data_quality": payload.get("data_quality", {}),
        "categories": payload.get("categories", []), "sentiments": payload.get("sentiments", []),
        "risks": payload.get("risks", []), "topics": payload.get("topics", []),
        "player_topics": payload.get("player_topics", []), "hotwords": payload.get("hotwords", {}),
        "daily_trend": payload.get("daily_trend", []), "daily_metrics": payload.get("daily_metrics", []),
        "representative_posts": representatives,
    }
    return evidence


def evidence_hash(evidence: dict[str, object]) -> str:
    return sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def conclusion_snapshot(
    conclusion: dict[str, object], *, input_hash: str, error_code: str = ""
) -> dict[str, object]:
    """Attach the versioned execution contract to one display-safe conclusion."""
    return {
        **conclusion,
        "model_version": runtime.ai_model or "未配置",
        "prompt_version": CONCLUSION_PROMPT_VERSION,
        "evidence_schema_version": CONCLUSION_SCHEMA_VERSION,
        "input_hash": input_hash,
        "error_code": error_code,
    }


def _known_evidence_ids(evidence: dict[str, object]) -> set[str]:
    known = {"summary", "data_quality", "categories", "sentiments", "risks", "topics", "player_topics", "hotwords", "daily_trend", "daily_metrics", "representative_posts"}
    return known


def validate_conclusion(value: object, *, evidence: dict[str, object]) -> dict[str, object]:
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ValueError("模型结论结构无效")
    items = value["items"]
    if len(items) > 5:
        raise ValueError("模型结论数量超限")
    known = _known_evidence_ids(evidence)
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("模型结论条目无效")
        text, references = item.get("text"), item.get("evidence_ids")
        kind = item.get("kind", "OBSERVATION")
        if not isinstance(text, str) or not text.strip() or not isinstance(references, list) or not references:
            raise ValueError("模型结论缺少证据")
        if not isinstance(kind, str) or kind not in CONCLUSION_KINDS:
            raise ValueError("模型结论类型无效")
        if (
            len(text) > 300
            or any(term in text for term in FORBIDDEN_TERMS)
            or NUMERIC_CLAIM_PATTERN.search(text)
        ):
            raise ValueError("模型结论不符合运营参考限制")
        if not all(isinstance(identifier, str) and identifier in known for identifier in references):
            raise ValueError("模型结论引用不存在")
        result.append({"kind": kind, "text": text.strip(), "evidence_ids": references})
    return {"status": "AVAILABLE", "items": result}


class PublicOpinionConclusionClient:
    def conclude(self, evidence: dict[str, object]) -> dict[str, object]:
        if not runtime.public_opinion_conclusion_enabled:
            raise ConclusionError("MODEL_CONCLUSION_DISABLED")
        if not (runtime.ai_base_url and runtime.ai_api_key and runtime.ai_model):
            raise ConclusionError("MODEL_NOT_CONFIGURED")
        prompt = (
            "你是游戏运营分析助手。只可依据输入证据生成至多5条运营参考；"
            "每条输出 text 和 evidence_ids，evidence_ids 只能取输入顶层键。"
            "不得归责、断言因果、提出自动处置或复述身份/正文；text 不得出现数值，"
            "精确数据仅保留在引用证据中。只返回 JSON 对象。"
        )
        try:
            response = httpx.post(
                f"{runtime.ai_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {runtime.ai_api_key}"},
                json={"model": runtime.ai_model, "response_format": {"type": "json_object"}, "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}]},
                timeout=runtime.ai_timeout_seconds, trust_env=False,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ConclusionError("MODEL_TRANSPORT") from exc
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ConclusionError("MODEL_RESPONSE_INVALID") from exc
        try:
            return validate_conclusion(json.loads(content), evidence=evidence)
        except json.JSONDecodeError as exc:
            raise ConclusionError("MODEL_NON_JSON") from exc
        except ValueError as exc:
            raise ConclusionError("MODEL_SCHEMA_INVALID") from exc
