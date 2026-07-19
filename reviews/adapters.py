"""Schema-validated, OpenAI-compatible AI and OCR clients."""

from __future__ import annotations

import json
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from config.settings import runtime
from jobs.adapters import SourceContractError, SourceTransportError


class RiskOutput(BaseModel):
    label: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)


class ReplyOutput(BaseModel):
    user_intent_summary: str
    reply_draft: str
    missing_information: list[str] = Field(default_factory=list)
    knowledge_sources: list[str] = Field(default_factory=list)
    risk_notice: str = ""


class AssessmentOutput(BaseModel):
    content_category: str
    secondary_categories: list[str] = Field(default_factory=list)
    risk_labels: list[RiskOutput] = Field(default_factory=list)
    sentiment: str = ""
    feedback_value: str = ""
    recommended_action: str
    overall_confidence: float = Field(ge=0, le=1)
    needs_human_review: Literal[True] = True
    reply_suggestion: ReplyOutput | None = None


def normalize_assessment_output(payload: object) -> dict:
    """Accept a limited safe fallback for models that omit nested JSON objects."""
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    risks = normalized.get("risk_labels")
    if isinstance(risks, list):
        normalized["risk_labels"] = [
            {"label": item, "confidence": 0.5, "evidence": []}
            if isinstance(item, str)
            else item
            for item in risks
        ]
    reply = normalized.get("reply_suggestion")
    if isinstance(reply, str):
        normalized["reply_suggestion"] = {
            "user_intent_summary": "",
            "reply_draft": reply,
            "missing_information": [],
            "knowledge_sources": [],
            "risk_notice": "模型未按结构返回回复依据，须由人工核验。",
        }
    return normalized


class OpenAiCompatibleReviewClient:
    def __init__(self, client: httpx.Client | None = None):
        if not (runtime.ai_base_url and runtime.ai_api_key and runtime.ai_model):
            raise SourceTransportError("AI 运行时配置不完整")
        self.client = client or httpx.Client(timeout=runtime.ai_timeout_seconds, trust_env=False)

    def evaluate(self, content: str, ocr_text: str) -> AssessmentOutput:
        try:
            response = self.client.post(
                f"{runtime.ai_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {runtime.ai_api_key}"},
                json={
                    "model": runtime.ai_model,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你是《城主别慌张》微信小游戏官方游戏圈的内容运营审核助手。\n\n"
                                "【业务背景】\n"
                                "- 当前内容来自微信小游戏《城主别慌张》官方游戏圈。\n"
                                "- 游戏圈允许玩家讨论玩法、阵容、关卡、资源、活动、版本、问题反馈和改进建议。\n"
                                "- 帖子可能包含正文、图片 OCR 文字和投票标题/选项；投票选项、首字串联、谐音、拆字和表情符号也可能用于传播引流或违规信息。\n"
                                "- 你只提供分类、风险识别、证据和建议；所有结果必须由人工审核，绝不代表自动处置。\n\n"
                                "【审核原则】\n"
                                "- 与《城主别慌张》玩法、账号、活动或社区相关的正常讨论，不得归为无关内容。\n"
                                "- 有具体问题、复现信息、体验描述或改进建议的负面内容，应归为 FEEDBACK 或 SUGGESTION，不得仅因负面情绪建议沉底。\n"
                                "- 识别引流、外部联系方式、私服、外挂、诈骗交易、人身攻击、无关广告和纯负面灌水。\n"
                                "- 对引流尤其检查：群号、联系方式、外链、交易话术，以及投票标题、选项首字拼接、竖读、横读、谐音、数字和符号组合。\n"
                                "- evidence 必须摘录命中的正文、投票或 OCR 原文；无法提供证据时降低置信度并建议 HUMAN_REVIEW。\n"
                                "- 若建议 BLOCK、MUTE 或 BLOCK_AND_MUTE，reply_suggestion 必须为 null。\n\n"
                                "【输出约束】\n"
                                "只返回一个合法 JSON 对象，不要 Markdown、解释或额外字段。\n"
                                "needs_human_review 必须为 true。\n"
                                "content_category 只能是 GUIDE、FEEDBACK、SUGGESTION、QUESTION、ACTIVITY、SOCIAL、OFF_TOPIC、OTHER。\n"
                                "recommended_action 只能是 KEEP、SINK、BLOCK、MUTE、BLOCK_AND_MUTE、HUMAN_REVIEW。\n"
                                "overall_confidence 和每项风险 confidence 必须是 0 到 1 的数字。\n"
                                "必须包含 content_category、secondary_categories、risk_labels、sentiment、feedback_value、"
                                "recommended_action、overall_confidence、needs_human_review 和 reply_suggestion。\n"
                                "risk_labels 必须是对象数组，例如 [{\"label\":\"TRAFFIC_DIVERSION\",\"confidence\":0.98,"
                                "\"evidence\":[\"命中的原文\"]}]，不得只返回字符串标签数组。\n"
                                "label 只能使用 TRAFFIC_DIVERSION、PRIVATE_SERVER、CHEAT、PERSONAL_ATTACK、FRAUD_SCAM、"
                                "PORNOGRAPHY、POLITICAL_SENSITIVE、ADVERTISEMENT_SPAM、TRANSACTION_RISK、OFF_TOPIC、"
                                "NEGATIVE_SPAM、CONTENT_ANOMALY；具体场景只能写入 evidence，不得自造一级风险标签。\n"
                                "reply_suggestion 必须为 null，或为含 user_intent_summary、reply_draft、missing_information、"
                                "knowledge_sources、risk_notice 的对象；不得只返回字符串。"
                            ),
                        },
                        {"role": "user", "content": f"正文：{content}\nOCR：{ocr_text}"},
                    ],
                },
            )
            response.raise_for_status()
            payload = response.json()
            content_json = payload["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise SourceTransportError("AI 评估请求失败") from exc
        try:
            if isinstance(content_json, str) and content_json.strip().startswith("```"):
                content_json = content_json.strip().split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            return AssessmentOutput.model_validate(normalize_assessment_output(json.loads(content_json)))
        except Exception as exc:
            raise SourceContractError("AI 评估输出不符合 Schema") from exc


class OpenAiCompatibleOcrClient:
    """Optional vision OCR using a separately configured OpenAI-compatible endpoint."""

    def __init__(self, client: httpx.Client | None = None):
        if not (runtime.ocr_base_url and runtime.ocr_api_key and runtime.ocr_model):
            raise SourceTransportError("OCR 运行时配置不完整")
        self.client = client or httpx.Client(timeout=runtime.ai_timeout_seconds, trust_env=False)

    def extract_text(self, image_url: str) -> tuple[str, float]:
        try:
            response = self.client.post(
                f"{runtime.ocr_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {runtime.ocr_api_key}"},
                json={
                    "model": runtime.ocr_model,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "提取图片文字，返回 text 和 confidence JSON。",
                                },
                                {"type": "image_url", "image_url": {"url": image_url}},
                            ],
                        }
                    ],
                },
            )
            response.raise_for_status()
            data = response.json()["choices"][0]["message"]["content"]
            result = OcrOutput.model_validate_json(data)
            return result.text, result.confidence
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise SourceTransportError("OCR 请求失败") from exc
        except Exception as exc:
            raise SourceContractError("OCR 输出不符合 Schema") from exc


class OcrOutput(BaseModel):
    text: str
    confidence: float = Field(ge=0, le=1)
