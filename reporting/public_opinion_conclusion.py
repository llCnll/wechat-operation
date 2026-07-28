"""Bounded model synthesis for already-computed public-opinion evidence."""

from __future__ import annotations

import json
import re
from hashlib import sha256

import httpx

from config.settings import runtime

CONCLUSION_PROMPT_VERSION = "public-opinion-conclusion-v2"
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
CONCLUSION_FAILURE_LABELS = {
    "MODEL_CONCLUSION_DISABLED": "模型结论未启用",
    "MODEL_NOT_CONFIGURED": "模型连接未配置",
    "MODEL_CONNECT_TIMEOUT": "模型连接超时",
    "MODEL_READ_TIMEOUT": "模型响应超时",
    "MODEL_WRITE_TIMEOUT": "模型请求发送超时",
    "MODEL_POOL_TIMEOUT": "模型请求排队超时",
    "MODEL_CONNECTION_FAILED": "模型连接失败",
    "MODEL_AUTH_FAILED": "模型鉴权失败",
    "MODEL_ENDPOINT_NOT_FOUND": "模型接口地址无效",
    "MODEL_RATE_LIMITED": "模型服务限流",
    "MODEL_REQUEST_REJECTED": "模型请求被拒绝",
    "MODEL_SERVICE_ERROR": "模型服务端错误",
    "MODEL_TRANSPORT": "模型服务请求失败",
    "MODEL_RESPONSE_INVALID": "模型响应结构无效",
    "MODEL_NON_JSON": "模型未返回 JSON 结论",
    "MODEL_SCHEMA_INVALID": "模型结论未通过约束校验",
    "MODEL_INTERRUPTED": "模型调用中断",
    "MODEL_UNAVAILABLE": "模型结论暂不可用",
}


class ConclusionError(RuntimeError):
    """A fixed, non-sensitive reason why the optional conclusion was unavailable."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def conclusion_failure_label(error_code: object) -> str:
    """Return a fixed display label without exposing upstream response details."""
    return CONCLUSION_FAILURE_LABELS.get(str(error_code), "模型结论未能生成")


def _transport_error_code(exc: httpx.HTTPError) -> str:
    """Classify transport failures while retaining no remote body or credentials."""
    if isinstance(exc, httpx.ConnectTimeout):
        return "MODEL_CONNECT_TIMEOUT"
    if isinstance(exc, httpx.ReadTimeout):
        return "MODEL_READ_TIMEOUT"
    if isinstance(exc, httpx.WriteTimeout):
        return "MODEL_WRITE_TIMEOUT"
    if isinstance(exc, httpx.PoolTimeout):
        return "MODEL_POOL_TIMEOUT"
    if isinstance(exc, httpx.ConnectError):
        return "MODEL_CONNECTION_FAILED"
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code in {401, 403}:
            return "MODEL_AUTH_FAILED"
        if status_code == 404:
            return "MODEL_ENDPOINT_NOT_FOUND"
        if status_code == 429:
            return "MODEL_RATE_LIMITED"
        if 400 <= status_code < 500:
            return "MODEL_REQUEST_REJECTED"
        return "MODEL_SERVICE_ERROR"
    return "MODEL_TRANSPORT"


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
                if isinstance(row, dict) and row.get("official_identity") == "PLAYER"
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
            "精确数据仅保留在引用证据中。每条 text 使用完整、带限定语的一至两句话，"
            "说明观察范围或需要人工核验的边界，避免口号式短句。只返回 JSON 对象。"
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
            raise ConclusionError(_transport_error_code(exc)) from exc
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
